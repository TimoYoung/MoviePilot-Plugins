"""智能清理使用的内存结构。规则、媒体库自动化和白名单都以可序列化的字典保存。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional


STATE_VERSION = 3


def empty_state() -> dict[str, Any]:
    """插件数据的初始文档。"""
    return {
        "version": STATE_VERSION,
        "libraries": [],
        "rules": [],
        "whitelist": {"manual": []},
    }


@dataclass
class Play:
    """一条播放事实。次数或时间缺失时保持 None，筛选会把它当成信息不足。"""

    play_count: Optional[int] = None
    last_played: Optional[datetime] = None


@dataclass
class Candidate:
    """一部电影，或一部剧的一季。"""

    key: str
    kind: str
    server: str
    server_type: str
    library_id: str
    library_name: str
    item_id: str
    title: str
    year: str = ""
    season: Optional[int] = None
    tmdb: str = ""
    imdb: str = ""
    douban: str = ""
    added_at: Optional[datetime] = None
    favorite: bool = False
    path: str = ""
    plays: list[Play] = field(default_factory=list)
    episode_ids: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        """冻结清单要保留的删除线索。播放时间写成字符串，方便直接入库。"""
        return {
            "key": self.key,
            "kind": self.kind,
            "server": self.server,
            "server_type": self.server_type,
            "library_id": self.library_id,
            "library_name": self.library_name,
            "item_id": self.item_id,
            "title": self.title,
            "year": self.year,
            "season": self.season,
            "tmdb": self.tmdb,
            "imdb": self.imdb,
            "douban": self.douban,
            "favorite": self.favorite,
            "path": self.path,
            "episode_ids": list(self.episode_ids),
        }

    def public(self) -> dict[str, Any]:
        """页面展示用的字段。"""
        data = asdict(self)
        data["added_at"] = self.added_at.isoformat() if self.added_at else ""
        data["plays"] = [
            {
                "play_count": play.play_count,
                "last_played": play.last_played.isoformat() if play.last_played else "",
            }
            for play in self.plays
        ]
        return data


@dataclass
class Subject:
    """豆瓣条目：看过列表或导入榜单里的一条。"""

    douban_id: str
    title: str
    year: str = ""
    season: Optional[int] = None
    ambiguous: bool = False
    stars: Optional[int] = None
    watched_on: Optional[datetime] = None
    imdb: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "douban_id": self.douban_id,
            "title": self.title,
            "year": self.year,
            "season": self.season,
            "stars": self.stars,
            "watched_on": self.watched_on.date().isoformat() if self.watched_on else "",
            "imdb": self.imdb,
        }


@dataclass
class TransferFact:
    """一条能对上用户的入库记录。季号集合为空表示电影。"""

    username: str
    transferred_at: datetime
    source: str
    media_id: str
    seasons: set[int] = field(default_factory=set)
    fileitem: Optional[dict[str, Any]] = None

    @property
    def path_key(self) -> tuple[str, str]:
        item = self.fileitem or {}
        return str(item.get("storage") or "local"), str(item.get("path") or "")
