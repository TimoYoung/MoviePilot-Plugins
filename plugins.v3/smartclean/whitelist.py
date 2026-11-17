"""白名单：仅手动添加（manual）。"""

from __future__ import annotations

from typing import Any, Optional
from uuid import uuid4

from .models import Candidate, Subject
from .titles import parse_title, subjects_match, norm_imdb


def empty_whitelist() -> dict[str, Any]:
    return {"manual": []}


def _text(value: Any) -> str:
    return str(value or "").strip()


def normalize_whitelist_item(raw: dict[str, Any], *, item_id: Optional[str] = None) -> dict[str, Any]:
    """对齐手工点选条目字段。"""
    kind = _text(raw.get("kind")) or "movie"
    if kind not in {"movie", "season"}:
        kind = "movie"
    season = raw.get("season")
    if season is not None and season != "":
        try:
            season = int(season)
        except (TypeError, ValueError):
            season = None
    else:
        season = None
    tmdb = _text(raw.get("tmdb"))
    imdb = norm_imdb(_text(raw.get("imdb") or raw.get("imdb_id")))
    douban_id = _text(raw.get("douban_id") or raw.get("douban"))
    server = _text(raw.get("server"))
    library_id = _text(raw.get("library_id"))
    media_item_id = _text(raw.get("item_id"))
    if tmdb or imdb or douban_id:
        server = library_id = media_item_id = ""
    return {
        "id": item_id or _text(raw.get("id")) or uuid4().hex,
        "kind": kind,
        "title": _text(raw.get("title")),
        "year": _text(raw.get("year")),
        "season": season,
        "tmdb": tmdb,
        "imdb": imdb,
        "douban_id": douban_id,
        "server": server,
        "library_id": library_id,
        "item_id": media_item_id,
        "link": _text(raw.get("link") or raw.get("douban_url")),
        "description": _text(raw.get("description")),
    }


def normalize_whitelist(raw: dict[str, Any]) -> dict[str, Any]:
    """只保留 manual；lists / exclusions / 旧 picks 一律丢弃或折叠为 manual。"""
    if not isinstance(raw, dict):
        return empty_whitelist()
    # 旧版 picks → manual；lists/exclusions 直接丢弃（测试数据，不做并入）
    manual_src = raw.get("manual")
    if manual_src is None and isinstance(raw.get("picks"), list):
        manual_src = raw.get("picks")
    manual = [normalize_whitelist_item(item) for item in (manual_src or [])]
    # 去重
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in manual:
        key = identity_key(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return {"manual": unique}


def identity_key(item: dict[str, Any]) -> str:
    """去重用的稳定身份键。"""
    kind = _text(item.get("kind")) or "movie"
    season = item.get("season")
    season_part = "" if season is None else str(season)
    tmdb = _text(item.get("tmdb"))
    if tmdb:
        return f"{kind}|s{season_part}|tmdb:{tmdb}"
    imdb = norm_imdb(_text(item.get("imdb")))
    if imdb:
        return f"{kind}|s{season_part}|imdb:{imdb}"
    douban_id = _text(item.get("douban_id") or item.get("douban"))
    if douban_id:
        return f"{kind}|s{season_part}|douban:{douban_id}"
    server = _text(item.get("server"))
    library_id = _text(item.get("library_id"))
    item_id = _text(item.get("item_id"))
    return f"{kind}|s{season_part}|item:{server}|{library_id}|{item_id}"


def candidate_identity_key(candidate: Candidate) -> str:
    return identity_key({
        "kind": candidate.kind,
        "season": candidate.season,
        "tmdb": candidate.tmdb,
        "imdb": candidate.imdb,
        "douban_id": candidate.douban,
        "server": candidate.server,
        "library_id": candidate.library_id,
        "item_id": candidate.item_id,
    })


def _subject_from_item(item: dict[str, Any]) -> Subject:
    name, season, ambiguous = parse_title(str(item.get("title") or ""))
    return Subject(
        douban_id=str(item.get("douban_id") or ""),
        title=name or str(item.get("title") or ""),
        year=str(item.get("year") or ""),
        season=season if item.get("season") is None else item.get("season"),
        ambiguous=ambiguous,
        imdb=str(item.get("imdb") or ""),
    )


def _has_external(item: dict[str, Any]) -> bool:
    return bool(item.get("tmdb") or item.get("imdb") or item.get("douban_id"))


def _candidate_has_external(candidate: Candidate) -> bool:
    return bool(candidate.tmdb or candidate.imdb or candidate.douban)


def item_matches(item: dict[str, Any], candidate: Candidate) -> bool:
    """四级匹配：tmdb → imdb → douban → item 兜底。剧集要求 season 一致。"""
    item = normalize_whitelist_item(item)
    if item["kind"] != candidate.kind:
        return False
    if candidate.kind == "season" and item.get("season") is not None and item["season"] != candidate.season:
        return False

    if item.get("tmdb") and candidate.tmdb and item["tmdb"] == candidate.tmdb:
        return True
    if item.get("imdb") and candidate.imdb and norm_imdb(item["imdb"]) == norm_imdb(candidate.imdb):
        return True
    if item.get("douban_id"):
        subject = _subject_from_item(item)
        if candidate.douban and subject.douban_id == candidate.douban and subjects_match(
            candidate, subject, trust_douban_id=True
        ):
            return True
        if subjects_match(candidate, subject, trust_douban_id=True):
            return True
    if not _has_external(item) and not _candidate_has_external(candidate):
        return (
            item.get("server") == candidate.server
            and item.get("library_id") == candidate.library_id
            and item.get("item_id") == candidate.item_id
        )
    return False


def is_whitelisted(whitelist: dict[str, Any], candidate: Candidate) -> bool:
    data = normalize_whitelist(whitelist)
    return any(item_matches(pick, candidate) for pick in data["manual"])


def pick_from_candidate(candidate: Candidate) -> dict[str, Any]:
    external = bool(candidate.tmdb or candidate.imdb or candidate.douban)
    return normalize_whitelist_item({
        "kind": candidate.kind,
        "title": candidate.title,
        "year": candidate.year,
        "season": candidate.season,
        "tmdb": candidate.tmdb,
        "imdb": candidate.imdb,
        "douban_id": candidate.douban,
        "server": "" if external else candidate.server,
        "library_id": "" if external else candidate.library_id,
        "item_id": "" if external else candidate.item_id,
    })


def add_manual(whitelist: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    data = normalize_whitelist(whitelist)
    entry = normalize_whitelist_item(item)
    key = identity_key(entry)
    if not any(identity_key(existing) == key for existing in data["manual"]):
        data["manual"].append(entry)
    return data


def remove_manual(whitelist: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """移出白名单：直接从 manual 删除。"""
    data = normalize_whitelist(whitelist)
    keys = {identity_key(normalize_whitelist_item(raw)) for raw in items}
    data["manual"] = [item for item in data["manual"] if identity_key(item) not in keys]
    return data


def effective_entries(whitelist: dict[str, Any]) -> list[dict[str, Any]]:
    """主列表：受保护项。"""
    data = normalize_whitelist(whitelist)
    return [{"item": item, "sources": ["手动"]} for item in data["manual"]]
