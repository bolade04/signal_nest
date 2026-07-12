"""Cache adapter. In-memory by default; Redis in full mode."""

from __future__ import annotations

import time
from typing import Any, Protocol

from app.core.config import get_settings


class Cache(Protocol):
    def get(self, key: str) -> Any | None: ...
    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None: ...
    def delete(self, key: str) -> None: ...


class InMemoryCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float | None, Any]] = {}

    def get(self, key: str) -> Any | None:
        item = self._store.get(key)
        if not item:
            return None
        expires, value = item
        if expires is not None and expires < time.time():
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        expires = time.time() + ttl_seconds if ttl_seconds else None
        self._store[key] = (expires, value)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)


def build_cache() -> Cache:
    settings = get_settings()
    if settings.cache_backend == "redis":  # pragma: no cover - full mode only
        import json

        import redis

        client = redis.from_url(settings.redis_url)  # type: ignore[arg-type]

        class RedisCache:
            def get(self, key: str) -> Any | None:
                raw = client.get(key)
                return json.loads(raw) if raw else None

            def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
                client.set(key, json.dumps(value), ex=ttl_seconds)

            def delete(self, key: str) -> None:
                client.delete(key)

        return RedisCache()
    return InMemoryCache()


cache: Cache = build_cache()
