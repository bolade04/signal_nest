"""P6-AUTH-5: browser security headers on every API response.

Each response carries the same fixed set, exact values (``API_HEADERS`` below is written out
here on purpose: importing the middleware's own constant would let a removed or weakened
header pass):

    Strict-Transport-Security  max-age=31536000   (no includeSubDomains / preload)
    X-Content-Type-Options     nosniff
    X-Frame-Options            DENY
    Referrer-Policy            no-referrer
    Cache-Control              no-store
    Content-Security-Policy    default-src 'none'; frame-ancestors 'none'

The CSP is left off exactly three routes -- FastAPI's HTML documentation pages
(``app.docs_url``, ``app.swagger_ui_oauth2_redirect_url``, ``app.redoc_url``), which load CDN
assets and run inline scripts (their hardening is P6-INF-18) -- and those still carry every
other header. The set holds on success, 401, 404, 422, CORS preflight, the rate limiter's 429
and the catch-all 500, which Starlette's ServerErrorMiddleware answers outside every user
middleware. No response sets a cookie: a cookie would need a CSRF design first.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.middleware.errors import ServerErrorMiddleware

from app.core.config import get_settings
from app.core.middleware import RateLimitMiddleware, SecurityHeadersMiddleware
from app.main import app, create_app
from app.tests._auth2_support import (
    ALICE,
    API,
    BOB,
    EMAIL,
    INVITATION_ACCEPT,
    INVITATION_REGISTER,
    LOGIN,
    LOGOUT,
    LOGOUT_ALL,
    ME,
    OLD_PASSWORD,
    REGISTER,
    Env,
    active_rate_limiter,
    environment,
    invite,
    open_session,
    post,
)

API_HEADERS = {
    "strict-transport-security": "max-age=31536000",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}
API_CSP = "default-src 'none'; frame-ancestors 'none'"
DOCS_ROUTES = {f"{API}/docs", "/docs/oauth2-redirect", "/redoc"}
ALLOWED_ORIGIN = "http://localhost:3000"  # the default CORS_ORIGINS entry

APP_DIR = Path(__file__).resolve().parents[1]


def assert_security_headers(response, *, csp: bool = True) -> None:
    got = {name: response.headers.get(name) for name in API_HEADERS}
    assert got == API_HEADERS, (response.request.url, response.status_code, got)
    if csp:
        assert response.headers.get("content-security-policy") == API_CSP, response.request.url
    else:
        assert "content-security-policy" not in response.headers, response.request.url
    assert "set-cookie" not in response.headers, response.request.url


@pytest.fixture()
def env(tmp_path) -> Iterator[Env]:
    with environment(tmp_path / "headers.db") as e:
        yield e


@pytest.fixture()
def fresh_client() -> Iterator[TestClient]:
    """A new application from the real factory. Its rate limiter is its own, and it
    configures logging, so the root logger is restored afterwards."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        yield TestClient(create_app(), raise_server_exceptions=False)
    finally:
        root.handlers = handlers
        root.level = level


def _chain(stack) -> list[object]:
    """The middleware stack, outermost first, walked through ``.app`` as the test helpers do."""
    nodes = []
    node = stack
    while node is not None and len(nodes) < 20:
        nodes.append(node)
        node = getattr(node, "app", None)
    return nodes


