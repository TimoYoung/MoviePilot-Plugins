# Google Drive 存储扩展插件

## Goal

在 `moviepilot-plugins` 仓库中新增一个 MoviePilot V3 插件（不是新仓库），把 Google Drive 接入宿主存储体系，让用户在目录配置中把 Google Drive 作为下载/媒体库存储使用。

## Background

- 宿主为 MoviePilot V3（`app.sdk.plugin._PluginBase`、`plugins.v3/`）。
- 宿主内置存储只包含 local/alipan/u115/rclone/alist/alistgo/smb；插件需要通过 `StorageHelper` 注册自定义存储、通过 `StorageOperSelection` 事件提供整理适配器、通过 `get_module()` 提供文件管理方法。
- 现有参考实现：`plugins.v3/u115autoupload/__init__.py`（界面、API envelope、生命周期）。
- Google Drive 使用官方 Drive API v3 + OAuth 2.0，不引入额外 Python SDK。

## Requirements

### R1 插件与配置

- 新增 `GDriveStorage` 插件，目录 `plugins.v3/gdrivestorage/`，类名、目录名、`package.v3.json` 键名一致。
- 配置项至少包括：启用、存储显示名、OAuth client_id/client_secret、refresh_token、授权回调 URI、授权码（临时）、根目录 ID、虚拟根路径、共享盘开关/ID、删除模式、上传分片大小、HTTP 超时、是否自动注册存储。
- `init_plugin()` 可重复调用；`stop_service()` 释放 HTTP 客户端、缓存和一次性任务。

### R2 OAuth2 授权

- 支持两种授权方式：
  1. 直接填写 refresh_token；
  2. 在插件详情页生成 Google OAuth 授权链接，回调接收 `code` 并换取 refresh_token。
- 回调端点允许匿名访问，但必须校验一次性 `state` 和有效期（默认 10 分钟）。
- 授权失败、refresh_token 失效时给出可读错误，不泄露 secret/token。

### R3 存储注册

- 启用且 `register_storage=True` 时，通过 `app.sdk.services.StorageHelper.add_storage("gdrive", name, {})` 注册存储；已存在时不重复追加。
- 宿主的 `GET /api/v1/storage/options` 应能看到该存储。
- 禁用插件时不删除系统存储条目（宿主没有安全的删除接口）。

### R4 文件管理能力

通过 `get_module()` 提供以下方法，并在 `storage != "gdrive"` 时返回 `None`：

- `list_files`、`any_files`、`create_folder`、`get_folder`
- `download_file`、`upload_file`、`delete_file`、`rename_file`
- `get_file_item`、`get_file_item_strict`、`get_parent_item`
- `snapshot_storage`、`storage_manage`

### R5 整理适配器

- 监听 `ChainEventType.StorageOperSelection`，当 `storage == "gdrive"` 时返回插件适配器。
- 适配器支持 `get_item`、`get_item_strict`、`get_folder`、`list`、`upload`、`download`、`copy`、`move`、`delete`、`is_support_transtype`。
- 声明支持的整理方式：`move`、`copy`；不支持 `link`/`softlink`/`reflink`。
- 目标路径不存在时递归创建目录；移动/复制/上传使用 Google Drive 服务端操作。

### R6 界面

- 配置页使用 Vuetify JSON 表单，字段中文标签、默认值与 README 一致。
- 详情页展示：启用/授权/存储注册状态、容量、根路径/共享盘信息、最近操作日志、操作按钮（测试连接、刷新容量、生成授权链接、用授权码换取令牌、清空日志、分页）。
- 所有页面按钮 API 返回恰好 `{success, message, data}` 三键。
- 固定高度 flex 容器中的 `VAlert` 使用 `style: "flex: 0 0 auto;"`。

### R7 文档、元数据与测试

- `README.md` 说明 Google Cloud 配置、OAuth 授权、目录配置、共享盘、限制与故障排查。
- `package.v3.json` 增加条目；`plugin_version`、`version`、`history` 一致。
- 图标放入 `icons/`。
- 增加可在无宿主环境执行的测试：客户端纯逻辑测试、页面 envelope 静态检查、版本/元数据一致性检查。

## Non-Goals

- 不实现 Google 服务账号授权（服务账号没有个人 Drive 配额，且配置复杂）。
- 不实现 Google Docs/Sheets/Slides 的完整编辑能力；下载时按导出映射处理或返回明确错误。
- 不实现多账号切换；一个插件实例对应一套 OAuth 凭据。
- 不修改宿主 `StorageSchema` 枚举或其它宿主内部状态。
- 不提交 npm/Vue 前端产物；本插件使用 Vuetify JSON 渲染模式。

## Constraints

- 不新增 Python 依赖；使用宿主提供的 `httpx2`（延迟导入）。
- 只使用 `app.sdk.*`、`app.schemas.*` 等公开契约；不导入 `app.db.models.*`、`SessionFactory`、宿主内部单例。
- 不在导入期发起网络请求或启动线程。
- 不把运行数据写入插件源码目录。
- Google Drive 的路径是 ID 驱动的；插件对外暴露虚拟 POSIX 路径，内部维护路径到 ID 的解析和缓存。

## Acceptance Criteria

- [ ] `python -m py_compile plugins.v3/gdrivestorage/__init__.py plugins.v3/gdrivestorage/gdrive_client.py` 通过。
- [ ] `python -m pytest tests/v3/gdrivestorage -q` 通过。
- [ ] `package.v3.json` 可解析，`GDriveStorage` 条目存在，版本与插件类一致。
- [ ] `get_api()` 的所有页面操作端点返回三键 envelope（静态检查通过）。
- [ ] `get_module()` 声明 R4 中全部方法；非 gdrive 存储返回 `None`。
- [ ] `StorageOperSelection` 只对 `gdrive` 返回适配器。
- [ ] 配置页/详情页字段与 README 一致，详情页无未捕获异常。
- [ ] 在真实 V3 宿主中至少验证一次：插件启用、存储出现在 `/storage/options`、目录配置选择 Google Drive、浏览/上传/下载/移动/复制可用。
- [ ] 文档说明 `StorageSchema` 限制：目录配置使用独立存储字段，不在路径字符串里写 `gdrive:`。

## Task Map

| 子任务 | 交付物 | 依赖 |
| --- | --- | --- |
| `11-13-gdrive-api-client` | `gdrive_client.py` 与客户端单测 | 无 |
| `11-13-gdrive-storage-integration` | `__init__.py` 插件/适配器/界面 | 客户端 |
| `11-13-gdrive-docs-release` | README、package.v3.json、图标、契约测试 | 前两者最终接口 |

## Notes

- 研究证据：`research/moviepilot-v3-storage-contract.md`、`research/google-drive-api-v3.md`、`research/plugin-ui-and-release.md`。
- 父任务负责最终集成检查；子任务按上表顺序推进。
