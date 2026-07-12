"""Request correlation + simple in-memory rate-limit placeholder."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.logging import get_logger, request_id_ctx, trace_id_ctx

logger = get_logger("signalnest.request")


class CorrelationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        tid = request.headers.get("x-trace-id") or rid
        request_id_ctx.set(rid)
        trace_id_ctx.set(tid)
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers["x-request-id"] = rid
        logger.info(
            "request",
            extra={
                "extra_fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "elapsed_ms": elapsed_ms,
                }
            },
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
