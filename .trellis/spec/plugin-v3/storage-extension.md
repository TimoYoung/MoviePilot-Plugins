# Scenario: 通过插件新增自定义存储后端

## 1. Scope / Trigger

适用于在 MoviePilot V3 插件中把第三方网盘/对象存储接入宿主存储体系，使其能够：

- 出现在系统存储选项和目录配置中；
- 参与文件浏览、上传、下载、移动、复制、删除、容量查询；
- 作为整理链的源存储或目标存储。

触发条件：新增 `plugins.v3/<plugin>/` 存储插件、修改 `get_module()` 存储方法、注册/更新系统存储、处理 `StorageOperSelection` 事件。

证据基线：MoviePilot `v3` 分支（2026-11-13），参考实现 `plugins.v3/gdrivestorage/`。

## 2. 必须同时提供的三块能力

自定义存储在 V3 里不是一个 `StorageBase` 子类文件就能被发现。宿主 `FileManagerModule.init_module()` 只加载 `app/modules/filemanager/storages` 下的内置类；插件必须提供：

1. **系统存储条目**：`StorageHelper.add_storage(type, name, {})`，已存在则 `set_storage(type, conf)`。
2. **模块方法**：`get_module()` 返回 `list_files`、`any_files`、`create_folder`、`get_folder`、`download_file`、`upload_file`、`delete_file`、`rename_file`、`get_file_item`、`get_file_item_strict`、`get_parent_item`、`snapshot_storage`、`storage_manage`。
3. **整理适配器**：监听 `ChainEventType.StorageOperSelection`，当 `storage` 匹配时把适配器赋给 `event_data.storage_oper`。

缺少第 3 块时，整理链 `plan_transfer` / `execute_transfer_plan` 会按内置存储查找目标适配器，找不到就返回“不支持的存储类型”。

## 3. Signatures

### 3.1 模块方法（`get_module()`）

| 方法 | 签名（参数名必须一致） | 返回 |
| --- | --- | --- |
| `list_files` | `(fileitem, recursion=False)` | `list[FileItem] \| None` |
| `any_files` | `(fileitem, extensions=None)` | `bool \| None` |
| `create_folder` | `(fileitem, name)` | `FileItem \| None` |
| `get_folder` | `(storage, path)` | `FileItem \| None` |
| `download_file` | `(fileitem, path=None)` | `Path \| None` |
| `upload_file` | `(fileitem, path, new_name=None)` | `FileItem \| None` |
| `delete_file` | `(fileitem)` | `bool \| None` |
| `rename_file` | `(fileitem, name)` | `bool \| FileItem \| None` |
| `get_file_item` | `(storage, path)` | `FileItem \| None` |
| `get_file_item_strict` | `(storage, path)` | `FileItemQueryResult \| None` |
| `get_parent_item` | `(fileitem)` | `FileItem \| None` |
| `snapshot_storage` | `(storage, path, last_snapshot_time=None, max_depth=5, previous_snapshot=None)` | `dict[str, dict] \| None` |
| `storage_manage` | `(storage, action, **params)` | `dict` |

非目标存储必须返回 `None`，让宿主继续使用内置模块；返回 `False`/`{}`/空列表会被视为有效结果并可能短路后续提供方。

### 3.2 整理适配器（鸭子类型）

`StorageOperSelection` 返回的对象不需要继承 `StorageBase`，但 `TransHandler` 会直接调用：

```text
get_item(path) -> FileItem | None
get_item_strict(path) -> FileItem | None        # 注意不是 FileItemQueryResult
get_folder(path) -> FileItem | None
list(fileitem) -> list[FileItem]
upload(folder, local_path, new_name=None) -> FileItem | None
download(fileitem=..., path=...) -> Path | None
copy(fileitem, target_dir, new_name) -> bool
move(fileitem, target_dir, new_name) -> bool
delete(fileitem) -> bool
is_support_transtype(transtype) -> bool
```

`get_item_strict` 的返回类型最容易写错：模块方法 `get_file_item_strict` 返回 `FileItemQueryResult`，但适配器 `get_item_strict` 必须返回 `FileItem | None`。

### 3.3 事件

```python
@eventmanager.register(ChainEventType.StorageOperSelection)
def storage_oper_selection(self, event: Event) -> None:
    if not self.get_state() or not self._provider:
        return
    data = event.event_data
    if getattr(data, "storage", None) != "gdrive":
        return
    data.storage_oper = self._provider
```

