"""Request correlation, security headers + the request rate limiter."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from logging import WARNING
from typing import Any, Literal

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.log_context import bound_context, new_request_id, normalize_request_id
from app.core.logging import get_logger, log_event
from app.core.metrics import (
    DEPENDENCY_OPERATION_TOTAL,
    HTTP_REQUEST_DURATION_MS,
    HTTP_REQUESTS_TOTAL,
    get_metrics,
)
from app.core.tracing import HTTP_REQUEST, extract_context, start_span

logger = get_logger("signalnest.request")
rate_limit_logger = get_logger("signalnest.rate_limit")


def _status_outcome(status_code: int) -> str:
    if status_code >= 500:
        return "server_error"
    if status_code >= 400:
        return "client_error"
    return "success"


def _status_class(status_code: int) -> str:
    # Bounded label: the status *class* only, never the raw code or the path.
    return f"{status_code // 100}xx"


def _route_template(request: Request) -> str:
    """A bounded, low-cardinality route template for the matched route.

    Uses the matched route's path *format* (``/api/v1/jobs/{job_id}``), which is
    populated in the request scope after routing. Falls back to the method-only
    label ``"<unmatched>"`` when nothing matched, so a raw, id-bearing path (high
    cardinality, potentially identifying) is never used as a span attribute.
    """
    route = request.scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    return template if isinstance(template, str) and template else "<unmatched>"


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Attach a bounded, validated request id to request-local context.

    An inbound ``x-request-id`` (or ``x-trace-id``) is accepted only when it matches
    the strict opaque format; anything else is discarded and a fresh id is generated,
    so a client can never inject an arbitrary/oversized/newline-bearing id into logs.
    The context is set for the duration of the request and **reset on exit**
    (``bound_context``), guaranteeing no cross-request contamination even on error.
    """

    async def dispatch(self, request: Request, call_next):
        rid = normalize_request_id(request.headers.get("x-request-id")) or new_request_id()
        tid = normalize_request_id(request.headers.get("x-trace-id")) or rid
        # A valid inbound W3C traceparent becomes the remote parent; anything
        # malformed/oversized/newline-bearing is discarded (a fresh root is started).
        parent = extract_context(request.headers.get("traceparent"))
        start = time.perf_counter()
        with bound_context(request_id=rid, trace_id=tid):
            # A recording span (tracing enabled + sampled) overrides trace_id in the
            # log context with the real trace id for the request's duration; when
            # tracing is disabled the span is a no-op and correlation is unchanged.
            with start_span(
                HTTP_REQUEST,
                kind="server",
                parent=parent,
                attributes={"component": "api", "http.request.method": request.method},
            ) as span:
                response = await call_next(request)
                elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
                response.headers["x-request-id"] = rid
                outcome = _status_outcome(response.status_code)
                status_class = _status_class(response.status_code)
                # Span attributes are bounded: the normalized route template and the
                # status *code* (a small enumerable set), never the raw path or ids.
                span.set_attribute("http.route", _route_template(request))
                span.set_attribute("http.response.status_code", response.status_code)
                span.set_attribute("outcome", outcome)
                log_event(
                    logger,
                    "http.request",
                    component="api",
                    outcome=outcome,
                    duration_ms=elapsed_ms,
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code,
                )
                # Metrics carry only bounded labels — never the path or raw code, which
                # would explode series cardinality.
                m = get_metrics()
                m.increment(HTTP_REQUESTS_TOTAL, outcome=outcome, status_class=status_class)
                m.observe(HTTP_REQUEST_DURATION_MS, elapsed_ms, outcome=outcome)
                return response


