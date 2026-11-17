"""一次自动化执行：扫描结果、冻结清单和到期删除。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from .filters import deletion_reason, hit_evidence, row_attributes
from .models import Candidate, Subject, TransferFact
from .pending import hit_from_candidate, is_due, merge_pending, replace_pending
from .titles import season_numbers
from .whitelist import is_whitelisted, pick_from_candidate


class Ports:
    """宿主能力。测试用假实现替换。"""

    def scan(self, library: dict[str, Any]) -> tuple[list[Candidate], list[str]]:
        raise NotImplementedError

    def transfers(self, candidates: list[Candidate]) -> list[TransferFact]:
        raise NotImplementedError

    def watched(self) -> tuple[list[Subject], Optional[str]]:
        raise NotImplementedError

    def resolve_douban_by_imdb(self, imdb: str) -> str:
        """IMDb → 豆瓣 ID。默认空；HostPorts 走 DoubanApi.imdbid 缓存桥。"""
        return ""

    def delete_library(self, server: str, server_type: str, item_id: str) -> bool:
        raise NotImplementedError

    def count_episodes(self, server: str, server_type: str, series_id: str) -> Optional[int]:
        raise NotImplementedError

    def delete_source(self, fileitem: dict[str, Any]) -> bool:
        raise NotImplementedError


def _facts_for_source(snapshot: dict[str, Any], facts: list[TransferFact]) -> list[TransferFact]:
    """源文件只取恰好覆盖这一季的转移。跨季包不删，避免带走其他季。"""
    chosen = []
    for fact in facts:
        if snapshot["kind"] == "movie":
            if fact.seasons:
                continue
        else:
            season = snapshot.get("season")
            if fact.seasons != {season}:
                continue
        if fact.source == "tmdb" and snapshot.get("tmdb") and fact.media_id == snapshot["tmdb"]:
            chosen.append(fact)
        elif fact.source == "imdb" and snapshot.get("imdb") and fact.media_id.lower() == str(snapshot["imdb"]).lower():
            chosen.append(fact)
    return chosen


def execute_delete(
    snapshot: dict[str, Any],
    mode: str,
    facts: list[TransferFact],
    ports: Ports,
    deleted_paths: set[tuple[str, str]],
) -> dict[str, Any]:
    """按删除方式清理。媒体库失败时调用方应保留清单项。"""
    library_deleted = 0
    source_deleted = 0
    source_skipped = False
    message = ""
    library_ok = True
    if mode in {"library", "all"}:
        ids = snapshot["episode_ids"] if snapshot["kind"] == "season" else [snapshot["item_id"]]
        for item_id in ids:
            if not ports.delete_library(snapshot["server"], snapshot["server_type"], item_id):
                library_ok = False
                message = "媒体库条目删除失败"
                break
            library_deleted += 1
        if library_ok and snapshot["kind"] == "season":
            remaining = ports.count_episodes(snapshot["server"], snapshot["server_type"], snapshot["item_id"])
            if remaining == 0:
                if ports.delete_library(snapshot["server"], snapshot["server_type"], snapshot["item_id"]):
                    library_deleted += 1
                else:
                    library_ok = False
                    message = "剧集条目删除失败"
    if library_ok and mode in {"source", "all"}:
        matched = _facts_for_source(snapshot, facts)
        paths = []
        for fact in matched:
            if not fact.fileitem or not fact.path_key[1]:
                continue
            if fact.path_key in deleted_paths or fact.path_key in {item[0] for item in paths}:
                continue
            paths.append((fact.path_key, fact.fileitem))
        if not paths:
            source_skipped = True
            message = message or "无源文件，已跳过"
        else:
            for key, fileitem in paths:
                if ports.delete_source(fileitem):
                    deleted_paths.add(key)
                    source_deleted += 1
                else:
                    library_ok = False
                    message = "源文件删除失败"
                    break
    return {
        "ok": library_ok,
        "library_deleted": library_deleted,
        "source_deleted": source_deleted,
        "source_skipped": source_skipped,
        "message": message,
    }


def delete_result_text(outcome: dict[str, Any]) -> str:
    """把删除结果整理成用户可读的一句话；成功、跳过、失败都有文案。"""
    parts: list[str] = []
    library_deleted = int(outcome.get("library_deleted") or 0)
    source_deleted = int(outcome.get("source_deleted") or 0)
    if library_deleted == 1:
        parts.append("已删除媒体库")
    elif library_deleted > 1:
        parts.append(f"已删除媒体库（{library_deleted} 项）")
    if source_deleted == 1:
        parts.append("已删除源文件")
    elif source_deleted > 1:
        parts.append(f"已删除源文件（{source_deleted} 个）")
    if outcome.get("source_skipped"):
        parts.append("无源文件，已跳过")
    raw = str(outcome.get("message") or "").strip()
    if raw and raw not in parts:
        parts.append(raw)
    return "；".join(parts) if parts else "无变更"


def _history_row(item: dict[str, Any], mode: str, outcome: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "id": uuid4().hex,
        "title": item.get("title") or "",
        "year": item.get("year") or "",
        "season": item.get("season"),
        "reason": item.get("reason") or "",
        "mode": mode,
        "deleted_at": now.isoformat(timespec="seconds"),
        "library_deleted": outcome["library_deleted"],
        "source_deleted": outcome["source_deleted"],
        "source_skipped": outcome["source_skipped"],
        "message": delete_result_text(outcome),
    }


def _candidate_from_snapshot(snapshot: dict[str, Any]) -> Candidate:
    return Candidate(
        key=str(snapshot.get("key") or ""),
        kind=str(snapshot.get("kind") or "movie"),
        server=str(snapshot.get("server") or ""),
        server_type=str(snapshot.get("server_type") or ""),
        library_id=str(snapshot.get("library_id") or ""),
        library_name=str(snapshot.get("library_name") or ""),
        item_id=str(snapshot.get("item_id") or ""),
        title=str(snapshot.get("title") or ""),
        year=str(snapshot.get("year") or ""),
        season=snapshot.get("season"),
        tmdb=str(snapshot.get("tmdb") or ""),
        imdb=str(snapshot.get("imdb") or ""),
        douban=str(snapshot.get("douban") or ""),
        favorite=bool(snapshot.get("favorite")),
        path=str(snapshot.get("path") or ""),
        episode_ids=list(snapshot.get("episode_ids") or []),
    )


def _enrich_douban(ports: Ports, candidates: list[Candidate], subjects: list[Subject]) -> None:
    from .bridge import enrich_candidates_with_douban

    enrich_candidates_with_douban(
        candidates,
        subjects,
        resolve_imdb=ports.resolve_douban_by_imdb,
    )


def scan_rows(
    library: dict[str, Any],
    ports: Ports,
    whitelist: dict[str, Any],
    now: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    """手动页：扫描并附带全属性。豆瓣看过与转移记录并行拉取。"""
    from concurrent.futures import ThreadPoolExecutor

    candidates, errors = ports.scan(library)
    with ThreadPoolExecutor(max_workers=2) as pool:
        watched_future = pool.submit(ports.watched)
        transfers_future = pool.submit(ports.transfers, candidates)
        subjects, watch_error = watched_future.result()
        facts = transfers_future.result()
    if watch_error:
        errors.append(watch_error)
    _enrich_douban(ports, candidates, subjects)
    rows = [
        row_attributes(candidate, subjects=subjects, facts=facts, whitelist=whitelist, now=now)
        for candidate in candidates
    ]
    return rows, errors


def _hit_fields_from_detail(detail: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    seen: set[str] = set()
    for key, matched in (detail.get("conditions") or {}).items():
        if not matched:
            continue
        field = str(key).split(":", 1)[0]
        if field and field not in seen:
            seen.add(field)
            fields.append(field)
    return fields


def run_automation(
    library: dict[str, Any],
    rule: Optional[dict[str, Any]],
    ports: Ports,
    whitelist: dict[str, Any],
    *,
    dry_run: bool = False,
    mode: str = "cron",
    now: datetime,
    only_keys: Optional[set[str]] = None,
    record_hits: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """refresh=执行一次整表替换不删；cron=merge 后删到期；only_keys=立即删选中。"""
    candidates, errors = ports.scan(library)
    subjects, watch_error = ports.watched()
    if watch_error:
        errors.append(watch_error)
    _enrich_douban(ports, candidates, subjects)
    pending = list(library.get("pending") or [])
    identity_candidates = list(candidates)
    identity_candidates.extend(_candidate_from_snapshot(item.get("snapshot") or {}) for item in pending)
    facts = ports.transfers(identity_candidates)
    hits = []
    if rule:
        for candidate in candidates:
            if is_whitelisted(whitelist, candidate):
                continue
            reason = deletion_reason(
                candidate, rule, subjects=subjects, facts=facts, now=now, whitelist=whitelist,
            )
            if reason:
                detail = hit_evidence(
                    candidate, rule, subjects=subjects, facts=facts, now=now, whitelist=whitelist,
                )
                attrs = row_attributes(
                    candidate, subjects=subjects, facts=facts, whitelist=whitelist, now=now,
                )
                hits.append(
                    hit_from_candidate(
                        candidate,
                        reason,
                        detail,
                        attrs=attrs,
                        hit_fields=_hit_fields_from_detail(detail),
                    )
                )
    if dry_run:
        return library, {
            "items": hits,
            "pending": pending,
            "errors": errors,
            "deleted": [],
            "scanned": len(candidates),
        }

    if record_hits:
        if mode == "refresh":
            pending = replace_pending(hits, now)
        else:
            pending = merge_pending(pending, hits, now)
    else:
        current_by_key = {candidate.key: candidate for candidate in candidates}
        for item in pending:
            current = current_by_key.get(item["key"])
            if current is not None:
                item["snapshot"] = current.snapshot()
                item["title"] = current.title
                item["year"] = current.year

    # 执行一次只刷新清单，不删媒体
    if mode == "refresh" and only_keys is None:
        library = dict(library)
        library["pending"] = pending
        return library, {
            "items": hits,
            "pending": pending,
            "errors": errors,
            "deleted": [],
            "scanned": len(candidates),
        }

    fresh = {candidate.key: candidate for candidate in candidates}
    delete_mode = library.get("delete_mode") or "library"
    delay = int(library.get("delay_days") or 0)
    deleted_paths: set[tuple[str, str]] = set()
    deleted = []
    remained = []
    history = list(library.get("history") or [])
    for item in pending:
        snapshot = dict(item.get("snapshot") or {})
        current = fresh.get(item["key"])
        if current is not None:
            snapshot = current.snapshot()
            item["snapshot"] = snapshot
        protected = False
        if current is not None and is_whitelisted(whitelist, current):
            protected = True
        elif current is None and is_whitelisted(whitelist, _candidate_from_snapshot(snapshot)):
            protected = True
        selected = only_keys is None or item["key"] in only_keys
        due = only_keys is not None or is_due(item, delay, now)
        if protected and selected:
            continue
        if not selected or not due:
            remained.append(item)
            continue
        outcome = execute_delete(snapshot, delete_mode, facts, ports, deleted_paths)
        if not outcome["ok"]:
            remained.append(item)
            continue
        history.append(_history_row(item, delete_mode, outcome, now))
        deleted.append({"key": item["key"], "title": item.get("title"), "message": outcome["message"]})
    library = dict(library)
    library["pending"] = remained
    library["history"] = history
    return library, {
        "items": hits,
        "pending": remained,
        "errors": errors,
        "deleted": deleted,
        "scanned": len(candidates),
    }


def delete_candidates_now(
    library: dict[str, Any],
    candidates: list[Candidate],
    ports: Ports,
    whitelist: dict[str, Any],
    *,
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """手动立即删除。白名单条目跳过。"""
    facts = ports.transfers(candidates)
    deleted_paths: set[tuple[str, str]] = set()
    deleted = []
    skipped = []
    mode = library.get("delete_mode") or "library"
    history_rows = []
    for candidate in candidates:
        if is_whitelisted(whitelist, candidate):
            skipped.append({"key": candidate.key, "title": candidate.title, "reason": "白名单"})
            continue
        snapshot = candidate.snapshot()
        outcome = execute_delete(snapshot, mode, facts, ports, deleted_paths)
        if not outcome["ok"]:
            skipped.append({"key": candidate.key, "title": candidate.title, "reason": outcome["message"] or "删除失败"})
            continue
        row = {
            "key": candidate.key,
            "title": candidate.title,
            "year": candidate.year,
            "season": candidate.season,
            "reason": "手动删除",
            "snapshot": snapshot,
        }
        history_rows.append(_history_row(row, mode, outcome, now))
        deleted.append({"key": candidate.key, "title": candidate.title, "message": outcome["message"]})
    return deleted, skipped, history_rows


def parse_transfer_seasons(text: str) -> set[int]:
    return season_numbers(text)


# 测试与旧调用兼容别名
run_task = run_automation
