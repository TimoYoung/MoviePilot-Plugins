"""Google Drive v3 REST 客户端。

本模块只负责 Google Drive API 的 HTTP 交互、OAuth2 令牌刷新、路径解析和
数据映射，不导入 MoviePilot 宿主模块，便于在无宿主环境下用注入的传输层做
单元测试。宿主相关的 ``FileItem`` 转换由插件层完成。
"""

from __future__ import annotations

import mimetypes
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Tuple
from urllib.parse import urlencode

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DOC_PREFIX = "application/vnd.google-apps."

FILE_FIELDS = (
    "id,name,mimeType,parents,size,modifiedTime,createdTime,trashed,"
    "webViewLink,webContentLink,thumbnailLink,md5Checksum,driveId,shortcutDetails"
)

# Google 官方要求可恢复上传分片为 256 KiB 的倍数
UPLOAD_ALIGN = 256 * 1024
MAX_RETRY = 3
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

_DRIVE_ID_IN_URL = re.compile(r"/folders/([A-Za-z0-9_-]+)")
_DRIVE_ID_IN_QUERY = re.compile(r"[?&]id=([A-Za-z0-9_-]+)")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class GoogleDriveError(Exception):
    """Google Drive API 或网络错误。"""

    def __init__(
            self,
            message: str,
            *,
            status_code: Optional[int] = None,
            reason: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.reason = reason

    def __str__(self) -> str:
        """返回不包含任何凭据的可读错误。"""
        if self.status_code is None:
            return self.message
        return f"HTTP {self.status_code}: {self.message}"


class GoogleDriveAuthError(GoogleDriveError):
    """OAuth 凭据缺失、失效或刷新失败。"""


class GoogleDriveNotFound(GoogleDriveError):
    """目标文件或目录不存在。"""


@dataclass(slots=True)
class DriveItem:
    """Google Drive 文件/目录的中立数据模型。"""

    id: str
    name: str
    mime_type: str
    parents: List[str] = field(default_factory=list)
    size: Optional[int] = None
    modified_time: Optional[float] = None
    created_time: Optional[float] = None
    trashed: bool = False
    web_view_link: Optional[str] = None
    thumbnail_link: Optional[str] = None
    drive_id: Optional[str] = None
    path: str = "/"

    @property
    def is_dir(self) -> bool:
        """是否为 Google Drive 文件夹。"""
        return self.mime_type == FOLDER_MIME

    @property
    def is_google_doc(self) -> bool:
        """是否为 Google 原生文档（需要 export 下载）。"""
        return self.mime_type.startswith(GOOGLE_DOC_PREFIX) and not self.is_dir

    @property
    def parent_id(self) -> Optional[str]:
        """返回第一个父目录标识。"""
        return self.parents[0] if self.parents else None


@dataclass(slots=True)
class DriveQuota:
    """Google Drive 容量信息，单位字节。"""

    total: int = 0
    available: int = 0
    usage: int = 0


class HttpTransport(Protocol):
    """客户端依赖的最小传输层协议。"""

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """发送一次普通请求并返回响应对象。"""

    def stream(self, method: str, url: str, **kwargs: Any) -> Any:
        """打开流式响应上下文。"""


class _HttpxTransport:
    """基于 httpx2（回退 httpx）的同步传输层。"""

    def __init__(self, timeout: float) -> None:
        """
        创建同步 HTTP 客户端。

        :param timeout: 单次请求超时时间（秒）
        """
        try:
            import httpx2 as _httpx
        except ImportError:  # pragma: no cover - 宿主已提供 httpx2
            import httpx as _httpx  # type: ignore[no-redef]
        self._client = _httpx.Client(timeout=timeout, follow_redirects=True)

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """发送普通请求。"""
        return self._client.request(method, url, **kwargs)

    def stream(self, method: str, url: str, **kwargs: Any) -> Any:
        """打开流式响应。"""
        return self._client.stream(method, url, **kwargs)

    def close(self) -> None:
        """关闭底层连接池。"""
        self._client.close()


def parse_drive_id(value: Optional[str]) -> str:
    """
    从文件夹 URL、``id=`` 查询串或裸 ID 中提取 Drive 标识。

    :param value: 用户填写的文件夹 ID 或 URL
    :return: 可传给 Drive API 的标识；空值返回空字符串
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    match = _DRIVE_ID_IN_URL.search(raw) or _DRIVE_ID_IN_QUERY.search(raw)
    if match:
        return match.group(1)
    return raw


def parse_rfc3339(value: Optional[str]) -> Optional[float]:
    """
    把 Google 的 RFC3339 时间转换为 Unix 秒。

    :param value: 例如 ``2024-01-01T00:00:00.000Z``
    :return: Unix 时间戳；空值或格式错误返回 None
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def safe_file_name(name: Optional[str]) -> Optional[str]:
    """
    提取可安全落盘的 basename，拒绝目录穿越片段。

    :param name: 远端文件名
    :return: 安全的文件名；无效时返回 None
    """
    if not name:
        return None
    candidate = PurePosixPath(str(name).replace("\\", "/")).name
    if candidate in ("", ".", ".."):
        return None
    return candidate


def format_size(size: int) -> str:
    """把字节数格式化为易读文本。"""
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}PB"


