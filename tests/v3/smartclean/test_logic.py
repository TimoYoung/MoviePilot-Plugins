"""规则、白名单、冻结清单和一次执行。"""

from datetime import datetime, timedelta

import pytest

from app.plugins.smartclean.douban import sync_watched_pages
from app.plugins.smartclean.filters import deletion_reason, hit_evidence, playback_summary
from app.plugins.smartclean.libraries import library_owner, normalize_library
from app.plugins.smartclean.models import Candidate, Play, Subject, TransferFact
from app.plugins.smartclean.pending import is_due, merge_pending, replace_pending
from app.plugins.smartclean.rules import libraries_using_rule, normalize_rule
from app.plugins.smartclean.scan import build_movie, build_seasons
from app.plugins.smartclean.service import Ports, run_automation
from app.plugins.smartclean.stateutil import migrate_state
from app.plugins.smartclean.titles import extract_douban_id, extract_year, parse_title
from app.plugins.smartclean.whitelist import (
    add_manual,
    effective_entries,
    empty_whitelist,
    is_whitelisted,
    normalize_whitelist,
    pick_from_candidate,
    remove_manual,
)


NOW = datetime(2026, 11, 6, 12, 0, 0)


def movie(**kwargs) -> Candidate:
    base = dict(
        key="s|lib|movie|1", kind="movie", server="s", server_type="emby", library_id="lib",
        library_name="电影", item_id="m1", title="肖申克的救赎", year="1994", tmdb="278", imdb="tt0111161",
        added_at=NOW - timedelta(days=40), plays=[Play(0, None)],
    )
    base.update(kwargs)
    return Candidate(**base)


def season(**kwargs) -> Candidate:
    base = dict(
        key="s|lib|season|1399|1", kind="season", server="s", server_type="jellyfin", library_id="lib",
        library_name="剧集", item_id="series", title="庆余年", year="2019", season=1, tmdb="1399",
        added_at=NOW - timedelta(days=40), plays=[Play(0, None)], episode_ids=["e1"],
    )
    base.update(kwargs)
    return Candidate(**base)


def rule(*conditions, combine="all", name="测试规则") -> dict:
    return normalize_rule({"name": name, "combine": combine, "conditions": list(conditions)})


def library(**kwargs) -> dict:
    base = {
        "server": "jellyfin",
        "server_type": "jellyfin",
        "library_id": "lib",
        "library_name": "电影",
        "library_kind": "movie",
        "enabled": True,
        "cron": "0 4 * * *",
        "delete_mode": "library",
        "delay_days": 7,
        "rule_id": "",
        "pending": [],
        "history": [],
    }
    base.update(kwargs)
    return normalize_library(base)


def test_migrate_drops_old_tasks_keeps_manual_only():
    state = migrate_state({
        "version": 1,
        "tasks": [{"id": "t1", "cards": {}}],
        "pending": {"t1": [{"key": "x"}]},
        "whitelist": {
            "picks": [{"id": "p", "kind": "movie", "title": "A", "tmdb": "1"}],
            "ranks": {"top": {"name": "TOP", "items": [{"title": "B", "douban_id": "2"}]}},
            "imports": {},
            "lists": {"x": {"name": "丢弃", "items": [{"title": "C", "tmdb": "9"}]}},
            "exclusions": [{"title": "E", "tmdb": "8"}],
        },
    })
    assert state["version"] == 3
    assert state["libraries"] == []
    assert state["rules"] == []
    assert state["whitelist"] == normalize_whitelist({"manual": [{"id": "p", "kind": "movie", "title": "A", "tmdb": "1"}]})
    assert "lists" not in state["whitelist"]
    assert "exclusions" not in state["whitelist"]
    assert state["whitelist"]["manual"][0]["tmdb"] == "1"


def test_library_owner_and_rule_binding():
    existing = library(id="old")
    created = library()
    assert library_owner([existing], created)["id"] == "old"
    users = libraries_using_rule([
        {**existing, "rule_id": "r1"},
        {**library(library_id="other", id="o2"), "rule_id": "r2"},
    ], "r1")
    assert [item["id"] for item in users] == ["old"]


def test_parse_season_titles():
    assert parse_title("庆余年 第一季")[:2] == ("庆余年", 1)
    assert parse_title("Person of Interest Season 2")[1] == 2
    assert extract_douban_id("https://movie.douban.com/subject/1292052/") == "1292052"
    assert extract_year("1994 / 美国 / 犯罪 剧情") == "1994"


