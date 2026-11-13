# Google Drive 文档与发布元数据

## Goal

补齐 Google Drive 插件的使用文档、市场元数据、图标和可在无宿主环境执行的契约测试，保证版本、README 与实现一致。

## Requirements

### R1 README

- 说明 Google Cloud Console 配置步骤：启用 Drive API、OAuth 同意屏幕、创建 Web 应用客户端、登记回调 URI。
- 说明两种授权方式：详情页授权链接 / 手动 refresh_token 或授权码。
- 说明目录配置：系统存储选择 “Google Drive”，设置根目录 ID、虚拟根路径、整理方式（移动/复制）。
- 说明共享盘配置。
- 说明限制：`gdrive:` URI 前缀不被 V3 `StorageSchema` 识别；下载器保存目录仍应为本地；共享盘容量 API 限制；Google 原生文档导出。
- 说明安全：凭据存储位置、不要公开 refresh_token。
- 配置项名称、默认值、枚举与 `get_form()` 完全一致。

### R2 元数据

- `package.v3.json` 增加 `GDriveStorage` 条目：
  - `name`、`description`、`labels`、`version`、`icon`、`author`、`level`、`system_version`、`history`。
- `plugin_version` == `version` == `history` 顶部键。
- 图标文件存在：`icons/gdrive.png`。

### R3 测试

- `tests/v3/gdrivestorage/test_plugin_contract.py`：
  - 读取 `plugins.v3/gdrivestorage/__init__.py` AST；
  - 检查 `get_module` 返回的方法名集合；
  - 检查所有页面 API 方法返回字典字面量的键集合为 `{success, message, data}`；
  - 检查 `package.v3.json` 版本与 `plugin_version` 一致；
  - 检查图标文件存在。
- 测试不导入宿主、不触网。

## Acceptance Criteria

- [ ] `README.md` 存在，包含 R1 全部主题。
- [ ] `python -m pytest tests/v3/gdrivestorage -q` 通过。
- [ ] `python -c "import json; json.load(open('package.v3.json'))"` 通过。
- [ ] 版本三处一致。
- [ ] 图标文件存在且大小 > 0。
- [ ] README 中配置项与 `get_form` 一致（人工核对）。

## Notes

- 版本号与 history 文案在实现完成后回填，避免与代码不一致。
- 图标使用简单云盘图形，不复制 Google 品牌标识。
