"""手动扫描结果的短时内存缓存，减轻切库重复等待。"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional


class ScanCache:
    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, list[dict[str, Any]], list[str]]] = {}

    @staticmethod
    def key(server: str, library_id: str) -> str:
        return f"{server}|{library_id}"

    def get(self, server: str, library_id: str) -> Optional[tuple[list[dict[str, Any]], list[str]]]:
        cache_key = self.key(server, library_id)
        now = time.monotonic()
        with self._lock:
            hit = self._items.get(cache_key)
            if not hit:
                return None
            stamped, rows, errors = hit
            if now - stamped > self._ttl:
                self._items.pop(cache_key, None)
                return None
            return list(rows), list(errors)

    def put(self, server: str, library_id: str, rows: list[dict[str, Any]], errors: list[str]) -> None:
        with self._lock:
            self._items[self.key(server, library_id)] = (time.monotonic(), list(rows), list(errors))

    def drop(self, server: str, library_id: str) -> None:
        with self._lock:
            self._items.pop(self.key(server, library_id), None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
