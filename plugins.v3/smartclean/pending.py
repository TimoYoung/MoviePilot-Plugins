"""待删除清单：命中后冻结，计时不重置，也不因为不再命中而移出。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .models import Candidate


def _pending_row(hit: dict[str, Any], started_at: str) -> dict[str, Any]:
    return {
        "key": hit["key"],
        "title": hit["title"],
        "year": hit.get("year") or "",
        "season": hit.get("season"),
        "reason": hit["reason"],
        "started_at": started_at,
        "snapshot": hit["snapshot"],
        "detail": hit.get("detail") or {},
        "attrs": hit.get("attrs") or {},
        "hit_fields": list(hit.get("hit_fields") or []),
    }


def replace_pending(hits: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """执行一次：整表替换，每条重新计时。"""
    stamp = now.isoformat(timespec="seconds")
    return [_pending_row(hit, stamp) for hit in hits]


def merge_pending(existing: list[dict[str, Any]], hits: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """定时：已有条目保留开始时间，只刷新展示字段与快照；新命中追加。"""
    kept = {item["key"]: dict(item) for item in existing}
    stamp = now.isoformat(timespec="seconds")
    for hit in hits:
        current = kept.get(hit["key"])
        if current is None:
            kept[hit["key"]] = _pending_row(hit, stamp)
            continue
        current["snapshot"] = hit["snapshot"]
        current["title"] = hit["title"]
        current["year"] = hit.get("year") or current.get("year") or ""
        current["season"] = hit.get("season") if hit.get("season") is not None else current.get("season")
        current["reason"] = hit["reason"]
        if hit.get("detail") is not None:
            current["detail"] = hit.get("detail") or {}
        if hit.get("attrs") is not None:
            current["attrs"] = hit.get("attrs") or {}
        if hit.get("hit_fields") is not None:
            current["hit_fields"] = list(hit.get("hit_fields") or [])
    return list(kept.values())


def is_due(item: dict[str, Any], delay_days: int, now: datetime) -> bool:
    """delay_days<=0 视为已到期。"""
    if delay_days <= 0:
        return True
    started = datetime.fromisoformat(item["started_at"])
    return now >= started + timedelta(days=delay_days)


def hit_from_candidate(
    candidate: Candidate,
    reason: str,
    detail: Optional[dict[str, Any]] = None,
    *,
    attrs: Optional[dict[str, Any]] = None,
    hit_fields: Optional[list[str]] = None,
) -> dict[str, Any]:
    return {
        "key": candidate.key,
        "title": candidate.title,
        "year": candidate.year,
        "season": candidate.season,
        "reason": reason,
        "snapshot": candidate.snapshot(),
        "detail": detail or {},
        "attrs": attrs or {},
        "hit_fields": list(hit_fields or []),
    }
