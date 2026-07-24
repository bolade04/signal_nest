"""Rate-limiter client-IP resolution behind a trusted proxy (INFRA-4 pre-live).

`RateLimitMiddleware` keys its fixed window on ``request.client.host``. Behind the
staging ALB the direct TCP peer is the ALB ENI, so without proxy-header handling
every real client collapses into one shared bucket (aws-staging-iac-plan.md §25).
The resolution is uvicorn's ``ProxyHeadersMiddleware`` trusting only in-VPC peers
(``--forwarded-allow-ips`` = the VPC CIDR): uvicorn then rewrites the ASGI
``client`` to the rightmost UNTRUSTED ``X-Forwarded-For`` entry — the entry the
ALB itself appended (``xff_header_processing_mode = "append"``) — so client-
prepended header values can never win. No second XFF parser exists in the app;
these tests pin the exact uvicorn semantics the deployment relies on.

Layers under test (outermost first): a test-only peer-address shim ->
``ProxyHeadersMiddleware`` (uvicorn) -> ``RateLimitMiddleware`` (ours).
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core.middleware import RateLimitMiddleware

VPC_CIDR = "10.0.0.0/16"
ALB_PEER = "10.0.5.7"  # in-VPC ALB ENI: the only peer the SG path allows
INTERNET_PEER = "203.0.113.66"  # a direct internet peer (never trusted)
CLIENT_A = "198.51.100.10"
CLIENT_B = "198.51.100.11"


class _ForcePeer:
    """Test shim: pins the ASGI ``client`` tuple to a chosen direct TCP peer.

    ``TestClient`` reports a fixed synthetic peer; this makes the peer an input
    so trust decisions (in-VPC ALB vs. arbitrary internet host) are testable.
    """

    def __init__(self, app, host: str, port: int = 40000):
        self.app = app
        self.addr = (host, port)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = self.addr
        await self.app(scope, receive, send)


async def _whoami(request):
    client = request.client.host if request.client else None
    return JSONResponse({"client": client})


def _client(*, peer: str, trusted_hosts: str | None = VPC_CIDR, limit: int = 1000) -> TestClient:
    app = Starlette(routes=[Route("/whoami", _whoami)])
    app.add_middleware(RateLimitMiddleware, limit=limit, window_seconds=60)
    stack = app if trusted_hosts is None else ProxyHeadersMiddleware(app, trusted_hosts)
    return TestClient(_ForcePeer(stack, peer))


# --------------------------------------------------------------------------- #
# Client-IP resolution through the trusted proxy
# --------------------------------------------------------------------------- #
def test_trusted_proxy_resolves_ipv4_client() -> None:
    c = _client(peer=ALB_PEER)
    resp = c.get("/whoami", headers={"x-forwarded-for": CLIENT_A})
    assert resp.status_code == 200
    assert resp.json()["client"] == CLIENT_A


def test_trusted_proxy_resolves_ipv6_client() -> None:
    # The ALB-to-target hop is IPv4-only, but the original internet client may be
    # IPv6 — its literal appears inside XFF and must resolve unmangled.
    c = _client(peer=ALB_PEER)
    resp = c.get("/whoami", headers={"x-forwarded-for": "2001:db8::1"})
    assert resp.status_code == 200
    assert resp.json()["client"] == "2001:db8::1"


def test_default_trust_ignores_xff_from_unknown_peer() -> None:
    # uvicorn's default trust is 127.0.0.1 (the local/dev CMD default): an XFF
    # header from any other peer must not rewrite the client.
    c = _client(peer="10.0.0.5", trusted_hosts="127.0.0.1")
    resp = c.get("/whoami", headers={"x-forwarded-for": CLIENT_A})
    assert resp.json()["client"] == "10.0.0.5"


def test_multi_hop_takes_rightmost_untrusted_entry() -> None:
    # A malicious client pre-sends its own XFF; the ALB appends the real address.
    # The RIGHTMOST untrusted entry (ALB-appended) must win — leftmost-first is
    # the classic spoofing bug.
    c = _client(peer=ALB_PEER)
    resp = c.get("/whoami", headers={"x-forwarded-for": f"1.2.3.4, {CLIENT_A}"})
    assert resp.json()["client"] == CLIENT_A


def test_malformed_xff_entries_fail_closed_without_crash() -> None:
    c = _client(peer=ALB_PEER)
    # Garbage prepended by the client sits left of the ALB-appended entry and is
    # never reached by the right-to-left walk.
    resp = c.get("/whoami", headers={"x-forwarded-for": f"not-an-ip%%%, {CLIENT_A}"})
    assert resp.status_code == 200
    assert resp.json()["client"] == CLIENT_A
    # Garbage as the sole entry (only possible from a trusted peer): no crash.
    resp = c.get("/whoami", headers={"x-forwarded-for": "not-an-ip%%%"})
    assert resp.status_code == 200


def test_empty_xff_header_leaves_peer_unchanged() -> None:
    c = _client(peer=ALB_PEER)
    resp = c.get("/whoami", headers={"x-forwarded-for": ""})
    assert resp.status_code == 200
    assert resp.json()["client"] == ALB_PEER


def test_whitespace_padded_entries_are_trimmed() -> None:
    c = _client(peer=ALB_PEER)
    resp = c.get("/whoami", headers={"x-forwarded-for": f"  1.2.3.4 ,   {CLIENT_A}  "})
    assert resp.json()["client"] == CLIENT_A


def test_absent_xff_with_trusted_peer_falls_back_to_peer() -> None:
    c = _client(peer=ALB_PEER)
    resp = c.get("/whoami")
    assert resp.status_code == 200
    assert resp.json()["client"] == ALB_PEER


def test_untrusted_direct_peer_cannot_spoof_via_xff() -> None:
    # The core spoofing case: an internet host connecting directly (hypothetically
    # bypassing the ALB) claims an arbitrary identity via XFF. It must be ignored.
    c = _client(peer=INTERNET_PEER)
    resp = c.get("/whoami", headers={"x-forwarded-for": "9.9.9.9"})
    assert resp.json()["client"] == INTERNET_PEER


def test_xff_port_suffix_is_stripped_not_fatal() -> None:
    # §24.5 disables XFF client-port preservation so a :port suffix should never
    # occur in staging, but the RFC allows it — the parser must not choke.
    c = _client(peer=ALB_PEER)
    resp = c.get("/whoami", headers={"x-forwarded-for": f"{CLIENT_A}:4711"})
    assert resp.status_code == 200
    assert resp.json()["client"] == CLIENT_A


def test_wildcard_trust_degenerates_to_leftmost_entry() -> None:
    # Documents WHY FORWARDED_ALLOW_IPS must never be "*" (root main.tf invariant):
    # with every host trusted, uvicorn returns the LEFTMOST entry — which the
    # client fully controls — instead of the ALB-appended rightmost one.
    c = _client(peer=ALB_PEER, trusted_hosts="*")
    resp = c.get("/whoami", headers={"x-forwarded-for": f"1.2.3.4, {CLIENT_A}"})
    assert resp.json()["client"] == "1.2.3.4"


# --------------------------------------------------------------------------- #
# Rate-limit bucket behavior on the resolved client
# --------------------------------------------------------------------------- #
def test_distinct_clients_get_independent_buckets() -> None:
    # Regression for the §25 collapsed-bucket defect: through the SAME direct
    # peer (the ALB), distinct resolved clients must not share a window.
    c = _client(peer=ALB_PEER, limit=2)
    for _ in range(2):
        assert c.get("/whoami", headers={"x-forwarded-for": CLIENT_A}).status_code == 200
    assert c.get("/whoami", headers={"x-forwarded-for": CLIENT_A}).status_code == 429
    assert c.get("/whoami", headers={"x-forwarded-for": CLIENT_B}).status_code == 200


def test_429_shape_on_resolved_client_overflow() -> None:
    c = _client(peer=ALB_PEER, limit=1)
    assert c.get("/whoami", headers={"x-forwarded-for": CLIENT_A}).status_code == 200
    resp = c.get("/whoami", headers={"x-forwarded-for": CLIENT_A})
    assert resp.status_code == 429
    assert resp.json() == {"error": {"code": "rate_limited", "message": "Too many requests"}}


def test_spoofed_prefix_cannot_escape_rate_limit() -> None:
    # An attacker rotating fabricated leftmost XFF entries must keep landing in
    # the same bucket (their real, ALB-appended address).
    c = _client(peer=ALB_PEER, limit=2)
    for i in range(2):
        headers = {"x-forwarded-for": f"6.6.6.{i}, {CLIENT_A}"}
        assert c.get("/whoami", headers=headers).status_code == 200
    resp = c.get("/whoami", headers={"x-forwarded-for": f"7.7.7.7, {CLIENT_A}"})
    assert resp.status_code == 429


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