# --------------------------------------------------------------------------- #
# T11 / T14 / T15 / T16 / T18: the header set on every response class
# --------------------------------------------------------------------------- #
class TestEveryResponseClass:
    def test_success(self, env: Env):
        r = env.client.get("/health")
        assert r.status_code == 200, r.text
        assert_security_headers(r)

    def test_authenticated_success(self, env: Env):
        token, _ = open_session(env.request_engine, ALICE)
        r = env.client.get(ME, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        assert_security_headers(r)

    def test_unauthenticated_401(self, env: Env):
        r = env.client.get(ME)
        assert r.status_code == 401, r.text
        assert_security_headers(r)

    def test_unknown_route_404(self, env: Env):
        r = env.client.get(f"{API}/no-such-route")
        assert r.status_code == 404, r.text
        assert_security_headers(r)

    def test_validation_422(self, env: Env):
        r = env.client.post(LOGIN, json={})
        assert r.status_code == 422, r.text
        assert_security_headers(r)

    def test_openapi_json_keeps_the_csp(self, env: Env):
        r = env.client.get(f"{API}/openapi.json")
        assert r.status_code == 200, r.text
        assert_security_headers(r)


# --------------------------------------------------------------------------- #
# T19: CORS still works, and its preflight answers carry the headers
# --------------------------------------------------------------------------- #
class TestCorsPreflight:
    def test_allowed_origin(self, env: Env):
        assert ALLOWED_ORIGIN in get_settings().cors_origins
        r = env.client.options(
            LOGIN,
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert r.status_code == 200, r.text
        assert r.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
        assert "authorization" in r.headers["access-control-allow-headers"].lower()
        assert_security_headers(r)

    def test_allowed_origin_actual_request(self, env: Env):
        token, _ = open_session(env.request_engine, ALICE)
        r = env.client.get(
            ME, headers={"Origin": ALLOWED_ORIGIN, "Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200, r.text
        assert r.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
        assert_security_headers(r)

    def test_disallowed_origin(self, env: Env):
        r = env.client.options(
            LOGIN,
            headers={"Origin": "https://attacker.invalid", "Access-Control-Request-Method": "POST"},
        )
        assert "access-control-allow-origin" not in r.headers
        assert_security_headers(r)


# --------------------------------------------------------------------------- #
# The two paths that bypass ordinary middleware wiring: the limiter's 429 and the 500
# --------------------------------------------------------------------------- #
class TestLimiterAndServerError:
    def test_rate_limited_429(self, fresh_client: TestClient):
        assert fresh_client.get("/health").status_code == 200  # builds the stack
        limiter = next(
            n
            for n in _chain(fresh_client.app.middleware_stack)
            if isinstance(n, RateLimitMiddleware)
        )
        limiter._hits.clear()
        limiter.limit = 1
        assert fresh_client.get("/health").status_code == 200
        r = fresh_client.get("/health")
        assert r.status_code == 429, r.text
        assert r.json()["error"]["code"] == "rate_limited"
        assert_security_headers(r)

    def test_unhandled_exception_500(self, fresh_client: TestClient):
        fresh = fresh_client.app
        assert fresh.debug is False  # in debug, Starlette would bypass the handler entirely

        def boom():
            raise RuntimeError("synthetic failure")

        fresh.add_api_route("/boom", boom)
        r = fresh_client.get("/boom")
        assert r.status_code == 500, r.text
        assert r.json()["error"]["code"] == "internal_error"
        assert_security_headers(r)

    def test_unhandled_exception_500_on_an_exempt_path_keeps_the_exemption(
        self, fresh_client: TestClient
    ):
        fresh = fresh_client.app
        fresh.router.routes = [
            route for route in fresh.router.routes if getattr(route, "path", None) != "/redoc"
        ]

        def boom():
            raise RuntimeError("synthetic failure")

        fresh.add_api_route("/redoc", boom)
        r = fresh_client.get("/redoc")
        assert r.status_code == 500, r.text
        assert_security_headers(r, csp=False)


# --------------------------------------------------------------------------- #
# The documentation pages: every header but the CSP, on exactly three paths
# --------------------------------------------------------------------------- #
class TestDocumentationRoutes:
    def test_the_exempt_set_is_exactly_the_three_documentation_pages(self):
        assert {app.docs_url, app.swagger_ui_oauth2_redirect_url, app.redoc_url} == DOCS_ROUTES
        outer = next(
            n for n in _chain(app.middleware_stack) if isinstance(n, SecurityHeadersMiddleware)
        )
        assert outer.csp_exempt_paths == DOCS_ROUTES

    @pytest.mark.parametrize("path", sorted(DOCS_ROUTES))
    def test_documentation_page(self, env: Env, path: str):
        r = env.client.get(path)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/html")
        assert_security_headers(r, csp=False)

    @pytest.mark.parametrize("path", ["/redoc/x", f"{API}/docs-x", f"{API}/docs/x", "/docs"])
    def test_near_misses_keep_the_csp(self, env: Env, path: str):
        r = env.client.get(path)
        assert r.status_code == 404, r.text
        assert_security_headers(r)


# --------------------------------------------------------------------------- #
# Middleware placement: outermost user middleware, with the chain still walkable
# --------------------------------------------------------------------------- #
class TestPlacement:
    def test_outermost_user_middleware_and_the_limiter_stays_reachable(self, env: Env):
        env.client.get("/health")
        chain = _chain(app.middleware_stack)
        assert isinstance(chain[0], ServerErrorMiddleware)
        assert isinstance(chain[1], SecurityHeadersMiddleware)
        assert any(isinstance(n, RateLimitMiddleware) for n in chain)
        assert isinstance(active_rate_limiter(), RateLimitMiddleware)


# --------------------------------------------------------------------------- #
# T20': no cookie anywhere (a cookie would need a CSRF design first)
# --------------------------------------------------------------------------- #
class TestNoCookie:
    def test_the_seven_session_routes_set_no_cookie(self, env: Env):
        registered = post(
            env,
            REGISTER,
            {
                "email": "headers-founder@example.com",
                "full_name": "Founder",
                "password": OLD_PASSWORD,
                "organization_name": "Headers Co",
            },
        )
        assert registered.status_code == 201, registered.text
        logged_in = post(env, LOGIN, {"email": EMAIL[ALICE], "password": OLD_PASSWORD})
        assert logged_in.status_code == 200, logged_in.text
        token = logged_in.json()["access_token"]
        me = env.client.get(ME, headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200, me.text
        newcomer = invite(env, "headers-newcomer@example.com")
        joined = post(
            env,
            INVITATION_REGISTER,
            {"token": newcomer["token"], "full_name": "Newcomer", "password": OLD_PASSWORD},
        )
        assert joined.status_code == 201, joined.text
        existing = invite(env, EMAIL[BOB])
        bob_token, _ = open_session(env.request_engine, BOB)
        accepted = post(env, INVITATION_ACCEPT, {"token": existing["token"]}, token=bob_token)
        assert accepted.status_code == 200, accepted.text
        logged_out = post(env, LOGOUT, {}, token=token)
        assert logged_out.status_code == 204, logged_out.text
        everywhere = post(env, LOGOUT_ALL, {}, user_id=ALICE)
        assert everywhere.status_code == 204, everywhere.text
        for r in (registered, logged_in, me, joined, accepted, logged_out, everywhere):
            assert_security_headers(r)

    def test_no_cookie_writer_in_the_application_source(self):
        pattern = re.compile(r"set_cookie|delete_cookie|set-cookie", re.IGNORECASE)
        sources = [p for p in APP_DIR.rglob("*.py") if "tests" not in p.relative_to(APP_DIR).parts]
        assert len(sources) > 50  # the scan really covers the application
        writers = [str(p) for p in sources if pattern.search(p.read_text(encoding="utf-8"))]
        assert writers == []
