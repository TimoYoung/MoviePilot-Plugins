# Google Drive API 客户端 — 技术设计

## 1. 模块结构

```python
# plugins.v3/gdrivestorage/gdrive_client.py

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME = "application/vnd.google-apps.folder"
FILE_FIELDS = "id,name,mimeType,parents,size,modifiedTime,createdTime,trashed,webViewLink,webContentLink,thumbnailLink,md5Checksum,driveId,shortcutDetails"

class GoogleDriveError(Exception): ...
class GoogleDriveAuthError(GoogleDriveError): ...
class GoogleDriveNotFound(GoogleDriveError): ...

class GoogleDriveClient:
    def __init__(..., transport: Optional[Any] = None): ...
```

## 2. 传输层

```python
class _HttpxTransport:
    def __init__(self, timeout):
        try:
            import httpx2 as _httpx
        except ImportError:
            import httpx as _httpx
        self._client = _httpx.Client(timeout=timeout, follow_redirects=True)

    def request(self, method, url, **kwargs):
        return self._client.request(method, url, **kwargs)

    def stream(self, method, url, **kwargs):
        return self._client.stream(method, url, **kwargs)

    def close(self):
        self._client.close()
```

客户端只使用响应的通用属性：`status_code`、`headers`、`json()`、`content`、`iter_bytes()`。测试 FakeResponse 实现这些即可。

## 3. 认证流程

```python
def build_auth_url(self, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": self.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE_DRIVE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"
```

- `exchange_code`：POST `TOKEN_URL`，`data={...}`；解析 `refresh_token`。
- `refresh_access_token`：加锁；检查缓存；POST `grant_type=refresh_token`；写 `_access_token`、`_expires_at`。
- `_request`：
  1. 确保 token；
  2. 发请求；
  3. 401 -> 强制刷新一次 -> 重试；
  4. 429 / 5xx -> 退避重试（最多 3 次，优先 `Retry-After`）；
  5. 其它 4xx -> 解析 Google error -> 抛 `GoogleDriveError`。
- `_parse_error(response)`：读取 `error.message`、`error.errors[0].reason`；不拼接请求头。

## 4. 路径解析

### 4.1 规范化

```python
def normalize_virtual_path(path) -> str:
    raw = str(path or "/").strip()
    if raw.startswith("gdrive:"):
        raw = raw[len("gdrive:"):]
    raw = raw.replace("\\", "/")
    if re.match(r"^[A-Za-z]:", raw):
        raise ValueError("Google Drive 路径不能使用 Windows 盘符")
    parts = []
    for part in PurePosixPath("/" + raw.lstrip("/")).parts:
        if part in ("/", ""):
            continue
        if part == "..":
            raise ValueError("路径不能包含 ..")
        if part == ".":
            continue
        parts.append(part)
    return "/" + "/".join(parts)
```

### 4.2 挂载点

```python
def _relative_parts(self, path) -> list[str]:
    virtual = self.normalize_virtual_path(path)
    root = self.normalize_virtual_path(self.root_path)
    if root != "/":
        if virtual == root:
            return []
        if not virtual.startswith(root + "/"):
            raise ValueError(f"路径不在 Google Drive 挂载点 {root} 下")
        virtual = virtual[len(root):]
    return [p for p in virtual.split("/") if p]
```

### 4.3 解析

```python
def resolve_folder_id(self, path, create=False) -> Optional[str]:
    parts = self._relative_parts(path)
    if not parts:
        return self.root_folder_id
    current_id = self.root_folder_id
    current_path = self.root_path.rstrip("/")
    for part in parts:
        current_path = f"{current_path}/{part}" if current_path else f"/{part}"
        cached = self._folder_cache.get(current_path)
        if cached and cached[1] > time.monotonic():
            current_id = cached[0]
            continue
        found = self._find_child(current_id, part, folder=True)
        if not found:
            if not create:
                return None
            found = self._create_folder(current_id, part)
        current_id = found["id"]
        self._folder_cache[current_path] = (current_id, time.monotonic() + 300)
    return current_id
```

- `_find_child(parent_id, name, folder=None)`：`files.list`，q 为 `name = '<escaped>' and '<parent_id>' in parents and trashed = false`，folder 为真时追加 `mimeType = '<folder>'`。
- 查询字段包含 `nextPageToken`；同名多项时优先目录（目录查询时天然只有目录），否则取第一项。
- 写操作后 `self._folder_cache.clear()`。

## 5. 数据映射

```python
def _to_file_item(self, raw, virtual_path) -> FileItem:
    is_dir = raw.get("mimeType") == FOLDER_MIME
    name = raw.get("name") or ""
    suffix = PurePosixPath(name).suffix
    return FileItem(
        storage="gdrive",
        path=virtual_path.rstrip("/") + ("/" if is_dir else ""),
        name=name,
        basename=name[:-len(suffix)] if suffix else name,
        extension=suffix[1:].lower() if suffix and not is_dir else None,
        type="dir" if is_dir else "file",
        size=int(raw["size"]) if raw.get("size") is not None else None,
        modify_time=_parse_rfc3339(raw.get("modifiedTime")),
        fileid=raw.get("id"),
        parent_fileid=(raw.get("parents") or [None])[0],
        url=raw.get("webViewLink"),
        thumbnail=raw.get("thumbnailLink"),
    )
```

