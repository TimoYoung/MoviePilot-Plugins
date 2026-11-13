# Google Drive 文档与发布元数据 — 执行计划

## 1. README

- [ ] 新建 `plugins.v3/gdrivestorage/README.md`。
- [ ] 章节：功能、前置条件、Google Cloud 配置、插件配置、OAuth 授权、目录配置、共享盘、限制、故障排查、安全。
- [ ] 配置项表格与 `get_form` 字段/默认值逐项核对。
- [ ] 不写源码行号、不写内部方法名。

## 2. 图标

- [ ] 用 Pillow 生成 `icons/gdrive.png`（512x512，简单云盘图形，透明/浅色背景）。
- [ ] 检查文件大小 > 0。

## 3. 元数据

- [ ] 在 `package.v3.json` 增加 `GDriveStorage` 条目。
- [ ] 版本与插件类一致。
- [ ] history 顶部为当前版本。
- [ ] `system_version: ">=3.0.0"`、`labels: "网盘"`。

## 4. 契约测试

- [ ] 新建 `tests/v3/gdrivestorage/test_plugin_contract.py`。
- [ ] 用 `ast` 解析 `__init__.py`，检查：
  - [ ] `get_module` 的 `return` 字典键集合。
  - [ ] `get_api` 中声明的端点方法与页面 API 方法名。
  - [ ] 页面 API 方法中 `return {...}` 的键集合恰为三键。
- [ ] 读取 `package.v3.json` 与 `plugin_version`。
- [ ] 检查图标存在。
- [ ] 不导入宿主；纯静态/JSON 测试。

## 5. 验证

```bash
python -m pytest tests/v3/gdrivestorage -q
python -m py_compile plugins.v3/gdrivestorage/__init__.py plugins.v3/gdrivestorage/gdrive_client.py
python -c "import json; json.load(open('package.v3.json', encoding='utf-8'))"
git diff --check
```

## 6. 回滚点

- README 与实现不一致：先修 README，不改实现语义。
- 版本不一致：以插件类为准，同步 package.v3.json。
- 图标生成失败：先用仓库已有 `clouddisk.png` 作为临时图标，再替换。
