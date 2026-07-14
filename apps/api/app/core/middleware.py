"""Request correlation + simple in-memory rate-limit placeholder."""

from __future__ import annotations

import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.log_context import bound_context, new_request_id, normalize_request_id
from app.core.logging import get_logger, log_event

logger = get_logger("signalnest.request")


def _status_outcome(status_code: int) -> str:
    if status_code >= 500:
        return "server_error"
    if status_code >= 400:
        return "client_error"
    return "success"


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
        start = time.perf_counter()
        with bound_context(request_id=rid, trace_id=tid):
            response = await call_next(request)
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            response.headers["x-request-id"] = rid
            log_event(
                logger,
                "http.request",
                component="api",
                outcome=_status_outcome(response.status_code),
                duration_ms=elapsed_ms,
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
            )
            return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Naive fixed-window limiter. Placeholder; production uses Redis adapter."""

    def __init__(self, app, limit: int = 240, window_seconds: int = 60):
        super().__init__(app)
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next):
        client = request.client.host if request.client else "anon"
        now = time.time()
        window_start = now - self.window
        hits = [t for t in self._hits[client] if t > window_start]
        hits.append(now)
        self._hits[client] = hits
        if len(hits) > self.limit:
            from starlette.responses import JSONResponse

            return JSONResponse(
                status_code=429,
                content={"error": {"code": "rate_limited", "message": "Too many requests"}},
            )
        return await call_next(request)
