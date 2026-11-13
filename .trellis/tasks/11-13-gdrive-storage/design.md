# Google Drive 存储扩展插件 — 技术设计

## 1. 目录与模块边界

```text
plugins.v3/gdrivestorage/
├── __init__.py        # GDriveStorage 插件类、存储注册、事件、get_module、页面/API
├── gdrive_client.py   # GoogleDriveClient：纯 REST/路径/传输逻辑，不依赖 app.*
└── README.md          # 使用文档

tests/v3/gdrivestorage/
├── test_gdrive_client.py   # 注入 FakeTransport 的客户端单测
└── test_plugin_contract.py # AST/JSON 静态契约检查
```

- `gdrive_client.py` 不导入 `app.*`，可在无宿主环境直接加载测试。
- `__init__.py` 只在运行时导入宿主模块；`httpx2` 在客户端构造时延迟导入。
- 不新增 `pyproject.toml`（无额外依赖）。

## 2. 客户端设计（`gdrive_client.py`）

### 2.1 构造参数

```python
GoogleDriveClient(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    root_folder_id: str = "root",
    root_path: str = "/",
    shared_drive_id: str = "",
    use_shared_drive: bool = False,
    delete_mode: str = "trash",
    chunk_size: int = 8 * 1024 * 1024,
    timeout: float = 60,
    transport: Optional[HttpTransport] = None,
)
```

`transport` 为空时创建 `_HttpxTransport`（延迟 `import httpx2`，失败回退 `httpx`）。测试注入 `FakeTransport`，不触网。

### 2.2 认证

- `build_auth_url(redirect_uri, state) -> str`：拼 `accounts.google.com/o/oauth2/v2/auth`，scope 固定 full drive，`access_type=offline`，`prompt=consent`。
- `exchange_code(code, redirect_uri) -> dict`：POST token endpoint。
- `refresh_access_token() -> str`：带锁刷新，缓存 `access_token` 到 `expires_at - 60s`。
- `_authorized_request(...)`：自动加 Bearer；遇到 401 刷新一次并重试。

### 2.3 路径解析

- 对外路径为虚拟 POSIX 路径，`root_path` 是挂载点（默认 `/`）。
- `normalize_virtual_path(path)`：转 POSIX、折叠 `.`、拒绝 `..`、剥离 `gdrive:` 前缀、补前导 `/`。
- `strip_root(path)`：校验路径位于 `root_path` 下，返回相对段列表。
- `resolve_folder_id(path, create=False)`：
  - 相对段为空 -> `root_folder_id`（共享盘且为空时用 `shared_drive_id`）。
  - 逐段用 `files.list` 查询 `name + parent + mimeType=folder + trashed=false`；命中写缓存。
  - `create=True` 时缺失段调用 `files.create` 创建。
- 缓存：`dict[virtual_path, (folder_id, expires_at)]`，默认 TTL 300s；写操作后清空相关前缀。
- 名称查询转义 `\` 和 `'`。

### 2.4 数据映射

`_to_file_item(raw, virtual_path)`：

```python
FileItem(
    storage="gdrive",
    path=virtual_path + ("/" if is_dir else ""),
    name=raw["name"],
    basename=Path(name).stem,
    extension=Path(name).suffix[1:] if file else None,
    type="dir" if mime == folder else "file",
    size=int(raw["size"]) if present else None,
    modify_time=parse_rfc3339(raw.get("modifiedTime")),
    fileid=raw["id"],
    parent_fileid=(raw.get("parents") or [None])[0],
    url=raw.get("webViewLink"),
    thumbnail=raw.get("thumbnailLink"),
)
```

### 2.5 操作语义

