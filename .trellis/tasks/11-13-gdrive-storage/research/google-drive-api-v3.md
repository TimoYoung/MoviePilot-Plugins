# Google Drive API v3 对接要点

> 证据来源：Google Drive API v3 Discovery Document（`https://www.googleapis.com/discovery/v1/apis/drive/v3/rest`，2026-11-13 拉取）与 Google Identity OAuth 2.0 Web Server Flow 文档。
> 本文只记录插件实际使用的接口和参数，不复制整份 API 文档。

## 1. OAuth 2.0

### 1.1 授权地址

```text
https://accounts.google.com/o/oauth2/v2/auth
```

必需/建议参数：

| 参数 | 值 |
| --- | --- |
| `client_id` | Google Cloud OAuth 客户端 ID |
| `redirect_uri` | 必须与 Google Cloud 控制台登记值完全一致 |
| `response_type` | `code` |
| `scope` | `https://www.googleapis.com/auth/drive` |
| `access_type` | `offline`（获取 refresh_token） |
| `prompt` | `consent`（每次同意都返回 refresh_token，便于重新授权） |
| `state` | 随机一次性值，回调时校验 |
| `include_granted_scopes` | `true`（可选） |

`redirect_uri` 规则：HTTPS 或 localhost；不能带 fragment；必须与控制台登记值完全匹配。

### 1.2 授权码换令牌

```http
POST https://oauth2.googleapis.com/token
Content-Type: application/x-www-form-urlencoded
```

参数：

```text
code=<授权码>
client_id=<client_id>
client_secret=<client_secret>
redirect_uri=<redirect_uri>
grant_type=authorization_code
```

响应关键字段：

```json
{
  "access_token": "...",
  "expires_in": 3920,
  "token_type": "Bearer",
  "refresh_token": "...",
  "scope": "https://www.googleapis.com/auth/drive"
}
```

### 1.3 刷新访问令牌

```http
POST https://oauth2.googleapis.com/token
Content-Type: application/x-www-form-urlencoded
```

参数：

```text
client_id=<client_id>
client_secret=<client_secret>
refresh_token=<refresh_token>
grant_type=refresh_token
```

失败时返回 `400` / `invalid_grant` 等错误，需要在界面提示重新授权。

## 2. Drive REST 基础

- 基础地址：`https://www.googleapis.com/drive/v3/`
- 上传地址：`https://www.googleapis.com/upload/drive/v3/`
- 认证头：`Authorization: Bearer <access_token>`
- 共享盘参数：`supportsAllDrives=true`；列举时再加 `includeItemsFromAllDrives=true`，并使用 `corpora=drive&driveId=<shared_drive_id>`。
- 文件字段建议：`id,name,mimeType,parents,size,modifiedTime,createdTime,trashed,webViewLink,webContentLink,thumbnailLink,md5Checksum,driveId,shortcutDetails`。
- 文件夹 MIME：`application/vnd.google-apps.folder`。
- Google 原生文档 MIME 前缀：`application/vnd.google-apps.`。

### 2.1 列举

```http
GET /drive/v3/files?q=<query>&fields=nextPageToken,files(...)&pageSize=1000&pageToken=...
```

`q` 示例：

```text
'<folderId>' in parents and trashed = false
name = 'Movies' and '<parentId>' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false
```

名称里的单引号和反斜杠需要转义。

### 2.2 取元数据

```http
GET /drive/v3/files/{fileId}?fields=...
```

### 2.3 创建文件夹

```http
POST /drive/v3/files
Content-Type: application/json

{"name":"Movies","mimeType":"application/vnd.google-apps.folder","parents":["<parentId>"]}
```

### 2.4 重命名 / 移动

```http
PATCH /drive/v3/files/{fileId}
Content-Type: application/json

{"name":"new-name"}

PATCH /drive/v3/files/{fileId}?addParents=<newParent>&removeParents=<oldParent>
```

### 2.5 复制

```http
POST /drive/v3/files/{fileId}/copy
Content-Type: application/json

{"name":"copy-name","parents":["<targetFolder>"]}
```

### 2.6 删除

- 永久删除：`DELETE /drive/v3/files/{fileId}?supportsAllDrives=true`（204）。
- 移入回收站：`PATCH /drive/v3/files/{fileId}`，body `{"trashed":true}`。

回收站文件仍占配额；插件默认“移入回收站”，可配置永久删除。

## 3. 上传（可恢复上传）

### 3.1 初始化

```http
POST https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&supportsAllDrives=true
Authorization: Bearer ...
Content-Type: application/json; charset=UTF-8
X-Upload-Content-Type: <file mime>
X-Upload-Content-Length: <total bytes>

{"name":"file.mkv","parents":["<folderId>"]}
```

成功响应（通常 200）在 `Location` 头返回 session URI。

### 3.2 分片上传

```http
PUT <session URI>
Content-Range: bytes <start>-<end>/<total>
Content-Length: <chunk bytes>
```

- 完成时返回 `200` / `201`，body 是 File 资源。
- 未完成时返回 `308 Resume Incomplete`，可能带 `Range: bytes=0-<last>`。
- 分片大小必须是 256 KiB 的倍数（最后一片除外）。
- `5xx` / `429` 可重试当前分片；`401` 刷新令牌后重试。

## 4. 下载

```http
GET /drive/v3/files/{fileId}?alt=media&supportsAllDrives=true
Authorization: Bearer ...
```

流式写入本地文件。Google 原生文档不支持 `alt=media`，需要走 `files.export`：

```http
GET /drive/v3/files/{fileId}/export?mimeType=application/pdf
```

## 5. 容量

```http
GET /drive/v3/about?fields=storageQuota
```

响应：

```json
{"storageQuota":{"limit":"...","usage":"...","usageInDrive":"...","usageInDriveTrash":"..."}}
```

- `limit` 可能缺失（无限容量）。
- 共享盘容量不由 `about` 返回；共享盘由组织管理员管理，插件显示“未提供”。

## 6. 错误格式

```json
{
  "error": {
    "code": 403,
    "message": "...",
    "errors": [{"reason": "rateLimitExceeded", "message": "..."}]
  }
}
```

处理策略：

- `401`：刷新 access_token，重试一次。
- `403` + `rateLimitExceeded` / `userRateLimitExceeded`：指数退避重试。
- `404`：路径/文件不存在；严格查询返回“确认不存在”。
- `429` / `5xx`：指数退避重试。
- `invalid_grant`：refresh_token 失效，提示重新授权。

## 7. 官方文档

- Drive API v3 参考：https://developers.google.com/workspace/drive/api/reference/rest/v3
- 文件搜索：https://developers.google.com/workspace/drive/api/guides/search-files
- 上传：https://developers.google.com/workspace/drive/api/guides/manage-uploads
- 下载：https://developers.google.com/workspace/drive/api/guides/manage-downloads
- OAuth Web Server：https://developers.google.com/identity/protocols/oauth2/web-server
- Scope：https://developers.google.com/workspace/drive/api/guides/api-specific-auth
