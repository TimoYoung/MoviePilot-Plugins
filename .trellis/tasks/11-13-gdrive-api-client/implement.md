# Google Drive API 客户端 — 执行计划

## 1. 实现

- [ ] 新建 `plugins.v3/gdrivestorage/`。
- [ ] 写 `gdrive_client.py`：
  - [ ] 常量、异常、RFC3339/路径/大小工具。
  - [ ] `_HttpxTransport`（延迟导入 httpx2，回退 httpx）。
  - [ ] `GoogleDriveClient.__init__` / `close`。
  - [ ] OAuth：`build_auth_url`、`exchange_code`、`refresh_access_token`。
  - [ ] `_request` / `_parse_error` / 重试 / 401 刷新。
  - [ ] 路径：`normalize_virtual_path`、`_relative_parts`、`_escape_query`、`resolve_folder_id`、`_find_child`、`_create_folder`、缓存。
  - [ ] 映射：`_to_file_item`、`_parse_rfc3339`、`_safe_name`。
  - [ ] 读：`list`、`get_item`、`get_item_strict`、`get_folder`、`get_parent`、`detail`。
  - [ ] 写：`create_folder`、`upload`、`download`、`rename`、`move`、`copy`、`delete`。
  - [ ] 管理：`usage`、`check`。
- [ ] 所有公开方法中文 docstring；关键分支注释。

## 2. 测试

- [ ] 新建 `tests/v3/gdrivestorage/test_gdrive_client.py`。
- [ ] 实现 `FakeResponse` / `FakeTransport` / `_FakeStream`。
- [ ] 覆盖设计文档第 10 节 10 类场景。
- [ ] 确保测试不触网、不读环境变量、不依赖宿主。

## 3. 验证

```bash
python -m py_compile plugins.v3/gdrivestorage/gdrive_client.py
python -m pytest tests/v3/gdrivestorage/test_gdrive_client.py -q
```

- [ ] 通过。
- [ ] `grep -n "^import app\|^from app" plugins.v3/gdrivestorage/gdrive_client.py` 无输出。

## 4. 回滚点

- 传输层导入失败：保留注入接口，用 FakeTransport 验证逻辑；宿主侧再确认 `httpx2` 可用。
- 上传协议实现复杂：先支持 `uploadType=media` 小文件路径，再补 resumable；不得省略大文件分片。
- 路径缓存导致测试不稳定：测试中使用 TTL=0 或清空缓存。
