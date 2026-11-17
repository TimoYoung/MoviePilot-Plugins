"""把 Emby / Jellyfin 条目收成电影和季。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from .models import Candidate, Play
from .titles import norm_imdb

_FRACTION = re.compile(r"\.(\d+)")


def parse_time(value: Any) -> Optional[datetime]:
    """解析媒体库时间。带时区的值折成 UTC 的朴素时间，便于和任务时钟比较。"""
    if value is None or value == "":
        return None
    text = str(value).strip().replace("Z", "+00:00")
    text = _FRACTION.sub(lambda match: "." + match.group(1)[:6], text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def provider(item: dict[str, Any], *names: str) -> str:
    wanted = {name.lower() for name in names}
    raw = item.get("ProviderIds") or {}
    if not isinstance(raw, dict):
        return ""
    for key, value in raw.items():
        if str(key).lower() in wanted and value not in (None, ""):
            text = str(value).strip()
            if "imdb" in wanted:
                return norm_imdb(text)
            return text
    return ""


def user_state(item: dict[str, Any]) -> tuple[Optional[int], Optional[datetime], bool]:
    data = item.get("UserData") if isinstance(item.get("UserData"), dict) else {}
    play = data.get("PlayCount", item.get("UserDataPlayCount"))
    last = data.get("LastPlayedDate", item.get("UserDataLastPlayedDate"))
    favorite = bool(data.get("IsFavorite") or item.get("UserDataIsFavorite"))
    try:
        count = int(play) if play is not None and play != "" else None
    except (TypeError, ValueError):
        count = None
    return count, parse_time(last), favorite


def is_virtual(item: dict[str, Any]) -> bool:
    return str(item.get("LocationType") or "").lower() == "virtual" or bool(item.get("IsMissing"))


def candidate_key(
    server: str,
    library_id: str,
    kind: str,
    identity: str,
    season: Optional[int],
    *,
    item_id: str,
) -> str:
    """行主键必须含媒体库 item_id：同一 TMDB 多副本时否则 Vue :key 冲突，排序会「钉死」一行。"""
    unique = str(item_id or identity or "").strip()
    if kind == "movie":
        return f"{server}|{library_id}|movie|{identity}|{unique}"
    return f"{server}|{library_id}|season|{identity}|{season}|{unique}"


def _identity(tmdb: str, imdb: str, item_id: str) -> str:
    return tmdb or imdb or item_id


def build_movie(item: dict[str, Any], *, server: str, server_type: str, library_id: str, library_name: str) -> Optional[Candidate]:
    if is_virtual(item):
        return None
    item_id = str(item.get("Id") or "")
    title = str(item.get("Name") or "").strip()
    if not item_id or not title:
        return None
    tmdb = provider(item, "tmdb")
    imdb = provider(item, "imdb")
    count, last, favorite = user_state(item)
    identity = _identity(tmdb, imdb, item_id)
    return Candidate(
        key=candidate_key(server, library_id, "movie", identity, None, item_id=item_id),
        kind="movie",
        server=server,
        server_type=server_type,
        library_id=library_id,
        library_name=library_name,
        item_id=item_id,
        title=title,
        year=str(item.get("ProductionYear") or ""),
        tmdb=tmdb,
        imdb=imdb,
        douban=provider(item, "douban", "doubanid"),
        added_at=parse_time(item.get("DateCreated")),
        favorite=favorite,
        path=str(item.get("Path") or "").strip(),
        plays=[Play(count, last)],
    )


def build_seasons(
    series_items: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    season_items: list[dict[str, Any]],
    *,
    server: str,
    server_type: str,
    library_id: str,
    library_name: str,
) -> list[Candidate]:
    """按剧和季号聚合单集。没有季号的单集不进入候选。"""
    series_by_id = {str(item.get("Id") or ""): item for item in series_items if item.get("Id")}
    favorite_seasons = set()
    for item in season_items:
        if is_virtual(item):
            continue
        _, _, favorite = user_state(item)
        series_id = str(item.get("SeriesId") or item.get("ParentId") or "")
        number = item.get("IndexNumber")
        if favorite and series_id and number is not None:
            favorite_seasons.add((series_id, int(number)))
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for episode in episodes:
        if is_virtual(episode):
            continue
        series_id = str(episode.get("SeriesId") or "")
        number = episode.get("ParentIndexNumber")
        if not series_id or number is None or not episode.get("Id"):
            continue
        grouped.setdefault((series_id, int(number)), []).append(episode)
    candidates = []
    for (series_id, season), rows in sorted(grouped.items()):
        series = series_by_id.get(series_id, {})
        title = str(series.get("Name") or rows[0].get("SeriesName") or "").strip()
        if not title:
            continue
        tmdb = provider(series, "tmdb") or provider(rows[0], "tmdb")
        imdb = provider(series, "imdb") or provider(rows[0], "imdb")
        douban = provider(series, "douban", "doubanid") or provider(rows[0], "douban", "doubanid")
        added = [parse_time(row.get("DateCreated")) for row in rows]
        present = [item for item in added if item is not None]
        plays = []
        favorite = (series_id, season) in favorite_seasons
        episode_ids = []
        for row in rows:
            count, last, row_favorite = user_state(row)
            plays.append(Play(count, last))
            favorite = favorite or row_favorite
            episode_ids.append(str(row.get("Id")))
        identity = _identity(tmdb, imdb, series_id)
        path = next((str(row.get("Path") or "").strip() for row in rows if row.get("Path")), "")
        candidates.append(Candidate(
            key=candidate_key(server, library_id, "season", identity, season, item_id=series_id),
            kind="season",
            server=server,
            server_type=server_type,
            library_id=library_id,
            library_name=library_name,
            item_id=series_id,
            title=title,
            year=str(series.get("ProductionYear") or ""),
            season=season,
            tmdb=tmdb,
            imdb=imdb,
            douban=douban,
            added_at=max(present) if present else None,
            favorite=favorite,
            path=path,
            plays=plays,
            episode_ids=episode_ids,
        ))
    return candidates
