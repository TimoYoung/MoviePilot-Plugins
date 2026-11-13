# Google Drive 存储

把 Google Drive 接入 MoviePilot V3 存储体系的自定义存储插件。插件使用 Google Drive API v3 与 OAuth 2.0，不依赖额外的 Google Python SDK。

## 功能

- 在系统存储中注册 `gdrive` 类型，可在目录配置中选择 “Google Drive”。
- 作为媒体库/目标存储参与整理：支持移动（服务端 move）和复制（服务端 copy）。
- 文件浏览、创建目录、上传、下载、重命名、删除、容量查询。
- OAuth2 授权码流程、refresh_token 自动刷新、共享盘列举。
- 详情页显示授权/注册/容量状态和最近操作日志。

## 前置条件

- MoviePilot V3（`system_version >= 3.0.0`）。
- 一个 Google 账号，以及可访问 Google Cloud Console 的权限。
- MoviePilot 能被浏览器访问；OAuth 回调地址必须能从浏览器打开。

## Google Cloud 配置

1. 打开 [Google Cloud Console](https://console.cloud.google.com/)，创建或选择一个项目。
2. 在 “API 和服务 → 库” 中启用 **Google Drive API**。
3. 在 “API 和服务 → OAuth 同意屏幕” 中配置：
   - 用户类型选择“外部”（个人使用可保持“测试”状态并添加测试用户）。
   - 填写应用名称、支持邮箱和开发者联系邮箱。
   - 范围添加 `https://www.googleapis.com/auth/drive`。
4. 在 “API 和服务 → 凭据” 中创建 **OAuth 客户端 ID**，应用类型选择 **Web 应用**。
5. 在“已获授权的重定向 URI”中添加插件回调地址，默认是：

   ```text
   http://localhost:3000/api/v1/plugin/GDriveStorage/oauth/callback
   ```

   如果你通过域名、其它端口或反向代理访问 MoviePilot，请改成实际可访问的地址，并保证与插件里的“OAuth 回调地址”**完全一致**。

6. 记录 client_id 和 client_secret。

> 插件使用完整 `drive` 权限，因为需要读取和修改你已有的媒体库目录。仅在“测试”状态使用时，只有同意屏幕中登记的测试账号可以授权。

## 插件配置

在插件配置页填写以下字段：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| 启用插件 | 关闭 | 启用后才注册存储并响应整理链 |
| 自动注册系统存储 | 开启 | 自动向系统存储写入 `gdrive` 条目 |
| 存储显示名 | `Google Drive` | 显示在系统存储和目录配置中 |
| OAuth client_id | 空 | Google Cloud 创建的 Web 客户端 ID |
| OAuth client_secret | 空 | 对应客户端密钥 |
| refresh_token | 空 | 可直接填写；也可通过授权链接获取 |
| 临时授权码 | 空 | 手动模式下粘贴 Google 返回的 `code` |
| OAuth 回调地址 | `http://localhost:3000/api/v1/plugin/GDriveStorage/oauth/callback` | 必须与 Google Cloud 登记值一致 |
| 根目录 ID 或分享链接 | `root` | 留空或 `root` 表示“我的云端硬盘”根目录；也可填文件夹 URL |
| 虚拟根路径 | `/` | 对外挂载路径；填 `/GDrive` 时整个 Drive 挂载到该路径 |
| 使用共享盘 | 关闭 | 开启后按共享盘列举 |
| 共享盘 ID | 空 | 共享盘 ID，不是普通文件夹 ID |
| 删除方式 | 移入回收站 | `trash` 或 `permanent` |
| 上传分片大小（MB） | `8` | 自动对齐到 256 KiB 的倍数 |
| HTTP 超时（秒） | `60` | 单次请求超时 |

保存后如果勾选“启用插件”，插件会注册 `gdrive` 存储；如果已存在同名类型则只更新配置，不会重复添加。

## 授权方式

### 方式一：详情页授权链接（推荐）

1. 填写 client_id、client_secret、OAuth 回调地址并保存。
2. 打开插件详情页，点击“打开 Google 授权页”。
3. 使用要挂载的 Google 账号登录并同意 `drive` 权限。
4. Google 会跳回回调地址；插件校验一次性 state 后自动保存 refresh_token。
5. 回到详情页点击“测试连接”，确认根目录和容量可读。

### 方式二：手动授权码

1. 打开详情页的授权链接，完成授权。
2. 浏览器跳转到回调地址后，从地址栏复制 `code=` 后面的值。
3. 把它粘贴到配置页“临时授权码”，保存。
4. 回到详情页点击“用授权码换取令牌”。

授权码是一次性的；如果提示过期，请重新生成授权链接。

### 方式三：直接填写 refresh_token

如果你已经通过其它工具拿到 refresh_token，可以直接填入配置页并保存。插件会在需要时自动刷新 access_token。

## 系统存储与目录配置

1. 插件启用并开启“自动注册系统存储”后，打开 MoviePilot 的“设置 → 存储”，应能看到 `Google Drive`（完成授权后才能实际读写）。
2. 在“目录配置”中新增或编辑目录：
   - 下载存储：通常保持 `local`，因为下载器写入本地磁盘。
   - 下载目录：本地路径，例如 `/downloads`。
   - 媒体库存储：选择 `Google Drive`。
   - 媒体库目录：Drive 内的目标路径，例如 `/Movies`。
   - 整理方式：选择“移动”或“复制”。
3. 整理时插件会自动创建缺失的目标目录，并通过 Google Drive 服务端操作完成移动/复制。

> 不要把 `gdrive:` 前缀写进路径字段。V3 的 `StorageSchema` 是封闭枚举，裸字符串 `gdrive:/...` 会被宿主按本地路径解析。目录配置请使用独立的“存储”下拉框。

## 共享盘

1. 在 Google Drive 中找到共享盘，复制共享盘 ID（共享盘 URL 或管理页面中可获取）。
2. 配置中开启“使用共享盘”，填写共享盘 ID。
3. “根目录 ID”填写共享盘内的实际文件夹 ID；留空时插件会尝试把共享盘 ID 当作根目录。
4. 共享盘容量由组织管理员管理，Google Drive API 的 `about.storageQuota` 通常只返回用户个人容量，详情页可能显示为 0/未提供。

## 支持的操作

| 操作 | 说明 |
| --- | --- |
| 浏览 | 按虚拟路径逐段解析 Google Drive 文件夹 |
| 创建目录 | 缺失目录逐段创建，已存在时复用 |
| 上传 | 可恢复分片上传，适合大文件 |
| 下载 | 流式下载；Google 原生文档按 PDF/Office 格式导出 |
| 移动 | 使用 `addParents` / `removeParents` 服务端移动 |
| 复制 | 使用 `files.copy` 服务端复制 |
| 重命名 | 修改 Drive 文件名称 |
| 删除 | 默认移入回收站，可切换永久删除 |
| 容量 | `about.storageQuota` |

支持的整理方式只有 **移动** 和 **复制**；不支持硬链接、软链接和 reflink。

## 限制与注意事项

- Google Drive 是 ID 驱动存储，插件对外暴露虚拟 POSIX 路径；路径解析会缓存目录 ID，写操作后自动清空缓存。
- 一个 MoviePilot 实例只应启用一个 Google Drive 存储插件实例；系统存储类型 `gdrive` 是唯一的。
- 下载器保存目录仍应是本地路径；Google Drive 适合作为媒体库/目标存储。
- 上传中断后不会持久化可恢复上传会话；下次会重新上传文件。目标端已存在的文件不会被插件自动删除。
- Google 原生文档（Docs/Sheets/Slides）没有原始二进制内容，下载时按 PDF/Office 格式导出。
- 共享盘的文件操作需要账号具有相应权限；共享盘容量查询受 API 限制。

## 安全

- client_secret、refresh_token、授权码只保存在 MoviePilot 插件配置中，不会写入日志。
- OAuth 回调是一次性 state，默认 10 分钟过期；回调失败不会修改已有授权。
- 页面只显示“已授权/未授权”和容量，不回显 refresh_token。
- 如需撤销授权，可在 Google 账号的“第三方应用”中移除该应用，并清空插件中的 refresh_token。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| `redirect_uri_mismatch` | 插件回调地址与 Google Cloud 登记值不一致；检查协议、端口、路径和结尾斜杠 |
| `invalid_grant` | refresh_token 失效或被撤销；重新生成授权链接 |
| `403 insufficientPermissions` | 账号没有目标文件夹权限，或共享盘权限不足 |
| `403 rateLimitExceeded` | Google API 限流；插件会退避重试，稍后再试 |
| 测试连接失败但授权成功 | 检查根目录 ID 是否正确、账号是否能访问该文件夹 |
| 目录配置里看不到 Google Drive | 确认插件已启用、`register_storage` 开启，并刷新设置页；实际读写还需要完成授权 |
| 整理提示“不支持的存储类型” | 确认插件处于启用状态且已完成授权；查看详情页日志 |
| 详情页容量为 0 | 无限容量或共享盘容量不由 `about` 提供，属正常现象 |

## 开发验证

```bash
python -m py_compile plugins.v3/gdrivestorage/__init__.py plugins.v3/gdrivestorage/gdrive_client.py
python -m pytest tests/v3/gdrivestorage -q
python -c "import json; json.load(open('package.v3.json', encoding='utf-8'))"
```

有 MoviePilot V3 宿主环境时，还应验证：

- 插件启用、停用、重载无残留资源。
- `/api/v1/storage/options` 出现 `gdrive`。
- 目录配置选择 Google Drive 后，浏览、上传、下载、移动、复制可用。
- 详情页按钮点击后页面自动刷新。
