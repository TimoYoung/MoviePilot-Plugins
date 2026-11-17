"""读取 Emby / Jellyfin 媒体库。凭据留在宿主实例里，插件只发带占位符的请求。"""

from __future__ import annotations

from typing import Any, Iterator, Optional
from urllib.parse import quote, urlencode

from .models import Candidate
from .scan import build_movie, build_seasons

FIELDS = (
    "ProviderIds,ProductionYear,DateCreated,Path,UserData,"
    "UserDataPlayCount,UserDataLastPlayedDate,"
    "ParentId,IndexNumber,ParentIndexNumber,SeriesId"
)
PAGE_SIZE = 200


class LibraryError(RuntimeError):
    """媒体库请求失败。"""


def prefix(server_type: str) -> str:
    return "emby/" if server_type == "emby" else ""


class LibraryReader:
    """分页读取一个已连接的媒体服务器。"""

    def __init__(self, name: str, server_type: str, instance: Any, page_size: int = PAGE_SIZE) -> None:
        self.name = name
        self.server_type = server_type
        self._instance = instance
        self._page_size = page_size

    def _user_id(self) -> Optional[str]:
        """播放/收藏等 UserData 必须挂在具体用户下，优先用宿主已选用户。"""
        user = getattr(self._instance, "user", None)
        text = str(user or "").strip()
        return text or None

    def _items_path(self) -> str:
        user = self._user_id()
        if user:
            return f"Users/{quote(user, safe='')}/Items"
        return "Items"

    def _request(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        query = urlencode({key: value for key, value in (params or {}).items() if value is not None})
        url = f"[HOST]{prefix(self.server_type)}{path}?{query + '&' if query else ''}api_key=[APIKEY]"
        response = self._instance.get_data(url)
        if response is None:
            raise LibraryError(f"{self.name} 请求 {path} 无响应")
        try:
            status = getattr(response, "status_code", 200)
            if status >= 300:
                raise LibraryError(f"{self.name} 请求 {path} 返回 HTTP {status}")
            try:
                return response.json()
            except ValueError as err:
                raise LibraryError(f"{self.name} 请求 {path} 返回内容无法解析") from err
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    def libraries(self) -> list[dict[str, Any]]:
        data = self._request("Library/VirtualFolders")
        return [item for item in data or [] if isinstance(item, dict)]

    def iter_items(self, parent_id: str, item_types: str) -> Iterator[dict[str, Any]]:
        start = 0
        seen: set[str] = set()
        while True:
            data = self._request(self._items_path(), {
                "ParentId": parent_id,
                "Recursive": "true",
                "IncludeItemTypes": item_types,
                "Fields": FIELDS,
                "StartIndex": start,
                "Limit": self._page_size,
                "SortBy": "SortName",
                "EnableImages": "false",
                "EnableTotalRecordCount": "false",
                "CollapseBoxSetItems": "false",
            })
            items = [item for item in (data or {}).get("Items") or [] if isinstance(item, dict)]
            fresh = [item for item in items if str(item.get("Id") or "") not in seen]
            seen.update(str(item.get("Id") or "") for item in fresh)
            yield from fresh
            if len(items) < self._page_size or not fresh:
                return
            start += self._page_size

    def episodes(self, series_id: str) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "Fields": FIELDS,
            "IsMissing": "false",
            "EnableImages": "false",
        }
        user = self._user_id()
        if user:
            params["userId"] = user
        data = self._request(f"Shows/{quote(series_id, safe='')}/Episodes", params)
        return [item for item in (data or {}).get("Items") or [] if isinstance(item, dict)]

    def seasons(self, series_id: str) -> list[dict[str, Any]]:
        data = self._request(self._items_path(), {
            "ParentId": series_id,
            "IncludeItemTypes": "Season",
            "Fields": FIELDS,
            "EnableImages": "false",
        })
        rows = []
        for item in (data or {}).get("Items") or []:
            if isinstance(item, dict):
                item.setdefault("SeriesId", series_id)
                rows.append(item)
        return rows


def library_catalog(reader: LibraryReader) -> list[dict[str, str]]:
    """列出电影库和剧集库，供创建任务时点选。"""
    catalog = []
    for library in reader.libraries():
        collection = str(library.get("CollectionType") or "").lower()
        if collection not in {"movies", "tvshows"}:
            continue
        library_id = str(library.get("ItemId") or library.get("Id") or "")
        name = str(library.get("Name") or "")
        if library_id and name:
            catalog.append({
                "server": reader.name,
                "server_type": reader.server_type,
                "library_id": library_id,
                "library_name": name,
                "library_kind": "movie" if collection == "movies" else "tv",
            })
    return catalog


def scan_library(reader: LibraryReader, task: dict[str, Any]) -> tuple[list[Candidate], list[str]]:
    """扫描任务对应的一个媒体库。"""
    library_id = str(task.get("library_id") or "")
    kind = task.get("library_kind")
    errors: list[str] = []
    try:
        if kind == "movie":
            candidates = []
            for item in reader.iter_items(library_id, "Movie"):
                candidate = build_movie(
                    item,
                    server=reader.name,
                    server_type=reader.server_type,
                    library_id=library_id,
                    library_name=str(task.get("library_name") or ""),
                )
                if candidate:
                    candidates.append(candidate)
            return candidates, errors
        series = list(reader.iter_items(library_id, "Series"))
        episodes: list[dict[str, Any]] = []
        seasons: list[dict[str, Any]] = []
        for item in series:
            series_id = str(item.get("Id") or "")
            if not series_id:
                continue
            try:
                episodes.extend(reader.episodes(series_id))
                seasons.extend(reader.seasons(series_id))
            except LibraryError as err:
                errors.append(f"{item.get('Name') or series_id}：{err}")
        return build_seasons(
            series,
            episodes,
            seasons,
            server=reader.name,
            server_type=reader.server_type,
            library_id=library_id,
            library_name=str(task.get("library_name") or ""),
        ), errors
    except LibraryError as err:
        return [], [str(err)]
