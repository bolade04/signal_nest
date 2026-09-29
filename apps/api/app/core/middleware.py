"""Request correlation, security headers + simple in-memory rate-limit placeholder."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterable

from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.log_context import bound_context, new_request_id, normalize_request_id
from app.core.logging import get_logger, log_event
from app.core.metrics import HTTP_REQUEST_DURATION_MS, HTTP_REQUESTS_TOTAL, get_metrics
from app.core.tracing import HTTP_REQUEST, extract_context, start_span

logger = get_logger("signalnest.request")


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
