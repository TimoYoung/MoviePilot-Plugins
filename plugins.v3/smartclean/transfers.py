"""把 MoviePilot 的转移记录和下载用户收成入库事实。"""

from __future__ import annotations

from typing import Any

from .models import Candidate, TransferFact
from .scan import parse_time
from .titles import norm_imdb, season_numbers


def facts_from_records(
    transfers: list[Any],
    usernames: dict[str, str],
) -> list[TransferFact]:
    """时间来自转移记录；用户名优先下载历史，缺失时记为「未知」以便仍能展示入库/有源。"""
    facts = []
    for row in transfers:
        if getattr(row, "status", True) is False:
            continue
        download_hash = str(getattr(row, "download_hash", "") or "")
        username = usernames.get(download_hash) or "未知"
        moment = parse_time(getattr(row, "date", None))
        if moment is None:
            continue
        source = str(getattr(row, "media_source", "") or "").strip().lower()
        media_id = str(getattr(row, "media_id", "") or "")
        if source in {"themoviedb", "tmdb"}:
            kind = "tmdb"
        elif source in {"imdb"}:
            kind = "imdb"
            media_id = norm_imdb(media_id)
        else:
            continue
        if not media_id:
            continue
        fileitem = getattr(row, "src_fileitem", None)
        if not isinstance(fileitem, dict) or not fileitem:
            # 部分记录只写了 src 路径
            src = str(getattr(row, "src", "") or "").strip()
            storage = str(getattr(row, "src_storage", "") or "local").strip() or "local"
            fileitem = {"path": src, "storage": storage} if src else None
        facts.append(TransferFact(
            username=username,
            transferred_at=moment,
            source=kind,
            media_id=media_id,
            seasons=season_numbers(getattr(row, "seasons", None)),
            fileitem=fileitem,
        ))
    return facts


def candidate_queries(candidates: list[Candidate]) -> list[tuple[str, str, str]]:
    """返回需要查询的 (媒体类型, 数据源, id)。电影和剧集分开。"""
    queries = []
    seen = set()
    for candidate in candidates:
        mtype = "电影" if candidate.kind == "movie" else "电视剧"
        pairs = []
        if candidate.tmdb:
            pairs.append(("themoviedb", candidate.tmdb))
        if candidate.imdb:
            pairs.append(("imdb", norm_imdb(candidate.imdb)))
        for source, media_id in pairs:
            key = (mtype, source, media_id)
            if key not in seen:
                seen.add(key)
                queries.append(key)
    return queries
