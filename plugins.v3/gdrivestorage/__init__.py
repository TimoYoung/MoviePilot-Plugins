"""Google Drive 存储插件。

插件把 Google Drive 作为 MoviePilot V3 的自定义存储接入：
- 通过 ``StorageHelper`` 注册 ``gdrive`` 存储类型；
- 通过 ``StorageOperSelection`` 为整理链提供源/目标适配器；
- 通过 ``get_module()`` 提供文件管理模块方法；
- 提供 OAuth2 配置页、详情页和操作日志。
"""

from __future__ import annotations

import math
import mimetypes
import secrets
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import Body

from app.schemas.file import FileItem, FileItemQueryResult, StorageUsage
from app.schemas.types import ChainEventType
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.sdk.services import StorageHelper

from .gdrive_client import (
    DriveItem,
    GoogleDriveAuthError,
    GoogleDriveClient,
    GoogleDriveError,
    GoogleDriveNotFound,
    format_size,
)

STORAGE_TYPE = "gdrive"
DEFAULT_DISPLAY_NAME = "Google Drive"
DEFAULT_ROOT_PATH = "/"
DEFAULT_DELETE_MODE = "trash"
DEFAULT_CHUNK_SIZE_MB = 8
DEFAULT_TIMEOUT = 60
MAX_LOGS = 100
OAUTH_STATE_TTL = 600

# 配置键顺序同时用于表单默认值和系统存储保存时的白名单
FORM_KEYS = (
    "enabled",
    "display_name",
    "register_storage",
    "client_id",
    "client_secret",
    "refresh_token",
    "redirect_uri",
    "oauth_code",
    "root_folder_id",
    "root_path",
    "use_shared_drive",
    "shared_drive_id",
    "delete_mode",
    "chunk_size_mb",
    "timeout",
)


def _now_text() -> str:
    """返回本地时间文本。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    """把配置值限制在闭区间内。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


