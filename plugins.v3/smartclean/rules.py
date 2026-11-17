"""全局筛选规则。条件字段与手动页筛选条共用。"""

from __future__ import annotations

from typing import Any, Optional
from uuid import uuid4

COMBINES = {"all", "any"}

# field -> allowed ops
FIELD_OPS: dict[str, set[str]] = {
    "title": {"contains", "eq"},
    "year": {"eq", "gte", "lte"},
    "season": {"eq"},
    "douban_stars": {"eq", "lte", "gte"},
    "douban_watched_days": {"gte"},
    "transfer_users": {"in"},
    "transfer_days": {"gte"},
    "library_age_days": {"gte"},
    "playback_unplayed": {"eq"},
    "playback_stale_days": {"gte"},
    "favorite": {"eq"},
    "has_source": {"eq"},
    "whitelisted": {"eq"},
}

FIELD_LABELS = {
    "title": "片名",
    "year": "年份",
    "season": "季号",
    "douban_stars": "豆瓣星级",
    "douban_watched_days": "豆瓣看过天数",
    "transfer_users": "MP入库用户",
    "transfer_days": "最近MP入库天数",
    "library_age_days": "在库天数",
    "playback_unplayed": "未播放",
    "playback_stale_days": "播放过期天数",
    "favorite": "收藏",
    "has_source": "有源文件",
    "whitelisted": "白名单",
}


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, low), high)


def normalize_condition(raw: dict[str, Any]) -> dict[str, Any]:
    field = str(raw.get("field") or "").strip()
    op = str(raw.get("op") or "").strip()
    if field not in FIELD_OPS:
        raise ValueError(f"不支持的筛选字段：{field or '(空)'}")
    if op not in FIELD_OPS[field]:
        raise ValueError(f"字段 {field} 不支持运算符 {op or '(空)'}")
    value = raw.get("value")
    if field in {"year", "season", "douban_stars", "douban_watched_days", "transfer_days", "library_age_days", "playback_stale_days"}:
        if field == "season":
            value = _int(value, 0, 0, 100)
        elif field == "douban_stars":
            value = _int(value, 4, 1, 5)
        elif field == "year":
            value = _int(value, 0, 0, 9999)
        else:
            value = _int(value, 0, 0, 3650)
    elif field in {"playback_unplayed", "favorite", "has_source", "whitelisted"}:
        if isinstance(value, str):
            value = value.strip().lower() in {"1", "true", "yes", "是"}
        else:
            value = bool(value)
    elif field == "transfer_users":
        if isinstance(value, str):
            names = [value]
        elif isinstance(value, list):
            names = value
        else:
            names = []
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in names:
            name = str(item or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            cleaned.append(name)
        if not cleaned:
            raise ValueError("MP入库用户条件至少选择一个用户")
        value = cleaned
    else:
        value = str(value or "").strip()
        if field == "title" and not value:
            raise ValueError("片名条件不能为空")
    return {"field": field, "op": op, "value": value}


def normalize_rule(raw: dict[str, Any], *, rule_id: Optional[str] = None) -> dict[str, Any]:
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("规则需要名称")
    combine = str(raw.get("combine") or "all").strip()
    if combine not in COMBINES:
        raise ValueError("筛选组合只能是全部满足或任一满足")
    conditions = [normalize_condition(item) for item in (raw.get("conditions") or [])]
    if not conditions:
        raise ValueError("规则至少需要一条条件")
    return {
        "id": rule_id or str(raw.get("id") or "") or uuid4().hex,
        "name": name,
        "combine": combine,
        "conditions": conditions,
    }


def rule_by_id(rules: list[dict[str, Any]], rule_id: str) -> Optional[dict[str, Any]]:
    return next((rule for rule in rules if rule.get("id") == rule_id), None)


def libraries_using_rule(libraries: list[dict[str, Any]], rule_id: str) -> list[dict[str, Any]]:
    return [lib for lib in libraries if str(lib.get("rule_id") or "") == rule_id]
