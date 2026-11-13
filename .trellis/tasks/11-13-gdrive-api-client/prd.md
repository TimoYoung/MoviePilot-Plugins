# Google Drive API 客户端

## Goal

实现一个不依赖 MoviePilot 宿主的 Google Drive v3 REST 客户端，供 `GDriveStorage` 插件复用；可在无宿主环境中通过注入 FakeTransport 做单元测试。

## Requirements

### R1 认证

- 生成 OAuth2 授权 URL（`access_type=offline`、`prompt=consent`、full drive scope）。
- 用授权码换取 access_token / refresh_token。
- 用 refresh_token 刷新 access_token；缓存到过期前 60 秒。
- 401 时自动刷新并重试一次。
- 错误信息不包含 secret/token。

### R2 路径

- 对外使用虚拟 POSIX 路径；支持 `root_path` 挂载点和 `root_folder_id`。
- 拒绝 `..`、空段、Windows 盘符混用；容错剥离 `gdrive:` 前缀。
- 路径到 Google Drive ID 的解析逐段进行；支持缓存与 TTL。
- 名称查询正确转义单引号和反斜杠。

### R3 读取

- 列举目录（分页取全）。
- 查询文件/目录（普通与严格两种）。
- 递归/逐段获取或创建目录。
- 获取父目录、详情。
- 容量查询。

### R4 写入

- 创建目录。
- 可恢复分片上传（256 KiB 对齐；308 继续；5xx 重试）。
- 流式下载（Google 原生文档走 export）。
- 重命名、移动、复制、删除（回收站/永久）。

### R5 共享盘

- 所有文件请求带 `supportsAllDrives=true`。
- 列举支持 `corpora=drive&driveId=<id>&includeItemsFromAllDrives=true`。
- 根目录为空时允许用共享盘 ID 作为根。

### R6 可测试性

- 构造函数可注入 transport；transport 只需提供 `request(...)` 和 `stream(...)`。
- 客户端模块不导入 `app.*`；`httpx2` 延迟导入。
- 单测不触网、不依赖环境变量。

## Acceptance Criteria

- [ ] `python -m py_compile plugins.v3/gdrivestorage/gdrive_client.py` 通过。
- [ ] 单测覆盖：授权 URL、token 刷新、路径规范化/越界拒绝、list 查询构造、FileItem 映射、上传分片、移动/复制参数、删除模式、错误重试。
- [ ] `python -m pytest tests/v3/gdrivestorage/test_gdrive_client.py -q` 通过。
- [ ] 代码中无 `app.` 导入、无 `os.environ` 读取、无网络请求发生在导入期。
- [ ] 所有公开方法有中文 docstring。

## Notes

- 参考 `research/google-drive-api-v3.md`。
- 客户端只负责 HTTP 与数据映射；存储语义（何时调用、错误如何转成宿主结果）由插件层决定。
