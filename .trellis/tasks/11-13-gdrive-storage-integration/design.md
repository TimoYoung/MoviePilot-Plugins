# Google Drive 存储接入与界面 — 技术设计

## 1. 宿主契约映射

| 插件方法 | 宿主调用方 | 返回 |
| --- | --- | --- |
| `storage_oper_selection(event)` | `TransferChain._select_storage_oper` | 设置 `StorageOperSelectionEventData.storage_oper` |
| `list_files(fileitem, recursion)` | `StorageChain.list_files` | `list[FileItem] \| None` |
| `any_files(fileitem, extensions)` | `StorageChain.any_files` | `bool \| None` |
| `create_folder(fileitem, name)` | `StorageChain.create_folder` | `FileItem \| None` |
| `get_folder(storage, path)` | `StorageChain.get_folder` | `FileItem \| None` |
| `download_file(fileitem, path)` | `StorageChain.download_file` | `Path \| None` |
| `upload_file(fileitem, path, new_name)` | `StorageChain.upload_file` | `FileItem \| None` |
| `delete_file(fileitem)` | `StorageChain.delete_file` | `bool \| None` |
| `rename_file(fileitem, name)` | `StorageChain.rename_file` | `bool \| None` |
| `get_file_item(storage, path)` | `StorageChain.get_file_item` | `FileItem \| None` |
| `get_file_item_strict(storage, path)` | `StorageChain.get_file_item_strict` | `FileItemQueryResult \| None` |
| `get_parent_item(fileitem)` | `StorageChain.get_parent_item` | `FileItem \| None` |
| `snapshot_storage(...)` | `StorageChain.snapshot_storage` | `dict[str, dict] \| None` |
| `storage_manage(storage, action, **params)` | `StorageChain.manage_storage` | `dict` |

模块调度器默认 `plugin_short_circuit=True`；返回非空结果后不再调用内置 `FileManagerModule`。`list_files` 是 `ORDERED_LIST_MERGE`，内置模块对 `gdrive` 返回 `None`，因此最终结果仍是插件列表。

## 2. 类结构

```python
class GDriveStorageProvider:
    """整理链/模块方法共用的 Google Drive 适配器。"""

    transtype = {"copy": "复制", "move": "移动"}

    def __init__(self, client: GoogleDriveClient, log: Callable[..., None]):
        self.client = client
        self.log = log

    # 读
    def list(self, fileitem): ...
    def get_item(self, path): ...
    def get_item_strict(self, path): ...
    def get_folder(self, path): ...
    def get_parent(self, fileitem): ...
    def detail(self, fileitem): ...

    # 写
    def create_folder(self, parent, name): ...
    def upload(self, folder, path, new_name=None): ...
    def download(self, fileitem, path=None): ...
    def copy(self, fileitem, path, new_name): ...
    def move(self, fileitem, path, new_name): ...
    def rename(self, fileitem, name): ...
    def delete(self, fileitem): ...

    # 管理
    def usage(self): ...
    def check(self): ...
    def is_support_transtype(self, transtype): ...
```

- `client` 由插件在 `init_plugin` 中按配置构建；配置变更后重建。
- Provider 不缓存宿主对象；路径缓存由 client 管理。
- 错误：读写方法捕获 `GoogleDriveError`，记录日志并返回 `None`/`False`；`get_item_strict` 只把“确认不存在”映射为 `FileItemQueryResult(item=None)`，API 故障向上抛。

## 3. 插件类

```python
class GDriveStorage(_PluginBase):
    plugin_name = "Google Drive 存储"
    plugin_desc = "把 Google Drive 接入 MoviePilot 存储体系，支持浏览、上传、下载、移动、复制和容量查询。"
    plugin_version = "1.0.0"
    plugin_label = "网盘"
    plugin_author = "local"
    plugin_config_prefix = "gdrivestorage_"
    plugin_order = 100
    auth_level = 1
```

### 3.1 状态字段

