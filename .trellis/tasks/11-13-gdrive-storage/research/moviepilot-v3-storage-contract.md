# MoviePilot V3 存储扩展契约（代码核对）

> 证据基线：`jxxghp/MoviePilot` 分支 `v3`，2026-11-13 拉取；插件仓 `jxxghp/MoviePilot-Plugins` 主分支同日拉取。
> 本文只记录宿主实际代码，不把 V2 FAQ 的旧描述当成 V3 契约。

## 1. 插件生命周期与扩展点

`app/sdk/plugin/base.py` 的 `_PluginBase` 提供：

- `init_plugin(config)`、`get_state()`、`get_api()`、`get_form()`、`get_page()`、`stop_service()`。
- `get_module() -> Optional[Dict[str, Any]]`：声明插件要接管的宿主模块方法（方法名 -> 绑定方法）。
- `get_database_models()`、`save_data()` / `get_data()`、`update_config()` / `get_config()`。
- `post_message()` 通知入口。

`app/runtime/extensions/plugin/projection.py`：

- `get_module()` 只对 `get_state()` 为真的插件投影。
- 返回必须是 `Mapping`，否则记录错误并跳过。
- 模块方法以 `(plugin_id, plugin_name)` 为键进入模块调度器。

## 2. 存储扩展的两条路径

### 2.1 文件管理模块方法（浏览、增删改查）

`app/runtime/extensions/module/contracts.py` 登记了 `storage` 族方法，默认 `public_to_plugins=True`、`plugin_short_circuit=True`：

| 方法 | 必需参数 | 结果形状 | 聚合 |
| --- | --- | --- | --- |
| `list_files` | `fileitem`, `recursion` | `list[FileItem]` | `ORDERED_LIST_MERGE` |
| `any_files` | `fileitem`, `extensions` | `bool \| None` | `FIRST_NON_EMPTY` |
| `create_folder` | `fileitem`, `name` | `FileItem \| None` | `FIRST_NON_EMPTY` |
| `get_folder` | `storage`, `path` | `FileItem \| None` | `FIRST_NON_EMPTY` |
| `download_file` | `fileitem`, `path` | `FileItem \| None` | `FIRST_NON_EMPTY` |
| `upload_file` | `fileitem`, `path`, `new_name` | `FileItem \| None` | `FIRST_NON_EMPTY` |
| `delete_file` | `fileitem` | `bool \| None` | `FIRST_NON_EMPTY` |
| `rename_file` | `fileitem`, `name` | `bool \| FileItem` | `FIRST_NON_EMPTY` |
| `get_file_item` | `storage`, `path` | `FileItem \| None` | `FIRST_NON_EMPTY` |
| `get_file_item_strict` | `storage`, `path` | `FileItemQueryResult \| None` | `FIRST_NON_EMPTY` |
| `get_parent_item` | `fileitem` | `FileItem \| None` | `FIRST_NON_EMPTY` |
| `snapshot_storage` | `storage`, `path`, `last_snapshot_time`, `max_depth`, `previous_snapshot` | `dict[str, dict] \| None` | `FIRST_NON_EMPTY` |
| `storage_manage` | `storage`, `action` | `StorageProviderResult` | `FIRST_NON_EMPTY` |

`app/chain/storage.py` 的 `StorageChain` 通过 `run_module(...)` 调用这些方法。插件返回 `None` 时继续交给下一个提供方；非空结果按契约短路或合并。

`app/modules/filemanager/module.py` 的 `FileManagerModule` 只把 `app/modules/filemanager/storages` 下自带 `schema` 的类加入 `_support_storages`。因此：

- 自定义存储不能靠“新增一个 StorageBase 子类文件”被自动发现。
- 浏览器/文件管理相关调用必须由插件 `get_module()` 提供同名方法。
- `FileManagerModule` 对未知存储通常返回 `None` 或 `{"success": False, ...}`，插件返回非空结果即可短路。

### 2.2 整理链的存储适配器选择（上传/下载/移动/复制）

`app/chain/transfer/plan.py` 的 `_TransferChain__select_storage_oper(storage)`：

```python
event_data = StorageOperSelectionEventData(storage=storage)
event = self.eventmanager.send_event(ChainEventType.StorageOperSelection, event_data)
return event.event_data.storage_oper if event and event.event_data else None
```

`app/schemas/event.py`：

```python
class StorageOperSelectionEventData(ChainEventData):
    storage: Optional[str]
    storage_oper: Optional[Callable[..., Any]]
```

`ChainEventData` 的 `model_config` 是 `arbitrary_types_allowed=True`，且未开启 `validate_assignment`；事件对象保留原始 payload 实例，插件处理器可以直接给 `event_data.storage_oper` 赋值。

`app/modules/filemanager/module.py` 的整理入口：