class GDriveStorageProvider:
    """Google Drive 存储适配器（整理链与模块方法共用）。"""

    transtype = {"copy": "复制", "move": "移动"}

    def __init__(
            self,
            client: GoogleDriveClient,
            log: Callable[[str, str, bool, str], None],
    ) -> None:
        """
        保存客户端和操作日志回调。

        :param client: Google Drive 客户端
        :param log: ``(action, path, ok, detail)`` 日志回调
        """
        self.client = client
        self._log = log

    # ------------------------------------------------------------------
    # 数据映射
    # ------------------------------------------------------------------
    def to_file_item(self, item: DriveItem) -> FileItem:
        """
        把客户端中立模型转换为宿主 FileItem。

        :param item: Google Drive 条目
        :return: 宿主文件项
        """
        name = item.name or ""
        suffix = PurePosixPath(name).suffix
        path = item.path or "/"
        if item.is_dir and not path.endswith("/"):
            path += "/"
        return FileItem(
            storage=STORAGE_TYPE,
            type="dir" if item.is_dir else "file",
            path=path,
            name=name,
            basename=name if item.is_dir else (name[:-len(suffix)] if suffix else name),
            extension=None if item.is_dir or not suffix else suffix[1:].lower(),
            size=None if item.is_dir else item.size,
            modify_time=item.modified_time,
            fileid=item.id,
            parent_fileid=item.parent_id,
            url=item.web_view_link,
            thumbnail=item.thumbnail_link,
        )

    # ------------------------------------------------------------------
    # 读操作
    # ------------------------------------------------------------------
    def list(self, fileitem: FileItem) -> List[FileItem]:
        """
        列举目录直接子项。

        :param fileitem: 目录项；文件项返回自身
        :return: 子项列表；失败返回空列表
        """
        if fileitem.type == "file":
            return [fileitem]
        try:
            folder_id = fileitem.fileid or self.client.resolve_folder_id(fileitem.path)
            if not folder_id:
                return []
            return [self.to_file_item(item) for item in self.client.list(folder_id, fileitem.path or "/")]
        except (GoogleDriveError, ValueError) as err:
            logger.warning(f"【GDrive】列举目录失败：{fileitem.path} - {err}")
            return []

    def list_strict(self, fileitem: FileItem) -> List[FileItem]:
        """
        严格列举目录，查询失败时抛出异常。

        :param fileitem: 目录项
        :return: 子项列表
        :raises GoogleDriveError: 查询失败
        """
        folder_id = fileitem.fileid or self.client.resolve_folder_id(fileitem.path)
        if not folder_id:
            raise GoogleDriveNotFound(f"目录不存在：{fileitem.path}")
        return [self.to_file_item(item) for item in self.client.list(folder_id, fileitem.path or "/")]

    def get_item(self, path: Any) -> Optional[FileItem]:
        """按路径查询文件或目录。"""
        try:
            item = self.client.get_item(path)
        except (GoogleDriveError, ValueError) as err:
            logger.warning(f"【GDrive】查询路径失败：{path} - {err}")
            return None
        return self.to_file_item(item) if item else None

    def get_item_strict(self, path: Any) -> Optional[FileItem]:
        """
        严格查询路径，供整理链直接使用。

        :param path: 对外路径
        :return: 文件项；确认不存在返回 None；API 故障向上抛出
        """
        item = self.client.get_item(path)
        return self.to_file_item(item) if item else None

    def get_folder(self, path: Any, create: bool = True) -> Optional[FileItem]:
        """解析目录，缺失时按需递归创建。"""
        try:
            folder_id = self.client.resolve_folder_id(path, create=create)
            if not folder_id:
                return None
            item = self.client.get_item(path)
            if item:
                return self.to_file_item(item)
            normalized = GoogleDriveClient.normalize_virtual_path(path)
            name = PurePosixPath(normalized).name or "/"
            return FileItem(
                storage=STORAGE_TYPE,
                type="dir",
                path=normalized.rstrip("/") + "/",
                name=name,
                basename=name,
                fileid=folder_id,
            )
        except (GoogleDriveError, ValueError) as err:
            logger.warning(f"【GDrive】获取/创建目录失败：{path} - {err}")
            return None

    def get_parent(self, fileitem: FileItem) -> Optional[FileItem]:
        """返回父目录项。"""
        path = str(fileitem.path or "/").rstrip("/")
        parent = str(PurePosixPath(path).parent)
        return self.get_item(parent or "/")

    def detail(self, fileitem: FileItem) -> Optional[FileItem]:
        """按 ID 重新读取详情。"""
        if not fileitem.fileid:
            return self.get_item(fileitem.path)
        try:
            item = self.client.get_item_by_id(fileitem.fileid, fileitem.path)
        except (GoogleDriveError, ValueError) as err:
            logger.warning(f"【GDrive】读取详情失败：{fileitem.path} - {err}")
            return None
        return self.to_file_item(item) if item else None

    # ------------------------------------------------------------------
    # 写操作
    # ------------------------------------------------------------------
    def create_folder(self, parent: FileItem, name: str) -> Optional[FileItem]:
        """在父目录下创建文件夹。"""
        if not name:
            return None
        try:
            parent_id = parent.fileid or self.client.resolve_folder_id(parent.path, create=True)
            if not parent_id:
                return None
            item = self.client.create_folder(parent_id, parent.path or "/", name)
        except (GoogleDriveError, ValueError) as err:
            logger.warning(f"【GDrive】创建目录失败：{parent.path}/{name} - {err}")
            self._log("create_folder", f"{parent.path}/{name}", False, str(err))
            return None
        if not item:
            return None
        self._log("create_folder", item.path, True, "")
        return self.to_file_item(item)

    def upload(
            self,
            folder: FileItem,
            path: Path,
            new_name: Optional[str] = None,
    ) -> Optional[FileItem]:
        """上传本地文件到目标目录。"""
        try:
            folder_id = folder.fileid or self.client.resolve_folder_id(folder.path, create=True)
            if not folder_id:
                return None
            item = self.client.upload(folder_id, folder.path or "/", Path(path), new_name)
        except (GoogleDriveError, OSError, ValueError) as err:
            logger.warning(f"【GDrive】上传失败：{path} - {err}")
            self._log("upload", str(path), False, str(err))
            return None
        if not item:
            return None
        self._log("upload", item.path, True, "")
        return self.to_file_item(item)

    def download(self, fileitem: FileItem, path: Optional[Path] = None) -> Optional[Path]:
        """下载远端文件到本地目录。"""
        if not fileitem.fileid:
            return None
        try:
            detail = self.client.get_item_by_id(fileitem.fileid, fileitem.path)
            mime_type = detail.mime_type if detail else (
                mimetypes.guess_type(fileitem.name or "")[0] or "application/octet-stream"
            )
            local_dir = Path(path) if path else Path(tempfile.mkdtemp(prefix="moviepilot_gdrive_"))
            result = self.client.download(
                fileitem.fileid,
                fileitem.name or "",
                mime_type,
                local_dir,
            )
        except (GoogleDriveError, OSError, ValueError) as err:
            logger.warning(f"【GDrive】下载失败：{fileitem.path} - {err}")
            self._log("download", fileitem.path or "", False, str(err))
            return None
        if result:
            self._log("download", fileitem.path or "", True, str(result))
        return result

    def copy(self, fileitem: FileItem, path: Path, new_name: str) -> bool:
        """复制文件到同一存储的目标目录。"""
        try:
            target_id = self.client.resolve_folder_id(path, create=True)
            if not target_id or not fileitem.fileid:
                return False
            ok = self.client.copy(fileitem.fileid, target_id, new_name)
        except (GoogleDriveError, ValueError) as err:
            logger.warning(f"【GDrive】复制失败：{fileitem.path} -> {path} - {err}")
            self._log("copy", f"{fileitem.path} -> {path}", False, str(err))
            return False
        self._log("copy", f"{fileitem.path} -> {path}", ok, "")
        return ok

    def move(self, fileitem: FileItem, path: Path, new_name: str) -> bool:
        """移动文件到目标目录，可同时改名。"""
        try:
            target_id = self.client.resolve_folder_id(path, create=True)
            old_parent = fileitem.parent_fileid
            if not old_parent and fileitem.fileid:
                detail = self.client.get_item_by_id(fileitem.fileid, fileitem.path)
                old_parent = detail.parent_id if detail else None
            if not target_id or not fileitem.fileid:
                return False
            ok = self.client.move(fileitem.fileid, old_parent or "", target_id, new_name)
        except (GoogleDriveError, ValueError) as err:
            logger.warning(f"【GDrive】移动失败：{fileitem.path} -> {path} - {err}")
            self._log("move", f"{fileitem.path} -> {path}", False, str(err))
            return False
        self._log("move", f"{fileitem.path} -> {path}", ok, "")
        return ok

    def rename(self, fileitem: FileItem, name: str) -> bool:
        """重命名文件或目录。"""
        if not fileitem.fileid:
            return False
        try:
            ok = self.client.rename(fileitem.fileid, name)
        except GoogleDriveError as err:
            logger.warning(f"【GDrive】重命名失败：{fileitem.path} - {err}")
            self._log("rename", fileitem.path or "", False, str(err))
            return False
        self._log("rename", fileitem.path or "", ok, name)
        return ok

    def delete(self, fileitem: FileItem) -> bool:
        """删除文件或目录。"""
        if not fileitem.fileid:
            return False
        try:
            ok = self.client.delete(fileitem.fileid)
        except GoogleDriveError as err:
            logger.warning(f"【GDrive】删除失败：{fileitem.path} - {err}")
            self._log("delete", fileitem.path or "", False, str(err))
            return False
        self._log("delete", fileitem.path or "", ok, "")
        return ok

    # ------------------------------------------------------------------
    # 管理
    # ------------------------------------------------------------------
    def usage(self) -> Optional[StorageUsage]:
        """返回容量信息。"""
        try:
            quota = self.client.usage()
        except GoogleDriveError as err:
            logger.warning(f"【GDrive】查询容量失败：{err}")
            return None
        return StorageUsage(total=float(quota.total), available=float(quota.available))

    def check(self) -> bool:
        """测试凭据与根目录可访问。"""
        try:
            self.client.check()
            return True
        except GoogleDriveError as err:
            logger.warning(f"【GDrive】连接测试失败：{err}")
            return False

    def is_support_transtype(self, transtype: str) -> bool:
        """判断是否支持指定整理方式。"""
        return transtype in self.transtype