| 方法 | Google Drive 调用 | 说明 |
| --- | --- | --- |
| `list(fileitem)` | `files.list` | 分页取全；目录在前/文件在后由宿主排序，插件保持 API 顺序 |
| `get_item(path)` | 解析父目录 + name 查询 | 不存在返回 `None` |
| `get_item_strict(path)` | 同上 | 返回 `FileItemQueryResult(item=...)`；API 故障抛异常 |
| `get_folder(path, create)` | 逐段解析/创建 | 宿主整理前创建目标目录 |
| `create_folder(parent, name)` | `files.create` | 同名已存在时返回已存在项 |
| `upload(folder, local, new_name)` | 可恢复上传 | 256KiB 对齐分片；308 继续；5xx 重试 |
| `download(fileitem, path)` | `files.get?alt=media` | 流式写本地；Google Docs 走 `files.export` |
| `copy(fileitem, target_dir, new_name)` | `files.copy` | 返回布尔 |
| `move(fileitem, target_dir, new_name)` | `files.update` + add/removeParents | 同盘移动 |
| `rename(fileitem, name)` | `files.update` | 返回布尔 |
| `delete(fileitem)` | `DELETE` 或 `PATCH trashed` | 由 `delete_mode` 决定 |
| `detail(fileitem)` | `files.get` | 刷新元数据 |
| `usage()` | `about.get?fields=storageQuota` | 无限容量返回 0 总容量 |
| `check()` | `about.get` + 根目录 `files.get` | 测试连接 |

### 2.6 错误与重试

- 统一 `GoogleDriveError(status_code, reason, message)`；认证错误子类 `GoogleDriveAuthError`。
- 401 刷新一次；429/5xx 指数退避（最多 3 次，尊重 `Retry-After`）。
- 404 在严格查询中映射为“确认不存在”，普通查询返回空/None。
- 不在异常消息中拼接 token/secret。

## 3. 插件设计（`__init__.py`）

### 3.1 类与生命周期

```python
class GDriveStorage(_PluginBase):
    plugin_name = "Google Drive 存储"
    plugin_version = "1.0.0"
    plugin_config_prefix = "gdrivestorage_"
    ...
```

- `init_plugin(config)`：
  1. `stop_service()`；
  2. 解析配置、构建 `GoogleDriveClient`（只要凭据齐全，不要求启用）；
  3. 启用且 `register_storage` 时注册/更新系统存储；
  4. 加载日志、配额缓存。
- `stop_service()`：关闭客户端、清空路径缓存、移除一次性任务、清空状态。
- `get_state()`：返回 `_enabled`。

### 3.2 存储注册

```python
storages = StorageHelper.get_storagies()
if not any(s.type == "gdrive" for s in storages):
    StorageHelper().add_storage("gdrive", display_name, {})
else:
    StorageHelper().set_storage("gdrive", {})
```

- 名称变更不强行覆盖（`set_storage` 不改 name），README 说明可在系统存储设置中改名。
- 禁用时不删除条目。

### 3.3 事件

```python
@eventmanager.register(ChainEventType.StorageOperSelection)
def storage_oper_selection(self, event: Event) -> None:
    if not self._enabled:
        return
    data = event.event_data
    if getattr(data, "storage", None) == "gdrive":
        data.storage_oper = self._provider
```

- 只有 `gdrive` 接管；其他存储保持 `None`，让宿主使用内置适配器。
- `_provider` 是 `GDriveStorageProvider` 实例（内部持有 client）。

### 3.4 `get_module()`

返回方法名到绑定方法的映射：

```python
{
    "list_files": self.list_files,
    "any_files": self.any_files,
    "create_folder": self.create_folder,
    "get_folder": self.get_folder,
    "download_file": self.download_file,
    "upload_file": self.upload_file,
    "delete_file": self.delete_file,
    "rename_file": self.rename_file,
    "get_file_item": self.get_file_item,
    "get_file_item_strict": self.get_file_item_strict,
    "get_parent_item": self.get_parent_item,
    "snapshot_storage": self.snapshot_storage,
    "storage_manage": self.storage_manage,
}
```

每个方法先判断 `storage`/`fileitem.storage`，非 `gdrive` 立即返回 `None`。

### 3.5 提供者（`GDriveStorageProvider`）

不继承宿主 `StorageBase`，按鸭子类型实现整理链需要的方法：