#: Browser security headers set on every API response (P6-AUTH-5). HSTS carries no
#: includeSubDomains/preload: the parent domain is undecided, and both would outlive a
#: rollback. Cache-Control ``no-store`` keeps token-bearing JSON (``SessionOut``, the
#: one-time invitation token) out of the browser's disk cache.
SECURITY_HEADERS: dict[str, str] = {
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

#: JSON needs no fetch; this only stops a response rendered as a document from loading
#: anything or being framed.
API_CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'"


def html_docs_paths(app) -> frozenset[str]:
    """The FastAPI HTML documentation routes, read from the app, never hard-coded.

    Swagger UI, its OAuth2 redirect page and ReDoc load CDN assets and run inline
    scripts, so the API policy would break them (their hardening is P6-INF-18).
    ``docs_url`` is prefixed here; the other two are FastAPI's unprefixed defaults.
    """
    urls = (app.docs_url, app.swagger_ui_oauth2_redirect_url, app.redoc_url)
    return frozenset(url for url in urls if url)


def security_headers_for(path: str, csp_exempt_paths: Iterable[str]) -> dict[str, str]:
    """The security headers for a response to ``path`` (exact path match only)."""
    headers = dict(SECURITY_HEADERS)
    if path not in csp_exempt_paths:
        headers["Content-Security-Policy"] = API_CONTENT_SECURITY_POLICY
    return headers


class SecurityHeadersMiddleware:
    """Set :data:`SECURITY_HEADERS` (+ the API CSP) on every HTTP response.

    Pure ASGI, registered last so it is the outermost user middleware: CORS preflight
    answers and rate-limit 429s pass through it. The catch-all 500 handler runs in
    Starlette's ServerErrorMiddleware, outside all user middleware, so it applies the
    same headers itself (``app.core.errors``). ``self.app`` stays public: test helpers
    walk ``app.middleware_stack`` through it to reach the rate limiter.
    """

    def __init__(self, app: ASGIApp, csp_exempt_paths: Iterable[str] = ()) -> None:
        self.app = app
        self.csp_exempt_paths = frozenset(csp_exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = security_headers_for(scope["path"], self.csp_exempt_paths)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in extra.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


#: Upper bound on the clients one per-process limiter tracks at a time.
DEFAULT_RATE_LIMIT_MAX_KEYS = 10_000
#: After the shared store fails, it is not tried again for this many seconds.
RATE_LIMIT_REDIS_RETRY_SECONDS = 5.0

_RATE_LIMITED_BODY = {"error": {"code": "rate_limited", "message": "Too many requests"}}
_RATE_LIMIT_UNAVAILABLE_BODY = {
    "error": {
        "code": "rate_limit_unavailable",
        "message": "The service is temporarily unavailable",
    }
}


class InMemoryRateLimiter:
    """Bounded, per-process fixed-window counter (P6-PLAT-3).

    A client's window opens at its first request and lasts ``window_seconds``; every
    request inside it counts, rejected ones included. At most ``max_keys`` clients are
    tracked: closed windows are dropped first and, if the table is still full, the
    oldest open window is evicted (that client starts afresh). Memory therefore stays
    bounded however many client addresses arrive.
    """

    def __init__(
        self,
        *,
        window_seconds: float,
        max_keys: int = DEFAULT_RATE_LIMIT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be greater than 0")
        if max_keys < 1:
            raise ValueError("max_keys must be >= 1")
        self.window = window_seconds
        self.max_keys = max_keys
        self._clock = clock
        #: client -> [window_end, count], oldest window first (every window has the
        #: same length, so insertion order is also expiry order).
        self.hits: OrderedDict[str, list[float]] = OrderedDict()

    def hit(self, key: str) -> int:
        """Count one request for ``key``; return the count in its current window."""
        now = self._clock()
        entry = self.hits.get(key)
        if entry is None or entry[0] <= now:
            self.hits.pop(key, None)
            self._make_room(now)
            entry = [now + self.window, 0]
            self.hits[key] = entry
        entry[1] += 1
        return int(entry[1])

    def _make_room(self, now: float) -> None:
        while self.hits and next(iter(self.hits.values()))[0] <= now:
            self.hits.popitem(last=False)
        while len(self.hits) >= self.max_keys:
            self.hits.popitem(last=False)


class RedisRateLimiter:
    """Shared fixed-window counter in Redis (P6-PLAT-3).

    One ``MULTI``/``EXEC`` per request: ``SET key 0 EX window NX`` opens the window,
    with its expiry, only if none is open; ``INCR`` counts the request (and keeps the
    expiry). The pair is atomic, so every replica sees one count per client and a key
    never exists without an expiry. Takes an already-built client, so it is testable
    with a fake implementing exactly these commands.
    """

    def __init__(self, client: Any, *, window_seconds: int, key_prefix: str) -> None:
        if window_seconds < 1:
            raise ValueError("window_seconds must be >= 1")
        self._client = client
        self.window = int(window_seconds)
        self._prefix = key_prefix

    def redis_key(self, key: str) -> str:
        return f"{self._prefix}:ratelimit:{key}"

    def hit(self, key: str) -> int:
        """Count one request for ``key``; return the count in its current window."""
        name = self.redis_key(key)
        with self._client.pipeline(transaction=True) as pipe:
            pipe.set(name, 0, ex=self.window, nx=True)
            pipe.incr(name)
            _opened, count = pipe.execute()
        return int(count)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window request limit per resolved client address (P6-PLAT-3).

    The key is ``request.client.host`` - behind the ALB, the real client resolved by
    uvicorn's trusted proxy headers (``test_rate_limit.py``). Without
    ``redis_client`` the count is per process (:class:`InMemoryRateLimiter`;
    development and test - production refuses it). With ``redis_client`` the count
    is shared by every replica (:class:`RedisRateLimiter`).

    When Redis cannot answer, requests are never let through unlimited.
    ``failure_mode="local"`` keeps limiting with this process's bounded counter -
    the per-replica limit - and logs a warning and counts the degradation;
    ``failure_mode="deny"`` answers 503 - except on ``liveness_paths``, which are
    then limited per process instead: the load balancer and container health checks
    probe ``/health``, and denying it would turn a Redis outage into a task
    replacement loop. Either way Redis is tried again only
    ``retry_after_failure_seconds`` later, so an outage costs one timeout per
    interval, not one per request.
    """

    def __init__(
        self,
        app: ASGIApp,
        limit: int = 240,
        window_seconds: int = 60,
        *,
        redis_client: Any | None = None,
        key_prefix: str = "signalnest",
        failure_mode: Literal["local", "deny"] = "local",
        max_keys: int = DEFAULT_RATE_LIMIT_MAX_KEYS,
        retry_after_failure_seconds: float = RATE_LIMIT_REDIS_RETRY_SECONDS,
        liveness_paths: Iterable[str] = ("/health",),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(app)
        if failure_mode not in ("local", "deny"):
            raise ValueError("failure_mode must be 'local' or 'deny'")
        self.limit = limit
        self.window = window_seconds
        self.failure_mode = failure_mode
        self._clock = clock
        self._local = InMemoryRateLimiter(
            window_seconds=window_seconds, max_keys=max_keys, clock=clock
        )
        self._shared = (
            RedisRateLimiter(redis_client, window_seconds=window_seconds, key_prefix=key_prefix)
            if redis_client is not None
            else None
        )
        self._retry_after_failure = retry_after_failure_seconds
        self._shared_retry_at = 0.0
        self._liveness_paths = frozenset(liveness_paths)

    @property
    def _hits(self) -> OrderedDict[str, list[float]]:
        """The per-process ledger (test helpers clear it between tests)."""
        return self._local.hits

    @property
    def is_shared(self) -> bool:
        return self._shared is not None

    async def dispatch(self, request: Request, call_next):
        client = request.client.host if request.client else "anon"
        count = await self._count(client, liveness=request.url.path in self._liveness_paths)
        if count is None:
            return JSONResponse(status_code=503, content=_RATE_LIMIT_UNAVAILABLE_BODY)
        if count > self.limit:
            return JSONResponse(status_code=429, content=_RATE_LIMITED_BODY)
        return await call_next(request)

    async def _count(self, client: str, *, liveness: bool = False) -> int | None:
        """The client's count in its window, or ``None`` when the request is denied."""
        if self._shared is None:
            return self._local.hit(client)
        if self._clock() >= self._shared_retry_at:
            try:
                return await run_in_threadpool(self._shared.hit, client)
            except Exception as exc:  # noqa: BLE001 - any store failure takes the fallback
                self._shared_retry_at = self._clock() + self._retry_after_failure
                # Once per retry interval: the exception class only, never its message
                # (a driver error can embed the Redis host or URL).
                log_event(
                    rate_limit_logger,
                    "rate_limit.shared_store_unavailable",
                    level=WARNING,
                    component="api",
                    outcome="denied" if self.failure_mode == "deny" else "degraded",
                    failure_mode=self.failure_mode,
                    error_class=type(exc).__name__,
                )
        denied = self.failure_mode == "deny" and not liveness
        get_metrics().increment(
            DEPENDENCY_OPERATION_TOTAL,
            dependency="redis",
            operation="rate_limit",
            outcome="denied" if denied else "degraded",
        )
        return None if denied else self._local.hit(client)


def build_rate_limit_redis_client(settings: Settings) -> Any:
    """A bounded, timeout-guarded Redis client for the limiter (P6-PLAT-3).

    Every request makes one limiter call, from Starlette's thread pool, which is larger
    than ``redis_pool_size``. A *blocking* pool makes a call that finds every
    connection busy wait up to ``redis_operation_timeout_seconds`` for one, instead of
    failing at once ("too many connections") and dropping the process to its fallback
    under ordinary concurrency. No connection is opened until the first request; the
    URL lives only inside the client and is never logged.
    """
    import redis

    pool = redis.BlockingConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_pool_size,
        timeout=settings.redis_operation_timeout_seconds,
        socket_timeout=settings.redis_operation_timeout_seconds,
        socket_connect_timeout=settings.redis_operation_timeout_seconds,
    )
    return redis.Redis(connection_pool=pool)


def rate_limit_middleware_options(settings: Settings) -> dict[str, Any]:
    """:class:`RateLimitMiddleware` options for the configured backend (P6-PLAT-3)."""
    if settings.rate_limit_backend != "redis":
        return {}
    return {
        "redis_client": build_rate_limit_redis_client(settings),
        "key_prefix": settings.redis_key_prefix,
        "failure_mode": settings.rate_limit_redis_failure_mode,
    }