def test_missing_data_does_not_satisfy_and():
    r = rule(
        {"field": "douban_stars", "op": "lte", "value": 4},
        {"field": "library_age_days", "op": "gte", "value": 30},
    )
    assert deletion_reason(movie(), r, subjects=[], facts=[], now=NOW) is None


def test_or_keeps_a_known_condition():
    r = rule(
        {"field": "douban_stars", "op": "lte", "value": 4},
        {"field": "library_age_days", "op": "gte", "value": 30},
        combine="any",
    )
    assert deletion_reason(movie(), r, subjects=[], facts=[], now=NOW) == "在库天数"


def test_douban_conditions():
    watched = Subject(
        douban_id="1", title="庆余年", season=1, stars=3, watched_on=NOW - timedelta(days=10),
    )
    r = rule(
        {"field": "douban_stars", "op": "lte", "value": 4},
        {"field": "douban_watched_days", "op": "gte", "value": 7},
    )
    assert deletion_reason(season(), r, subjects=[watched], facts=[], now=NOW) == "豆瓣星级、豆瓣看过天数"
    missing_star = Subject(douban_id="3", title="庆余年", season=1, stars=None, watched_on=NOW - timedelta(days=10))
    assert deletion_reason(season(), r, subjects=[missing_star], facts=[], now=NOW) is None


def test_transfer_users_and_days_scope():
    r = rule(
        {"field": "transfer_users", "op": "in", "value": ["甲"]},
        {"field": "transfer_days", "op": "gte", "value": 30},
    )
    old = TransferFact("甲", NOW - timedelta(days=50), "tmdb", "1399", seasons={1})
    recent = TransferFact("甲", NOW - timedelta(days=2), "tmdb", "1399", seasons={1})
    assert deletion_reason(season(), r, subjects=[], facts=[old], now=NOW) == "入库用户、最近入库天数"
    assert deletion_reason(season(), r, subjects=[], facts=[old, recent], now=NOW) is None
    other = TransferFact("乙", NOW - timedelta(days=50), "tmdb", "1399", seasons={1})
    assert deletion_reason(season(), r, subjects=[], facts=[other], now=NOW) is None


def test_hit_evidence_and_playback_summary():
    r = rule(
        {"field": "douban_stars", "op": "lte", "value": 4},
        {"field": "transfer_users", "op": "in", "value": ["甲"]},
    )
    subject = Subject(
        douban_id="25853071", title="庆余年", season=1, stars=3,
        watched_on=NOW - timedelta(days=20),
    )
    fact = TransferFact("甲", NOW - timedelta(days=50), "tmdb", "1399", seasons={1})
    detail = hit_evidence(season(), r, subjects=[subject], facts=[fact], now=NOW)
    assert detail["douban"]["stars"] == 3
    assert detail["transfers"][0]["username"] == "甲"
    empty_play = playback_summary(season(plays=[Play(None, None), Play(None, None)]))
    assert empty_play["has_data"] is False


def test_playback_stale():
    r = rule({"field": "playback_stale_days", "op": "gte", "value": 30})
    stale = season(plays=[Play(2, NOW - timedelta(days=40)), Play(1, NOW - timedelta(days=31))])
    mixed = season(plays=[Play(0, None), Play(1, NOW - timedelta(days=1))])
    assert deletion_reason(stale, r, subjects=[], facts=[], now=NOW) == "播放过期天数"
    assert deletion_reason(mixed, r, subjects=[], facts=[], now=NOW) is None
    assert deletion_reason(movie(), r, subjects=[], facts=[], now=NOW) == "播放过期天数"


def test_whitelist_hard_excludes_automation_hits():
    r = rule({"field": "library_age_days", "op": "gte", "value": 30})
    loved = movie(favorite=True)
    protected = movie(key="w", item_id="w2", tmdb="999", imdb="", title="受保护")

    class Fake(Ports):
        def scan(self, library):
            return [loved, protected, movie()], []

        def transfers(self, candidates):
            return []

        def watched(self):
            return [], None

        def delete_library(self, server, server_type, item_id):
            return True

        def count_episodes(self, server, server_type, series_id):
            return 0

        def delete_source(self, fileitem):
            return True

    whitelist = add_manual(empty_whitelist(), pick_from_candidate(protected))
    lib = library(delay_days=0, rule_id="r")
    _, result = run_automation(lib, r, Fake(), whitelist, dry_run=True, now=NOW)
    keys = [item["key"] for item in result["items"]]
    assert movie().key in keys
    assert loved.key in keys  # favorite 不再自动排除
    assert protected.key not in keys


