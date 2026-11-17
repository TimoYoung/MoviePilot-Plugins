"""IMDb → 豆瓣 ID：复用 MoviePilot DoubanApi.imdbid，本地持久化缓存。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Optional

from .models import Candidate, Subject
from .titles import matching_subjects, norm_imdb

_LOCK = threading.Lock()
_MEMORY: dict[str, str] = {}
_LOADED = False


def _cache_path() -> Path:
    from app.runtime.config import settings

    root = Path(str(getattr(settings, "CONFIG_DIR", "") or ".")).expanduser()
    return root / "plugins" / "smartclean" / "imdb_douban.json"


def _load_disk() -> None:
    global _LOADED
    if _LOADED:
        return
    path = _cache_path()
    try:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                for key, value in payload.items():
                    imdb = norm_imdb(str(key))
                    douban = str(value or "").strip()
                    if imdb and douban.isdigit():
                        _MEMORY[imdb] = douban
    except Exception:  # noqa: BLE001
        pass
    _LOADED = True


def _save_disk() -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_MEMORY, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def clear_imdb_bridge_cache(*, disk: bool = False) -> None:
    """测试或强制刷新时清空内存映射；disk=True 时同时删除持久化文件。"""
    global _LOADED
    with _LOCK:
        _MEMORY.clear()
        _LOADED = True
        if disk:
            path = _cache_path()
            try:
                if path.is_file():
                    path.unlink()
            except Exception:  # noqa: BLE001
                pass
            _LOADED = False


def cached_douban_id(imdb: str) -> str:
    key = norm_imdb(imdb)
    if not key:
        return ""
    with _LOCK:
        _load_disk()
        return _MEMORY.get(key, "")


def remember_douban_id(imdb: str, douban_id: str) -> None:
    key = norm_imdb(imdb)
    douban = str(douban_id or "").strip()
    if not key or not douban.isdigit():
        return
    with _LOCK:
        _load_disk()
        if _MEMORY.get(key) == douban:
            return
        _MEMORY[key] = douban
        _save_disk()


def resolve_douban_id_by_imdb(imdb: str) -> str:
    """优先读缓存；未命中时走 Frodo imdbid（与 Douban.match_doubaninfo 的 IMDb 分支同源）。"""
    key = norm_imdb(imdb)
    if not key:
        return ""
    hit = cached_douban_id(key)
    if hit:
        return hit
    try:
        from app.modules.douban import Douban
        from app.modules.douban.apiv2 import DoubanApi

        raw = DoubanApi().imdbid(key)
        processed = Douban._process_imdbid_result(raw, key)
    except Exception:  # noqa: BLE001
        return ""
    douban_id = str((processed or {}).get("id") or "").strip()
    if douban_id.isdigit():
        remember_douban_id(key, douban_id)
        return douban_id
    return ""


def enrich_candidates_with_douban(
    candidates: list[Candidate],
    subjects: list[Subject],
    resolve_imdb: Optional[Callable[[str], str]] = None,
) -> int:
    """为缺豆瓣 ID 的候选补齐身份：先用看过列表里的 IMDb，再对标题未命中项调桥接。

    返回实际发起 resolve 的次数（不含缓存命中前的 subject.imdb 回填）。
    """
    by_imdb: dict[str, str] = {}
    for subject in subjects:
        key = norm_imdb(subject.imdb)
        if key and subject.douban_id:
            by_imdb[key] = subject.douban_id

    resolved = 0
    for candidate in candidates:
        if candidate.douban:
            continue
        imdb = norm_imdb(candidate.imdb)
        if not imdb:
            continue
        if imdb in by_imdb:
            candidate.douban = by_imdb[imdb]
            continue
        cached = cached_douban_id(imdb)
        if cached:
            candidate.douban = cached
            by_imdb[imdb] = cached
            continue
        # 标题年份已能对上看过列表时不必打豆瓣
        if matching_subjects(candidate, subjects, trust_douban_id=False):
            continue
        if resolve_imdb is None:
            continue
        douban_id = str(resolve_imdb(imdb) or "").strip()
        if not douban_id.isdigit():
            continue
        candidate.douban = douban_id
        by_imdb[imdb] = douban_id
        remember_douban_id(imdb, douban_id)
        resolved += 1
    return resolved
