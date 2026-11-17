"""按媒体库清理电影和剧集季。"""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from apscheduler.triggers.cron import CronTrigger

from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .api import (
    DoubanWatchedSyncBody,
    ExcludeBody,
    LibraryAction,
    LibraryAutomationBody,
    ManualDeleteBody,
    ManualWhitelistBody,
    RuleAction,
    RuleBody,
    ScanBody,
    SmartCleanApi,
    fail,
    ok,
)
from .host import (
    HostPorts,
    clear_watched_cache,
    list_libraries,
    list_usernames,
    main_douban_user,
    resolve_main_user,
    sync_watched,
)
from . import watchedstore
from .libraries import find_library, library_owner, normalize_library
from .rules import libraries_using_rule, normalize_rule, rule_by_id
from .scancache import ScanCache
from .service import _candidate_from_snapshot, delete_candidates_now, run_automation, scan_rows
from .stateutil import migrate_state
from .whitelist import (
    add_manual,
    effective_entries,
    normalize_whitelist,
    pick_from_candidate,
    remove_manual,
)


class SmartClean(_PluginBase):
    plugin_name = "智能清理"
    plugin_desc = "按电影或剧集季清理 Emby / Jellyfin 媒体库，支持手动清理、规则与自动化。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.3.8"
    plugin_author = "TimoYoung"
    author_url = "https://github.com/TimoYoung"
    plugin_config_prefix = "smartclean_"
    plugin_order = 50
    auth_level = 1

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._main_user = ""
        self._douban_cookie = ""
        self._douban_watched_sync_enabled = True
        self._douban_watched_sync_cron = "0 6 * * *"
        self._lock = threading.Lock()
        self._api = SmartCleanApi(self)
        self._scan_cache = ScanCache(ttl_seconds=300.0)

    def init_plugin(self, config: Optional[dict[str, Any]] = None) -> None:
        config = config or {}
        prev_user = self._main_user
        prev_cookie = self._douban_cookie
        self._enabled = config.get("enabled") is True
        self._main_user = str(config.get("main_user") or "").strip()
        self._douban_cookie = str(config.get("douban_cookie") or "")
        # 缺省开启每天增量；显式 false 才关
        if "douban_watched_sync_enabled" in config:
            self._douban_watched_sync_enabled = config.get("douban_watched_sync_enabled") is True
        else:
            self._douban_watched_sync_enabled = True
        self._douban_watched_sync_cron = (
            str(config.get("douban_watched_sync_cron") or "0 6 * * *").strip() or "0 6 * * *"
        )
        if prev_user != self._main_user or prev_cookie != self._douban_cookie:
            clear_watched_cache()
        logger.info("智能清理已加载")

    def get_state(self) -> bool:
        return self._enabled

    def get_api(self) -> list[dict[str, Any]]:
        return self._api.routes()

    def get_form(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return [], {
            "enabled": self._enabled,
            "main_user": self._main_user,
            "douban_cookie": self._douban_cookie,
            "douban_watched_sync_enabled": self._douban_watched_sync_enabled,
            "douban_watched_sync_cron": self._douban_watched_sync_cron,
        }

    def _douban_user(self) -> str:
        return main_douban_user(self._main_user)

    def _owner_name(self) -> str:
        return resolve_main_user(self._main_user)

    def get_page(self) -> list[dict[str, Any]]:
        return []

    @staticmethod
    def get_render_mode() -> tuple[str, str]:
        return "vue", "dist/assets"

    def get_service(self) -> list[dict[str, Any]]:
        if not self._enabled:
            return []
        services = []
        for library in self._load_state().get("libraries") or []:
            if not library.get("enabled") or not library.get("cron") or not library.get("rule_id"):
                continue
            try:
                trigger = CronTrigger.from_crontab(library["cron"])
            except ValueError:
                logger.warning(f"智能清理：媒体库 {library.get('library_name')} 的定时表达式无效")
                continue
            services.append({
                "id": f"SmartClean.{library['id']}",
                "name": f"智能清理 {library.get('library_name')}",
                "trigger": trigger,
                "func": self._scheduled,
                "func_kwargs": {"library_id": library["id"]},
            })
        if self._douban_watched_sync_enabled:
            try:
                watched_trigger = CronTrigger.from_crontab(self._douban_watched_sync_cron)
            except ValueError:
                logger.warning("智能清理：豆瓣看过同步定时表达式无效")
            else:
                services.append({
                    "id": "SmartClean.douban_watched_sync",
                    "name": "智能清理 豆瓣看过增量同步",
                    "trigger": watched_trigger,
                    "func": self._scheduled_douban_watched_sync,
                    "func_kwargs": {},
                })
        return services

    def stop_service(self) -> None:
        return None

    def _scheduled(self, library_id: str) -> None:
        self._execute(library_id, mode="cron")

    def _scheduled_douban_watched_sync(self) -> None:
        try:
            self._sync_douban_watched(mode="incremental")
        except Exception as err:  # noqa: BLE001
            logger.warning(f"智能清理：定时同步豆瓣看过失败：{err}")

    def _sync_douban_watched(self, *, mode: str = "incremental") -> dict[str, Any]:
        return sync_watched(self._douban_user(), self._douban_cookie, mode=mode)

    def _douban_watched_status(self) -> dict[str, Any]:
        user = self._douban_user()
        if not user:
            return {
                **watchedstore.empty_snapshot(),
                "has_snapshot": False,
                "count": 0,
                "hint": "请先在主用户个人资料填写豆瓣用户",
            }
        return watchedstore.status_of(user)

    def _load_state(self) -> dict[str, Any]:
        return migrate_state(self.get_data("state"))

    def _save_state(self, state: dict[str, Any]) -> None:
        self.save_data("state", state)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _ports(self) -> HostPorts:
        return HostPorts(self._douban_user(), self._douban_cookie)

    def _upsert_library_row(self, state: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
        library = normalize_library(raw, library_row_id=raw.get("id") or None)
        owner = library_owner(state["libraries"], library)
        if owner and owner.get("id") != library["id"]:
            library["id"] = owner["id"]
            library["pending"] = list(owner.get("pending") or [])
            library["history"] = list(owner.get("history") or [])
        state["libraries"] = [item for item in state["libraries"] if item.get("id") != library["id"]]
        state["libraries"].append(library)
        return library

    def _execute(
        self,
        library_id: str,
        *,
        mode: str = "cron",
        dry_run: bool = False,
        only_keys: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._load_state()
            library = find_library(state["libraries"], library_id)
            if library is None:
                raise KeyError("媒体库配置不存在")
            rule = rule_by_id(state["rules"], str(library.get("rule_id") or ""))
            if not dry_run and only_keys is None and rule is None:
                raise KeyError("请先绑定一条规则")
            douban_user = self._douban_user()
            owner = self._owner_name()
            label = "测试" if dry_run else ("执行一次" if mode == "refresh" else "定时")
            logger.info(
                f"智能清理：{label} {library.get('library_name')}，"
                f"主用户 {owner or '未设置'}，豆瓣用户 {douban_user or '未设置'}"
            )
            ports = self._ports()
            updated_library, result = run_automation(
                library,
                rule,
                ports,
                state["whitelist"],
                dry_run=dry_run,
                mode=mode,
                now=self._now(),
                only_keys=only_keys,
                record_hits=only_keys is None,
            )
            if not dry_run:
                state["libraries"] = [
                    updated_library if item.get("id") == library_id else item
                    for item in state["libraries"]
                ]
                self._save_state(state)
            if rule and any(
                cond.get("field", "").startswith("douban") for cond in rule.get("conditions") or []
            ) and not douban_user:
                who = owner or "所选主用户"
                result["errors"].append(f"{who} 还没有填写豆瓣用户。请到该用户的个人资料中填写。")
            return result

    def api_overview(self) -> Any:
        state = self._load_state()
        whitelist = normalize_whitelist(state["whitelist"])
        return ok({
            "enabled": self._enabled,
            "main_user": self._owner_name(),
            "douban_user": self._douban_user(),
            "libraries": copy.deepcopy(state["libraries"]),
            "rules": copy.deepcopy(state["rules"]),
            "whitelist": copy.deepcopy(whitelist),
            "effective": effective_entries(whitelist),
            "douban_watched_sync_enabled": self._douban_watched_sync_enabled,
            "douban_watched_sync_cron": self._douban_watched_sync_cron,
            "douban_watched": self._douban_watched_status(),
        })

    def api_douban_watched_status(self) -> Any:
        return ok(self._douban_watched_status())

    def api_douban_watched_sync(self, payload: DoubanWatchedSyncBody) -> Any:
        mode = "full" if str(payload.mode or "").lower() == "full" else "incremental"
        try:
            result = self._sync_douban_watched(mode=mode)
        except Exception as err:  # noqa: BLE001
            return fail(str(err))
        label = "全量重建" if mode == "full" else "增量更新"
        return ok(result, f"豆瓣看过已{label}（{result.get('count') or 0} 条）")

    def api_libraries(self) -> Any:
        return ok({"libraries": list_libraries()})

    def api_users(self) -> Any:
        return ok({"users": list_usernames()})

    def api_save_rule(self, payload: RuleBody) -> Any:
        try:
            rule = normalize_rule(payload.model_dump(), rule_id=payload.id or None)
        except ValueError as err:
            return fail(str(err))
        with self._lock:
            state = self._load_state()
            if payload.id and rule_by_id(state["rules"], payload.id) is None:
                return fail("规则不存在")
            state["rules"] = [item for item in state["rules"] if item.get("id") != rule["id"]]
            state["rules"].append(rule)
            self._save_state(state)
        return ok(rule, "规则已保存")

    def api_delete_rule(self, payload: RuleAction) -> Any:
        with self._lock:
            state = self._load_state()
            if rule_by_id(state["rules"], payload.rule_id) is None:
                return fail("规则不存在")
            users = libraries_using_rule(state["libraries"], payload.rule_id)
            if users:
                names = "、".join(item.get("library_name") or item.get("library_id") for item in users)
                return fail(f"规则已被媒体库使用：{names}。请先解绑。")
            state["rules"] = [item for item in state["rules"] if item.get("id") != payload.rule_id]
            self._save_state(state)
        return ok({"rule_id": payload.rule_id}, "规则已删除")

    def api_save_automation(self, payload: LibraryAutomationBody) -> Any:
        data = payload.model_dump()
        try:
            library = normalize_library(data, library_row_id=payload.id or None)
        except ValueError as err:
            return fail(str(err))
        if library["cron"]:
            try:
                CronTrigger.from_crontab(library["cron"])
            except ValueError:
                return fail("定时表达式无效")
        with self._lock:
            state = self._load_state()
            if library["rule_id"] and rule_by_id(state["rules"], library["rule_id"]) is None:
                return fail("绑定的规则不存在")
            owner = library_owner(state["libraries"], library)
            if owner and owner.get("id") != library["id"]:
                library["id"] = owner["id"]
                library["pending"] = list(owner.get("pending") or [])
                library["history"] = list(owner.get("history") or [])
            if payload.id and find_library(state["libraries"], payload.id) is None and library["id"] == payload.id:
                return fail("媒体库配置不存在")
            state["libraries"] = [item for item in state["libraries"] if item.get("id") != library["id"]]
            state["libraries"].append(library)
            self._save_state(state)
        return ok(library, "自动化已保存")

    def api_scan(self, payload: ScanBody) -> Any:
        library = {
            "server": payload.server,
            "server_type": payload.server_type,
            "library_id": payload.library_id,
            "library_name": payload.library_name,
            "library_kind": payload.library_kind,
        }
        if not payload.force:
            cached = self._scan_cache.get(payload.server, payload.library_id)
            if cached is not None:
                rows, errors = cached
                return ok({"rows": rows, "errors": errors, "scanned": len(rows), "cached": True})
        else:
            # 强制刷新时丢掉可能截断的豆瓣缓存与该库扫描缓存
            clear_watched_cache()
            self._scan_cache.drop(payload.server, payload.library_id)
        with self._lock:
            whitelist = (self._load_state().get("whitelist") or {})
        # 扫描不持全局锁，避免切库时把其他接口堵死
        rows, errors = scan_rows(library, self._ports(), whitelist, self._now())
        self._scan_cache.put(payload.server, payload.library_id, rows, errors)
        return ok({"rows": rows, "errors": errors, "scanned": len(rows), "cached": False})

    def api_manual_delete(self, payload: ManualDeleteBody) -> Any:
        if not payload.items:
            return fail("没有选择条目")
        library = {
            "server": payload.server,
            "server_type": payload.server_type,
            "library_id": payload.library_id,
            "library_name": payload.library_name,
            "library_kind": payload.library_kind,
            "delete_mode": payload.delete_mode or "library",
        }
        candidates = [_candidate_from_snapshot(item.get("snapshot") or item) for item in payload.items]
        with self._lock:
            state = self._load_state()
            deleted, skipped, history_rows = delete_candidates_now(
                library, candidates, self._ports(), state["whitelist"], now=self._now(),
            )
            owner = library_owner(state["libraries"], library)
            if owner is None:
                try:
                    owner = self._upsert_library_row(state, {**library, "enabled": False, "cron": "", "rule_id": ""})
                except ValueError as err:
                    return fail(str(err))
            owner["history"] = list(owner.get("history") or []) + history_rows
            state["libraries"] = [
                owner if item.get("id") == owner["id"] else item for item in state["libraries"]
            ]
            self._save_state(state)
        self._scan_cache.drop(payload.server, payload.library_id)
        return ok(
            {"deleted": deleted, "skipped": skipped},
            f"已删除 {len(deleted)} 项" + (f"，跳过 {len(skipped)} 项" if skipped else ""),
        )

    def api_manual_whitelist(self, payload: ManualWhitelistBody) -> Any:
        if not payload.items:
            return fail("没有选择条目")
        with self._lock:
            state = self._load_state()
            whitelist = normalize_whitelist(state["whitelist"])
            added = 0
            for item in payload.items:
                snapshot = item.get("snapshot") if isinstance(item, dict) else None
                if not isinstance(snapshot, dict):
                    snapshot = item if isinstance(item, dict) else {}
                whitelist = add_manual(whitelist, pick_from_candidate(_candidate_from_snapshot(snapshot)))
                added += 1
            state["whitelist"] = whitelist
            self._save_state(state)
        return ok({"added": added}, "已加入自定义白名单")

    def api_dry_run(self, payload: LibraryAction) -> Any:
        try:
            result = self._execute(payload.library_id, dry_run=True)
        except KeyError as err:
            return fail(str(err))
        return ok(result)

    def api_run(self, payload: LibraryAction) -> Any:
        try:
            result = self._execute(payload.library_id, mode="refresh")
        except KeyError as err:
            return fail(str(err))
        return ok(result, "已更新待删除清单")

    def api_remove_pending(self, payload: LibraryAction) -> Any:
        if not payload.keys:
            return fail("没有选择条目")
        with self._lock:
            state = self._load_state()
            library = find_library(state["libraries"], payload.library_id)
            if library is None:
                return fail("媒体库配置不存在")
            chosen = set(payload.keys)
            library["pending"] = [item for item in library.get("pending") or [] if item.get("key") not in chosen]
            self._save_state(state)
        return ok({"removed": len(payload.keys)})

    def api_whitelist_pending(self, payload: LibraryAction) -> Any:
        if not payload.keys:
            return fail("没有选择条目")
        with self._lock:
            state = self._load_state()
            library = find_library(state["libraries"], payload.library_id)
            if library is None:
                return fail("媒体库配置不存在")
            chosen = set(payload.keys)
            remained = []
            added = 0
            whitelist = normalize_whitelist(state["whitelist"])
            for item in library.get("pending") or []:
                if item.get("key") not in chosen:
                    remained.append(item)
                    continue
                whitelist = add_manual(
                    whitelist,
                    pick_from_candidate(_candidate_from_snapshot(item.get("snapshot") or {})),
                )
                added += 1
            library["pending"] = remained
            state["whitelist"] = whitelist
            self._save_state(state)
        return ok({"added": added}, "已加入自定义白名单")

    def api_delete_now(self, payload: LibraryAction) -> Any:
        if not payload.keys:
            return fail("没有选择条目")
        try:
            result = self._execute(payload.library_id, dry_run=False, only_keys=set(payload.keys))
        except KeyError as err:
            return fail(str(err))
        return ok(result, "已删除选中项")

    def api_clear_history(self, payload: LibraryAction) -> Any:
        with self._lock:
            state = self._load_state()
            library = find_library(state["libraries"], payload.library_id)
            if library is None:
                return fail("媒体库配置不存在")
            library["history"] = []
            self._save_state(state)
        return ok({"library_id": payload.library_id}, "删除历史已清空")

    def api_whitelist_effective(self) -> Any:
        state = self._load_state()
        return ok({"entries": effective_entries(state["whitelist"])})

    def api_whitelist_exclude(self, payload: ExcludeBody) -> Any:
        if not payload.items:
            return fail("没有选择条目")
        with self._lock:
            state = self._load_state()
            state["whitelist"] = remove_manual(state["whitelist"], payload.items)
            self._save_state(state)
        return ok({"removed": len(payload.items)}, "已移出白名单")
