# 插件界面、目录与发布约定

> 参考：本仓 `plugins.v3/u115autoupload/__init__.py`，以及上游 `MoviePilot-Plugins` 的 `AGENTS.md`、`docs/Plugin_Development.md`、`docs/Repository_Guide.md`。

## 1. 目录与命名

- 插件 ID：`GDriveStorage`。
- 目录：`plugins.v3/gdrivestorage/`（类名全小写）。
- 主类：`plugins.v3/gdrivestorage/__init__.py` 中的 `GDriveStorage`。
- 市场元数据：`package.v3.json` 的 `GDriveStorage` 条目。
- 图标：`icons/gdrive.png`。
- 测试：`tests/v3/gdrivestorage/`，禁止放进插件源码目录。
- 额外依赖：本插件不需要，不创建 `pyproject.toml`。

## 2. 版本同步

- `GDriveStorage.plugin_version`。
- `package.v3.json` 的 `version`。
- `history` 顶部当前版本条目。

三者必须一致；每个用户可见版本在 `history` 增加一条。

## 3. 配置表单

`get_form()` 返回 `(page_json, default_config)`：

- 使用 `VForm` 包裹，`VRow` / `VCol` 布局。
- `VTextField` 的 `props.model` 对应配置键。
- 密码类字段用 `"type": "password"`。
- 条件显示用 `props.show` 表达式（如需）。
- 默认值必须与插件实际默认值一致。

参考 u115 插件：`VAlert` 说明、`VSwitch`、`VTextField`、`VSelect`（`items: [{title, value}]`）。

## 4. 详情页

`get_page()` 返回 Vuetify JSON 列表：

- 根容器使用固定高度 flex 列，例如 `height: 72vh; min-height: 520px; max-height: 800px; padding-bottom: 84px;`。
- `VAlert` 必须加 `style: "flex: 0 0 auto;"`，否则会被 Vuetify 的 `flex: 1 1` 压扁。
- 表格使用 `VTable`，外层 `div` 设置 `min-height: 0; overflow: auto;`。
- 页面按钮通过 `events.click` 调用插件 API：
  ```python
  {"component": "VBtn", "text": "刷新", "events": {"click": {"api": "plugin/GDriveStorage/status", "method": "post", "params": {}}}}
  ```
- 需要动态链接（OAuth 授权）时可用 `VBtn` 的 `href` prop。

## 5. API envelope（强制）

页面按钮 API 必须恰好返回三键：

```python
{"success": True, "message": "...", "data": {...}}
```

- 失败：`{"success": False, "message": "可读错误", "data": None}`。
- 异常在端点内部捕获，不要向页面抛 500。
- 成功才会触发页面重新加载。

## 6. 生命周期

- `init_plugin` 必须可重复调用：先 `stop_service()`，再重建状态。
- `stop_service` 关闭 HTTP 客户端、清空缓存、取消一次性任务。
- 不在模块导入期或类定义期发网络请求、连数据库、启动线程。
- 不在源码目录写运行数据；使用 `get_data_path()`。

## 7. 安全

- 不在日志中输出 `client_secret`、`refresh_token`、授权码。
- OAuth 回调设置 `allow_anonymous: True`，必须校验一次性 `state` 和有效期。
- 页面只显示“已授权/未授权”，不回显 refresh_token。
- 路径解析拒绝 `..` 越界和 Windows 盘符混用。

## 8. 可离线执行的验证

本仓没有 MoviePilot 宿主环境时，至少执行：

```bash
python -m py_compile plugins.v3/gdrivestorage/__init__.py plugins.v3/gdrivestorage/gdrive_client.py
python -m pytest tests/v3/gdrivestorage -q
python -c "import json; json.load(open('package.v3.json'))"
git diff --check
```

有宿主环境时再验证：

- 插件市场发现、安装、启用/停用/重载。
- `/api/v1/storage/options` 出现 `gdrive`。
- 目录配置选择 Google Drive 后，浏览、上传、下载、移动、复制可用。
- 详情页按钮点击后页面自动刷新。