def test_pending_merge_keeps_timer():
    started = NOW - timedelta(days=3)
    existing = [{
        "key": "keep", "title": "旧", "year": "2000", "season": None, "reason": "在库天数",
        "started_at": started.isoformat(timespec="seconds"),
        "snapshot": {"item_id": "old"},
    }]
    merged = merge_pending(existing, [{
        "key": "keep", "title": "旧", "year": "2000", "season": None, "reason": "新原因",
        "snapshot": {"item_id": "new"},
        "attrs": {"library_age_days": 40},
        "hit_fields": ["library_age_days"],
    }, {
        "key": "add", "title": "新", "year": "", "season": 1, "reason": "播放过期天数",
        "snapshot": {"item_id": "n"},
    }], NOW)
    by_key = {item["key"]: item for item in merged}
    assert by_key["keep"]["started_at"] == started.isoformat(timespec="seconds")
    assert by_key["keep"]["snapshot"]["item_id"] == "new"
    assert by_key["keep"]["reason"] == "新原因"
    assert by_key["keep"]["hit_fields"] == ["library_age_days"]


def test_is_due_zero_delay_and_replace_pending():
    item = {
        "key": "k", "title": "x", "year": "", "season": None, "reason": "r",
        "started_at": NOW.isoformat(timespec="seconds"),
        "snapshot": {},
    }
    assert is_due(item, 0, NOW) is True
    assert is_due(item, 7, NOW) is False
    assert is_due(item, 7, NOW + timedelta(days=7)) is True
    replaced = replace_pending([{
        "key": "a", "title": "新", "year": "2020", "season": None, "reason": "在库天数",
        "snapshot": {"item_id": "1"}, "detail": {}, "attrs": {"title": "新"}, "hit_fields": ["library_age_days"],
    }], NOW)
    assert len(replaced) == 1
    assert replaced[0]["started_at"] == NOW.isoformat(timespec="seconds")
    assert replaced[0]["attrs"]["title"] == "新"


def test_run_automation_refresh_replaces_without_delete():
    r = rule({"field": "library_age_days", "op": "gte", "value": 30})

    class Fake(Ports):
        def scan(self, library):
            return [movie()], []

        def transfers(self, candidates):
            return []

        def watched(self):
            return [], None

        def delete_library(self, server, server_type, item_id):
            raise AssertionError("refresh 不应删除")

        def count_episodes(self, server, server_type, series_id):
            return 0

        def delete_source(self, fileitem):
            raise AssertionError("refresh 不应删除")

    old = [{
        "key": "gone", "title": "旧", "year": "", "season": None, "reason": "旧",
        "started_at": (NOW - timedelta(days=10)).isoformat(timespec="seconds"),
        "snapshot": {"item_id": "old"},
    }]
    lib = library(delay_days=0, rule_id="r", pending=old)
    updated, result = run_automation(
        lib, r, Fake(), empty_whitelist(), dry_run=False, mode="refresh", now=NOW,
    )
    assert result["deleted"] == []
    assert len(updated["pending"]) == 1
    assert updated["pending"][0]["key"] == movie().key
    assert updated["pending"][0]["started_at"] == NOW.isoformat(timespec="seconds")
    assert "library_age_days" in (updated["pending"][0].get("hit_fields") or [])


def test_normalize_drops_lists_and_exclusions():
    data = normalize_whitelist({
        "manual": [{"kind": "movie", "title": "A", "tmdb": "1"}],
        "lists": {"top": {"name": "TOP", "items": [{"title": "B", "douban_id": "2"}]}},
        "exclusions": [{"title": "C", "tmdb": "3"}],
    })
    assert data == {"manual": [data["manual"][0]]}
    assert data["manual"][0]["tmdb"] == "1"


def test_pick_prefers_external_ids():
    pick = pick_from_candidate(movie())
    assert pick["tmdb"] == "278"
    assert pick["imdb"] == "tt0111161"
    assert pick["server"] == ""
    assert is_whitelisted(add_manual(empty_whitelist(), pick), movie())


