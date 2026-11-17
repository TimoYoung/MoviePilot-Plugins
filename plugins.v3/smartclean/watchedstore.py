"""豆瓣看过本地快照：落盘、水位与合并。扫描只读此处，不打豆瓣。"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .models import Subject
from .scan import parse_time

_LOCK = threading.Lock()
_SAFE_USER = re.compile(r"[^A-Za-z0-9_.-]+")


def _cache_dir() -> Path:
    from app.runtime.config import settings

    root = Path(str(getattr(settings, "CONFIG_DIR", "") or ".")).expanduser()
    return root / "plugins" / "smartclean"


def _user_key(user: str) -> str:
    cleaned = _SAFE_USER.sub("_", str(user or "").strip()) or "unknown"
    return cleaned[:80]


def store_path(user: str) -> Path:
    return _cache_dir() / f"watched_{_user_key(user)}.json"


def subject_to_dict(subject: Subject) -> dict[str, Any]:
    return {
        "douban_id": subject.douban_id,
        "title": subject.title,
        "year": subject.year or "",
        "season": subject.season,
        "ambiguous": bool(subject.ambiguous),
        "stars": subject.stars,
        "watched_on": subject.watched_on.isoformat(timespec="seconds") if subject.watched_on else "",
        "imdb": subject.imdb or "",
    }


def subject_from_dict(raw: dict[str, Any]) -> Optional[Subject]:
    douban_id = str(raw.get("douban_id") or "").strip()
    if not douban_id:
        return None
    return Subject(
        douban_id=douban_id,
        title=str(raw.get("title") or "").strip(),
        year=str(raw.get("year") or "").strip(),
        season=raw.get("season"),
        ambiguous=bool(raw.get("ambiguous")),
        stars=raw.get("stars") if isinstance(raw.get("stars"), int) else None,
        watched_on=parse_time(raw.get("watched_on")),
        imdb=str(raw.get("imdb") or "").strip(),
    )


def empty_snapshot(user: str = "") -> dict[str, Any]:
    return {
        "user": str(user or ""),
        "subjects": [],
        "updated_at": "",
        "total": 0,
        "watermark": {"douban_id": "", "watched_on": ""},
        "last_error": "",
        "last_sync_mode": "",
        "pages_fetched": 0,
    }


def load_snapshot(user: str) -> dict[str, Any]:
    path = store_path(user)
    with _LOCK:
        try:
            if not path.is_file():
                return empty_snapshot(user)
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return empty_snapshot(user)
            subjects = []
            for item in payload.get("subjects") or []:
                if isinstance(item, dict):
                    subjects.append(item)
            watermark = payload.get("watermark") if isinstance(payload.get("watermark"), dict) else {}
            return {
                "user": str(payload.get("user") or user),
                "subjects": subjects,
                "updated_at": str(payload.get("updated_at") or ""),
                "total": int(payload.get("total") or len(subjects)),
                "watermark": {
                    "douban_id": str(watermark.get("douban_id") or ""),
                    "watched_on": str(watermark.get("watched_on") or ""),
                },
                "last_error": str(payload.get("last_error") or ""),
                "last_sync_mode": str(payload.get("last_sync_mode") or ""),
                "pages_fetched": int(payload.get("pages_fetched") or 0),
            }
        except Exception:  # noqa: BLE001
            return empty_snapshot(user)


def load_subjects(user: str) -> list[Subject]:
    snapshot = load_snapshot(user)
    found: list[Subject] = []
    for raw in snapshot.get("subjects") or []:
        subject = subject_from_dict(raw)
        if subject is not None:
            found.append(subject)
    return found


def status_of(user: str) -> dict[str, Any]:
    snap = load_snapshot(user)
    return {
        "user": snap.get("user") or user,
        "count": len(snap.get("subjects") or []),
        "updated_at": snap.get("updated_at") or "",
        "total": snap.get("total") or 0,
        "watermark": snap.get("watermark") or {},
        "last_error": snap.get("last_error") or "",
        "last_sync_mode": snap.get("last_sync_mode") or "",
        "pages_fetched": snap.get("pages_fetched") or 0,
        "has_snapshot": bool(snap.get("subjects") or snap.get("updated_at")),
    }


def _watermark_from_subjects(subjects: list[Subject]) -> dict[str, str]:
    """水位取 watched_on 最新的一条；并列时取 douban_id 较大者。"""
    best: Optional[Subject] = None
    for item in subjects:
        if item.watched_on is None:
            continue
        if best is None or item.watched_on > best.watched_on:
            best = item
            continue
        if item.watched_on == best.watched_on and item.douban_id > best.douban_id:
            best = item
    if best is None and subjects:
        best = subjects[0]
    if best is None:
        return {"douban_id": "", "watched_on": ""}
    return {
        "douban_id": best.douban_id,
        "watched_on": best.watched_on.isoformat(timespec="seconds") if best.watched_on else "",
    }


def merge_subjects(existing: list[Subject], incoming: list[Subject]) -> list[Subject]:
    by_id = {item.douban_id: item for item in existing}
    for item in incoming:
        by_id[item.douban_id] = item
    return list(by_id.values())


def save_snapshot(
    user: str,
    subjects: list[Subject],
    *,
    total: Optional[int] = None,
    mode: str = "",
    pages_fetched: int = 0,
    last_error: str = "",
    updated_at: Optional[datetime] = None,
) -> dict[str, Any]:
    stamp = (updated_at or datetime.now()).isoformat(timespec="seconds")
    rows = [subject_to_dict(item) for item in subjects]
    payload = {
        "user": str(user or ""),
        "subjects": rows,
        "updated_at": stamp,
        "total": int(total if total is not None else len(rows)),
        "watermark": _watermark_from_subjects(subjects),
        "last_error": str(last_error or ""),
        "last_sync_mode": str(mode or ""),
        "pages_fetched": int(pages_fetched or 0),
    }
    path = store_path(user)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def record_sync_error(user: str, error: str, *, mode: str = "") -> dict[str, Any]:
    """失败时保留已有 subjects，只写 last_error。"""
    snap = load_snapshot(user)
    subjects = []
    for raw in snap.get("subjects") or []:
        item = subject_from_dict(raw)
        if item is not None:
            subjects.append(item)
    return save_snapshot(
        user,
        subjects,
        total=snap.get("total"),
        mode=mode or str(snap.get("last_sync_mode") or ""),
        pages_fetched=int(snap.get("pages_fetched") or 0),
        last_error=error,
        updated_at=parse_time(snap.get("updated_at")) or datetime.now(),
    )


def page_all_known(
    batch: list[Subject],
    known_ids: set[str],
    watermark_on: Optional[datetime],
) -> bool:
    """本页全部已见，且都不新于水位 → 增量可停。"""
    if not batch:
        return False
    if not known_ids:
        return False
    for item in batch:
        if item.douban_id not in known_ids:
            return False
        if watermark_on is None:
            continue
        if item.watched_on is not None and item.watched_on > watermark_on:
            return False
    return True


def clear_snapshot(user: str) -> None:
    path = store_path(user)
    with _LOCK:
        try:
            if path.is_file():
                path.unlink()
        except Exception:  # noqa: BLE001
            pass
