"""Cache adapter. In-memory by default; hardened Redis in full mode.

Both backends share one contract:

* **Namespaced keys.** Every key is prefixed with ``settings.redis_key_prefix``
  (Redis) so multiple apps/environments can share an instance without collisions.
* **Tenant-aware keys.** :func:`tenant_cache_key` builds a key from
  ``organization_id`` + ``workspace_id`` + parts so cache entries are scoped to a
  tenant; a tenant can never read another tenant's cache entry by key.
* **Miss vs. cached-falsey are distinguishable.** Values are stored wrapped, so a
  cached ``0`` / ``False`` / ``""`` / ``None`` round-trips as that value while an
  absent key returns the caller's ``default`` (default :data:`MISS`/``None``).
* **Safe serialization.** JSON only — never :mod:`pickle` (which would allow
  arbitrary-code execution on a poisoned cache entry).
* **Bounded + sanitized.** The Redis client uses a bounded connection pool and
  socket timeout; a driver error becomes a :class:`RedisUnavailableError` whose
  message is static (no URL, password or key is ever logged or surfaced).
"""

from __future__ import annotations

import json
import time
from typing import Any, Final, Protocol

from app.core.config import Settings, get_settings
from app.core.errors import RedisUnavailableError
from app.core.tracing import REDIS_CACHE, start_span


class _Miss:
    """Singleton sentinel meaning 'no cache entry' (distinct from a cached None)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "MISS"


#: Returned by ``get`` when a key is absent and no other ``default`` was given.
MISS: Final[_Miss] = _Miss()


def _encode_key_component(component: str) -> str:
    """Escape the ``:`` delimiter so a component can never forge a key boundary.

    ``%`` is escaped first (so the escaping is unambiguous and reversible), then the
    ``:`` delimiter. Because a raw ``:`` can no longer appear inside an encoded
    component, joining components with ``:`` stays injective: distinct component
    tuples always map to distinct keys. Without this, ``("a:b")`` and ``("a", "b")``
    would collide and one tenant could address another tenant's entry. Components
    with no ``:`` or ``%`` (ordinary ids) are returned unchanged.
    """
    return component.replace("%", "%25").replace(":", "%3A")


def tenant_cache_key(organization_id: str, workspace_id: str, *parts: str) -> str:
    """Build a tenant-scoped cache key. Parts are joined under the tenant scope.

    The tenant segment is derived server-side from the caller's context, never
    from client input, so one tenant cannot address another tenant's entries. Every
    component is delimiter-encoded so a value containing ``:`` cannot collide with a
    differently-split component tuple.
    """
    components = ["t", organization_id, workspace_id, *parts]
    return ":".join(_encode_key_component(c) for c in components)


def _validate_ttl(ttl_seconds: int | None) -> None:
    if ttl_seconds is not None and ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be a positive integer or None")


class Cache(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...
    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None: ...
    def delete(self, key: str) -> None: ...
    def ping(self) -> bool: ...
    def close(self) -> None: ...


class InMemoryCache:
    """Process-local cache. Mirrors the Redis contract (wrapping, TTL, sentinel)."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float | None, Any]] = {}

    def get(self, key: str, default: Any = None) -> Any:
        item = self._store.get(key)
        if item is None:
            return default
        expires, value = item
        if expires is not None and expires < time.time():
            self._store.pop(key, None)
            return default
        return value

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        _validate_ttl(ttl_seconds)
        expires = time.time() + ttl_seconds if ttl_seconds else None
        self._store[key] = (expires, value)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        self._store.clear()


class RedisCache:
    """Hardened Redis-backed cache.

    Constructed with an already-built client so it is unit-testable with
    ``fakeredis`` and never imports the driver at test time. Values are wrapped
    (``{"v": value}``) and JSON-encoded; every driver error is converted to a
    sanitized :class:`RedisUnavailableError`.
    """

    def __init__(self, client: Any, *, key_prefix: str) -> None:
        self._client = client
        self._prefix = key_prefix

    def _redis_key(self, key: str) -> str:
        return f"{self._prefix}:cache:{key}"

    def get(self, key: str, default: Any = None) -> Any:
        # Dependency span carries only the bounded operation + outcome — never the
        # key, value or Redis URL.
        with start_span(
            REDIS_CACHE,
            kind="client",
            attributes={"component": "cache", "dependency": "redis", "operation": "get"},
        ) as span:
            try:
                raw = self._client.get(self._redis_key(key))
            except Exception as exc:  # redis.RedisError and friends
                raise RedisUnavailableError() from exc
            if raw is None:
                span.set_attribute("outcome", "miss")
                return default
            span.set_attribute("outcome", "hit")
            try:
                return json.loads(raw)["v"]
            except (ValueError, KeyError, TypeError):
                # A corrupt/foreign entry is treated as a miss rather than crashing a
                # request; it will be overwritten on the next set.
                return default

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        _validate_ttl(ttl_seconds)
        payload = json.dumps({"v": value})
        with start_span(
            REDIS_CACHE,
            kind="client",
            attributes={"component": "cache", "dependency": "redis", "operation": "set"},
        ) as span:
            try:
                self._client.set(self._redis_key(key), payload, ex=ttl_seconds)
            except Exception as exc:
                raise RedisUnavailableError() from exc
            span.set_attribute("outcome", "success")

    def delete(self, key: str) -> None:
        with start_span(
            REDIS_CACHE,
            kind="client",
            attributes={"component": "cache", "dependency": "redis", "operation": "delete"},
        ) as span:
            try:
                self._client.delete(self._redis_key(key))
            except Exception as exc:
                raise RedisUnavailableError() from exc
            span.set_attribute("outcome", "success")

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:
            # Readiness never raises; a failed ping is reported as not-ready.
            return False

    def close(self) -> None:
        # Release the pool without raising during shutdown.
        try:
            close = getattr(self._client, "close", None)
            if close is not None:
                close()
            pool = getattr(self._client, "connection_pool", None)
            if pool is not None:
                pool.disconnect()
        except Exception:  # pragma: no cover - best-effort shutdown
            pass


def build_redis_client(settings: Settings) -> Any:  # pragma: no cover - full mode only
    """Build a bounded, timeout-guarded Redis client from settings.

    The URL/password live only inside the client; they are never logged. Import
    is lazy so local mode never requires the ``redis`` package.
    """
    import redis

    pool = redis.ConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_pool_size,
        socket_timeout=settings.redis_operation_timeout_seconds,
        socket_connect_timeout=settings.redis_operation_timeout_seconds,
    )
    return redis.Redis(connection_pool=pool)


def build_cache(settings: Settings | None = None) -> Cache:
    settings = settings or get_settings()
    if settings.cache_backend == "redis":  # pragma: no cover - full mode only
        client = build_redis_client(settings)
        return RedisCache(client, key_prefix=settings.redis_key_prefix)
    return InMemoryCache()


cache: Cache = build_cache()