- `plan_transfer(source_oper=...)`：`source_oper` 为空时按源存储标识取内置适配器。
- `execute_transfer_plan(target_oper=...)`：`target_oper` 为空时按 `checkpoint.target_storage` 取内置适配器；取不到直接返回“不支持的存储类型”。

因此，目标存储为 `gdrive` 时，**必须**在 `StorageOperSelection` 事件里返回插件适配器；否则整理会失败。

`app/modules/filemanager/transhandler.py` 对适配器实际使用的方法（grep `source_oper.` / `target_oper.`）：

- `get_item`、`get_item_strict`
- `get_folder`
- `list`
- `upload`
- `download`
- `copy`
- `move`
- `delete`
- `is_support_transtype`

适配器不需要继承 `StorageBase`，鸭子类型即可；但必须提供上述方法。

## 3. 存储配置注册

`app/application/storage.py`：

```python
class StorageHelper:
    @staticmethod
    def get_storagies() -> List[StorageConf]
    def get_storage(self, storage: str) -> Optional[StorageConf]
    def set_storage(self, storage: str, conf: dict[str, object]) -> None
    def add_storage(self, storage: str, name: str, conf: dict[str, object]) -> None
    def reset_storage(self, storage: str) -> None
```

- `add_storage` 直接追加一条 `StorageConf` 到系统配置 `SystemConfigKey.Storages`。
- `set_storage` 只更新同类型条目的 `config`，不更新 `name`。
- 没有删除接口；禁用插件时不要删除系统存储条目，避免破坏用户目录配置。

`app/api/endpoints/storage.py`：

- `GET /storage/options` 返回所有已配置存储的 `{name, type}`，包括插件注册的自定义类型。
- `GET /storage/catalog` 只返回 `_STORAGE_CATALOG_META` 中的内置类型，自定义类型不会出现在这里。
- `POST /storage/manage` 走 `StorageChain().manage_storage(storage, action, **params)`，最终由插件 `storage_manage` 处理。

结论：插件启动时用 `StorageHelper.add_storage("gdrive", name, {})` 注册；已存在则 `set_storage("gdrive", {})`，不重复追加。用户在目录配置中从 `/storage/options` 选择 “Google Drive”。

## 4. `StorageSchema` 是封闭枚举

`app/schemas/types.py`：

```python
class StorageSchema(Enum):
    Local = "local"
    Alipan = "alipan"
    U115 = "u115"
    Rclone = "rclone"
    Alist = "alist"
    AlistGo = "alistgo"
    SMB = "smb"
```

`FileURI.from_uri`、`DirectoryHelper._split_file_uri`、`_has_storage_prefix` 都只遍历这个枚举。`"gdrive:/..."` 这种裸字符串会被当成 `local`。

处理策略（本插件采用）：

- 不修改宿主枚举，不写宿主内部状态。
- 插件返回的所有 `FileItem` 显式设置 `storage="gdrive"`。
- 目录配置使用独立的 `storage` / `library_storage` 字段，不在路径字符串里写 `gdrive:` 前缀。
- 客户端在解析路径时容错剥离 `gdrive:` 前缀，避免宿主把带前缀的路径原样传入时失败。
- README 明确说明：Google Drive 用作媒体库/目标存储；下载器保存目录仍应是本地路径。

## 5. `FileItem` 约定

`app/schemas/file.py`：

- 目录 `path` 以 `/` 结尾（本地/115 实现均如此）。
- 文件 `extension` 不含点，例如 `mkv`。
- `size` 为字节数；目录或未知时为 `None`。
- `modify_time` 为 Unix 秒（float）。
- `fileid` 是存储服务标识；`parent_fileid` 是父标识。
- `FileItemQueryResult(item=...)` 用于严格查询：`item=None` 表示“已确认不存在”，返回 `None` 表示“此提供方不支持”。

## 6. 页面 API envelope

`.trellis/spec/plugin-v3/page-api-envelope.md`：

- 页面按钮 API 必须返回恰好三键：`success`、`message`、`data`。
- `success=True` 才会触发宿主重新请求 `plugin/page/{plugin_id}`。
- 固定高度 flex 容器中的 `VAlert` 必须加 `style: "flex: 0 0 auto;"`。

## 7. 匿名 OAuth 回调

`app/adapters/web/plugin/routes.py` 支持在 `get_api()` 条目中设置：

```python
{"allow_anonymous": True}
```

设置后不注入 `bear` / `apikey` 依赖。Google OAuth 回调可以据此在未登录状态下接收 `code` 和 `state`；必须自行校验一次性 `state`。

## 8. 第三方依赖

`app/sdk/network.py` 使用 HTTPX2。插件可直接 `import httpx2` 使用同步 `Client`；宿主共享环境已提供。为降低导入副作用，客户端模块应延迟导入 `httpx2`，并允许注入测试传输层。