- `_enabled`、`_display_name`、`_register_storage`
- `_client: Optional[GoogleDriveClient]`
- `_provider: Optional[GDriveStorageProvider]`
- `_config: dict`
- `_logs: list[dict]`、`_page`、`_page_size = 10`
- `_last_quota: dict`（缓存 `{total, available, checked_at}`）
- `_client_lock`（重建时保护）

### 3.2 `init_plugin`

```python
def init_plugin(self, config=None):
    self.stop_service()
    cfg = self._normalize_config(config or {})
    self._config = cfg
    self._enabled = bool(cfg.get("enabled"))
    self._display_name = cfg["display_name"] or "Google Drive"
    self._register_storage = bool(cfg.get("register_storage", True))
    self._logs = list(self.get_data("logs") or [])
    self._last_quota = self.get_data("quota") or {}
    self._client = self._build_client(cfg)
    self._provider = GDriveStorageProvider(self._client, logger) if self._client else None
    if self._enabled and self._register_storage:
        self._ensure_storage_registered()
    if self._enabled and not self._client:
        logger.warning("【GDrive】插件已启用，但 OAuth 凭据不完整")
```

`_build_client` 只要 `client_id`、`client_secret`、`refresh_token` 齐全就构建；否则返回 `None`。不主动发网络请求。

### 3.3 `stop_service`

```python
def stop_service(self):
    if self._client:
        self._client.close()
    self._client = None
    self._provider = None
    try:
        remove_plugin_once_job(self.__class__.__name__, "GDriveStorage_RefreshQuota")
    except Exception:
        pass
```

- 不删除日志/配额缓存（持久化数据）。
- 可重复调用。

## 4. 存储注册

```python
def _ensure_storage_registered(self):
    try:
        helper = StorageHelper()
        storages = helper.get_storagies()
        if any(s.type == STORAGE_TYPE for s in storages):
            helper.set_storage(STORAGE_TYPE, {})
        else:
            helper.add_storage(STORAGE_TYPE, self._display_name, {})
    except Exception as err:
        logger.warning(f"【GDrive】注册系统存储失败：{err}")
```

- `StorageHelper` 从 `app.sdk.services` 导入。
- 不修改 name；README 说明名称可在系统设置中改。

## 5. 事件

```python
@eventmanager.register(ChainEventType.StorageOperSelection)
def storage_oper_selection(self, event: Event) -> None:
    if not self._enabled or not self._provider:
        return
    data = event.event_data
    if getattr(data, "storage", None) != STORAGE_TYPE:
        return
    data.storage_oper = self._provider
```

- 装饰器在类定义期注册；宿主按 owner class 解析运行实例。
- 不处理其他存储。

## 6. `get_module` 与模块方法

```python
def get_module(self) -> Dict[str, Any]:
    return {
        "list_files": self.list_files,
        ...
        "storage_manage": self.storage_manage,
    }
```

统一的 gdrive 判断：

```python
def _is_gdrive_storage(self, storage: Optional[str]) -> bool:
    return (storage or "") == STORAGE_TYPE
```

方法签名严格匹配契约：

```python
def list_files(self, fileitem: FileItem, recursion: bool = False) -> Optional[List[FileItem]]
def any_files(self, fileitem: FileItem, extensions: Optional[List[str]] = None) -> Optional[bool]
def create_folder(self, fileitem: FileItem, name: str) -> Optional[FileItem]
def get_folder(self, storage: str, path: Path) -> Optional[FileItem]
def download_file(self, fileitem: FileItem, path: Optional[Path] = None) -> Optional[Path]
def upload_file(self, fileitem: FileItem, path: Path, new_name: Optional[str] = None) -> Optional[FileItem]
def delete_file(self, fileitem: FileItem) -> Optional[bool]
def rename_file(self, fileitem: FileItem, name: str) -> Optional[bool]
def get_file_item(self, storage: str, path: Path) -> Optional[FileItem]
def get_file_item_strict(self, storage: str, path: Path) -> Optional[FileItemQueryResult]
def get_parent_item(self, fileitem: FileItem) -> Optional[FileItem]
def snapshot_storage(self, storage: str, path: Path, last_snapshot_time: Optional[float] = None, max_depth: int = 5, previous_snapshot: Optional[Dict[str, Dict[str, Any]]] = None) -> Optional[Dict[str, Dict[str, Any]]]
def storage_manage(self, storage: str, action: str, **params: Any) -> Optional[Dict[str, Any]]
```

