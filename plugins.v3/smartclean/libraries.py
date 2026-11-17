"""每库一条自动化配置。pending/history 挂在库上。"""

from __future__ import annotations

from typing import Any, Optional
from uuid import uuid4

DELETE_MODES = {"source", "library", "all"}
LIBRARY_KINDS = {"movie", "tv"}


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, low), high)


def library_key(server: str, library_id: str) -> str:
    return f"{server}|{library_id}"


def normalize_library(raw: dict[str, Any], *, library_row_id: Optional[str] = None) -> dict[str, Any]:
    kind = raw.get("library_kind")
    mode = raw.get("delete_mode") or "library"
    if kind not in LIBRARY_KINDS:
        raise ValueError("媒体库类型只能是电影或剧集")
    if mode not in DELETE_MODES:
        raise ValueError("删除方式无效")
    server = str(raw.get("server") or "").strip()
    library_id = str(raw.get("library_id") or "").strip()
    library_name = str(raw.get("library_name") or "").strip()
    server_type = str(raw.get("server_type") or "").strip().lower()
    if not server or not library_id or not library_name:
        raise ValueError("需要指定媒体服务器和媒体库")
    if server_type not in {"emby", "jellyfin"}:
        raise ValueError("只支持 Emby 和 Jellyfin")
    cron = str(raw.get("cron") or "").strip()
    rule_id = str(raw.get("rule_id") or "").strip()
    return {
        "id": library_row_id or str(raw.get("id") or "") or uuid4().hex,
        "server": server,
        "server_type": server_type,
        "library_id": library_id,
        "library_name": library_name,
        "library_kind": kind,
        "enabled": bool(raw.get("enabled", False)),
        "cron": cron,
        "delete_mode": mode,
        "delay_days": _int(raw.get("delay_days"), 0, 0, 3650),
        "rule_id": rule_id,
        "pending": list(raw.get("pending") or []),
        "history": list(raw.get("history") or []),
    }


def library_owner(libraries: list[dict[str, Any]], library: dict[str, Any]) -> Optional[dict[str, Any]]:
    for current in libraries:
        if current.get("server") == library.get("server") and current.get("library_id") == library.get("library_id"):
            return current
    return None


def find_library(libraries: list[dict[str, Any]], library_row_id: str) -> Optional[dict[str, Any]]:
    return next((item for item in libraries if item.get("id") == library_row_id), None)


def find_library_by_ref(
    libraries: list[dict[str, Any]],
    *,
    server: str,
    library_id: str,
) -> Optional[dict[str, Any]]:
    for item in libraries:
        if item.get("server") == server and item.get("library_id") == library_id:
            return item
    return None
