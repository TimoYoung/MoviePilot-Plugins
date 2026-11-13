# Google Drive 存储接入与界面 — 执行计划

## 1. 插件骨架

- [ ] 新建 `__init__.py`，导入宿主公开契约：
  - `_PluginBase`、`FileItem`、`StorageUsage`、`FileItemQueryResult`
  - `StorageHelper`
  - `Event`、`eventmanager`、`ChainEventType`
  - `StorageOperSelectionEventData`（仅类型注释）
  - `remove_plugin_once_job`（如使用）
- [ ] 定义常量：`STORAGE_TYPE = "gdrive"`、`DEFAULT_REDIRECT_URI`、`MAX_LOGS = 100`。
- [ ] 定义 `GDriveStorageProvider`。
- [ ] 定义 `GDriveStorage` 类与元数据。

## 2. 生命周期

- [ ] `init_plugin`：停止旧资源 -> 归一化配置 -> 建 client/provider -> 注册存储 -> 读日志/配额缓存。
- [ ] `stop_service`：关闭 client、清空 provider、移除一次性任务；可重复调用。
- [ ] `get_state`。
- [ ] `_rebuild_client`：OAuth 成功后按新 refresh_token 重建。

## 3. 存储注册与事件

- [ ] `_ensure_storage_registered`。
- [ ] `@eventmanager.register(ChainEventType.StorageOperSelection)`。
- [ ] `get_module()` 返回 13 个存储方法。

## 4. 模块方法

- [ ] `list_files`、`any_files`、`create_folder`、`get_folder`。
- [ ] `download_file`、`upload_file`、`delete_file`、`rename_file`。
- [ ] `get_file_item`、`get_file_item_strict`、`get_parent_item`。
- [ ] `snapshot_storage`。
- [ ] `storage_manage`。
- [ ] 所有方法先判断 gdrive；异常记录日志；严格查询保留异常。

## 5. 配置与页面

- [ ] `get_form()`：字段/默认值/密码类型/说明。
- [ ] `get_page()`：状态、配额、注册状态、按钮、日志表格、分页。
- [ ] `_auth_url()` + state 持久化。
- [ ] `get_api()`：test/quota/auth-url/auth-exchange/oauth-callback/logs-page/logs-clear。
- [ ] 所有页面 API 三键 envelope；回调 `allow_anonymous: True`。
- [ ] 日志环形缓冲、分页、清空。
- [ ] `VAlert` 内联 `flex: 0 0 auto;`。

## 6. 验证

```bash
python -m py_compile plugins.v3/gdrivestorage/__init__.py
python -m pytest tests/v3/gdrivestorage -q
```

- [ ] AST envelope 检查通过。
- [ ] `get_module` 方法集合断言通过。
- [ ] 非 gdrive 返回 `None` 的单测通过。
- [ ] 无 `app.core.*` / `app.helper.*` / `app.utils.*` / `app.db.models.*` 导入。

## 7. 回滚点

- 宿主导入失败：逐项移除新增导入，用 `py_compile` 定位；客户端不受影响。
- 事件注册报错：先保留 `get_module`，事件作为后置步骤；整理链功能在事件修复后启用。
- 页面渲染异常：先返回最小 `VAlert`，再逐步加表格/按钮。
- OAuth 回调异常：关闭匿名回调，保留手动 refresh_token/授权码模式。
