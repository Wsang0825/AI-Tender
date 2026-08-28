"""基于 DiskCache 的本地缓存，不引入 Redis。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from diskcache import Cache

from tender_ai.config_loader import APP_ROOT


class DiskCache:
    """为 HTTP、搜索、PDF、哈希和短期失败记录提供统一命名空间。"""

    def __init__(self, directory: str | Path | None = None, *, default_expire: float | None = None):
        target = Path(directory) if directory else APP_ROOT.parent / "data" / "cache"
        target.mkdir(parents=True, exist_ok=True)
        self.directory = target
        self.default_expire = default_expire
        self._cache = Cache(str(target))

    @staticmethod
    def make_key(namespace: str, value: Any) -> str:
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"{namespace}:{digest}"

    def get(self, namespace: str, value: Any, default: Any = None) -> Any:
        return self._cache.get(self.make_key(namespace, value), default=default)

    def set(self, namespace: str, value: Any, result: Any, *, expire: float | None = None) -> None:
        self._cache.set(self.make_key(namespace, value), result, expire=self.default_expire if expire is None else expire)

    def remember_short_failure(self, namespace: str, value: Any, message: str, *, expire: float = 300) -> None:
        self.set(f"failure:{namespace}", value, {"message": message}, expire=expire)

    def close(self) -> None:
        self._cache.close()
