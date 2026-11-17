"""通用规则条件。缺数据的条件不成立，不会因为未知被删掉。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .models import Candidate, Subject, TransferFact
from .rules import FIELD_LABELS
from .titles import matching_subjects, norm_imdb
from .whitelist import is_whitelisted


def _age_reached(moment: datetime, days: int, now: datetime) -> bool:
    return now - moment >= timedelta(days=max(int(days or 0), 0))


def _days_between(moment: datetime, now: datetime) -> int:
    return max(int((now - moment).total_seconds() // 86400), 0)


def _identity_hits(candidate: Candidate, fact: TransferFact) -> bool:
    if fact.source == "tmdb" and candidate.tmdb and fact.media_id == candidate.tmdb:
        matched = True
    elif fact.source == "imdb" and candidate.imdb and norm_imdb(fact.media_id) == norm_imdb(candidate.imdb):
        matched = True
    else:
        return False
    if candidate.kind == "movie":
        return matched and not fact.seasons
    return matched and candidate.season is not None and candidate.season in fact.seasons


def matched_douban_subject(candidate: Candidate, subjects: list[Subject]) -> Optional[Subject]:
    found = matching_subjects(candidate, subjects, trust_douban_id=False)
    return found[0] if len(found) == 1 else None


def candidate_transfers(candidate: Candidate, facts: list[TransferFact]) -> list[TransferFact]:
    own = [fact for fact in facts if _identity_hits(candidate, fact)]
    return sorted(own, key=lambda item: item.transferred_at, reverse=True)


def playback_summary(candidate: Candidate) -> dict[str, Any]:
    plays = candidate.plays or []
    if not plays:
        return {
            "has_data": False,
            "unplayed": False,
            "last_played": "",
            "play_count": None,
            "episode_count": 0,
        }
    counts = [play.play_count for play in plays if play.play_count is not None]
    played_times = [play.last_played for play in plays if play.last_played is not None]
    has_data = bool(counts or played_times)
    unplayed = has_data and all((play.play_count or 0) == 0 for play in plays) and not played_times
    return {
        "has_data": has_data,
        "unplayed": unplayed,
        "last_played": _iso(max(played_times) if played_times else None),
        "play_count": max(counts) if counts else None,
        "episode_count": len(plays),
    }


def _iso(moment: Optional[datetime]) -> str:
    """统一到秒；前端直接展示，不再截成日期。"""
    return moment.isoformat(timespec="seconds") if moment else ""


def _compare_number(left: Optional[int], op: str, right: int) -> bool:
    if left is None:
        return False
    if op == "eq":
        return left == right
    if op == "gte":
        return left >= right
    if op == "lte":
        return left <= right
    return False


def _has_source(candidate: Candidate, facts: list[TransferFact]) -> Optional[bool]:
    """仅依据 MoviePilot 转移记录里的源路径；无转移记录时未知（None）。"""
    own = candidate_transfers(candidate, facts)
    if not own:
        return None
    if any(bool(fact.fileitem and fact.path_key[1]) for fact in own):
        return True
    return False


def _playback_stale(candidate: Candidate, days: int, now: datetime) -> Optional[bool]:
    if not candidate.plays:
        return None
    summary = playback_summary(candidate)
    if not summary["has_data"]:
        return None
    if summary["unplayed"]:
        return True
    for play in candidate.plays:
        if play.last_played is None or not _age_reached(play.last_played, days, now):
            return False
    return True


def evaluate_condition(
    condition: dict[str, Any],
    candidate: Candidate,
    *,
    subjects: list[Subject],
    facts: list[TransferFact],
    now: datetime,
    whitelist: Optional[dict[str, Any]] = None,
    transfer_user_scope: Optional[set[str]] = None,
) -> bool:
    field = condition.get("field")
    op = condition.get("op")
    value = condition.get("value")

    if field == "title":
        title = candidate.title or ""
        needle = str(value or "")
        if op == "contains":
            return bool(needle) and needle.lower() in title.lower()
        return title == needle

    if field == "year":
        try:
            year = int(candidate.year) if candidate.year else None
        except ValueError:
            year = None
        return _compare_number(year, op, int(value))

    if field == "season":
        return _compare_number(candidate.season, op, int(value))

    if field == "douban_stars":
        subject = matched_douban_subject(candidate, subjects)
        if subject is None or subject.stars is None:
            return False
        return _compare_number(subject.stars, op, int(value))

    if field == "douban_watched_days":
        subject = matched_douban_subject(candidate, subjects)
        if subject is None or subject.watched_on is None:
            return False
        return _compare_number(_days_between(subject.watched_on, now), op, int(value))

    if field == "transfer_users":
        names = set(value or [])
        own = candidate_transfers(candidate, facts)
        return any(fact.username in names for fact in own)

    if field == "transfer_days":
        own = candidate_transfers(candidate, facts)
        if transfer_user_scope:
            own = [fact for fact in own if fact.username in transfer_user_scope]
        if not own:
            return False
        latest = max(fact.transferred_at for fact in own)
        return _compare_number(_days_between(latest, now), op, int(value))

    if field == "library_age_days":
        if candidate.added_at is None:
            return False
        return _compare_number(_days_between(candidate.added_at, now), op, int(value))

    if field == "playback_unplayed":
        summary = playback_summary(candidate)
        if not summary["has_data"]:
            return False
        return summary["unplayed"] is bool(value)

    if field == "playback_stale_days":
        stale = _playback_stale(candidate, int(value), now)
        return bool(stale)

    if field == "favorite":
        return bool(candidate.favorite) is bool(value)

    if field == "has_source":
        return _has_source(candidate, facts) is bool(value)

    if field == "whitelisted":
        return is_whitelisted(whitelist or {}, candidate) is bool(value)

    return False


def evaluate_rule(
    candidate: Candidate,
    rule: dict[str, Any],
    *,
    subjects: list[Subject],
    facts: list[TransferFact],
    now: datetime,
    whitelist: Optional[dict[str, Any]] = None,
) -> tuple[bool, list[str]]:
    """返回是否命中，以及命中的条件标签列表。"""
    conditions = rule.get("conditions") or []
    if not conditions:
        return False, []
    scoped_users: set[str] = set()
    for condition in conditions:
        if condition.get("field") == "transfer_users":
            scoped_users.update(condition.get("value") or [])
    hits: list[str] = []
    for condition in conditions:
        ok = evaluate_condition(
            condition,
            candidate,
            subjects=subjects,
            facts=facts,
            now=now,
            whitelist=whitelist,
            transfer_user_scope=scoped_users or None,
        )
        if ok:
            label = FIELD_LABELS.get(condition.get("field") or "", condition.get("field") or "")
            hits.append(label)
    combine = rule.get("combine") or "all"
    if combine == "any":
        return bool(hits), hits
    if len(hits) != len(conditions):
        return False, hits
    return True, hits


def deletion_reason(
    candidate: Candidate,
    rule: dict[str, Any],
    *,
    subjects: list[Subject],
    facts: list[TransferFact],
    now: datetime,
    whitelist: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    matched, hits = evaluate_rule(
        candidate,
        rule,
        subjects=subjects,
        facts=facts,
        now=now,
        whitelist=whitelist,
    )
    if not matched:
        return None
    return "、".join(hits)


def hit_evidence(
    candidate: Candidate,
    rule: dict[str, Any],
    *,
    subjects: list[Subject],
    facts: list[TransferFact],
    now: datetime,
    whitelist: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    transfers = candidate_transfers(candidate, facts)
    subject = matched_douban_subject(candidate, subjects)
    play = playback_summary(candidate)
    scoped_users: set[str] = set()
    for condition in rule.get("conditions") or []:
        if condition.get("field") == "transfer_users":
            scoped_users.update(condition.get("value") or [])
    condition_hits = {}
    for condition in rule.get("conditions") or []:
        key = f"{condition.get('field')}:{condition.get('op')}"
        condition_hits[key] = evaluate_condition(
            condition,
            candidate,
            subjects=subjects,
            facts=facts,
            now=now,
            whitelist=whitelist,
            transfer_user_scope=scoped_users or None,
        )
    douban = None
    if subject is not None:
        douban = {
            "stars": subject.stars,
            "watched_on": _iso(subject.watched_on),
            "title": subject.title,
            "douban_id": subject.douban_id,
        }
    return {
        "added_at": _iso(candidate.added_at),
        "favorite": bool(candidate.favorite),
        "whitelisted": is_whitelisted(whitelist or {}, candidate),
        "douban": douban,
        "transfers": [
            {"username": fact.username, "transferred_at": _iso(fact.transferred_at)}
            for fact in transfers
        ],
        "playback": play,
        "has_source": _has_source(candidate, facts),
        "conditions": condition_hits,
    }


def row_attributes(
    candidate: Candidate,
    *,
    subjects: list[Subject],
    facts: list[TransferFact],
    whitelist: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """手动页全属性行。"""
    transfers = candidate_transfers(candidate, facts)
    subject = matched_douban_subject(candidate, subjects)
    play = playback_summary(candidate)
    latest = transfers[0] if transfers else None
    library_age_days = _days_between(candidate.added_at, now) if candidate.added_at else None
    douban_watched_days = None
    if subject and subject.watched_on:
        douban_watched_days = _days_between(subject.watched_on, now)
    if latest is not None:
        # 同一用户多次入库只保留一次（按首次出现顺序）
        transfer_users = list(dict.fromkeys(
            fact.username for fact in transfers if str(fact.username or "").strip()
        ))
        latest_transfer_at = _iso(latest.transferred_at)
        latest_transfer_user = latest.username
        transfer_days = _days_between(latest.transferred_at, now)
        transfer_origin = "moviepilot"
    else:
        # 入库/有源只认 MoviePilot 整理记录；在库时间单独用媒体库 added_at
        transfer_users = []
        latest_transfer_at = ""
        latest_transfer_user = ""
        transfer_days = None
        transfer_origin = ""
    return {
        **candidate.public(),
        "snapshot": candidate.snapshot(),
        "whitelisted": is_whitelisted(whitelist, candidate),
        "has_source": _has_source(candidate, facts),
        "library_age_days": library_age_days,
        "transfer_users": transfer_users,
        "latest_transfer_at": latest_transfer_at,
        "latest_transfer_user": latest_transfer_user,
        "transfer_days": transfer_days,
        "transfer_origin": transfer_origin,
        "douban_stars": subject.stars if subject else None,
        "douban_watched_on": _iso(subject.watched_on) if subject and subject.watched_on else "",
        "douban_watched_days": douban_watched_days,
        "douban_title": subject.title if subject else "",
        "douban_id": subject.douban_id if subject else (candidate.douban or ""),
        "playback": play,
        "added_at": _iso(candidate.added_at),
    }