`list_files` 递归：宿主 `recursion=True` 时只返回文件；目录项不返回。实现：

```python
def list_files(self, fileitem, recursion=False):
    if not self._is_gdrive(fileitem.storage) or not self._provider:
        return None
    items = self._provider.list(fileitem)
    if not recursion:
        return items
    files = []
    for item in items:
        if item.type == "dir":
            files.extend(self.list_files(item, True) or [])
        else:
            files.append(item)
    return files
```

`any_files` 按扩展名过滤，扩展名统一小写并补点。

`snapshot_storage`：

```python
def snapshot_storage(self, storage, path, last_snapshot_time=None, max_depth=5, previous_snapshot=None):
    if storage != STORAGE_TYPE: return None
    root = self._provider.get_item(path)
    if not root: return {}
    result = {}
    def walk(item, depth):
        if item.type == "dir":
            if depth >= max_depth: return
            for child in self._provider.list(item) or []:
                walk(child, depth + 1)
        else:
            result[item.path] = {
                "size": item.size or 0,
                "modify_time": item.modify_time or 0,
                "fileid": item.fileid,
                "type": item.type,
            }
    walk(root, 0)
    return result
```

`storage_manage`：

- `usage` -> `{"success": True, "message": "容量已刷新", "data": {total, available}}`
- `support_transtype` -> `{"success": True, "message": "", "data": {"transtype": {...}}}`
- `generate_auth_url` -> `{"success": True, "message": "", "data": {"authUrl": ..., "state": ...}}`
- `check_login` -> `{"success": bool, "message": ..., "data": {"status": 1/0, "tip": ...}}`
- 其他动作 -> `{"success": False, "message": "不支持的动作", "data": None}`

## 7. 配置归一化

```python
def _normalize_config(self, cfg):
    return {
        "enabled": bool(cfg.get("enabled")),
        "display_name": str(cfg.get("display_name") or "Google Drive").strip(),
        "register_storage": bool(cfg.get("register_storage", True)),
        "client_id": str(cfg.get("client_id") or "").strip(),
        "client_secret": str(cfg.get("client_secret") or "").strip(),
        "refresh_token": str(cfg.get("refresh_token") or "").strip(),
        "redirect_uri": str(cfg.get("redirect_uri") or DEFAULT_REDIRECT_URI).strip(),
        "oauth_code": str(cfg.get("oauth_code") or "").strip(),
        "root_folder_id": str(cfg.get("root_folder_id") or "root").strip(),
        "root_path": self._normalize_root_path(cfg.get("root_path")),
        "use_shared_drive": bool(cfg.get("use_shared_drive")),
        "shared_drive_id": str(cfg.get("shared_drive_id") or "").strip(),
        "delete_mode": "permanent" if cfg.get("delete_mode") == "permanent" else "trash",
        "chunk_size_mb": max(1, int(cfg.get("chunk_size_mb") or 8)),
        "timeout": max(5, int(cfg.get("timeout") or 60)),
    }
```

`root_path` 规范为 `/` 或 `/xxx`，拒绝 `..`。

配置写回：

```python
def _save_config(self, updates):
    merged = dict(self._config)
    merged.update(updates)
    self.update_config(merged)
    self._config = merged
```

用于 OAuth 成功后写回 `refresh_token`、清除 `oauth_code`/state。

## 8. OAuth 页面流程

### 8.1 生成授权链接

`get_page()` 中调用 `_auth_url()`：

```python
def _auth_url(self):
    if not self._client or not self._config.get("redirect_uri"):
        return None
    state = self.get_data("oauth_state") or {}
    now = time.time()
    if not state.get("value") or float(state.get("expires_at") or 0) < now:
        state = {"value": secrets.token_urlsafe(24), "expires_at": now + 600}
        self.save_data("oauth_state", state)
    return self._client.build_auth_url(self._config["redirect_uri"], state["value"])
```

页面用：