def test_remove_manual_unprotects():
    item = {"kind": "movie", "title": "肖申克的救赎", "douban_id": "1292052", "year": "1994", "tmdb": "", "imdb": ""}
    cand = movie(title="肖申克的救赎", tmdb="", imdb="", douban="1292052")
    whitelist = add_manual(empty_whitelist(), item)
    assert is_whitelisted(whitelist, cand)
    assert effective_entries(whitelist)[0]["sources"] == ["手动"]
    whitelist = remove_manual(whitelist, [item])
    assert not is_whitelisted(whitelist, cand)
    assert whitelist["manual"] == []
    assert effective_entries(whitelist) == []
    assert is_whitelisted(add_manual(whitelist, item), cand)


def test_scan_groups_episodes():
    series = [{"Id": "show", "Name": "庆余年", "ProductionYear": 2019, "ProviderIds": {"Tmdb": "1399"}}]
    episodes = [
        {"Id": "e1", "SeriesId": "show", "ParentIndexNumber": 1, "DateCreated": "2026-01-01T00:00:00Z", "UserData": {"PlayCount": 0}},
        {"Id": "e2", "SeriesId": "show", "ParentIndexNumber": 1, "DateCreated": "2026-10-01T00:00:00Z", "UserData": {"PlayCount": 0, "IsFavorite": True}},
        {"Id": "e3", "SeriesId": "show", "ParentIndexNumber": 2, "DateCreated": "2026-02-01T00:00:00Z", "UserData": {"PlayCount": 0}},
    ]
    found = build_seasons(series, episodes, [], server="jf", server_type="jellyfin", library_id="lib", library_name="剧")
    by_season = {item.season: item for item in found}
    assert by_season[1].favorite is True
    built = build_movie(
        {"Id": "m", "Name": "电影", "ProductionYear": 1994, "ProviderIds": {"Imdb": "0111161"}, "UserData": {"PlayCount": 0}},
        server="emby", server_type="emby", library_id="lib", library_name="电影",
    )
    assert built is not None and built.imdb == "tt0111161"


def test_duplicate_tmdb_movies_get_distinct_keys():
    from app.plugins.smartclean.scan import build_movie

    a = build_movie(
        {"Id": "id-a", "Name": "曾经", "ProductionYear": 2006, "ProviderIds": {"Tmdb": "5723"}, "UserData": {"PlayCount": 0}},
        server="jellyfin", server_type="jellyfin", library_id="lib", library_name="电影",
    )
    b = build_movie(
        {"Id": "id-b", "Name": "曾经", "ProductionYear": 2007, "ProviderIds": {"Tmdb": "5723"}, "UserData": {"PlayCount": 0}},
        server="jellyfin", server_type="jellyfin", library_id="lib", library_name="电影",
    )
    assert a is not None and b is not None
    assert a.tmdb == b.tmdb == "5723"
    assert a.key != b.key
    assert a.key.endswith("|id-a") and b.key.endswith("|id-b")


def test_normalize_rule_rejects_empty():
    with pytest.raises(ValueError, match="至少需要一条条件"):
        normalize_rule({"name": "空", "conditions": []})


def test_parse_interests_keeps_year_and_full_time():
    from app.plugins.smartclean.douban import parse_interests_payload
    from app.plugins.smartclean.titles import subjects_match

    subjects = parse_interests_payload({
        "interests": [{
            "create_time": "2024-02-16 09:02:12",
            "rating": {"value": 4, "star_count": 4, "max": 5},
            "subject": {"id": "25761178", "title": "百元之恋", "year": "2014"},
        }],
    })
    assert subjects[0].year == "2014"
    assert subjects[0].stars == 4
    assert subjects[0].watched_on == datetime(2024, 2, 16, 9, 2, 12)
    assert subjects_match(movie(title="百元之恋", year="2014", tmdb="314606", imdb=""), subjects[0], trust_douban_id=False)


def test_transfer_fact_without_download_user_still_counts():
    from types import SimpleNamespace
    from app.plugins.smartclean.transfers import facts_from_records

    row = SimpleNamespace(
        status=True,
        download_hash="abc",
        date="2024-02-15 18:55:07",
        media_source="themoviedb",
        media_id="314606",
        seasons=None,
        src_fileitem={"storage": "local", "path": "/media/a.mkv"},
        src="",
        src_storage="local",
    )
    facts = facts_from_records([row], {})
    assert len(facts) == 1
    assert facts[0].username == "未知"
    assert facts[0].path_key[1] == "/media/a.mkv"