`_parse_rfc3339` 使用 `datetime.fromisoformat(value.replace("Z", "+00:00"))`，返回 `timestamp()`；失败返回 `None`。

## 6. 上传

```python
def upload(self, folder_item, local_path, new_name=None):
    parent_id = folder_item.fileid or self.resolve_folder_id(folder_item.path, create=True)
    size = local_path.stat().st_size
    metadata = {"name": new_name or local_path.name, "parents": [parent_id]}
    init = self._request("POST", f"{DRIVE_UPLOAD_API}/files",
                         params={"uploadType": "resumable", "supportsAllDrives": "true"},
                         json=metadata,
                         headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)})
    location = init.headers.get("Location")
    chunk_size = self._aligned_chunk_size()
    with open(local_path, "rb") as fh:
        offset = 0
        while offset < size:
            chunk = fh.read(chunk_size)
            end = offset + len(chunk) - 1
            resp = self._request("PUT", location,
                                 headers={"Content-Range": f"bytes {offset}-{end}/{size}"},
                                 content=chunk, auth=False, retry=False)
            if resp.status_code in (200, 201):
                return self._to_file_item(resp.json(), join(folder_item.path, metadata["name"]))
            if resp.status_code == 308:
                offset = end + 1
                continue
            raise ...
        # 空文件
        resp = self._request("PUT", location, headers={"Content-Range": f"bytes */0"}, ...)
```

- 空文件：`POST` metadata with `uploadType=media` 或 resumable init + `PUT` `Content-Range: bytes */0`。简化：空文件走 `uploadType=multipart`? Better: resumable init with `X-Upload-Content-Length: 0`, then `PUT` with `Content-Range: bytes */0` and empty content; Google returns 200. Fake test can cover.
- 分片大小：`max(256*1024, (int(mb*1024*1024) // (256*1024)) * (256*1024))`。
- 重试：每个分片独立重试 3 次；`308` 继续；`5xx/429` 退避。

## 7. 下载

```python
def download(self, fileitem, local_dir):
    target = Path(local_dir) / self._safe_name(fileitem.name)
    if self._is_google_doc(fileitem):
        url = f"{DRIVE_API}/files/{fileitem.fileid}/export"
        params = {"mimeType": self._export_mime(fileitem)}
    else:
        url = f"{DRIVE_API}/files/{fileitem.fileid}"
        params = {"alt": "media", "supportsAllDrives": "true"}
    with self._transport.stream("GET", url, headers=self._auth_headers(), params=params) as resp:
        if resp.status_code != 200: raise ...
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as fh:
            for chunk in resp.iter_bytes(1024 * 1024):
                fh.write(chunk)
    return target
```

`_safe_name` 取 `PurePosixPath(name).name`，拒绝空/`.`/`..`。

## 8. 复制/移动/删除

- `copy`：POST `/files/{id}/copy`，body `{name, parents:[target]}`，`supportsAllDrives=true`。
- `move`：先取 `parents`（fileitem.parent_fileid 或 detail），PATCH `/files/{id}?addParents=<target>&removeParents=<old>`，body `{"name": new_name}`（如不同）。
- `rename`：PATCH body `{"name": name}`。
- `delete`：`delete_mode == "permanent"` -> DELETE；否则 PATCH `{"trashed": True}`。204/200 视为成功。

## 9. 容量

```python
def usage(self) -> StorageUsage:
    data = self._request("GET", f"{DRIVE_API}/about", params={"fields": "storageQuota"})
    quota = (data.json() or {}).get("storageQuota") or {}
    limit = int(quota.get("limit") or 0)
    usage = int(quota.get("usage") or 0)
    return StorageUsage(total=float(limit), available=float(max(0, limit - usage)))
```

共享盘/无限容量时 `limit=0`，插件页面显示“未提供/无限”。

## 10. 测试设计

`FakeTransport`：

```python
class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.content = content
    def json(self): return self._payload
    def iter_bytes(self, chunk_size=0):
        yield self.content

class FakeTransport:
    def __init__(self, responses): self.responses = list(responses); self.calls = []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)
    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _FakeStream(self.responses.pop(0))
```

覆盖：

1. `build_auth_url` 包含 `access_type=offline`、`prompt=consent`、scope。
2. `refresh_access_token` 解析 `access_token`；缓存命中不重复请求。
3. 401 -> 刷新 -> 重试成功。
4. `normalize_virtual_path` 剥离 `gdrive:`、拒绝 `..`/盘符。
5. `_relative_parts` 挂载点校验。
6. `list` 构造 q、分页、FileItem 映射。
7. `upload` 初始化 + 分片 + 308 + 完成。
8. `move`/`copy` 参数与 body。
9. `delete` trash/permanent 分支。
10. 429 退避（注入 sleep）。

## 11. 失败边界

- 构造时凭据为空不报错；调用需要认证的方法时抛 `GoogleDriveAuthError`。
- 根目录 ID 为空时使用 `"root"`；共享盘且根为空时使用 `shared_drive_id`。
- 路径不存在：普通查询返回 `None` / 空列表；严格查询返回 `FileItemQueryResult(item=None)`。
- 任何异常都不记录 token/secret 原文。
