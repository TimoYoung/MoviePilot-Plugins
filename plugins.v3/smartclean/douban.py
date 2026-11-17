"""解析豆瓣看过。优先走 Frodo 接口，避免网页 418。"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Callable, Optional

from .models import Subject
from .scan import parse_time
from .titles import parse_title
from .watchedstore import merge_subjects, page_all_known

_BLOCK = re.compile(r'<div class="item">(.*?)(?:<div class="item">|$)', re.IGNORECASE | re.DOTALL)
_SUBJECT = re.compile(r"/subject/(\d+)/")
_TITLE_LINK = re.compile(r'<a[^>]*href="[^"]*/subject/\d+/[^"]*"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_DATE = re.compile(r'class="date"[^>]*>\s*([^<]+)')
_STARS = re.compile(r"rating([1-5])-t")
_TAG = re.compile(r"<[^>]+>")


def parse_collect_html(html: str) -> list[Subject]:
    """从看过列表页抽出条目。没有看过日期或星级的条目仍保留，筛选时视为不成立。"""
    subjects = []
    seen = set()
    for block in _BLOCK.findall(html or ""):
        identity = _SUBJECT.search(block)
        if not identity:
            continue
        douban_id = identity.group(1)
        if douban_id in seen:
            continue
        seen.add(douban_id)
        title = ""
        for raw_title in _TITLE_LINK.findall(block):
            text = _TAG.sub("", raw_title).strip()
            if text:
                title = text.split("/")[0].strip()
                break
        name, season, ambiguous = parse_title(title)
        date_match = _DATE.search(block)
        watched = parse_time(date_match.group(1).strip()[:10]) if date_match else None
        stars_match = _STARS.search(block)
        stars: Optional[int] = int(stars_match.group(1)) if stars_match else None
        subjects.append(Subject(
            douban_id=douban_id,
            title=name or title,
            season=season,
            ambiguous=ambiguous,
            stars=stars,
            watched_on=watched,
        ))
    return subjects


def _stars_from_interest(item: dict[str, Any]) -> Optional[int]:
    rating = item.get("rating") if isinstance(item.get("rating"), dict) else {}
    raw = rating.get("value", rating.get("star_count"))
    if raw is None:
        return None
    try:
        stars = int(float(raw))
    except (TypeError, ValueError):
        return None
    if 1 <= stars <= 5:
        return stars
    return None


def _year_from_subject(subject: dict[str, Any]) -> str:
    year = str(subject.get("year") or "").strip()
    if year.isdigit():
        return year
    for key in ("card_subtitle", "pubdate", "release_date"):
        text = str(subject.get(key) or "")
        match = re.search(r"(19|20)\d{2}", text)
        if match:
            return match.group(0)
    return ""


def parse_interests_payload(payload: dict[str, Any]) -> list[Subject]:
    """从 Frodo 用户兴趣接口抽出看过条目，星级用用户打分。"""
    subjects = []
    seen = set()
    for item in payload.get("interests") or []:
        if not isinstance(item, dict):
            continue
        subject = item.get("subject") if isinstance(item.get("subject"), dict) else {}
        douban_id = str(subject.get("id") or "").strip()
        if not douban_id or douban_id in seen:
            continue
        seen.add(douban_id)
        title = str(subject.get("title") or "").strip()
        name, season, ambiguous = parse_title(title)
        subjects.append(Subject(
            douban_id=douban_id,
            title=name or title,
            year=_year_from_subject(subject),
            season=season,
            ambiguous=ambiguous,
            stars=_stars_from_interest(item),
            watched_on=parse_time(item.get("create_time")),
            imdb=str(subject.get("imdb_id") or subject.get("imdb") or "").strip(),
        ))
    return subjects


def collect_watched(fetch_page: Callable[[int], dict[str, Any]], *, page_size: int = 50, limit_pages: int = 400) -> list[Subject]:
    """全量拉取（兼容旧调用）。内部走 sync_watched_pages(mode=full)。"""
    result = sync_watched_pages(fetch_page, existing=[], mode="full", page_size=page_size, limit_pages=limit_pages)
    return result["subjects"]


def sync_watched_pages(
    fetch_page: Callable[[int], dict[str, Any]],
    *,
    existing: list[Subject],
    mode: str = "incremental",
    page_size: int = 50,
    limit_pages: int = 400,
    page_sleep: float = 0.8,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """增量或全量分页。增量在「整页已见且不新于水位」时停止；页间 sleep 降频。"""
    force_full = mode == "full" or not existing
    known_ids = {item.douban_id for item in existing}
    watermark_on: Optional[datetime] = None
    for item in existing:
        if item.watched_on is None:
            continue
        if watermark_on is None or item.watched_on > watermark_on:
            watermark_on = item.watched_on

    incoming: list[Subject] = []
    seen_this_run: set[str] = set()
    total: Optional[int] = None
    page = 0
    empty_retries = 0
    pages_fetched = 0
    stopped_early = False

    while page < limit_pages:
        start = page * page_size
        if total is not None and start >= total:
            break
        if pages_fetched > 0 and page_sleep > 0:
            sleep_fn(page_sleep)
        payload = fetch_page(start)
        pages_fetched += 1
        if not isinstance(payload, dict):
            raise RuntimeError(f"豆瓣看过第 {start} 条起返回无效数据")
        if total is None:
            try:
                total = int(payload.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
        batch = parse_interests_payload(payload)
        fresh = [item for item in batch if item.douban_id not in seen_this_run]
        if not fresh:
            if total is not None and start < total and empty_retries < 3:
                empty_retries += 1
                sleep_fn(min(page_sleep * (2 ** empty_retries), 8.0) if page_sleep > 0 else 0)
                continue
            if total is not None and start < total:
                raise RuntimeError(f"豆瓣看过列表在 {start}/{total} 处中断（本页无新条目）")
            break
        empty_retries = 0
        seen_this_run.update(item.douban_id for item in fresh)
        incoming.extend(fresh)

        if not force_full and page_all_known(batch, known_ids, watermark_on):
            stopped_early = True
            break

        page += 1
        if total == 0 and len(batch) < page_size:
            break

    if force_full and total and incoming and len(incoming) < int(total * 0.9):
        raise RuntimeError(f"豆瓣看过列表不完整：仅 {len(incoming)}/{total}")

    merged = merge_subjects(existing, incoming) if existing else incoming
    return {
        "subjects": merged,
        "total": total if total is not None else len(merged),
        "pages_fetched": pages_fetched,
        "stopped_early": stopped_early,
        "mode": "full" if force_full else "incremental",
        "new_or_updated": len(incoming),
    }