def test_years_compatible_allows_one_year_slack():
    from app.plugins.smartclean.titles import subjects_match, years_compatible
    from datetime import datetime as dt

    assert years_compatible("1985", "1984")
    assert years_compatible("1985", "1986")
    assert not years_compatible("1985", "1980")
    cand = movie(title="高山下的花环", year="1985", tmdb="258424", imdb="tt0090340")
    sub = Subject(
        douban_id="1422283", title="高山下的花环", year="1984",
        stars=5, watched_on=dt(2021, 8, 27, 1, 52, 24),
    )
    assert subjects_match(cand, sub, trust_douban_id=False)


def test_douban_id_and_imdb_outrank_year_and_title():
    from app.plugins.smartclean.titles import subjects_match

    sub = Subject(douban_id="1422283", title="高山下的花环", year="1984", imdb="tt0090340")
    by_id = movie(title="别名", year="1999", imdb="", douban="1422283")
    by_imdb = movie(title="别名", year="1999", imdb="tt0090340", douban="")
    assert subjects_match(by_id, sub, trust_douban_id=False)
    assert subjects_match(by_imdb, sub, trust_douban_id=False)


def test_transfer_and_source_come_from_moviepilot_only():
    from app.plugins.smartclean.filters import row_attributes
    from app.plugins.smartclean.models import TransferFact

    cand = movie(path="/media/library/a.mkv", added_at=NOW - timedelta(days=10))
    empty = row_attributes(cand, subjects=[], facts=[], whitelist=empty_whitelist(), now=NOW)
    assert empty["latest_transfer_at"] == ""
    assert empty["transfer_users"] == []
    assert empty["has_source"] is None
    assert empty["added_at"].startswith("2026-10-27")
    assert empty["library_age_days"] == 10

    fact = TransferFact(
        username="alice",
        transferred_at=NOW - timedelta(days=40),
        source="tmdb",
        media_id="278",
        seasons=set(),
        fileitem={"storage": "local", "path": "/downloads/a.mkv"},
    )
    filled = row_attributes(cand, subjects=[], facts=[fact], whitelist=empty_whitelist(), now=NOW)
    assert filled["latest_transfer_user"] == "alice"
    assert filled["has_source"] is True
    assert filled["transfer_origin"] == "moviepilot"
    assert filled["added_at"] != filled["latest_transfer_at"]

    again = TransferFact(
        username="alice",
        transferred_at=NOW - timedelta(days=1),
        source="tmdb",
        media_id="278",
        seasons=set(),
        fileitem={"storage": "local", "path": "/downloads/a2.mkv"},
    )
    other = TransferFact(
        username="bob",
        transferred_at=NOW - timedelta(days=3),
        source="tmdb",
        media_id="278",
        seasons=set(),
        fileitem={"storage": "local", "path": "/downloads/b.mkv"},
    )
    deduped = row_attributes(
        cand, subjects=[], facts=[again, fact, other], whitelist=empty_whitelist(), now=NOW,
    )
    assert deduped["transfer_users"] == ["alice", "bob"]

    no_path = TransferFact(
        username="bob",
        transferred_at=NOW - timedelta(days=5),
        source="tmdb",
        media_id="278",
        seasons=set(),
        fileitem=None,
    )
    missing = row_attributes(cand, subjects=[], facts=[no_path], whitelist=empty_whitelist(), now=NOW)
    assert missing["has_source"] is False
    assert missing["latest_transfer_user"] == "bob"


def _interest(douban_id: str, title: str, create_time: str) -> dict:
    return {
        "create_time": create_time,
        "rating": {"value": 4},
        "subject": {"id": douban_id, "title": title, "year": "2020"},
    }