class GoogleDriveClient:
    """Google Drive v3 同步客户端。"""

    def __init__(
            self,
            *,
            client_id: str,
            client_secret: str,
            refresh_token: str,
            root_folder_id: str = "root",
            root_path: str = "/",
            shared_drive_id: str = "",
            use_shared_drive: bool = False,
            delete_mode: str = "trash",
            chunk_size: int = 8 * 1024 * 1024,
            timeout: float = 60,
            transport: Optional[HttpTransport] = None,
            cache_ttl: float = 300,
            sleep: Any = time.sleep,
    ) -> None:
        """
        保存配置并准备认证/路径缓存状态。

        :param client_id: Google OAuth 客户端 ID
        :param client_secret: Google OAuth 客户端密钥
        :param refresh_token: 离线授权得到的 refresh_token
        :param root_folder_id: 挂载的 Drive 文件夹 ID；为空时使用 root
        :param root_path: 对外虚拟挂载路径，例如 ``/`` 或 ``/GDrive``
        :param shared_drive_id: 共享盘 ID
        :param use_shared_drive: 是否按共享盘列举
        :param delete_mode: ``trash`` 或 ``permanent``
        :param chunk_size: 可恢复上传分片大小（字节）
        :param timeout: HTTP 超时（秒）
        :param transport: 可注入传输层；为空时使用 httpx2
        :param cache_ttl: 路径到 ID 的缓存秒数
        :param sleep: 重试等待函数，测试可注入
        """
        self.client_id = str(client_id or "").strip()
        self.client_secret = str(client_secret or "").strip()
        self.refresh_token = str(refresh_token or "").strip()
        self.root_folder_id = parse_drive_id(root_folder_id) or "root"
        self.root_path = self.normalize_virtual_path(root_path)
        self.shared_drive_id = parse_drive_id(shared_drive_id)
        self.use_shared_drive = bool(use_shared_drive)
        self.delete_mode = "permanent" if delete_mode == "permanent" else "trash"
        self.chunk_size = self._align_chunk_size(chunk_size)
        self.timeout = max(5.0, float(timeout or 60))
        self._transport: HttpTransport = transport or _HttpxTransport(self.timeout)
        self._owns_transport = transport is None
        self._cache_ttl = max(0.0, float(cache_ttl))
        self._sleep = sleep

        self._token_lock = threading.RLock()
        self._access_token: Optional[str] = None
        self._expires_at = 0.0
        self._folder_cache: Dict[str, Tuple[str, float]] = {}
        self._cache_lock = threading.RLock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭自有传输层；注入的传输层由调用方负责。"""
        if self._owns_transport:
            close = getattr(self._transport, "close", None)
            if callable(close):
                close()

    def clear_cache(self) -> None:
        """清空路径到 ID 的缓存。"""
        with self._cache_lock:
            self._folder_cache.clear()

    # ------------------------------------------------------------------
    # OAuth2
    # ------------------------------------------------------------------
    def build_auth_url(self, redirect_uri: str, state: str) -> str:
        """
        生成 Google OAuth2 授权地址。

        :param redirect_uri: Google Cloud 控制台登记的完整回调地址
        :param state: 一次性校验值
        :return: 可直接在浏览器打开的授权 URL
        """
        if not self.client_id:
            raise GoogleDriveAuthError("尚未配置 Google OAuth client_id")
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

    def exchange_code(self, code: str, redirect_uri: str) -> Dict[str, Any]:
        """
        用授权码换取 access_token / refresh_token。

        :param code: Google 回调携带的授权码
        :param redirect_uri: 与授权请求一致的 redirect_uri
        :return: token 端点的 JSON 响应
        """
        if not code:
            raise GoogleDriveAuthError("授权码为空")
        if not self.client_id or not self.client_secret:
            raise GoogleDriveAuthError("尚未配置 Google OAuth client_id/client_secret")
        payload = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        try:
            response = self._request(
                "POST",
                TOKEN_URL,
                data=payload,
                auth=False,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                retry=True,
            )
        except GoogleDriveError as err:
            raise GoogleDriveAuthError(f"授权码换取令牌失败：{err}") from err
        data = self._json(response)
        access_token = data.get("access_token")
        if not access_token:
            raise GoogleDriveAuthError("Google 未返回 access_token")
        self._set_token(access_token, data.get("expires_in"))
        return data

    def refresh_access_token(self, force: bool = False) -> str:
        """
        刷新并缓存 access_token。

        :param force: 忽略缓存强制刷新
        :return: 可用的 access_token
        """
        with self._token_lock:
            now = time.monotonic()
            if not force and self._access_token and now < self._expires_at:
                return self._access_token
            if not self.refresh_token:
                raise GoogleDriveAuthError("尚未配置 refresh_token，请先完成 Google 授权")
            if not self.client_id or not self.client_secret:
                raise GoogleDriveAuthError("尚未配置 Google OAuth client_id/client_secret")
            payload = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            }
            try:
                response = self._request(
                    "POST",
                    TOKEN_URL,
                    data=payload,
                    auth=False,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    retry=True,
                )
            except GoogleDriveError as err:
                raise GoogleDriveAuthError(
                    f"刷新 access_token 失败，refresh_token 可能已失效：{err}"
                ) from err
            data = self._json(response)
            access_token = data.get("access_token")
            if not access_token:
                raise GoogleDriveAuthError("Google 未返回 access_token，refresh_token 可能已失效")
            self._set_token(access_token, data.get("expires_in"))
            return access_token

    def _set_token(self, access_token: str, expires_in: Any) -> None:
        """缓存 access_token 与安全余量后的过期时间。"""
        try:
            expires = max(60, int(expires_in or 3600))
        except (TypeError, ValueError):
            expires = 3600
        self._access_token = access_token
        self._expires_at = time.monotonic() + max(30, expires - 60)

    def _auth_headers(self) -> Dict[str, str]:
        """返回 Bearer 认证头。"""
        return {"Authorization": f"Bearer {self.refresh_access_token()}"}

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    @staticmethod
    def normalize_virtual_path(path: Any) -> str:
        """
        规范化虚拟 POSIX 路径，剥离 ``gdrive:`` 前缀并拒绝越界。

        :param path: 任意路径输入
        :return: 以 ``/`` 开头的规范路径，根路径为 ``/``
        """
        raw = str(path or "/").strip()
        if raw.startswith("gdrive:"):
            raw = raw[len("gdrive:"):]
        raw = raw.replace("\\", "/")
        if _WINDOWS_DRIVE.match(raw):
            raise ValueError("Google Drive 路径不能使用 Windows 盘符")
        parts: List[str] = []
        for part in PurePosixPath("/" + raw.lstrip("/")).parts:
            if part in ("/", "", "."):
                continue
            if part == "..":
                raise ValueError("Google Drive 路径不能包含 ..")
            parts.append(part)
        return "/" + "/".join(parts)

    def _relative_parts(self, path: Any) -> List[str]:
        """
        把对外路径转换为挂载点下的相对段。

        :param path: 对外虚拟路径
        :return: 相对段列表；挂载点根返回空列表
        :raises ValueError: 路径不在挂载点下
        """
        virtual = self.normalize_virtual_path(path)
        root = self.normalize_virtual_path(self.root_path)
        if root != "/":
            # 允许宿主从 "/" 开始浏览；它映射到配置的挂载点根目录
            if virtual in ("/", root):
                return []
            if not virtual.startswith(root + "/"):
                raise ValueError(f"路径不在 Google Drive 挂载点 {root} 下")
            virtual = virtual[len(root):]
        return [part for part in virtual.split("/") if part]

    def _join_path(self, parent: str, name: str) -> str:
        """拼接对外虚拟路径。"""
        base = self.normalize_virtual_path(parent).rstrip("/")
        return f"{base}/{name}" if base else f"/{name}"

    def _effective_virtual_path(self, path: Any) -> str:
        """把宿主传入的 "/" 映射到配置的挂载点根路径。"""
        virtual = self.normalize_virtual_path(path)
        root = self.normalize_virtual_path(self.root_path)
        if virtual == "/" and root != "/":
            return root
        return virtual

    def _cache_key(self, parts: Iterable[str]) -> str:
        """生成稳定的路径缓存键。"""
        root = self.normalize_virtual_path(self.root_path).rstrip("/")
        suffix = "/".join(parts)
        if not suffix:
            return root or "/"
        return f"{root}/{suffix}" if root else f"/{suffix}"

    def _cache_get(self, key: str) -> Optional[str]:
        """读取未过期的路径缓存。"""
        with self._cache_lock:
            cached = self._folder_cache.get(key)
            if not cached:
                return None
            value, expires_at = cached
            if expires_at and expires_at < time.monotonic():
                self._folder_cache.pop(key, None)
                return None
            return value

    def _cache_put(self, key: str, folder_id: str) -> None:
        """写入路径缓存。"""
        if self._cache_ttl <= 0:
            return
        with self._cache_lock:
            self._folder_cache[key] = (folder_id, time.monotonic() + self._cache_ttl)

    def _root_id(self) -> str:
        """返回实际可用的根目录 ID。"""
        if self.root_folder_id and self.root_folder_id != "root":
            return self.root_folder_id
        if self.use_shared_drive and self.shared_drive_id:
            return self.shared_drive_id
        return self.root_folder_id or "root"

    def resolve_folder_id(self, path: Any, create: bool = False) -> Optional[str]:
        """
        解析目录路径为 Drive 文件夹 ID。

        :param path: 对外虚拟路径
        :param create: 缺失时是否逐段创建
        :return: 文件夹 ID；不存在且不创建时返回 None
        """
        parts = self._relative_parts(path)
        if not parts:
            return self._root_id()
        current_id = self._root_id()
        built: List[str] = []
        for part in parts:
            built.append(part)
            key = self._cache_key(built)
            cached = self._cache_get(key)
            if cached:
                current_id = cached
                continue
            found = self._find_child(current_id, part, folder=True)
            if not found:
                if not create:
                    return None
                found = self._create_folder_raw(current_id, part)
            current_id = found.id
            self._cache_put(key, current_id)
        return current_id

    def get_item(self, path: Any) -> Optional[DriveItem]:
        """
        按虚拟路径查询文件或目录。

        :param path: 对外虚拟路径
        :return: 命中的文件/目录；不存在返回 None
        """
        try:
            parts = self._relative_parts(path)
            if not parts:
                return self.get_item_by_id(self._root_id(), self.root_path)
            virtual = self.normalize_virtual_path(path)
            parent_virtual = "/" + "/".join(virtual.strip("/").split("/")[:-1])
            parent_id = self.resolve_folder_id(parent_virtual)
            if not parent_id:
                return None
            name = parts[-1]
            found = self._find_child(parent_id, name)
            if not found:
                return None
            found.path = self.normalize_virtual_path(path)
            return found
        except GoogleDriveNotFound:
            # 父目录已被删除或不可见时按“不存在”处理
            return None

    def get_item_by_id(self, file_id: str, virtual_path: Optional[str] = None) -> Optional[DriveItem]:
        """
        按文件 ID 查询元数据。

        :param file_id: Drive 文件/目录 ID
        :param virtual_path: 对外路径；为空时使用挂载点根
        :return: 文件/目录；已删除或不存在返回 None
        """
        if not file_id:
            return None
        try:
            response = self._request(
                "GET",
                f"{DRIVE_API}/files/{file_id}",
                params={"fields": FILE_FIELDS, "supportsAllDrives": "true"},
            )
        except GoogleDriveNotFound:
            return None
        item = self._to_drive_item(self._json(response), virtual_path or self.root_path)
        if item.trashed:
            return None
        return item

    def _find_child(
            self,
            parent_id: str,
            name: str,
            *,
            folder: Optional[bool] = None,
    ) -> Optional[DriveItem]:
        """
        在指定父目录下按名称查找子项。

        :param parent_id: 父目录 ID
        :param name: 子项名称
        :param folder: True 只找目录，False 只找文件，None 不限
        :return: 命中的子项；不存在返回 None
        """
        query = f"name = '{self._escape_query(name)}' and '{self._escape_query(parent_id)}' in parents and trashed = false"
        if folder is True:
            query += f" and mimeType = '{FOLDER_MIME}'"
        elif folder is False:
            query += f" and mimeType != '{FOLDER_MIME}'"
        items = self._list_query(query, parent_path=self.root_path)
        if not items:
            return None
        # 同名项可能存在；目录查询优先目录，普通查询优先非目录
        if folder is None:
            for item in items:
                if not item.is_dir:
                    return item
        return items[0]

    def _list_query(self, query: str, parent_path: str) -> List[DriveItem]:
        """
        执行一次 files.list 查询并取全分页。

        :param query: Drive 查询表达式
        :param parent_path: 子项虚拟路径的父路径
        :return: 转换后的条目列表
        """
        items: List[DriveItem] = []
        page_token: Optional[str] = None
        while True:
            params: Dict[str, Any] = {
                "q": query,
                "fields": f"nextPageToken,files({FILE_FIELDS})",
                "pageSize": 1000,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if self.use_shared_drive and self.shared_drive_id:
                params["corpora"] = "drive"
                params["driveId"] = self.shared_drive_id
            if page_token:
                params["pageToken"] = page_token
            response = self._request("GET", f"{DRIVE_API}/files", params=params)
            data = self._json(response)
            for raw in data.get("files") or []:
                if raw.get("trashed"):
                    continue
                items.append(self._to_drive_item(raw, self._join_path(parent_path, raw.get("name") or "")))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return items

    @staticmethod
    def _escape_query(value: str) -> str:
        """转义 Drive 查询字符串中的反斜杠和单引号。"""
        return str(value).replace("\\", "\\\\").replace("'", "\\'")

    # ------------------------------------------------------------------
    # 读操作
    # ------------------------------------------------------------------
    def list(self, folder_id: str, folder_path: str) -> List[DriveItem]:
        """
        列举目录直接子项。

        :param folder_id: 目录 ID
        :param folder_path: 目录对外路径
        :return: 子项列表（目录和文件）
        """
        query = f"'{self._escape_query(folder_id)}' in parents and trashed = false"
        # 宿主从 "/" 浏览时，返回路径必须落在配置的挂载点下
        return self._list_query(query, self._effective_virtual_path(folder_path))

    def get_folder_id(self, path: Any, create: bool = True) -> Optional[str]:
        """解析目录 ID；``create=True`` 时缺失则创建。"""
        return self.resolve_folder_id(path, create=create)

    def create_folder(self, parent_id: str, parent_path: str, name: str) -> Optional[DriveItem]:
        """
        在父目录下创建文件夹，同名已存在时返回已有项。

        :param parent_id: 父目录 ID
        :param parent_path: 父目录对外路径
        :param name: 新目录名
        :return: 目录项；失败返回 None
        """
        parent_path = self._effective_virtual_path(parent_path)
        existing = self._find_child(parent_id, name, folder=True)
        if existing:
            existing.path = self._directory_path(parent_path, existing.name)
            return existing
        created = self._create_folder_raw(parent_id, name)
        created.path = self._directory_path(parent_path, created.name)
        self.clear_cache()
        return created

    def _directory_path(self, parent_path: str, name: str) -> str:
        """拼接目录的对外路径并保留结尾斜杠。"""
        path = self._join_path(parent_path, name)
        return path if path.endswith("/") else path + "/"

    def _create_folder_raw(self, parent_id: str, name: str) -> DriveItem:
        """直接调用 files.create 创建文件夹。"""
        response = self._request(
            "POST",
            f"{DRIVE_API}/files",
            params={"supportsAllDrives": "true", "fields": FILE_FIELDS},
            json={
                "name": name,
                "mimeType": FOLDER_MIME,
                "parents": [parent_id],
            },
        )
        return self._to_drive_item(self._json(response), self._join_path(self.root_path, name))

    def detail(self, file_id: str, virtual_path: Optional[str] = None) -> Optional[DriveItem]:
        """按 ID 重新读取详情。"""
        return self.get_item_by_id(file_id, virtual_path)

    # ------------------------------------------------------------------
    # 写操作
    # ------------------------------------------------------------------
    def rename(self, file_id: str, name: str) -> bool:
        """
        重命名文件或目录。

        :param file_id: 目标 ID
        :param name: 新名称
        :return: 是否成功
        """
        if not file_id or not name:
            return False
        self._request(
            "PATCH",
            f"{DRIVE_API}/files/{file_id}",
            params={"supportsAllDrives": "true", "fields": "id,name"},
            json={"name": name},
        )
        self.clear_cache()
        return True

    def move(
            self,
            file_id: str,
            old_parent_id: str,
            target_parent_id: str,
            new_name: Optional[str] = None,
    ) -> bool:
        """
        移动文件到另一个目录，可同时改名。

        :param file_id: 源文件 ID
        :param old_parent_id: 旧父目录 ID
        :param target_parent_id: 目标目录 ID
        :param new_name: 可选的新名称
        :return: 是否成功
        """
        if not file_id or not target_parent_id:
            return False
        params: Dict[str, Any] = {
            "addParents": target_parent_id,
            "supportsAllDrives": "true",
            "fields": "id,name,parents",
        }
        if old_parent_id:
            params["removeParents"] = old_parent_id
        body: Dict[str, Any] = {}
        if new_name:
            body["name"] = new_name
        self._request("PATCH", f"{DRIVE_API}/files/{file_id}", params=params, json=body)
        self.clear_cache()
        return True

    def copy(self, file_id: str, target_parent_id: str, new_name: str) -> bool:
        """
        复制文件到目标目录。

        :param file_id: 源文件 ID
        :param target_parent_id: 目标目录 ID
        :param new_name: 新文件名
        :return: 是否成功
        """
        if not file_id or not target_parent_id or not new_name:
            return False
        self._request(
            "POST",
            f"{DRIVE_API}/files/{file_id}/copy",
            params={"supportsAllDrives": "true", "fields": FILE_FIELDS},
            json={"name": new_name, "parents": [target_parent_id]},
        )
        self.clear_cache()
        return True

    def delete(self, file_id: str) -> bool:
        """
        删除文件或目录。

        :param file_id: 目标 ID
        :return: 是否成功
        """
        if not file_id:
            return False
        if self.delete_mode == "permanent":
            self._request(
                "DELETE",
                f"{DRIVE_API}/files/{file_id}",
                params={"supportsAllDrives": "true"},
            )
        else:
            self._request(
                "PATCH",
                f"{DRIVE_API}/files/{file_id}",
                params={"supportsAllDrives": "true", "fields": "id,trashed"},
                json={"trashed": True},
            )
        self.clear_cache()
        return True

    def upload(
            self,
            folder_id: str,
            folder_path: str,
            local_path: Path,
            new_name: Optional[str] = None,
    ) -> Optional[DriveItem]:
        """
        可恢复分片上传本地文件。

        :param folder_id: 目标目录 ID
        :param folder_path: 目标目录对外路径
        :param local_path: 本地文件
        :param new_name: 可选的目标文件名
        :return: 上传后的文件项；失败返回 None
        """
        path = Path(local_path)
        if not folder_id or not path.is_file():
            return None
        folder_path = self._effective_virtual_path(folder_path)
        name = new_name or path.name
        size = path.stat().st_size
        mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        init_response = self._request(
            "POST",
            f"{DRIVE_UPLOAD_API}/files",
            params={"uploadType": "resumable", "supportsAllDrives": "true", "fields": FILE_FIELDS},
            json={"name": name, "parents": [folder_id]},
            headers={
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(size),
            },
        )
        location = init_response.headers.get("Location") or init_response.headers.get("location")
        if not location:
            raise GoogleDriveError("Google Drive 未返回可恢复上传会话地址")
        if size == 0:
            response = self._request(
                "PUT",
                location,
                headers={"Content-Range": "bytes */0"},
                content=b"",
                auth=False,
                retry=False,
            )
            if response.status_code not in (200, 201):
                raise GoogleDriveError("空文件上传失败", status_code=response.status_code)
            item = self._to_drive_item(self._json(response), self._join_path(folder_path, name))
            self.clear_cache()
            return item

        with open(path, "rb") as file:
            offset = 0
            while offset < size:
                chunk = file.read(self.chunk_size)
                if not chunk:
                    break
                end = offset + len(chunk) - 1
                response = self._upload_chunk(location, offset, end, size, chunk)
                if response.status_code in (200, 201):
                    item = self._to_drive_item(
                        self._json(response), self._join_path(folder_path, name)
                    )
                    self.clear_cache()
                    return item
                if response.status_code == 308:
                    offset = end + 1
                    continue
                raise GoogleDriveError(
                    "Google Drive 分片上传失败",
                    status_code=response.status_code,
                )
        # 最后一片已发送但未收到完成响应时，用零长度请求查询/触发最终状态
        response = self._request(
            "PUT",
            location,
            headers={"Content-Range": f"bytes */{size}"},
            content=b"",
            auth=False,
            retry=False,
            allow_status={308},
        )
        if response.status_code in (200, 201):
            item = self._to_drive_item(
                self._json(response), self._join_path(folder_path, name)
            )
            self.clear_cache()
            return item
        raise GoogleDriveError("Google Drive 上传未返回完成响应")

    def _upload_chunk(
            self,
            location: str,
            start: int,
            end: int,
            total: int,
            chunk: bytes,
    ) -> Any:
        """
        上传一个分片，遇到可重试状态时按指数退避重试。

        :param location: 可恢复上传会话地址
        :param start: 分片起始字节
        :param end: 分片结束字节（含）
        :param total: 文件总字节数
        :param chunk: 分片内容
        :return: 最后一次响应对象
        """
        last_error: Optional[GoogleDriveError] = None
        for attempt in range(MAX_RETRY):
            try:
                return self._request(
                    "PUT",
                    location,
                    headers={"Content-Range": f"bytes {start}-{end}/{total}"},
                    content=chunk,
                    auth=False,
                    retry=False,
                    allow_status={308},
                )
            except GoogleDriveError as err:
                last_error = err
                if err.status_code not in RETRYABLE_STATUS or attempt >= MAX_RETRY - 1:
                    raise
                self._sleep(self._retry_delay(attempt))
        if last_error:
            raise last_error
        raise GoogleDriveError("Google Drive 分片上传失败")

    def download(
            self,
            file_id: str,
            name: str,
            mime_type: str,
            local_dir: Path,
    ) -> Optional[Path]:
        """
        流式下载文件到本地目录。

        :param file_id: 文件 ID
        :param name: 远端文件名
        :param mime_type: 远端 MIME 类型
        :param local_dir: 本地目录
        :return: 本地文件路径；失败返回 None
        """
        safe_name = safe_file_name(name)
        if not file_id or not safe_name:
            return None
        is_google_doc = str(mime_type or "").startswith(GOOGLE_DOC_PREFIX)
        if is_google_doc:
            url = f"{DRIVE_API}/files/{file_id}/export"
            export_mime = self._export_mime(mime_type)
            params = {"mimeType": export_mime}
            export_ext = mimetypes.guess_extension(export_mime) or ""
            if export_ext and not safe_name.lower().endswith(export_ext.lower()):
                safe_name += export_ext
        else:
            url = f"{DRIVE_API}/files/{file_id}"
            params = {"alt": "media", "supportsAllDrives": "true"}
        target = Path(local_dir) / safe_name
        target.parent.mkdir(parents=True, exist_ok=True)
        headers = self._auth_headers()
        with self._transport.stream("GET", url, headers=headers, params=params) as response:
            if response.status_code != 200:
                raise GoogleDriveError(
                    "Google Drive 下载失败",
                    status_code=response.status_code,
                    reason=self._response_reason(response),
                )
            with open(target, "wb") as file:
                for chunk in response.iter_bytes(1024 * 1024):
                    if chunk:
                        file.write(chunk)
        return target

    @staticmethod
    def _export_mime(mime_type: str) -> str:
        """为 Google 原生文档选择导出 MIME。"""
        mapping = {
            "application/vnd.google-apps.document": "application/pdf",
            "application/vnd.google-apps.spreadsheet": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            "application/vnd.google-apps.presentation": (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ),
            "application/vnd.google-apps.drawing": "image/png",
        }
        return mapping.get(mime_type, "application/pdf")

    # ------------------------------------------------------------------
    # 管理
    # ------------------------------------------------------------------
    def usage(self) -> DriveQuota:
        """
        查询用户存储容量。

        :return: 总容量/剩余/已用字节；无限容量时 total=0
        """
        response = self._request(
            "GET",
            f"{DRIVE_API}/about",
            params={"fields": "storageQuota"},
        )
        quota = (self._json(response) or {}).get("storageQuota") or {}
        total = int(quota.get("limit") or 0)
        usage = int(quota.get("usage") or 0)
        available = max(0, total - usage) if total else 0
        return DriveQuota(total=total, available=available, usage=usage)

    def check(self) -> Dict[str, Any]:
        """
        检查凭据和根目录是否可访问。

        :return: 包含 user/root/quota 的可读结果
        :raises GoogleDriveError: 认证或网络失败
        """
        about = self._request(
            "GET",
            f"{DRIVE_API}/about",
            params={"fields": "user,storageQuota"},
        )
        about_data = self._json(about) or {}
        root = self.get_item_by_id(self._root_id(), self.root_path)
        if not root:
            raise GoogleDriveError("无法访问 Google Drive 根目录，请检查 root_folder_id 和授权范围")
        quota = about_data.get("storageQuota") or {}
        total = int(quota.get("limit") or 0)
        used = int(quota.get("usage") or 0)
        return {
            "user": about_data.get("user") or {},
            "root": {
                "id": root.id,
                "name": root.name,
                "path": root.path,
            },
            "quota": {
                "total": total,
                "used": used,
                "available": max(0, total - used) if total else 0,
            },
        }

    # ------------------------------------------------------------------
    # HTTP / 数据转换
    # ------------------------------------------------------------------
    def _request(
            self,
            method: str,
            url: str,
            *,
            params: Optional[Mapping[str, Any]] = None,
            json: Optional[Mapping[str, Any]] = None,
            data: Optional[Mapping[str, Any]] = None,
            headers: Optional[Mapping[str, str]] = None,
            content: Any = None,
            auth: bool = True,
            retry: Optional[bool] = None,
            allow_status: Optional[Iterable[int]] = None,
    ) -> Any:
        """
        发送一次请求，处理认证、重试和错误映射。

        :param method: HTTP 方法
        :param url: 完整 URL
        :param params: 查询参数
        :param json: JSON body
        :param data: 表单 body
        :param headers: 额外请求头
        :param content: 原始 body
        :param auth: 是否附加 Bearer 头
        :param retry: 是否对 429/5xx 重试；为空时仅对幂等方法重试
        :param allow_status: 允许作为正常结果返回的非 2xx 状态码
        :return: 成功的响应对象
        """
        request_headers: Dict[str, str] = dict(headers or {})
        if auth:
            request_headers.update(self._auth_headers())
        should_retry = (
            retry if retry is not None
            else method.upper() in {"GET", "PATCH", "DELETE", "PUT"}
        )
        refreshed = False
        attempt = 0
        while True:
            try:
                response = self._transport.request(
                    method,
                    url,
                    params=dict(params or {}),
                    json=dict(json) if json is not None else None,
                    data=dict(data) if data is not None else None,
                    headers=request_headers,
                    content=content,
                    timeout=self.timeout,
                )
            except Exception as err:  # 传输层异常（连接、超时、TLS）
                if should_retry and attempt < MAX_RETRY - 1:
                    attempt += 1
                    self._sleep(self._retry_delay(attempt - 1))
                    continue
                raise GoogleDriveError(f"Google Drive 网络请求失败：{err}") from err

            status = int(getattr(response, "status_code", 0) or 0)
            if status == 401 and auth and not refreshed:
                refreshed = True
                # 401 表示当前 token 已失效，必须强制刷新，不能命中缓存
                self.refresh_access_token(force=True)
                request_headers.update(self._auth_headers())
                continue
            if should_retry and status in RETRYABLE_STATUS and attempt < MAX_RETRY - 1:
                self._sleep(self._retry_delay(attempt, response))
                attempt += 1
                continue
            if 200 <= status < 300 or (allow_status and status in allow_status):
                return response
            reason = self._response_reason(response)
            message = self._response_message(response)
            if status == 404:
                raise GoogleDriveNotFound(message or "Google Drive 目标不存在", status_code=status, reason=reason)
            raise GoogleDriveError(message or "Google Drive 请求失败", status_code=status, reason=reason)

    def _retry_delay(self, attempt: int, response: Any = None) -> float:
        """计算指数退避时间，优先使用 Retry-After。"""
        if response is not None:
            retry_after = getattr(response, "headers", {}).get("Retry-After") if getattr(response, "headers", None) else None
            if retry_after:
                try:
                    return min(30.0, max(0.5, float(retry_after)))
                except (TypeError, ValueError):
                    pass
        return min(8.0, 0.5 * (2 ** attempt))

    @staticmethod
    def _json(response: Any) -> Dict[str, Any]:
        """安全读取 JSON 响应。"""
        try:
            data = response.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _response_message(response: Any) -> str:
        """从 Google 错误响应中提取可读消息。"""
        try:
            data = response.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or "")
            if isinstance(error, str):
                return error
            description = data.get("error_description")
            if description:
                return str(description)
        text = getattr(response, "text", "") or ""
        return str(text)[:300]

    @staticmethod
    def _response_reason(response: Any) -> Optional[str]:
        """从 Google 错误响应中提取 reason。"""
        try:
            data = response.json()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        error = data.get("error")
        if isinstance(error, dict):
            errors = error.get("errors")
            if isinstance(errors, list) and errors:
                return str(errors[0].get("reason") or "") or None
        return None

    def _to_drive_item(self, raw: Mapping[str, Any], virtual_path: str) -> DriveItem:
        """
        把 Drive File JSON 转换为中立模型。

        :param raw: Drive API 的 File 资源
        :param virtual_path: 对外虚拟路径
        :return: DriveItem
        """
        mime_type = str(raw.get("mimeType") or "")
        is_dir = mime_type == FOLDER_MIME
        name = str(raw.get("name") or "")
        path = self.normalize_virtual_path(virtual_path)
        if is_dir and not path.endswith("/"):
            path += "/"
        size_raw = raw.get("size")
        size: Optional[int]
        try:
            size = int(size_raw) if size_raw is not None and not is_dir else None
        except (TypeError, ValueError):
            size = None
        parents = raw.get("parents")
        return DriveItem(
            id=str(raw.get("id") or ""),
            name=name,
            mime_type=mime_type,
            parents=[str(parent) for parent in parents] if isinstance(parents, list) else [],
            size=size,
            modified_time=parse_rfc3339(raw.get("modifiedTime")),
            created_time=parse_rfc3339(raw.get("createdTime")),
            trashed=bool(raw.get("trashed")),
            web_view_link=raw.get("webViewLink"),
            thumbnail_link=raw.get("thumbnailLink"),
            drive_id=raw.get("driveId"),
            path=path,
        )

    @staticmethod
    def _align_chunk_size(size: Any) -> int:
        """把分片大小调整为 256 KiB 的整数倍。"""
        try:
            value = int(size or 8 * 1024 * 1024)
        except (TypeError, ValueError):
            value = 8 * 1024 * 1024
        value = max(UPLOAD_ALIGN, value)
        return value // UPLOAD_ALIGN * UPLOAD_ALIGN