class GDriveStorage(_PluginBase):
    """Google Drive 存储插件。"""

    plugin_name = "Google Drive 存储"
    plugin_desc = "把 Google Drive 接入 MoviePilot 存储体系，支持浏览、上传、下载、移动、复制、删除和容量查询。"
    plugin_version = "1.0.0"
    plugin_label = "网盘"
    plugin_author = "local"
    plugin_config_prefix = "gdrivestorage_"
    plugin_order = 100
    auth_level = 1

    _enabled = False
    _display_name = DEFAULT_DISPLAY_NAME
    _register_storage = True
    _page = 1
    _page_size = 10
    _config: Dict[str, Any] = {}
    _client: Optional[GoogleDriveClient] = None
    _provider: Optional[GDriveStorageProvider] = None
    _logs: List[Dict[str, Any]] = []
    _last_quota: Dict[str, Any] = {}
    _client_lock: threading.RLock

    def __init__(self) -> None:
        """初始化线程锁和运行时状态。"""
        super().__init__()
        self._client_lock = threading.RLock()
        self._config = {}
        self._logs = []
        self._last_quota = {}
        self._client = None
        self._provider = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def init_plugin(self, config: dict = None) -> None:
        """读取配置并重建客户端、适配器和系统存储注册。"""
        self.stop_service()
        cfg = self._normalize_config(config or {})
        self._config = cfg
        self._enabled = bool(cfg.get("enabled"))
        self._display_name = cfg["display_name"]
        self._register_storage = bool(cfg.get("register_storage", True))
        self._page = 1
        stored_logs = self.get_data("logs")
        self._logs = list(stored_logs)[-MAX_LOGS:] if isinstance(stored_logs, list) else []
        stored_quota = self.get_data("quota")
        self._last_quota = stored_quota if isinstance(stored_quota, dict) else {}
        self._client = self._build_client(cfg)
        self._provider = (
            GDriveStorageProvider(self._client, self._record_log)
            if self._client and cfg.get("refresh_token") else None
        )
        if self._enabled and self._register_storage:
            self._ensure_storage_registered()
        if self._enabled and not self._client:
            logger.warning("【GDrive】插件已启用，但 OAuth 客户端凭据不完整，存储暂不可用")
        elif self._enabled and not cfg.get("refresh_token"):
            logger.warning("【GDrive】插件已启用，但尚未完成授权，存储暂不可读写")
        elif self._enabled:
            logger.info(
                f"【GDrive】插件已启用：存储={self._display_name}，"
                f"根路径={cfg['root_path']}，共享盘={cfg['use_shared_drive']}"
            )

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """不注册远程命令。"""
        return []

    def stop_service(self) -> None:
        """关闭 HTTP 客户端和适配器，可重复调用。"""
        with self._client_lock:
            client = self._client
            self._client = None
            self._provider = None
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.debug("【GDrive】关闭 HTTP 客户端失败", exc_info=True)

    def _build_client(self, cfg: Dict[str, Any]) -> Optional[GoogleDriveClient]:
        """凭据完整时构建客户端；不发起网络请求。"""
        # 授权码交换阶段允许暂时没有 refresh_token，因此只要求客户端凭据
        if not (cfg.get("client_id") and cfg.get("client_secret")):
            return None
        try:
            chunk_size = int(cfg.get("chunk_size_mb") or DEFAULT_CHUNK_SIZE_MB) * 1024 * 1024
            return GoogleDriveClient(
                client_id=cfg["client_id"],
                client_secret=cfg["client_secret"],
                refresh_token=cfg["refresh_token"],
                root_folder_id=cfg.get("root_folder_id") or "root",
                root_path=cfg.get("root_path") or DEFAULT_ROOT_PATH,
                shared_drive_id=cfg.get("shared_drive_id") or "",
                use_shared_drive=bool(cfg.get("use_shared_drive")),
                delete_mode=cfg.get("delete_mode") or DEFAULT_DELETE_MODE,
                chunk_size=chunk_size,
                timeout=float(cfg.get("timeout") or DEFAULT_TIMEOUT),
            )
        except Exception as err:
            logger.error(f"【GDrive】初始化 Google Drive 客户端失败：{err}")
            return None

    def _rebuild_client(self) -> None:
        """配置变化后重建客户端和适配器。"""
        with self._client_lock:
            old = self._client
            self._client = self._build_client(self._config)
            self._provider = (
                GDriveStorageProvider(self._client, self._record_log)
                if self._client and self._config.get("refresh_token") else None
            )
        if old is not None and old is not self._client:
            try:
                old.close()
            except Exception:
                logger.debug("【GDrive】关闭旧客户端失败", exc_info=True)

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _default_redirect_uri(self) -> str:
        """返回当前实例默认的 OAuth 回调地址。"""
        return (
            f"http://localhost:3000/api/v1/plugin/"
            f"{self.__class__.__name__}/oauth/callback"
        )

    def _normalize_config(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """把用户配置归一化为稳定类型和默认值。"""
        root_path = str(cfg.get("root_path") or DEFAULT_ROOT_PATH).strip() or DEFAULT_ROOT_PATH
        try:
            root_path = GoogleDriveClient.normalize_virtual_path(root_path)
        except ValueError:
            logger.warning(f"【GDrive】root_path 无效，回退为 {DEFAULT_ROOT_PATH}")
            root_path = DEFAULT_ROOT_PATH
        delete_mode = "permanent" if cfg.get("delete_mode") == "permanent" else "trash"
        return {
            "enabled": bool(cfg.get("enabled")),
            "display_name": str(cfg.get("display_name") or DEFAULT_DISPLAY_NAME).strip() or DEFAULT_DISPLAY_NAME,
            "register_storage": bool(cfg.get("register_storage", True)),
            "client_id": str(cfg.get("client_id") or "").strip(),
            "client_secret": str(cfg.get("client_secret") or "").strip(),
            "refresh_token": str(cfg.get("refresh_token") or "").strip(),
            "redirect_uri": str(cfg.get("redirect_uri") or self._default_redirect_uri()).strip(),
            "oauth_code": str(cfg.get("oauth_code") or "").strip(),
            "root_folder_id": str(cfg.get("root_folder_id") or "root").strip() or "root",
            "root_path": root_path,
            "use_shared_drive": bool(cfg.get("use_shared_drive")),
            "shared_drive_id": str(cfg.get("shared_drive_id") or "").strip(),
            "delete_mode": delete_mode,
            "chunk_size_mb": _clamp_int(cfg.get("chunk_size_mb"), 1, 1024, DEFAULT_CHUNK_SIZE_MB),
            "timeout": _clamp_int(cfg.get("timeout"), 5, 600, DEFAULT_TIMEOUT),
        }

    def _save_config(self, updates: Dict[str, Any]) -> None:
        """合并并持久化插件配置。"""
        merged = dict(self._config)
        merged.update(updates)
        self._config = self._normalize_config(merged)
        try:
            self.update_config(self._config)
        except Exception:
            logger.exception("【GDrive】保存插件配置失败")

    # ------------------------------------------------------------------
    # 系统存储注册
    # ------------------------------------------------------------------
    def _ensure_storage_registered(self) -> None:
        """确保系统存储中存在 gdrive 条目。"""
        try:
            helper = StorageHelper()
            existing = helper.get_storage(STORAGE_TYPE)
            if existing:
                helper.set_storage(STORAGE_TYPE, existing.config or {})
            else:
                helper.add_storage(STORAGE_TYPE, self._display_name, {})
            logger.info(f"【GDrive】系统存储已注册：{self._display_name}")
        except Exception as err:
            logger.warning(f"【GDrive】注册系统存储失败：{err}")

    def _storage_registered(self) -> bool:
        """查询系统存储中是否存在 gdrive。"""
        try:
            return StorageHelper().get_storage(STORAGE_TYPE) is not None
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    @eventmanager.register(ChainEventType.StorageOperSelection)
    def storage_oper_selection(self, event: Event) -> None:
        """仅接管 gdrive 存储的整理适配器选择。"""
        if not self._enabled or not self._provider:
            return
        try:
            data = event.event_data
        except Exception:
            return
        storage = getattr(data, "storage", None)
        if storage is None and isinstance(data, dict):
            storage = data.get("storage")
        if storage != STORAGE_TYPE:
            return
        try:
            data.storage_oper = self._provider
        except Exception:
            logger.debug("【GDrive】无法写入整理适配器", exc_info=True)

    # ------------------------------------------------------------------
    # 模块方法
    # ------------------------------------------------------------------
    def get_module(self) -> Dict[str, Any]:
        """声明插件接管的文件管理模块方法。"""
        return {
            "list_files": self.list_files,
            "any_files": self.any_files,
            "create_folder": self.create_folder,
            "get_folder": self.get_folder,
            "download_file": self.download_file,
            "upload_file": self.upload_file,
            "delete_file": self.delete_file,
            "rename_file": self.rename_file,
            "get_file_item": self.get_file_item,
            "get_file_item_strict": self.get_file_item_strict,
            "get_parent_item": self.get_parent_item,
            "snapshot_storage": self.snapshot_storage,
            "storage_manage": self.storage_manage,
        }

    def _is_gdrive(self, storage: Optional[str]) -> bool:
        """判断存储标识是否为当前插件。"""
        return (storage or "") == STORAGE_TYPE

    def _provider_or_none(self) -> Optional[GDriveStorageProvider]:
        """返回可用适配器。"""
        return self._provider if self._enabled else None

    def list_files(
            self,
            fileitem: FileItem,
            recursion: bool = False,
    ) -> Optional[List[FileItem]]:
        """列举目录；递归时只返回文件。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        provider = self._provider_or_none()
        if not provider:
            return None
        items = provider.list(fileitem)
        if not recursion:
            return items
        files: List[FileItem] = []
        for item in items:
            if item.type == "dir":
                files.extend(self.list_files(item, True) or [])
            else:
                files.append(item)
        return files

    def any_files(
            self,
            fileitem: FileItem,
            extensions: Optional[List[str]] = None,
    ) -> Optional[bool]:
        """递归判断目录下是否存在指定扩展名的文件。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        provider = self._provider_or_none()
        if not provider:
            return None
        wanted = {
            str(ext).lower().lstrip(".")
            for ext in (extensions or [])
            if str(ext).strip()
        }

        def walk(item: FileItem) -> bool:
            for child in provider.list(item):
                if child.type == "file":
                    if not wanted or (child.extension or "").lower() in wanted:
                        return True
                elif child.type == "dir" and walk(child):
                    return True
            return False

        return walk(fileitem)

    def create_folder(self, fileitem: FileItem, name: str) -> Optional[FileItem]:
        """创建目录。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        provider = self._provider_or_none()
        return provider.create_folder(fileitem, name) if provider else None

    def get_folder(self, storage: str, path: Path) -> Optional[FileItem]:
        """获取目录，缺失时创建。"""
        if not self._is_gdrive(storage):
            return None
        provider = self._provider_or_none()
        return provider.get_folder(path, create=True) if provider else None

    def download_file(self, fileitem: FileItem, path: Optional[Path] = None) -> Optional[Path]:
        """下载文件到本地。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        provider = self._provider_or_none()
        return provider.download(fileitem, path) if provider else None

    def upload_file(
            self,
            fileitem: FileItem,
            path: Path,
            new_name: Optional[str] = None,
    ) -> Optional[FileItem]:
        """上传本地文件。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        provider = self._provider_or_none()
        return provider.upload(fileitem, path, new_name) if provider else None

    def delete_file(self, fileitem: FileItem) -> Optional[bool]:
        """删除文件或目录。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        provider = self._provider_or_none()
        return provider.delete(fileitem) if provider else None

    def rename_file(self, fileitem: FileItem, name: str) -> Optional[bool]:
        """重命名文件或目录。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        provider = self._provider_or_none()
        return provider.rename(fileitem, name) if provider else None

    def get_file_item(self, storage: str, path: Path) -> Optional[FileItem]:
        """按路径查询文件或目录。"""
        if not self._is_gdrive(storage):
            return None
        provider = self._provider_or_none()
        return provider.get_item(path) if provider else None

    def get_file_item_strict(self, storage: str, path: Path) -> Optional[FileItemQueryResult]:
        """严格查询文件或目录，返回宿主模块契约要求的查询回执。"""
        if not self._is_gdrive(storage):
            return None
        provider = self._provider_or_none()
        if not provider:
            return None
        return FileItemQueryResult(item=provider.get_item_strict(path))

    def get_parent_item(self, fileitem: FileItem) -> Optional[FileItem]:
        """获取父目录。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        provider = self._provider_or_none()
        return provider.get_parent(fileitem) if provider else None

    def snapshot_storage(
            self,
            storage: str,
            path: Path,
            last_snapshot_time: Optional[float] = None,
            max_depth: int = 5,
            previous_snapshot: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        """生成目录文件快照；查询失败返回 None 以保留旧基线。"""
        del last_snapshot_time, previous_snapshot
        if not self._is_gdrive(storage):
            return None
        provider = self._provider_or_none()
        if not provider:
            return None
        try:
            root = provider.get_item_strict(path)
        except GoogleDriveError as err:
            logger.warning(f"【GDrive】快照根目录查询失败：{path} - {err}")
            return None
        if not root:
            return {}
        result: Dict[str, Dict[str, Any]] = {}

        def walk(item: FileItem, depth: int) -> None:
            if item.type == "dir":
                if depth >= max_depth:
                    return
                for child in provider.list_strict(item):
                    walk(child, depth + 1)
            else:
                result[item.path or ""] = {
                    "size": item.size or 0,
                    "modify_time": item.modify_time or 0,
                    "fileid": item.fileid,
                    "type": item.type,
                }

        try:
            walk(root, 0)
        except GoogleDriveError as err:
            logger.warning(f"【GDrive】生成快照失败：{path} - {err}")
            return None
        return result

    def storage_manage(self, storage: str, action: str, **params: Any) -> Optional[Dict[str, Any]]:
        """处理系统存储页的通用管理动作。"""
        if not self._is_gdrive(storage):
            return None
        action_value = getattr(action, "value", action)
        action_value = str(action_value or "")
        if action_value == "usage":
            provider = self._provider_or_none()
            usage = provider.usage() if provider else None
            if not usage:
                return {"success": False, "message": "容量查询失败", "data": None}
            return {
                "success": True,
                "message": "容量已刷新",
                "data": {
                    "total": usage.total,
                    "available": usage.available,
                    "total_text": format_size(int(usage.total)),
                    "available_text": format_size(int(usage.available)),
                },
            }
        if action_value == "support_transtype":
            return {
                "success": True,
                "message": "",
                "data": {"transtype": dict(GDriveStorageProvider.transtype)},
            }
        if action_value == "generate_auth_url":
            url = self._auth_url()
            if not url:
                return {"success": False, "message": "请先填写 client_id 和 redirect_uri", "data": None}
            return {
                "success": True,
                "message": "",
                "data": {"authUrl": url, "state": (self.get_data("oauth_state") or {}).get("value", "")},
            }
        if action_value == "check_login":
            provider = self._provider_or_none()
            ok = bool(provider and provider.check())
            return {
                "success": ok,
                "message": "连接正常" if ok else "连接失败",
                "data": {"status": 1 if ok else 0, "tip": "连接正常" if ok else "请检查 OAuth 凭据"},
            }
        if action_value == "save_config":
            conf = params.get("conf") or {}
            updates = {key: conf[key] for key in FORM_KEYS if key in conf}
            if updates:
                self._save_config(updates)
                self._rebuild_client()
                if self._enabled and self._register_storage:
                    self._ensure_storage_registered()
            return {"success": True, "message": "配置已保存", "data": None}
        return {"success": False, "message": "不支持的管理动作", "data": None}

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------
    def _auth_url(self) -> Optional[str]:
        """生成并缓存一次性 state 的授权链接。"""
        if not self._client:
            return None
        state = self.get_data("oauth_state") or {}
        now = time.time()
        if (
                not isinstance(state, dict)
                or not state.get("value")
                or float(state.get("expires_at") or 0) < now
        ):
            state = {
                "value": secrets.token_urlsafe(24),
                "expires_at": now + OAUTH_STATE_TTL,
            }
            self.save_data("oauth_state", state)
        try:
            return self._client.build_auth_url(self._config["redirect_uri"], state["value"])
        except GoogleDriveAuthError as err:
            logger.warning(f"【GDrive】生成授权链接失败：{err}")
            return None

    def _exchange_code(self, code: str) -> Tuple[bool, str]:
        """用授权码换取 refresh_token 并持久化。"""
        if not self._client:
            return False, "尚未配置完整的 OAuth 凭据"
        if not code:
            return False, "授权码为空"
        try:
            token = self._client.exchange_code(code, self._config["redirect_uri"])
        except GoogleDriveError as err:
            logger.warning(f"【GDrive】授权码换取令牌失败：{err}")
            return False, str(err)
        refresh_token = str(token.get("refresh_token") or "").strip()
        if not refresh_token:
            return False, "Google 未返回 refresh_token，请重新授权并确认 prompt=consent"
        self._save_config({"refresh_token": refresh_token, "oauth_code": ""})
        self.del_data("oauth_state")
        self._rebuild_client()
        if self._enabled and self._register_storage:
            self._ensure_storage_registered()
        self._record_log("oauth", "", True, "授权成功")
        return True, "授权成功，refresh_token 已保存"

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def get_api(self) -> List[Dict[str, Any]]:
        """注册详情页操作和 OAuth 回调 API。"""
        return [
            {
                "path": "/test",
                "endpoint": self.api_test,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "测试 Google Drive 连接",
            },
            {
                "path": "/quota",
                "endpoint": self.api_quota,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "刷新 Google Drive 容量",
            },
            {
                "path": "/auth/url",
                "endpoint": self.api_auth_url,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "生成 Google OAuth 授权链接",
            },
            {
                "path": "/auth/exchange",
                "endpoint": self.api_auth_exchange,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "用配置中的授权码换取 refresh_token",
            },
            {
                "path": "/oauth/callback",
                "endpoint": self.api_oauth_callback,
                "methods": ["GET"],
                "auth": "bear",
                "allow_anonymous": True,
                "summary": "Google OAuth 回调",
            },
            {
                "path": "/logs/page",
                "endpoint": self.api_logs_page,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "切换操作日志页码",
            },
            {
                "path": "/logs/clear",
                "endpoint": self.api_logs_clear,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清空操作日志",
            },
        ]

    def api_test(self) -> Dict[str, Any]:
        """测试连接并刷新容量。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用", "data": None}
        if not self._client:
            return {"success": False, "message": "尚未配置完整的 OAuth 凭据", "data": None}
        try:
            info = self._client.check()
            quota = self._client.usage()
        except GoogleDriveError as err:
            logger.warning(f"【GDrive】连接测试失败：{err}")
            self._record_log("test", "", False, str(err))
            return {"success": False, "message": f"连接失败：{err}", "data": None}
        self._last_quota = {
            "total": quota.total,
            "available": quota.available,
            "usage": quota.usage,
            "checked_at": _now_text(),
        }
        self.save_data("quota", self._last_quota)
        self._record_log("test", "", True, "连接正常")
        user = info.get("user") or {}
        root = info.get("root") or {}
        return {
            "success": True,
            "message": "连接正常",
            "data": {
                "user": user.get("displayName") or user.get("emailAddress") or "",
                "root": root.get("path") or self._config.get("root_path"),
                "root_name": root.get("name") or "",
                "total": quota.total,
                "available": quota.available,
                "total_text": format_size(quota.total),
                "available_text": format_size(quota.available),
            },
        }

    def api_quota(self) -> Dict[str, Any]:
        """刷新容量并缓存。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用", "data": None}
        provider = self._provider_or_none()
        if not provider:
            return {"success": False, "message": "尚未配置完整的 OAuth 凭据", "data": None}
        usage = provider.usage()
        if not usage:
            self._record_log("quota", "", False, "容量查询失败")
            return {"success": False, "message": "容量查询失败，请检查授权", "data": None}
        self._last_quota = {
            "total": usage.total,
            "available": usage.available,
            "usage": max(0.0, usage.total - usage.available) if usage.total else 0.0,
            "checked_at": _now_text(),
        }
        self.save_data("quota", self._last_quota)
        self._record_log("quota", "", True, "")
        return {
            "success": True,
            "message": "容量已刷新",
            "data": {
                "total": usage.total,
                "available": usage.available,
                "total_text": format_size(int(usage.total)),
                "available_text": format_size(int(usage.available)),
            },
        }

    def api_auth_url(self) -> Dict[str, Any]:
        """返回 OAuth 授权链接。"""
        url = self._auth_url()
        if not url:
            return {"success": False, "message": "请先填写 client_id 和 redirect_uri", "data": None}
        state = (self.get_data("oauth_state") or {}).get("value", "")
        return {"success": True, "message": "", "data": {"authUrl": url, "state": state}}

    def api_auth_exchange(self) -> Dict[str, Any]:
        """使用配置页保存的授权码换取 refresh_token。"""
        code = str(self._config.get("oauth_code") or "").strip()
        if not code:
            return {"success": False, "message": "请先在配置页填写授权码并保存", "data": None}
        ok, message = self._exchange_code(code)
        return {"success": ok, "message": message, "data": None}

    def api_oauth_callback(
            self,
            code: str = "",
            state: str = "",
            error: str = "",
    ) -> Dict[str, Any]:
        """处理 Google OAuth 回调；必须校验一次性 state。"""
        if error:
            self._record_log("oauth", "", False, f"Google 返回错误：{error}")
            return {"success": False, "message": f"Google 授权失败：{error}", "data": None}
        saved = self.get_data("oauth_state") or {}
        if (
                not isinstance(saved, dict)
                or not state
                or state != saved.get("value")
                or float(saved.get("expires_at") or 0) < time.time()
        ):
            return {"success": False, "message": "授权状态校验失败或已过期，请重新生成授权链接", "data": None}
        ok, message = self._exchange_code(code)
        return {"success": ok, "message": message, "data": None}

    def api_logs_page(self, payload: dict = Body(...)) -> Dict[str, Any]:
        """切换操作日志页码。"""
        try:
            page = int((payload or {}).get("page") or 1)
        except (TypeError, ValueError):
            page = 1
        pages = self._log_pages()
        self._page = min(max(1, page), pages)
        return {
            "success": True,
            "message": f"第 {self._page} 页",
            "data": {"page": self._page},
        }

    def api_logs_clear(self) -> Dict[str, Any]:
        """清空操作日志。"""
        self._logs = []
        self._page = 1
        self.save_data("logs", [])
        return {"success": True, "message": "日志已清空", "data": None}

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def _record_log(self, action: str, path: str, ok: bool, detail: str = "") -> None:
        """追加一条操作日志并持久化，最多保留 MAX_LOGS 条。"""
        entry = {
            "time": _now_text(),
            "action": str(action)[:40],
            "path": str(path or "")[:300],
            "ok": bool(ok),
            "detail": str(detail or "")[:500],
        }
        self._logs.append(entry)
        if len(self._logs) > MAX_LOGS:
            self._logs = self._logs[-MAX_LOGS:]
        try:
            self.save_data("logs", self._logs)
        except Exception:
            logger.debug("【GDrive】保存操作日志失败", exc_info=True)

    def _log_pages(self) -> int:
        """返回日志总页数。"""
        return max(1, math.ceil(len(self._logs) / self._page_size))

    def _page_logs(self) -> List[Dict[str, Any]]:
        """返回当前页日志。"""
        pages = self._log_pages()
        self._page = min(max(1, self._page), pages)
        start = (self._page - 1) * self._page_size
        return list(reversed(self._logs))[start:start + self._page_size]

    # ------------------------------------------------------------------
    # 表单与页面
    # ------------------------------------------------------------------
    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回 Google Drive 配置表单和默认值。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "class": "mb-3",
                            "text": (
                                "先在 Google Cloud Console 启用 Google Drive API、创建 OAuth Web 客户端，"
                                "并把下方回调地址登记为授权重定向 URI。插件使用完整 drive 权限。"
                            ),
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "register_storage", "label": "自动注册系统存储"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "display_name",
                                        "label": "存储显示名",
                                        "placeholder": "Google Drive",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "client_id",
                                        "label": "OAuth client_id",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "client_secret",
                                        "label": "OAuth client_secret",
                                        "type": "password",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "refresh_token",
                                        "label": "refresh_token（可留空，通过授权链接获取）",
                                        "type": "password",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "oauth_code",
                                        "label": "临时授权码",
                                        "hint": "从回调页复制 code 后保存，再点详情页“用授权码换取令牌”",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "redirect_uri",
                            "label": "OAuth 回调地址",
                            "hint": "必须与 Google Cloud 控制台登记的重定向 URI 完全一致",
                            "persistent-hint": True,
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "root_folder_id",
                                        "label": "根目录 ID 或分享链接",
                                        "placeholder": "root 或文件夹 ID",
                                        "hint": "留空/root 表示我的云端硬盘根目录；共享盘可填共享盘文件夹 ID",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "root_path",
                                        "label": "虚拟根路径",
                                        "placeholder": "/",
                                        "hint": "默认为 /；填写 /GDrive 时对外挂载到该路径",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "use_shared_drive", "label": "使用共享盘"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "shared_drive_id",
                                        "label": "共享盘 ID",
                                        "hint": "共享盘 ID 不是文件夹 ID；可在共享盘 URL 中获取",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "delete_mode",
                                        "label": "删除方式",
                                        "items": [
                                            {"title": "移入回收站（推荐）", "value": "trash"},
                                            {"title": "永久删除", "value": "permanent"},
                                        ],
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "chunk_size_mb",
                                        "label": "上传分片大小（MB）",
                                        "type": "number",
                                        "min": 1,
                                        "max": 1024,
                                        "step": 1,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "timeout",
                                        "label": "HTTP 超时（秒）",
                                        "type": "number",
                                        "min": 5,
                                        "max": 600,
                                        "step": 5,
                                    },
                                }],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "display_name": DEFAULT_DISPLAY_NAME,
            "register_storage": True,
            "client_id": "",
            "client_secret": "",
            "refresh_token": "",
            "redirect_uri": self._default_redirect_uri(),
            "oauth_code": "",
            "root_folder_id": "root",
            "root_path": DEFAULT_ROOT_PATH,
            "use_shared_drive": False,
            "shared_drive_id": "",
            "delete_mode": DEFAULT_DELETE_MODE,
            "chunk_size_mb": DEFAULT_CHUNK_SIZE_MB,
            "timeout": DEFAULT_TIMEOUT,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回状态、容量、操作按钮和日志列表。"""
        contents: List[dict] = []
        if not self._enabled:
            contents.append(self._alert("warning", "插件未启用，以下为最近状态和日志。"))
        if not self._client:
            contents.append(self._alert(
                "warning",
                "尚未配置 OAuth 客户端：请填写 client_id、client_secret，"
                "并通过详情页授权链接或手动 refresh_token 完成授权。",
            ))
        elif not self._config.get("refresh_token"):
            contents.append(self._alert(
                "info",
                "OAuth 客户端已配置，但尚未授权：请打开下方授权链接，或填写 refresh_token/授权码。",
            ))
        else:
            contents.append(self._alert(
                "success",
                f"已配置 Google Drive 客户端；根路径 {self._config.get('root_path')}；"
                f"删除方式 {'永久删除' if self._config.get('delete_mode') == 'permanent' else '移入回收站'}。",
            ))
        registered = self._storage_registered()
        contents.append(self._alert(
            "info" if registered else "warning",
            f"系统存储：{'已注册 gdrive' if registered else '未注册 gdrive'}；"
            f"显示名：{self._display_name}；共享盘："
            f"{self._config.get('shared_drive_id') or '未设置'}。",
        ))
        quota_text = "容量尚未刷新，可点击“刷新容量”。"
        if self._last_quota:
            quota_text = (
                f"总容量 {format_size(int(self._last_quota.get('total') or 0))}，"
                f"剩余 {format_size(int(self._last_quota.get('available') or 0))}；"
                f"检查时间 {self._last_quota.get('checked_at') or '-'}。"
            )
        contents.append(self._alert("info", quota_text))

        buttons: List[dict] = [
            self._page_button("测试连接", "mdi-connection", "/test"),
            self._page_button("刷新容量", "mdi-cloud-refresh", "/quota"),
        ]
        auth_url = self._auth_url()
        if auth_url:
            buttons.append({
                "component": "VBtn",
                "props": {
                    "href": auth_url,
                    "target": "_blank",
                    "color": "primary",
                    "variant": "tonal",
                    "prepend-icon": "mdi-google",
                },
                "text": "打开 Google 授权页",
            })
        if self._config.get("oauth_code"):
            buttons.append(self._page_button("用授权码换取令牌", "mdi-key", "/auth/exchange"))
        buttons.append(self._page_button("清空日志", "mdi-broom", "/logs/clear"))
        contents.append({
            "component": "div",
            "props": {"class": "d-flex flex-wrap ga-2 mb-3 flex-shrink-0"},
            "content": buttons,
        })

        logs = self._page_logs()
        if logs:
            headers = ["时间", "操作", "路径", "结果", "详情"]
            ths = [{"component": "th", "text": header} for header in headers]
            rows = []
            for entry in logs:
                rows.append({
                    "component": "tr",
                    "content": [
                        self._text_cell(entry.get("time") or "-"),
                        self._text_cell(entry.get("action") or "-"),
                        self._text_cell(entry.get("path") or "-", title=entry.get("path")),
                        {
                            "component": "td",
                            "content": [{
                                "component": "VChip",
                                "props": {
                                    "color": "success" if entry.get("ok") else "error",
                                    "size": "small",
                                    "variant": "tonal",
                                },
                                "text": "成功" if entry.get("ok") else "失败",
                            }],
                        },
                        self._text_cell(entry.get("detail") or "-", title=entry.get("detail")),
                    ],
                })
            contents.append({
                "component": "div",
                "props": {"class": "flex-grow-1", "style": "min-height: 0; overflow: auto;"},
                "content": [{
                    "component": "VTable",
                    "props": {"density": "compact", "hover": True},
                    "content": [
                        {
                            "component": "thead",
                            "props": {
                                "style": (
                                    "position: sticky; top: 0; z-index: 1; "
                                    "background: rgb(var(--v-theme-surface));"
                                ),
                            },
                            "content": [{"component": "tr", "content": ths}],
                        },
                        {"component": "tbody", "content": rows},
                    ],
                }],
            })
        else:
            contents.append({
                "component": "div",
                "props": {"class": "flex-grow-1 d-flex align-center justify-center", "style": "min-height: 0;"},
                "content": [self._alert("info", "暂无操作日志。")],
            })

        pages = self._log_pages()
        contents.append({
            "component": "div",
            "props": {"class": "d-flex align-center justify-center ga-2 mt-2 flex-shrink-0"},
            "content": [
                self._page_button("上一页", "mdi-chevron-left", "/logs/page", {"page": self._page - 1}, disabled=self._page <= 1),
                {"component": "span", "props": {"class": "text-body-2"}, "text": f"第 {self._page} / {pages} 页"},
                self._page_button("下一页", "mdi-chevron-right", "/logs/page", {"page": self._page + 1}, disabled=self._page >= pages),
            ],
        })

        return [{
            "component": "div",
            "props": {
                "class": "d-flex flex-column",
                "style": (
                    "height: 72vh; min-height: 520px; max-height: 800px; "
                    "padding-bottom: 84px; box-sizing: border-box;"
                ),
            },
            "content": contents,
        }]

    @staticmethod
    def _alert(level: str, text: str) -> dict:
        """构建固定高度容器内不会被压扁的告警。"""
        return {
            "component": "VAlert",
            "props": {
                "type": level,
                "variant": "tonal",
                "class": "mb-3",
                "style": "flex: 0 0 auto;",
                "text": text,
            },
        }

    def _page_button(
            self,
            text: str,
            icon: str,
            path: str,
            params: Optional[Dict[str, Any]] = None,
            disabled: bool = False,
    ) -> dict:
        """构建调用插件 API 的页面按钮。"""
        return {
            "component": "VBtn",
            "props": {
                "variant": "tonal",
                "color": "primary",
                "prepend-icon": icon,
                "disabled": bool(disabled),
            },
            "text": text,
            "events": {
                "click": {
                    "api": f"plugin/{self.__class__.__name__}{path}",
                    "method": "post",
                    "params": params or {},
                },
            },
        }

    @staticmethod
    def _text_cell(text: Any, title: Any = None) -> dict:
        """构建截断显示的表格单元格。"""
        value = str(text or "-")
        title_value = str(title or value)
        return {
            "component": "td",
            "props": {
                "style": "max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;",
                "title": title_value,
            },
            "text": value,
        }