def test_sync_watched_full_then_incremental_stops():
    pages = {
        0: {
            "total": 3,
            "interests": [
                _interest("1", "新片", "2026-11-12 10:00:00"),
                _interest("2", "旧片A", "2026-11-01 10:00:00"),
            ],
        },
        2: {  # page_size=2 → start=2
            "total": 3,
            "interests": [_interest("3", "旧片B", "2026-10-01 10:00:00")],
        },
    }
    calls: list[int] = []
    sleeps: list[float] = []

    def fetch(start: int) -> dict:
        calls.append(start)
        return pages[start]

    full = sync_watched_pages(
        fetch, existing=[], mode="full", page_size=2, page_sleep=0.1, sleep_fn=sleeps.append,
    )
    assert full["mode"] == "full"
    assert len(full["subjects"]) == 3
    assert calls == [0, 2]
    assert sleeps  # 页间有 sleep

    calls.clear()
    sleeps.clear()
    # 本地已有 2、3；远端首页多了一条 1，第二页全是已见 → 停
    existing = [s for s in full["subjects"] if s.douban_id != "1"]
    pages[0] = {
        "total": 4,
        "interests": [
            _interest("1", "新片", "2026-11-12 10:00:00"),
            _interest("2", "旧片A", "2026-11-01 10:00:00"),
        ],
    }
    pages[2] = {
        "total": 4,
        "interests": [
            _interest("2", "旧片A", "2026-11-01 10:00:00"),
            _interest("3", "旧片B", "2026-10-01 10:00:00"),
        ],
    }
    incr = sync_watched_pages(
        fetch, existing=existing, mode="incremental", page_size=2, page_sleep=0.1, sleep_fn=sleeps.append,
    )
    assert incr["mode"] == "incremental"
    assert incr["stopped_early"] is True
    assert {s.douban_id for s in incr["subjects"]} == {"1", "2", "3"}
    assert calls == [0, 2]


def test_watched_port_reads_local_only(tmp_path, monkeypatch):
    from app.plugins.smartclean import host as host_mod
    from app.plugins.smartclean import watchedstore

    monkeypatch.setattr(watchedstore, "_cache_dir", lambda: tmp_path)
    host_mod.clear_watched_cache()
    watchedstore.save_snapshot(
        "u1",
        [Subject(douban_id="9", title="本地", year="2020", watched_on=NOW)],
        mode="incremental",
    )

    class Boom:
        def __call__(self, *args, **kwargs):
            raise AssertionError("已有快照时不应请求豆瓣")

    monkeypatch.setattr(host_mod, "_fetch_interests", Boom())
    ports = host_mod.HostPorts("u1", "")
    subjects, err = ports.watched()
    assert err is None
    assert len(subjects) == 1
    assert subjects[0].douban_id == "9"


def test_watched_empty_snapshot_auto_full(tmp_path, monkeypatch):
    from app.plugins.smartclean import host as host_mod
    from app.plugins.smartclean import watchedstore

    monkeypatch.setattr(watchedstore, "_cache_dir", lambda: tmp_path)
    host_mod.clear_watched_cache()
    calls: list[int] = []

    def fetch(start: int) -> dict:
        calls.append(start)
        return {
            "total": 1,
            "interests": [{
                "create_time": "2026-11-12 10:00:00",
                "rating": {"value": 5},
                "subject": {"id": "77", "title": "自动建库", "year": "2021"},
            }],
        }

    monkeypatch.setattr(host_mod, "_fetch_interests", lambda user, cookie, start: fetch(start))
    ports = host_mod.HostPorts("auto1", "ck=x")
    subjects, err = ports.watched()
    assert err is None
    assert calls == [0]
    assert len(subjects) == 1
    assert subjects[0].douban_id == "77"
    # 再次读取应走本地，不再打豆瓣
    calls.clear()
    host_mod.clear_watched_cache()
    subjects, err = ports.watched()
    assert err is None
    assert calls == []
    assert subjects[0].douban_id == "77"


def test_enrich_uses_subject_imdb_and_resolve_only_on_miss():
    from app.plugins.smartclean.bridge import clear_imdb_bridge_cache, enrich_candidates_with_douban

    clear_imdb_bridge_cache(disk=True)
    subjects = [
        Subject(douban_id="100", title="甲", year="2001", imdb="tt8800001"),
        Subject(douban_id="200", title="乙真实名", year="2002"),
    ]
    has_imdb = movie(title="甲别名", year="2001", imdb="tt8800001", douban="")
    needs_bridge = movie(title="完全对不上", year="2002", imdb="tt8800002", douban="")
    already_title = movie(title="乙真实名", year="2002", imdb="tt8800003", douban="")
    calls: list[str] = []

    def resolve(imdb: str) -> str:
        calls.append(imdb)
        return "200" if imdb == "tt8800002" else ""

    n = enrich_candidates_with_douban(
        [has_imdb, needs_bridge, already_title], subjects, resolve_imdb=resolve,
    )
    assert has_imdb.douban == "100"
    assert needs_bridge.douban == "200"
    assert already_title.douban == ""
    assert n == 1
    assert calls == ["tt8800002"]
    clear_imdb_bridge_cache(disk=True)