- `get_item`、`get_item_strict`、`get_folder`、`list`
- `upload`、`download`、`copy`、`move`、`delete`
- `is_support_transtype` / `transtype = {"copy": "复制", "move": "移动"}`
- 额外：`rename`、`detail`、`usage`、`check`、`get_parent`

内部方法直接委托 `GoogleDriveClient`；错误转成日志并返回 `None`/`False`，严格查询保留异常。

### 3.6 页面与 API

API：

| path | method | auth | 用途 |
| --- | --- | --- | --- |
| `/test` | POST | bear | 测试连接，返回账号/根目录信息 |
| `/quota` | POST | bear | 刷新容量 |
| `/auth/url` | POST | bear | 生成授权链接 |
| `/auth/exchange` | POST | bear | 用配置中的 `oauth_code` 换 refresh_token |
| `/oauth/callback` | GET | `allow_anonymous: True` | Google 回调，校验 state 后换 token |
| `/logs/page` | POST | bear | 日志分页 |
| `/logs/clear` | POST | bear | 清空日志 |

所有页面端点返回 `{success, message, data}` 三键。

配置页：

- 基础：启用、存储显示名、自动注册存储。
- 凭据：client_id、client_secret（password）、refresh_token（password）、redirect_uri、oauth_code。
- 路径：root_folder_id、root_path、共享盘开关、shared_drive_id。
- 行为：delete_mode（trash/permanent）、chunk_size_mb、timeout。

详情页：

- 状态卡片：启用、授权状态、存储注册状态、容量。
- 操作按钮：测试连接、刷新容量、生成授权链接、用授权码换取令牌、清空日志、分页。
- 最近日志表格（时间、操作、路径、结果、详情）。
- 固定高度 flex 列 + `VAlert` 内联 `flex: 0 0 auto;`。

### 3.7 OAuth 回调

- `get_page` 生成授权链接时创建 `state = secrets.token_urlsafe(24)`，保存到插件数据 `oauth_state`，带 10 分钟过期。
- 回调端点：
  1. 读取 `state`/`code`/`error`；
  2. 校验 state 与有效期；
  3. `exchange_code(code, redirect_uri)`；
  4. 合并写回插件的 `refresh_token`，清除 `oauth_code` 和 state；
  5. 返回简单 HTML 或三键 JSON（匿名端点不要求 envelope，但保持一致更安全）。
- 授权码手动模式：配置页保存 `oauth_code`，详情页按钮调用 `/auth/exchange`；成功后同样写回 refresh_token。

## 4. 兼容与限制

- 不修改宿主 `StorageSchema`；所有 `FileItem` 显式带 `storage="gdrive"`。
- 宿主 `FileURI.from_uri("gdrive:/...")` 会按 local 解析；README 要求使用目录配置的存储下拉框，不在路径里写前缀。
- 共享盘容量不由 `about` 暴露；页面显示“由管理员管理/未提供”。
- Google 原生文档下载需要 export；媒体文件是二进制文件，正常走 `alt=media`。
- 多实例：插件配置按实例隔离，但系统存储条目 `gdrive` 只有一个；README 说明一个宿主只应启用一个 Google Drive 存储实例。

## 5. 回滚与失败边界

- 插件启用失败不应影响宿主启动：`init_plugin` 捕获网络/配置异常，记录日志并保持禁用状态。
- 存储注册失败（系统配置不可写）记录警告，不阻塞插件加载。
- 上传中断：可恢复上传 session 不持久化；下次重新上传。已有文件不删除。
- 删除失败：返回 `False`，宿主保留任务重试状态。
- OAuth state 过期/不匹配：拒绝回调，不修改配置。

## 6. 验证

- 客户端：FakeTransport 覆盖 token 刷新、路径解析、列表、上传、移动、复制、错误映射。
- 插件：AST 检查 API envelope、`get_module` 方法集合、`package.v3.json` 版本、README 字段一致性（人工/简单断言）。
- 宿主：真实 V3 环境验证存储注册、目录配置、浏览、上传、下载、移动、复制、页面按钮刷新。
