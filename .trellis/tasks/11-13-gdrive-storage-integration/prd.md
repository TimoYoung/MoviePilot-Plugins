# Google Drive 存储接入与界面

## Goal

把 `GoogleDriveClient` 接入 MoviePilot V3 存储体系：注册自定义存储、响应整理链的 `StorageOperSelection`、通过 `get_module()` 提供文件管理方法，并提供与现有 V3 插件一致的配置页和详情页。

## Requirements

### R1 插件生命周期

- `GDriveStorage` 位于 `plugins.v3/gdrivestorage/__init__.py`。
- `init_plugin(config)` 可重复调用；先停止旧资源，再按配置重建。
- `stop_service()` 关闭 HTTP 客户端、清空路径缓存、移除一次性任务。
- `get_state()` 返回启用状态。

### R2 存储注册

- 插件启用且 `register_storage=True` 时，确保系统存储中存在 `type="gdrive"` 的条目。
- 已存在时只更新 config，不重复 append；名称不强行覆盖。
- 注册失败记录警告，不阻塞插件加载。
- 禁用插件时不删除系统存储条目。

### R3 整理链适配

- 监听 `ChainEventType.StorageOperSelection`；仅当 `storage == "gdrive"` 且插件启用时设置 `event_data.storage_oper`。
- 提供者实现：`get_item`、`get_item_strict`、`get_folder`、`list`、`upload`、`download`、`copy`、`move`、`delete`、`is_support_transtype`。
- `transtype` 只声明 `copy`、`move`。
- 目标目录不存在时递归创建。
- 非 gdrive 存储一律返回 `None`，不干扰宿主内置存储。

### R4 `get_module()`

声明：`list_files`、`any_files`、`create_folder`、`get_folder`、`download_file`、`upload_file`、`delete_file`、`rename_file`、`get_file_item`、`get_file_item_strict`、`get_parent_item`、`snapshot_storage`、`storage_manage`。

每个方法：

- 先判断 `storage` 或 `fileitem.storage`；
- 非 `gdrive` 返回 `None`；
- 捕获可预期异常并记录日志；严格查询保留异常语义。

### R5 配置界面

字段（键名固定）：

- `enabled`、`display_name`、`register_storage`
- `client_id`、`client_secret`、`refresh_token`、`redirect_uri`、`oauth_code`
- `root_folder_id`、`root_path`、`use_shared_drive`、`shared_drive_id`
- `delete_mode`、`chunk_size_mb`、`timeout`

默认值：

```python
{
    "enabled": False,
    "display_name": "Google Drive",
    "register_storage": True,
    "client_id": "",
    "client_secret": "",
    "refresh_token": "",
    "redirect_uri": "http://localhost:8000/api/v1/plugin/GDriveStorage/oauth/callback",
    "oauth_code": "",
    "root_folder_id": "root",
    "root_path": "/",
    "use_shared_drive": False,
    "shared_drive_id": "",
    "delete_mode": "trash",
    "chunk_size_mb": 8,
    "timeout": 60,
}
```

### R6 详情页与 API

- 详情页展示启用/授权/注册/容量/配置摘要/最近日志。
- 按钮：测试连接、刷新容量、生成授权链接、用授权码换取令牌、清空日志、日志分页。
- API 路径：
  - `POST /test`
  - `POST /quota`
  - `POST /auth/url`
  - `POST /auth/exchange`
  - `GET /oauth/callback`（`allow_anonymous: True`）
  - `POST /logs/page`
  - `POST /logs/clear`
- 所有页面 API 返回恰好 `{success, message, data}`。
- OAuth 回调校验一次性 state（10 分钟过期）。
- 日志环形缓冲最多 100 条，使用 `save_data("logs")`；不记录 token/secret/code。

## Acceptance Criteria

- [ ] `python -m py_compile plugins.v3/gdrivestorage/__init__.py` 通过。
- [ ] AST 检查：所有页面 API 的 `return` 字典字面量键集合为 `{success, message, data}`。
- [ ] AST/反射检查：`get_module()` 包含 R4 全部方法。
- [ ] `storage_oper_selection` 只对 gdrive 设置 `storage_oper`。
- [ ] 非 gdrive 分支的模块方法返回 `None`（静态检查 + 单测 mock）。
- [ ] `get_form()` 默认值与 R5 一致。
- [ ] `get_page()` 在未启用/未授权/已授权三种状态下都不抛异常。
- [ ] `stop_service()` 可重复调用，关闭客户端。
- [ ] 详情页 `VAlert` 带 `style: "flex: 0 0 auto;"`。

## Notes

- 依赖子任务 `11-13-gdrive-api-client` 的 `GoogleDriveClient` 接口。
- 参考 `research/plugin-ui-and-release.md` 和 `plugins.v3/u115autoupload/__init__.py`。
