"""GoogleDriveClient 的无网络单元测试。"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CLIENT_PATH = REPO_ROOT / "plugins.v3" / "gdrivestorage" / "gdrive_client.py"


def _load_client_module():
    """按文件路径加载客户端模块，避免依赖 MoviePilot 宿主包。"""
    spec = importlib.util.spec_from_file_location("gdrive_client_under_test", CLIENT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


client_module = _load_client_module()
GoogleDriveClient = client_module.GoogleDriveClient
GoogleDriveError = client_module.GoogleDriveError
GoogleDriveAuthError = client_module.GoogleDriveAuthError
DriveQuota = client_module.DriveQuota


class FakeResponse:
    """最小响应对象，覆盖客户端实际读取的属性。"""

    def __init__(
            self,
            status_code: int = 200,
            payload: Optional[Dict[str, Any]] = None,
            headers: Optional[Dict[str, str]] = None,
            content: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.content = content

    def json(self) -> Dict[str, Any]:
        """返回预设 JSON；未设置时抛错以验证容错。"""
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    @property
    def text(self) -> str:
        """返回文本内容。"""
        return self.content.decode("utf-8", errors="replace")

    def iter_bytes(self, chunk_size: int = 0) -> Iterable[bytes]:
        """按块返回预设内容。"""
        if not self.content:
            return
        step = chunk_size if chunk_size and chunk_size > 0 else len(self.content)
        for offset in range(0, len(self.content), step):
            yield self.content[offset:offset + step]


class FakeStream:
    """流式响应上下文。"""

    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def __enter__(self) -> FakeResponse:
        return self.response

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeTransport:
    """按顺序返回预设响应并记录调用。"""

    def __init__(self, responses: List[Any]) -> None:
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        """记录并返回下一个响应。"""
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def stream(self, method: str, url: str, **kwargs: Any) -> FakeStream:
        """记录并返回下一个流式响应。"""
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"unexpected stream: {method} {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return FakeStream(response)


def _client(
        transport: FakeTransport,
        *,
        seed_token: bool = True,
        **overrides: Any,
) -> Any:
    """构造带测试传输层的客户端；默认预置有效 access_token。"""
    params = {
        "client_id": "cid",
        "client_secret": "secret",
        "refresh_token": "refresh",
        "transport": transport,
        "sleep": lambda _seconds: None,
    }
    params.update(overrides)
    client = GoogleDriveClient(**params)
    if seed_token:
        client._access_token = "seeded-token"
        client._expires_at = time.monotonic() + 3600
    return client


def _file_payload(
        file_id: str = "f1",
        name: str = "movie.mkv",
        mime: str = "video/x-matroska",
        parents: Optional[List[str]] = None,
        size: str = "1024",
) -> Dict[str, Any]:
    """构造 Drive File JSON。"""
    return {
        "id": file_id,
        "name": name,
        "mimeType": mime,
        "parents": parents or ["root"],
        "size": size,
        "modifiedTime": "2024-01-01T00:00:00.000Z",
    }


def test_build_auth_url_contains_offline_and_drive_scope() -> None:
    """授权 URL 必须包含 offline/consent/full drive scope。"""
    transport = FakeTransport([])
    url = _client(transport).build_auth_url("http://localhost/cb", "state-1")
    assert "accounts.google.com/o/oauth2/v2/auth" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive" in url
    assert "state=state-1" in url


def test_refresh_access_token_is_cached() -> None:
    """重复刷新在有效期内只发一次 token 请求。"""
    transport = FakeTransport([
        FakeResponse(payload={"access_token": "a1", "expires_in": 3600}),
    ])
    client = _client(transport, seed_token=False)
    assert client.refresh_access_token() == "a1"
    assert client.refresh_access_token() == "a1"
    assert len(transport.calls) == 1
    assert transport.calls[0]["data"]["grant_type"] == "refresh_token"


def test_request_refreshes_on_401_and_retries() -> None:
    """401 后刷新一次并重试原请求。"""
    transport = FakeTransport([
        FakeResponse(status_code=401, payload={"error": {"message": "expired"}}),
        FakeResponse(payload={"access_token": "a2", "expires_in": 3600}),
        FakeResponse(payload={"storageQuota": {"limit": "1000", "usage": "250"}}),
    ])
    quota = _client(transport).usage()
    assert isinstance(quota, DriveQuota)
    assert quota.total == 1000
    assert quota.available == 750
    assert len(transport.calls) == 3


def test_normalize_virtual_path() -> None:
    """路径规范化剥离 gdrive 前缀并拒绝越界。"""
    assert client_module.GoogleDriveClient.normalize_virtual_path("gdrive:/Movies/a.mkv") == "/Movies/a.mkv"
    assert client_module.GoogleDriveClient.normalize_virtual_path("/a/./b") == "/a/b"
    with pytest.raises(ValueError):
        client_module.GoogleDriveClient.normalize_virtual_path("/a/../b")
    with pytest.raises(ValueError):
        client_module.GoogleDriveClient.normalize_virtual_path("C:/Movies")


def test_relative_parts_respects_mount_point() -> None:
    """挂载点之外的路径必须拒绝。"""
    client = _client(FakeTransport([]), root_path="/GDrive")
    assert client._relative_parts("/GDrive/Movies") == ["Movies"]
    assert client._relative_parts("/GDrive") == []
    with pytest.raises(ValueError):
        client._relative_parts("/Other/Movies")


def test_list_maps_virtual_root_to_mount_point() -> None:
    """从虚拟根 "/" 列举时，子项路径仍落在配置的挂载点下。"""
    transport = FakeTransport([
        FakeResponse(payload={"files": [{
            "id": "dir-1",
            "name": "Movies",
            "mimeType": client_module.FOLDER_MIME,
            "parents": ["root"],
        }], "nextPageToken": None}),
    ])
    items = _client(transport, root_path="/GDrive").list("root", "/")
    assert len(items) == 1
    assert items[0].path == "/GDrive/Movies/"


def test_list_builds_query_and_maps_item() -> None:
    """list 构造父目录查询并映射虚拟路径。"""
    transport = FakeTransport([
        FakeResponse(payload={"files": [_file_payload()], "nextPageToken": None}),
    ])
    items = _client(transport).list("folder-1", "/Movies")
    assert len(items) == 1
    item = items[0]
    assert item.id == "f1"
    assert item.path == "/Movies/movie.mkv"
    assert item.size == 1024
    assert item.modified_time is not None
    call = transport.calls[0]
    assert call["params"]["q"] == "'folder-1' in parents and trashed = false"
    assert "files(id,name,mimeType" in call["params"]["fields"]


def test_get_item_resolves_nested_path() -> None:
    """按路径查询时逐段解析目录并返回文件。"""
    transport = FakeTransport([
        # 解析 /Movies 目录
        FakeResponse(payload={"files": [{
            "id": "dir-1",
            "name": "Movies",
            "mimeType": client_module.FOLDER_MIME,
            "parents": ["root"],
        }]}),
        # 查询 /Movies/a.mkv
        FakeResponse(payload={"files": [_file_payload("file-1", "a.mkv", parents=["dir-1"])]}),
    ])
    item = _client(transport).get_item("/Movies/a.mkv")
    assert item is not None
    assert item.id == "file-1"
    assert item.path == "/Movies/a.mkv"
    assert transport.calls[0]["params"]["q"].startswith("name = 'Movies'")
    assert "'root' in parents" in transport.calls[0]["params"]["q"]
    assert transport.calls[1]["params"]["q"].startswith("name = 'a.mkv'")


def test_get_item_returns_none_when_missing() -> None:
    """路径不存在时普通查询返回 None。"""
    transport = FakeTransport([
        FakeResponse(payload={"files": []}),
    ])
    assert _client(transport).get_item("/missing.mkv") is None


def test_create_folder_returns_existing() -> None:
    """同名目录已存在时不再创建。"""
    transport = FakeTransport([
        FakeResponse(payload={"files": [{
            "id": "dir-1",
            "name": "Movies",
            "mimeType": client_module.FOLDER_MIME,
            "parents": ["root"],
        }]}),
    ])
    item = _client(transport).create_folder("root", "/", "Movies")
    assert item is not None
    assert item.id == "dir-1"
    assert item.path == "/Movies/"
    assert len(transport.calls) == 1


def test_create_folder_maps_root_to_mount_point() -> None:
    """从虚拟根 "/" 创建目录时，返回路径落在挂载点下。"""
    transport = FakeTransport([
        FakeResponse(payload={"files": [{
            "id": "dir-1",
            "name": "Movies",
            "mimeType": client_module.FOLDER_MIME,
            "parents": ["root"],
        }]}),
    ])
    item = _client(transport, root_path="/GDrive").create_folder("root", "/", "Movies")
    assert item is not None
    assert item.path == "/GDrive/Movies/"


def test_upload_resumable_chunks(tmp_path: Path) -> None:
    """分片上传先 308 再完成，并返回文件项。"""
    local = tmp_path / "movie.mkv"
    local.write_bytes(os.urandom(300 * 1024))
    transport = FakeTransport([
        FakeResponse(status_code=200, headers={"Location": "https://upload/session"}, payload={}),
        FakeResponse(status_code=308, payload={}),
        FakeResponse(status_code=200, payload=_file_payload("uploaded", "movie.mkv", size="307200")),
    ])
    client = _client(transport, chunk_size=256 * 1024)
    item = client.upload("folder-1", "/Movies", local)
    assert item is not None
    assert item.id == "uploaded"
    assert item.path == "/Movies/movie.mkv"
    assert len(transport.calls) == 3
    assert transport.calls[0]["headers"]["X-Upload-Content-Length"] == str(300 * 1024)
    assert transport.calls[1]["headers"]["Content-Range"] == f"bytes 0-{256 * 1024 - 1}/{300 * 1024}"
    assert transport.calls[2]["headers"]["Content-Range"].startswith("bytes 262144-")


def test_upload_maps_root_to_mount_point(tmp_path: Path) -> None:
    """从虚拟根 "/" 上传时，返回路径落在挂载点下。"""
    local = tmp_path / "movie.mkv"
    local.write_bytes(b"x" * 1024)
    transport = FakeTransport([
        FakeResponse(status_code=200, headers={"Location": "https://upload/session"}, payload={}),
        FakeResponse(status_code=200, payload=_file_payload("uploaded", "movie.mkv", size="1024")),
    ])
    item = _client(transport, root_path="/GDrive").upload("folder-1", "/", local)
    assert item is not None
    assert item.path == "/GDrive/movie.mkv"


def test_move_and_copy_parameters() -> None:
    """移动/复制使用正确的父目录和名称参数。"""
    transport = FakeTransport([
        FakeResponse(payload={"id": "f1", "name": "new.mkv", "parents": ["dir-2"]}),
        FakeResponse(payload={"id": "f2", "name": "copy.mkv"}),
    ])
    client = _client(transport)
    assert client.move("f1", "dir-1", "dir-2", "new.mkv") is True
    assert transport.calls[0]["params"]["addParents"] == "dir-2"
    assert transport.calls[0]["params"]["removeParents"] == "dir-1"
    assert transport.calls[0]["json"] == {"name": "new.mkv"}
    assert client.copy("f1", "dir-2", "copy.mkv") is True
    assert transport.calls[1]["url"].endswith("/files/f1/copy")
    assert transport.calls[1]["json"] == {"name": "copy.mkv", "parents": ["dir-2"]}


def test_delete_modes() -> None:
    """删除模式分别使用 PATCH trash 和 DELETE。"""
    trash_transport = FakeTransport([FakeResponse(payload={"id": "f1", "trashed": True})])
    assert _client(trash_transport, delete_mode="trash").delete("f1") is True
    assert trash_transport.calls[0]["method"] == "PATCH"
    assert trash_transport.calls[0]["json"] == {"trashed": True}

    permanent_transport = FakeTransport([FakeResponse(status_code=204)])
    assert _client(permanent_transport, delete_mode="permanent").delete("f1") is True
    assert permanent_transport.calls[0]["method"] == "DELETE"


def test_retry_on_429() -> None:
    """429 使用退避重试。"""
    sleeps: List[float] = []
    transport = FakeTransport([
        FakeResponse(status_code=429, payload={"error": {"message": "rate"}}, headers={"Retry-After": "1"}),
        FakeResponse(payload={"storageQuota": {"limit": "10", "usage": "1"}}),
    ])
    client = _client(transport, sleep=lambda seconds: sleeps.append(seconds))
    quota = client.usage()
    assert quota.total == 10
    assert sleeps == [1.0]


def test_download_binary_and_google_doc(tmp_path: Path) -> None:
    """二进制走 alt=media，Google 文档走 export。"""
    binary_transport = FakeTransport([FakeResponse(content=b"abc")])
    binary_path = _client(binary_transport).download("f1", "a.mkv", "video/x-matroska", tmp_path)
    assert binary_path is not None
    assert binary_path.read_bytes() == b"abc"
    assert binary_transport.calls[0]["params"]["alt"] == "media"

    doc_transport = FakeTransport([FakeResponse(content=b"pdf")])
    doc_path = _client(doc_transport).download(
        "doc1", "report", "application/vnd.google-apps.document", tmp_path
    )
    assert doc_path is not None
    assert doc_transport.calls[0]["url"].endswith("/files/doc1/export")
    assert doc_transport.calls[0]["params"]["mimeType"] == "application/pdf"


def test_auth_error_without_refresh_token() -> None:
    """缺少 refresh_token 时给出认证错误。"""
    client = _client(FakeTransport([]), seed_token=False, refresh_token="")
    with pytest.raises(GoogleDriveAuthError):
        client.refresh_access_token()
