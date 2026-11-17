"""插件 HTTP 接口。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.schemas import Response


class RuleBody(BaseModel):
    id: str = ""
    name: str
    combine: str = "all"
    conditions: list[dict[str, Any]] = Field(default_factory=list)


class RuleAction(BaseModel):
    rule_id: str


class LibraryAutomationBody(BaseModel):
    id: str = ""
    server: str
    server_type: str
    library_id: str
    library_name: str
    library_kind: str
    enabled: bool = False
    cron: str = ""
    delete_mode: str = "library"
    delay_days: int = 0
    rule_id: str = ""


class LibraryAction(BaseModel):
    library_id: str
    keys: list[str] = Field(default_factory=list)


class ScanBody(BaseModel):
    server: str
    server_type: str
    library_id: str
    library_name: str
    library_kind: str
    force: bool = False


class ManualDeleteBody(BaseModel):
    server: str
    server_type: str
    library_id: str
    library_name: str
    library_kind: str
    delete_mode: str = "library"
    items: list[dict[str, Any]] = Field(default_factory=list)


class ManualWhitelistBody(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


class ExcludeBody(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


class DoubanWatchedSyncBody(BaseModel):
    mode: str = "incremental"


class SmartCleanApi:
    """把插件方法登记成路由。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    def routes(self) -> list[dict[str, Any]]:
        plugin = self._plugin
        return [
            {"path": "/overview", "endpoint": plugin.api_overview, "methods": ["GET"], "auth": "bear", "summary": "概览"},
            {"path": "/libraries", "endpoint": plugin.api_libraries, "methods": ["GET"], "auth": "bear", "summary": "可选媒体库"},
            {"path": "/users", "endpoint": plugin.api_users, "methods": ["GET"], "auth": "bear", "summary": "可选用户"},
            {"path": "/rules", "endpoint": plugin.api_save_rule, "methods": ["POST"], "auth": "bear", "summary": "新建或更新规则"},
            {"path": "/rules/delete", "endpoint": plugin.api_delete_rule, "methods": ["POST"], "auth": "bear", "summary": "删除规则"},
            {"path": "/libraries/automation", "endpoint": plugin.api_save_automation, "methods": ["POST"], "auth": "bear", "summary": "保存库自动化"},
            {"path": "/libraries/scan", "endpoint": plugin.api_scan, "methods": ["POST"], "auth": "bear", "summary": "手动扫描"},
            {"path": "/libraries/delete", "endpoint": plugin.api_manual_delete, "methods": ["POST"], "auth": "bear", "summary": "手动立即删除"},
            {"path": "/libraries/whitelist", "endpoint": plugin.api_manual_whitelist, "methods": ["POST"], "auth": "bear", "summary": "手动加白"},
            {"path": "/libraries/dry-run", "endpoint": plugin.api_dry_run, "methods": ["POST"], "auth": "bear", "summary": "自动化试跑"},
            {"path": "/libraries/run", "endpoint": plugin.api_run, "methods": ["POST"], "auth": "bear", "summary": "执行一次（替换待删除）"},
            {"path": "/libraries/remove", "endpoint": plugin.api_remove_pending, "methods": ["POST"], "auth": "bear", "summary": "移出待删除"},
            {"path": "/libraries/pending/whitelist", "endpoint": plugin.api_whitelist_pending, "methods": ["POST"], "auth": "bear", "summary": "待删除加白"},
            {"path": "/libraries/delete-now", "endpoint": plugin.api_delete_now, "methods": ["POST"], "auth": "bear", "summary": "待删除立即删除"},
            {"path": "/libraries/history/clear", "endpoint": plugin.api_clear_history, "methods": ["POST"], "auth": "bear", "summary": "清空删除历史"},
            {"path": "/whitelist/effective", "endpoint": plugin.api_whitelist_effective, "methods": ["GET"], "auth": "bear", "summary": "有效白名单列表"},
            {"path": "/whitelist/exclude", "endpoint": plugin.api_whitelist_exclude, "methods": ["POST"], "auth": "bear", "summary": "移出白名单"},
            {"path": "/douban/watched/status", "endpoint": plugin.api_douban_watched_status, "methods": ["GET"], "auth": "bear", "summary": "豆瓣看过本地快照状态"},
            {"path": "/douban/watched/sync", "endpoint": plugin.api_douban_watched_sync, "methods": ["POST"], "auth": "bear", "summary": "同步豆瓣看过（增量/全量）"},
        ]


def fail(message: str) -> Response:
    return Response(success=False, message=message, data=None)


def ok(data: Any, message: str = "") -> Response:
    return Response(success=True, message=message, data=data)
