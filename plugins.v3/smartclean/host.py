"""连接 MoviePilot 的扫描、转移记录、豆瓣看过和删除。"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

from app.sdk.logging import logger

from .douban import sync_watched_pages
from .library import LibraryError, LibraryReader, library_catalog, scan_library
from .models import Candidate, Subject, TransferFact
from .service import Ports
from .transfers import candidate_queries, facts_from_records
from . import watchedstore


def readers() -> list[LibraryReader]:
    """已连接的 Emby 和 Jellyfin。"""
    from app.sdk.services import MediaServerHelper

    found = []
    helper = MediaServerHelper()
    for server_type in ("emby", "jellyfin"):
        try:
            services = helper.get_services(type_filter=server_type) or {}
        except Exception as err:  # noqa: BLE001
            logger.warning(f"智能清理：读取 {server_type} 服务失败：{err}")
            continue
        for name, service in services.items():
            instance = getattr(service, "instance", None)
            if instance is None or (hasattr(instance, "is_inactive") and instance.is_inactive()):
                continue
            found.append(LibraryReader(str(name), server_type, instance))
    return found


def reader_for(server: str, server_type: str) -> Optional[LibraryReader]:
    for item in readers():
        if item.name == server and item.server_type == server_type:
            return item
    return None


_WATCHED_CACHE: dict[str, tuple[float, list[Subject], Optional[str]]] = {}
_WATCHED_TTL_SECONDS = 300.0
_WATCHED_LOCK = threading.Lock()
_WATCHED_BOOTSTRAP_LOCKS: dict[str, threading.Lock] = {}
_WATCHED_BOOTSTRAP_GUARD = threading.Lock()


def clear_watched_cache() -> None:
    with _WATCHED_LOCK:
        _WATCHED_CACHE.clear()


def _bootstrap_lock_for(user: str) -> threading.Lock:
    with _WATCHED_BOOTSTRAP_GUARD:
        lock = _WATCHED_BOOTSTRAP_LOCKS.get(user)
        if lock is None:
            lock = threading.Lock()
            _WATCHED_BOOTSTRAP_LOCKS[user] = lock
        return lock


class HostPorts(Ports):
    """正式运行时使用的宿主能力。"""

    def __init__(self, douban_user: str, douban_cookie: str) -> None:
        self._douban_user = douban_user
        self._douban_cookie = douban_cookie

    def scan(self, task: dict[str, Any]) -> tuple[list[Candidate], list[str]]:
        reader = reader_for(str(task.get("server") or ""), str(task.get("server_type") or ""))
        if reader is None:
            return [], [f"媒体服务器 {task.get('server') or ''} 未连接"]
        candidates, errors = scan_library(reader, task)
        logger.info(
            f"智能清理：{task.get('library_name') or task.get('library_id')} 扫描到 {len(candidates)} 条"
            + (f"，{len(errors)} 个错误" if errors else "")
        )
        return candidates, errors

    def transfers(self, candidates: list[Candidate]) -> list[TransferFact]:
        from app.db.oper.downloadhistory import DownloadHistoryOper
        from app.db.oper.transferhistory import TransferHistoryOper
        from app.schemas.types import MediaSource

        from .filters import candidate_transfers
        from .titles import years_compatible

        source_map = {"themoviedb": MediaSource.TMDB, "imdb": MediaSource.IMDb}
        transfer_oper = TransferHistoryOper()
        rows = []
        seen: set[int] = set()

        def _absorb(found: list) -> None:
            for row in found or []:
                row_id = getattr(row, "id", None)
                if row_id in seen:
                    continue
                if row_id is not None:
                    seen.add(row_id)
                rows.append(row)

        for mtype, source, media_id in candidate_queries(candidates):
            try:
                _absorb(transfer_oper.get_by(
                    mtype=mtype,
                    media_source=source_map[source],
                    media_id=media_id,
                    match_all_versions=True,
                ))
            except Exception as err:  # noqa: BLE001
                logger.warning(f"智能清理：查询转移记录失败 {media_id}：{err}")

        # 身份没命中时按标题补查（年份 ±1）
        provisional = facts_from_records(rows, {})
        for candidate in candidates:
            if candidate_transfers(candidate, provisional):
                continue
            if not candidate.title:
                continue
            mtype = "电影" if candidate.kind == "movie" else "电视剧"
            years = [candidate.year] if candidate.year else [None]
            if candidate.year and candidate.year.isdigit():
                y = int(candidate.year)
                years = [str(y), str(y - 1), str(y + 1)]
            for year in years:
                try:
                    found = transfer_oper.get_by(
                        mtype=mtype,
                        title=candidate.title,
                        year=year,
                        match_all_versions=True,
                    )
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"智能清理：按标题查询转移记录失败 {candidate.title}：{err}")
                    continue
                matched = []
                for row in found or []:
                    row_year = str(getattr(row, "year", "") or "")
                    if year and row_year and not years_compatible(candidate.year, row_year):
                        continue
                    matched.append(row)
                if matched:
                    _absorb(matched)
                    break

        hashes = [str(getattr(row, "download_hash", "") or "") for row in rows if getattr(row, "download_hash", None)]
        usernames: dict[str, str] = {}
        if hashes:
            try:
                histories = DownloadHistoryOper().get_by_hashes(hashes)
            except Exception as err:  # noqa: BLE001
                logger.warning(f"智能清理：查询下载用户失败：{err}")
                histories = {}
            for download_hash, history in (histories or {}).items():
                name = str(getattr(history, "username", "") or "").strip()
                if name:
                    usernames[str(download_hash)] = name
        facts = facts_from_records(rows, usernames)
        if candidates and not facts:
            logger.info("智能清理：未查到任何整理/转移记录，入库与有源列将为空（在库仍用媒体库添加时间）")
        else:
            logger.info(f"智能清理：整理记录 {len(rows)} 条，入库事实 {len(facts)} 条")
        return facts

    def watched(self) -> tuple[list[Subject], Optional[str]]:
        """优先读本地快照；快照为空时自动全量重建一次（扫描/自动化共用）。"""
        if not self._douban_user:
            return [], None
        cache_key = self._douban_user
        now = time.monotonic()
        with _WATCHED_LOCK:
            hit = _WATCHED_CACHE.get(cache_key)
            if hit and now - hit[0] <= _WATCHED_TTL_SECONDS:
                return list(hit[1]), hit[2]
        subjects = watchedstore.load_subjects(self._douban_user)
        error = None
        if not subjects:
            boot = _bootstrap_lock_for(cache_key)
            with boot:
                subjects = watchedstore.load_subjects(self._douban_user)
                if not subjects:
                    try:
                        logger.info(f"智能清理：豆瓣看过快照为空，自动全量重建（用户 {self._douban_user}）")
                        sync_watched(self._douban_user, self._douban_cookie, mode="full")
                        subjects = watchedstore.load_subjects(self._douban_user)
                    except Exception as err:  # noqa: BLE001
                        status = watchedstore.status_of(self._douban_user)
                        detail = status.get("last_error") or str(err)
                        error = f"豆瓣看过快照为空且全量重建失败：{detail}"
                        logger.warning(f"智能清理：{error}")
        if not subjects and error is None:
            error = "豆瓣看过本地快照为空。"
        with _WATCHED_LOCK:
            _WATCHED_CACHE[cache_key] = (time.monotonic(), list(subjects), error)
        return subjects, error

    def resolve_douban_by_imdb(self, imdb: str) -> str:
        from .bridge import resolve_douban_id_by_imdb

        return resolve_douban_id_by_imdb(imdb)

    def delete_library(self, server: str, server_type: str, item_id: str) -> bool:
        reader = reader_for(server, server_type)
        if reader is None or not item_id:
            return False
        from urllib.parse import quote

        from app.adapters.network.http import RequestUtils

        from .library import prefix

        safe_id = quote(item_id, safe="")
        # 与 get_data 同形：走实例 _request，带 MediaBrowser Authorization；裸 RequestUtils 会被 Jellyfin 拒掉
        # DeleteFileLocation 删盘可能较慢，超时拉长；无响应时再核实条目是否已消失
        url = f"[HOST]{prefix(server_type)}Items/{safe_id}?api_key=[APIKEY]&DeleteFileLocation=true"
        instance = reader._instance
        host = getattr(instance, "_host", None)
        key = getattr(instance, "_apikey", None)
        if not host or not key:
            return False
        user = str(getattr(instance, "user", "") or "")
        expanded = url.replace("[HOST]", host).replace("[APIKEY]", key).replace("[USER]", user)

        def _client(timeout: float = 120.0):
            make_request = getattr(instance, "_request", None)
            if callable(make_request):
                return make_request(timeout=timeout)
            return RequestUtils(timeout=timeout)

        def _item_gone() -> bool:
            probe = (
                f"{host}{prefix(server_type)}Items/{safe_id}?api_key={key}"
            )
            try:
                response = _client(timeout=15.0).get_res(url=probe)
            except Exception:  # noqa: BLE001
                return False
            if response is None:
                return False
            try:
                return getattr(response, "status_code", 500) == 404
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()

        try:
            response = _client(timeout=120.0).delete_res(url=expanded)
        except Exception as err:  # noqa: BLE001
            if _item_gone():
                logger.info(f"智能清理：删除异常但条目已不存在，视为成功 {item_id}")
                return True
            logger.warning(f"智能清理：删除媒体库条目失败 {item_id}：{err}")
            return False
        if response is None:
            if _item_gone():
                logger.info(f"智能清理：删除无响应但条目已不存在，视为成功 {item_id}")
                return True
            logger.warning(f"智能清理：删除媒体库条目无响应 {item_id}")
            return False
        try:
            status = getattr(response, "status_code", 500)
            ok = status in {200, 204, 404}
            if not ok:
                body = (getattr(response, "text", None) or "")[:200]
                logger.warning(f"智能清理：删除媒体库条目 HTTP {status} {item_id} {body}")
                if _item_gone():
                    logger.info(f"智能清理：删除 HTTP {status} 但条目已不存在，视为成功 {item_id}")
                    return True
            return ok
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    def count_episodes(self, server: str, server_type: str, series_id: str) -> Optional[int]:
        reader = reader_for(server, server_type)
        if reader is None:
            return None
        try:
            return len(reader.episodes(series_id))
        except LibraryError as err:
            logger.warning(f"智能清理：统计剩余集数失败 {series_id}：{err}")
            return None

    def delete_source(self, fileitem: dict[str, Any]) -> bool:
        from app import schemas
        from app.chain.storage import StorageChain

        path = str(fileitem.get("path") or "")
        if not path:
            return False
        try:
            return bool(StorageChain().delete_file(schemas.FileItem(**fileitem)))
        except Exception as err:  # noqa: BLE001
            logger.warning(f"智能清理：删除源文件失败 {path}：{err}")
            return False


def sync_watched(user: str, cookie: str, *, mode: str = "incremental") -> dict[str, Any]:
    """增量或全量同步豆瓣看过并落盘。失败保留旧快照，只写 last_error。"""
    user = str(user or "").strip()
    if not user:
        raise RuntimeError("未配置豆瓣用户（主用户个人资料里的豆瓣用户）")
    sync_mode = "full" if mode == "full" else "incremental"
    existing = watchedstore.load_subjects(user)
    try:
        result = sync_watched_pages(
            lambda start: _fetch_interests(user, cookie, start),
            existing=existing,
            mode=sync_mode,
            page_size=50,
            page_sleep=0.8,
        )
        subjects = result["subjects"]
        watchedstore.save_snapshot(
            user,
            subjects,
            total=result.get("total"),
            mode=result.get("mode") or sync_mode,
            pages_fetched=int(result.get("pages_fetched") or 0),
            last_error="",
        )
        with _WATCHED_LOCK:
            _WATCHED_CACHE[user] = (time.monotonic(), list(subjects), None)
        logger.info(
            f"智能清理：豆瓣看过已同步 {len(subjects)} 条"
            f"（模式 {result.get('mode')}，拉 {result.get('pages_fetched')} 页，用户 {user}）"
        )
        return {
            **watchedstore.status_of(user),
            "new_or_updated": int(result.get("new_or_updated") or 0),
            "stopped_early": bool(result.get("stopped_early")),
            "mode": result.get("mode") or sync_mode,
        }
    except Exception as err:  # noqa: BLE001
        logger.warning(f"智能清理：同步豆瓣看过失败：{err}")
        watchedstore.record_sync_error(user, str(err), mode=sync_mode)
        raise


def _cookie_ck(cookie: str) -> str:
    """从浏览器 Cookie 串里取出 ck，供 Rexxar 私密列表鉴权。"""
    for part in str(cookie or "").split(";"):
        piece = part.strip()
        if not piece.lower().startswith("ck="):
            continue
        return piece.split("=", 1)[1].strip().strip('"')
    return ""


def _fetch_interests_rexxar(user: str, cookie: str, start: int) -> dict:
    """带登录 Cookie 走 m.douban Rexxar，才能拿到 is_private=true 的看过。"""
    from app.adapters.network.http import RequestUtils

    ck = _cookie_ck(cookie)
    if not ck:
        raise RuntimeError("豆瓣 Cookie 缺少 ck，无法读取私密看过")
    url = f"https://m.douban.com/rexxar/api/v2/user/{user}/interests"
    params = {
        "type": "movie",
        "status": "done",
        "start": start,
        "count": 50,
        "ck": ck,
        "for_mobile": 1,
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
            "Mobile/15E148 Safari/604.1"
        ),
        "Cookie": cookie,
        "Referer": "https://m.douban.com/",
    }
    response = RequestUtils(headers=headers).get_res(url=url, params=params)
    if response is None:
        raise RuntimeError("豆瓣私密看过接口无响应")
    try:
        status = getattr(response, "status_code", 500)
        if status >= 300:
            raise RuntimeError(f"豆瓣私密看过接口返回 HTTP {status}")
        payload = response.json()
    except RuntimeError:
        raise
    except Exception as err:  # noqa: BLE001
        raise RuntimeError(f"豆瓣私密看过接口响应无效：{err}") from err
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if not isinstance(payload, dict):
        raise RuntimeError("豆瓣私密看过接口返回了非对象数据")
    if payload.get("code") not in (None, 0, "0"):
        raise RuntimeError(
            f"豆瓣私密看过接口业务错误：{payload.get('msg') or payload.get('message') or payload.get('code')}"
        )
    return payload


def _fetch_interests_frodo(user: str, start: int) -> dict:
    """公开看过走 MoviePilot 同款 Frodo 签名接口，避免网页抓取被 418。"""
    from random import choice

    from app.adapters.network.http import RequestUtils
    from app.modules.douban.apiv2 import DoubanApi

    api = DoubanApi()
    path = f"/user/{user}/interests"
    req_url, params = api._prepare_get_request(
        path,
        type="movie",
        status="done",
        start=start,
        count=50,
    )
    headers = {"User-Agent": choice(api._user_agents)}
    response = RequestUtils(headers=headers).get_res(url=req_url, params=params)
    if response is None:
        raise RuntimeError("豆瓣看过接口无响应")
    try:
        status = getattr(response, "status_code", 500)
        if status >= 300:
            raise RuntimeError(f"豆瓣看过接口返回 HTTP {status}")
        payload = response.json()
    except RuntimeError:
        raise
    except Exception as err:  # noqa: BLE001
        raise RuntimeError(f"豆瓣看过接口响应无效：{err}") from err
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if not isinstance(payload, dict):
        raise RuntimeError("豆瓣看过接口返回了非对象数据")
    if payload.get("code") not in (None, 0, "0"):
        raise RuntimeError(
            f"豆瓣看过接口业务错误：{payload.get('msg') or payload.get('message') or payload.get('code')}"
        )
    return payload


def _fetch_interests(user: str, cookie: str, start: int) -> dict:
    """有 Cookie 时走 Rexxar（含私密）；否则走 Frodo 公开列表。"""
    if cookie and _cookie_ck(cookie):
        return _fetch_interests_rexxar(user, cookie, start)
    return _fetch_interests_frodo(user, start)


def list_libraries() -> list[dict[str, str]]:
    catalog = []
    errors = []
    for reader in readers():
        try:
            catalog.extend(library_catalog(reader))
        except LibraryError as err:
            errors.append(str(err))
    return catalog


def list_usernames() -> list[str]:
    from app.db.oper.user import UserOper

    names = []
    try:
        users = UserOper().list() or []
    except Exception as err:  # noqa: BLE001
        logger.warning(f"智能清理：读取用户失败：{err}")
        return []
    for user in users:
        name = str(getattr(user, "name", "") or "").strip()
        if name:
            names.append(name)
    return names


def resolve_main_user(configured: str = "") -> str:
    """插件里选中的主用户；未选时回退到系统超级用户。"""
    from app.runtime.config import settings

    username = str(configured or "").strip()
    if username:
        return username
    return str(getattr(settings, "SUPERUSER", "") or "").strip()


def main_douban_user(configured_main_user: str = "") -> str:
    """读取指定 MoviePilot 用户个人资料里的豆瓣用户。"""
    from app.db.oper.user import UserOper

    username = resolve_main_user(configured_main_user)
    if not username:
        return ""
    try:
        value = UserOper().get_setting(username, "douban_userid")
    except Exception as err:  # noqa: BLE001
        logger.warning(f"智能清理：读取用户 {username} 豆瓣账号失败：{err}")
        return ""
    return str(value or "").strip()