`StorageOperSelectionEventData` 允许 `arbitrary_types_allowed=True` 且未开启 `validate_assignment`，可以直接赋任意适配器对象。

## 4. Contracts

### 4.1 路径与存储标识

- `StorageSchema` 是封闭枚举（local/alipan/u115/rclone/alist/alistgo/smb）。插件不要修改宿主枚举。
- 插件返回的 `FileItem` 必须显式设置 `storage="<type>"`；目录 `path` 以 `/` 结尾，文件 `extension` 不含点。
- 宿主 `FileURI.from_uri("gdrive:/...")` 不会识别自定义类型，会按 local 解析。目录配置应使用独立的 `storage`/`library_storage` 字段，不要把 `type:` 前缀写进路径。
- 插件路径解析层应容错剥离 `type:` 前缀，并拒绝 `..` 和 Windows 盘符混用。

### 4.2 严格查询语义

- 普通查询：不存在返回 `None`/空列表，API 故障可记录日志后返回 `None`。
- 严格查询：确认不存在返回 `FileItemQueryResult(item=None)`；API 故障应抛出异常，让宿主保留旧基线或失败重试。
- 快照：根目录查询失败返回 `None`（保留旧基线），确认空目录返回 `{}`。

### 4.3 重试与副作用

- 只对幂等方法（GET/PATCH/DELETE/PUT）默认重试；POST 创建/复制/上传初始化不应自动重试，避免超时后重复创建。
- 可恢复上传分片使用 PUT，可单独重试；308 表示未完成，不是错误。
- `StorageOperSelection` 适配器未就绪（未授权/未启用）时不要返回，避免把失败留给用户整理任务。

## 5. Validation & Error Matrix

| 条件 | 宿主行为 | 修复 |
| --- | --- | --- |
| 只注册存储、没有 `get_module` | 浏览/增删改查走内置模块，未知类型返回空 | 补齐模块方法 |
| 目标存储没有 `StorageOperSelection` 适配器 | 整理返回“不支持的存储类型” | 事件返回适配器 |
| `get_item_strict` 返回 `FileItemQueryResult` | `TransHandler` 把模型当 `FileItem` 使用，路径/标识为空或异常 | 适配器返回 `FileItem | None` |
| 非目标存储返回 `False` | 短路内置模块，内置存储失效 | 非目标存储返回 `None` |
| 页面 API 缺少 `data` | 宿主判定非法 envelope，不刷新页面 | 恰好三键 `{success,message,data}` |
| 固定高度 flex 中的 `VAlert` | 文本被压扁 | `style: "flex: 0 0 auto;"` |
| 匿名 OAuth 回调不校验 state | CSRF/授权码劫持风险 | 一次性 state + 过期时间 |

## 6. Tests Required

本仓库无宿主时至少：

1. `python -m py_compile` 插件源码。
2. AST 检查：`get_module()` 方法集合、页面 API 三键 envelope、`package.v3.json` 版本/图标。
3. 客户端纯逻辑单测（注入 FakeTransport）：OAuth、路径、列表、上传分片、移动/复制、错误重试。
4. 有宿主时：`/storage/options` 出现自定义类型；目录配置可选择；浏览/上传/下载/移动/复制可用；页面按钮触发自动刷新。

## 7. Wrong vs Correct

### Wrong

```python
# 适配器返回了模块契约的回执模型，TransHandler 会把它当 FileItem
def get_item_strict(self, path):
    return FileItemQueryResult(item=self._to_file_item(...))
```

```python
# 非目标存储返回 False，宿主认为插件已处理并短路内置存储
def delete_file(self, fileitem):
    if fileitem.storage != "gdrive":
        return False
```

```python
# 修改宿主封闭枚举（不可取）
StorageSchema._member_names_.append("GDrive")
```

### Correct

```python
# 适配器：FileItem | None
def get_item_strict(self, path):
    item = self.client.get_item(path)
    return self._to_file_item(item) if item else None

# 模块方法：FileItemQueryResult
def get_file_item_strict(self, storage, path):
    if storage != "gdrive":
        return None
    return FileItemQueryResult(item=self._provider.get_item_strict(path))

# 非目标存储交还宿主
def delete_file(self, fileitem):
    if fileitem.storage != "gdrive":
        return None
    return self._provider.delete(fileitem)
```
