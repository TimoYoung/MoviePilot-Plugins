"""插件持久化文档的加载与规范化。"""

from __future__ import annotations

from typing import Any

from .libraries import normalize_library
from .models import STATE_VERSION, empty_state
from .rules import normalize_rule
from .whitelist import normalize_whitelist


def migrate_state(raw: Any) -> dict[str, Any]:
    """升级持久化文档。version < 2 丢弃旧 tasks；白名单只保留 manual。"""
    if not isinstance(raw, dict):
        return empty_state()
    version = int(raw.get("version") or 1)
    whitelist = normalize_whitelist(raw.get("whitelist") or {})

    if version < 2:
        state = empty_state()
        state["whitelist"] = whitelist
        return state

    libraries = []
    for item in raw.get("libraries") or []:
        try:
            libraries.append(normalize_library(item, library_row_id=str(item.get("id") or "") or None))
        except ValueError:
            continue
    rules = []
    for item in raw.get("rules") or []:
        try:
            rules.append(normalize_rule(item, rule_id=str(item.get("id") or "") or None))
        except ValueError:
            continue
    return {
        "version": STATE_VERSION,
        "libraries": libraries,
        "rules": rules,
        "whitelist": whitelist,
    }
