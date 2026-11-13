# Google Drive 存储扩展插件 — 执行计划

## 0. 前置

- [x] 核对 MoviePilot V3 宿主存储契约（`research/moviepilot-v3-storage-contract.md`）。
- [x] 核对 Google Drive API v3 / OAuth2（`research/google-drive-api-v3.md`）。
- [x] 核对插件目录、版本、界面、发布约定（`research/plugin-ui-and-release.md`）。
- [x] 创建父任务与三个子任务，写入 prd/design/implement/jsonl。

## 1. 子任务 `11-13-gdrive-api-client`

- [ ] 创建 `plugins.v3/gdrivestorage/` 目录。
- [ ] 实现 `gdrive_client.py`：
  - [ ] `_HttpxTransport` 延迟导入 `httpx2`，支持注入 `transport`。
  - [ ] OAuth：`build_auth_url`、`exchange_code`、`refresh_access_token`、401 重试。
  - [ ] 路径：`normalize_virtual_path`、`strip_root`、`resolve_folder_id`、名称转义、缓存/TTL。
  - [ ] 元数据：`_to_file_item`、RFC3339 时间转换、目录路径尾斜杠。
  - [ ] 读：`list`、`get_item`、`get_item_strict`、`get_folder`、`detail`。
  - [ ] 写：`create_folder`、`upload`（可恢复分片）、`download`（流式）、`rename`、`move`、`copy`、`delete`。
  - [ ] 管理：`usage`、`check`、`build_auth_url`。
  - [ ] 错误：`GoogleDriveError` / `GoogleDriveAuthError`、429/5xx 退避。
- [ ] 创建 `tests/v3/gdrivestorage/test_gdrive_client.py`，使用 FakeTransport 覆盖关键路径。
- [ ] 验证：`python -m py_compile plugins.v3/gdrivestorage/gdrive_client.py`、`python -m pytest tests/v3/gdrivestorage -q`。

## 2. 子任务 `11-13-gdrive-storage-integration`

- [ ] 实现 `__init__.py` 插件类与配置模型。
- [ ] `init_plugin` / `stop_service` / `get_state` 生命周期可重复执行。
- [ ] `StorageHelper` 注册/更新 `gdrive` 存储。
- [ ] `@eventmanager.register(ChainEventType.StorageOperSelection)` 返回提供者。
- [ ] `get_module()` 声明全部存储方法；非 gdrive 返回 `None`。
- [ ] `GDriveStorageProvider` 实现整理链所需的鸭子类型方法。
- [ ] `get_form()` 配置页字段与默认值。
- [ ] `get_page()` 状态/容量/日志/按钮；`VAlert` 内联 `flex: 0 0 auto;`。
- [ ] `get_api()` 页面 API + 匿名 OAuth 回调 + 三键 envelope。
- [ ] OAuth state 持久化、过期、校验；授权码交换写回 refresh_token。
- [ ] 日志环形缓冲（最多 100 条），`save_data("logs")`；分页/清空 API。
- [ ] 验证：`py_compile`、AST envelope 检查、`get_module` 方法集合断言。

## 3. 子任务 `11-13-gdrive-docs-release`

- [ ] 编写 `plugins.v3/gdrivestorage/README.md`，与 `get_form` 字段/默认值一致。
- [ ] 生成 `icons/gdrive.png`（简单云盘图标，不复制品牌标识）。
- [ ] 更新 `package.v3.json`：`GDriveStorage` 条目、版本、history、system_version、labels。
- [ ] 增加 `tests/v3/gdrivestorage/test_plugin_contract.py`：
  - [ ] `get_api` 端点返回三键 envelope（AST）。
  - [ ] `get_module` 声明必需方法（AST）。
  - [ ] `package.v3.json` 版本与 `plugin_version` 一致。
  - [ ] 图标文件存在。
- [ ] 验证：`pytest tests/v3/gdrivestorage -q`、`json.load(package.v3.json)`、`git diff --check`。

## 4. 集成检查

- [ ] `python -m compileall plugins.v3/gdrivestorage`。
- [ ] `python -m pytest tests/v3/gdrivestorage -q`。
- [ ] 静态检查：所有页面 API 返回三键；`get_module` 方法齐全；非 gdrive 分支返回 `None`。
- [ ] README 字段、默认值、枚举与 `get_form` 一致。
- [ ] `package.v3.json` 版本/history/图标一致。
- [ ] 检查没有导入 `app.db.models.*`、`SessionFactory`、`app.core.*`、`app.helper.*`、`app.utils.*`。
- [ ] 真实宿主回归（有环境时）：存储出现在 `/storage/options`；目录配置选择 Google Drive；浏览/上传/下载/移动/复制；页面按钮刷新。

## 5. 回滚点

- 客户端单测失败：只回退 `gdrive_client.py`，不影响插件外壳。
- 宿主导入失败：先移除 `__init__.py` 中的新导入，保留客户端；用 `py_compile` 定位。
- 存储注册导致宿主配置异常：将 `register_storage` 默认改为 `False`，让用户手动配置；保留事件/模块能力。
- OAuth 回调异常：禁用匿名回调 API，保留手动授权码/refresh_token 模式。

## 6. 验证命令汇总

```bash
python -m py_compile plugins.v3/gdrivestorage/__init__.py plugins.v3/gdrivestorage/gdrive_client.py
python -m pytest tests/v3/gdrivestorage -q
python - <<'PY'
import json
json.load(open('package.v3.json', encoding='utf-8'))
print('package.v3.json OK')
PY
git diff --check
```