```python
{"component": "VBtn", "props": {"href": auth_url, "target": "_blank", "color": "primary", "prepend-icon": "mdi-google"}, "text": "打开 Google 授权页"}
```

### 8.2 回调

```python
def api_oauth_callback(self, code: str = "", state: str = "", error: str = ""):
    if error:
        return {"success": False, "message": f"Google 授权失败：{error}", "data": None}
    saved = self.get_data("oauth_state") or {}
    if not state or state != saved.get("value") or float(saved.get("expires_at") or 0) < time.time():
        return {"success": False, "message": "授权状态校验失败或已过期，请重新生成授权链接", "data": None}
    try:
        token = self._client.exchange_code(code, self._config["redirect_uri"])
    except Exception as err:
        logger.warning(f"【GDrive】授权码换取令牌失败：{err}")
        return {"success": False, "message": "授权码换取令牌失败", "data": None}
    refresh = token.get("refresh_token")
    if not refresh:
        return {"success": False, "message": "Google 未返回 refresh_token，请使用 prompt=consent 重新授权", "data": None}
    self._save_config({"refresh_token": refresh, "oauth_code": ""})
    self.del_data("oauth_state")
    self._rebuild_client()
    return {"success": True, "message": "授权成功，refresh_token 已保存", "data": None}
```

- 回调端点 `allow_anonymous: True`；不记录 code/state。
- 返回 JSON 三键；浏览器显示原始 JSON 可接受。可选返回 HTML，但三键更符合页面契约。

### 8.3 手动授权码

`api_auth_exchange` 读取 `self._config["oauth_code"]`，流程同上；成功后清空 `oauth_code`。
页面按钮调用 `/auth/exchange`，配置页保存授权码后再点。

## 9. 页面设计

`get_page()` 返回：

```python
[
  {"component": "div", "props": {"class": "d-flex flex-column", "style": "height: 72vh; min-height: 520px; max-height: 800px; padding-bottom: 84px; box-sizing: border-box;"}, "content": [
      {"component": "VAlert", "props": {"type": "warning"| "info"| "success", "variant": "tonal", "class": "mb-3", "style": "flex: 0 0 auto;", "text": "..."}},
      {"component": "VRow", ... 状态卡片 ...},
      {"component": "div", "props": {"class": "d-flex flex-wrap ga-2 mb-3 flex-shrink-0"}, "content": [按钮...]},
      {"component": "div", "props": {"class": "flex-grow-1", "style": "min-height: 0; overflow: auto;"}, "content": [VTable...]},
      {"component": "div", ... 分页 ...}
  ]}
]
```

状态数据：

- 未启用 -> warning。
- 未配置凭据 -> warning，提示填写 client_id/secret/refresh_token。
- 已配置未测试 -> info，提示点“测试连接”。
- 配额缓存 -> 显示总/剩余（`_format_size`）。
- 存储注册 -> 查询 `StorageHelper.get_storage("gdrive")`，显示“已注册/未注册”。

日志表格列：时间、操作、路径、结果、详情。日志条目：

```python
{"time": "2026-11-13 12:00:00", "action": "upload", "path": "/Movies/a.mkv", "ok": True, "detail": ""}
```

分页：`_page` 实例字段；`/logs/page` 修改后返回三键，页面刷新。

## 10. 安全与限制

- 不在日志/页面显示 `client_secret`、`refresh_token`、`oauth_code`。
- `api_oauth_callback` 校验 state；state 10 分钟过期；一次性删除。
- 路径解析拒绝 `..`；下载文件名取 basename，防目录穿越。
- 不修改宿主 `StorageSchema`；README 说明 `gdrive:` 前缀限制。
- 一个宿主只启用一个 Google Drive 实例（系统存储类型唯一）。

## 11. 验证

- `py_compile`。
- AST 检查页面 API 三键、`get_module` 方法集合。
- Mock client/provider 单测：非 gdrive 返回 None；gdrive 委托；事件只设置 gdrive；form 默认值。
- 真实宿主：启用、存储选项、目录配置、浏览、上传、下载、移动、复制、页面按钮。
