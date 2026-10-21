"""P6-DATA-1 / P6-DATA-4: the live-egress guard (``app.connectors.egress``).

No test here leaves the machine. DNS is an injected resolver; the wire is either an
``httpx.MockTransport`` that records exactly what would have been sent, or a real HTTP
server bound to 127.0.0.1 (for the socket-level timeout, streaming-cap and replay
cases). Every refusal is asserted by its classified kind, and every SSRF refusal is
asserted to happen BEFORE any connection: the transport records nothing.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.connectors.base import FailureKind
from app.connectors.egress import (
    FetchLimits,
    SafeFeedFetcher,
    Validators,
    address_problem,
    build_user_agent,
    parse_replay_origin,
)
from app.connectors.retry import ConnectorFetchError

HOST = "feeds.publisher.test"
FEED = f"https://{HOST}/rss.xml"
ALLOWED = frozenset({HOST, "cdn.publisher.test"})
PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"
RSS = b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'


class Recorder:
    """A MockTransport handler that answers from a script and records every request."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


def ok(body: bytes = RSS, **headers: str) -> httpx.Response:
    return httpx.Response(
        200, content=body, headers={"content-type": "application/rss+xml", **headers}
    )


def resolver_for(table: dict[str, list[str]], calls: list[str] | None = None):
    def resolve(host: str, port: int) -> list[str]:
        if calls is not None:
            calls.append(host)
        return table[host]

    return resolve


def fetcher(
    recorder: Recorder | None = None,
    *,
    table: dict[str, list[str]] | None = None,
    calls: list[str] | None = None,
    limits: FetchLimits | None = None,
    clock=time.monotonic,
    replay_origin=None,
    transport: httpx.BaseTransport | None = None,
) -> SafeFeedFetcher:
    return SafeFeedFetcher(
        user_agent="SignalNestBot/test (+https://operator.test/bot)",
        limits=limits,
        resolver=resolver_for(table or {HOST: [PUBLIC_V4]}, calls),
        transport=transport or (httpx.MockTransport(recorder) if recorder else None),
        replay_origin=replay_origin,
        clock=clock,
    )


def refused(fn, kind: FailureKind) -> ConnectorFetchError:
    with pytest.raises(ConnectorFetchError) as info:
        fn()
    assert info.value.failure.kind is kind, info.value.failure
    return info.value


# --------------------------------------------------------------------------- #
# Address policy
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "127.8.9.10", "10.0.0.1", "172.16.0.1", "172.31.255.255",
        "192.168.0.1", "169.254.169.254", "169.254.0.1", "100.64.0.1", "100.100.100.200",
        "0.0.0.0", "224.0.0.1", "239.255.255.250", "198.18.0.1", "192.0.2.1",
        "198.51.100.1", "203.0.113.1", "240.0.0.1", "255.255.255.255",
        "::1", "::", "fe80::1", "fc00::1", "fd00:ec2::254", "fec0::1", "ff02::1",
        "2001:db8::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",
        "2002:0a00:0001::1", "2002:7f00:0001::1", "64:ff9b::a00:1", "64:ff9b::7f00:1",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2", "not-an-address", "",
    ],
)
def test_non_public_addresses_are_refused(address: str) -> None:
    assert address_problem(address) is not None


@pytest.mark.parametrize(
    "address", [PUBLIC_V4, "8.8.8.8", PUBLIC_V6, "::ffff:8.8.8.8", "2002:0808:0808::1"]
)
def test_global_unicast_addresses_are_allowed(address: str) -> None:
    assert address_problem(address) is None


def test_loopback_allowance_is_loopback_only() -> None:
    assert address_problem("127.0.0.1", loopback_ok=True) is None
    assert address_problem("::1", loopback_ok=True) is None
    for other in ("10.0.0.1", "169.254.169.254", "192.168.1.1", "fc00::1"):
        assert address_problem(other, loopback_ok=True) is not None


# --------------------------------------------------------------------------- #
# URL allow-list — refused before DNS and before any connection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        f"http://{HOST}/rss.xml",  # plain http
        f"https://user:pw@{HOST}/rss.xml",  # credentials
        f"https://{HOST}@evil.test/rss.xml",  # userinfo smuggling
        "https://93.184.216.34/rss.xml",  # raw IPv4
        "https://[2606:4700:4700::1111]/rss.xml",  # raw IPv6
        "https://localhost/rss.xml",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://printer.local/feed",
        "https://other.publisher.test/rss.xml",  # not on the allow-list
        f"https://{HOST}:8443/rss.xml",  # port
        f"https://{HOST}:99999/rss.xml",  # malformed port
        "file:///etc/passwd",
        "gopher://feeds.publisher.test/",
        "ftp://feeds.publisher.test/rss.xml",
    ],
)
def test_disallowed_urls_are_refused_before_dns_or_connect(url: str) -> None:
    calls: list[str] = []
    rec = Recorder(ok())
    f = fetcher(rec, calls=calls)
    refused(lambda: f.fetch(url, allowed_hosts=ALLOWED), FailureKind.EGRESS_BLOCKED)
    assert calls == [] and rec.requests == []


# --------------------------------------------------------------------------- #
# DNS: resolve, check every address, pin
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "answer",
    [["10.0.0.5"], ["169.254.169.254"], ["127.0.0.1"], ["::1"], [PUBLIC_V4, "10.0.0.5"],
     ["fd00:ec2::254"], ["::ffff:192.168.0.1"]],
)
def test_a_host_resolving_to_any_non_public_address_is_never_dialled(answer) -> None:
    rec = Recorder(ok())
    f = fetcher(rec, table={HOST: answer})
    refused(lambda: f.fetch(FEED, allowed_hosts=ALLOWED), FailureKind.EGRESS_BLOCKED)
    assert rec.requests == []


def test_empty_or_failed_resolution_is_a_network_failure() -> None:
    rec = Recorder(ok())
    refused(
        lambda: fetcher(rec, table={HOST: []}).fetch(FEED, allowed_hosts=ALLOWED),
        FailureKind.NETWORK,
    )

    def boom(host: str, port: int) -> list[str]:
        raise ConnectorFetchError(FailureKind.NETWORK, "dns resolution failed")

    f = SafeFeedFetcher(user_agent="x", resolver=boom, transport=httpx.MockTransport(rec))
    refused(lambda: f.fetch(FEED, allowed_hosts=ALLOWED), FailureKind.NETWORK)
    assert rec.requests == []


def test_the_connection_is_pinned_to_the_checked_address_with_host_and_sni() -> None:
    rec = Recorder(ok())
    response = fetcher(rec).fetch(FEED, allowed_hosts=ALLOWED)
    [req] = rec.requests
    assert req.url.host == PUBLIC_V4  # dialled the checked address, not the name
    assert req.url.path == "/rss.xml"
    assert req.headers["host"] == HOST
    assert req.extensions["sni_hostname"] == HOST  # TLS verified against the hostname
    assert req.headers["user-agent"] == "SignalNestBot/test (+https://operator.test/bot)"
    assert req.headers["accept-encoding"] == "identity"
    assert "if-none-match" not in req.headers
    assert response.status == 200 and response.body == RSS
    assert response.final_url == FEED and response.redirects == 0


def test_ipv6_pinning_brackets_the_address() -> None:
    rec = Recorder(ok())
    fetcher(rec, table={HOST: [PUBLIC_V6]}).fetch(FEED, allowed_hosts=ALLOWED)
    [req] = rec.requests
    assert req.url.host == PUBLIC_V6 and req.headers["host"] == HOST


def test_conditional_request_sends_validators_and_reports_not_modified() -> None:
    rec = Recorder(httpx.Response(304, headers={"etag": '"v2"'}))
    response = fetcher(rec).fetch(
        FEED,
        allowed_hosts=ALLOWED,
        validators=Validators(etag='"v1"', last_modified="Mon, 19 Oct 2026 12:00:00 GMT"),
    )
    [req] = rec.requests
    assert req.headers["if-none-match"] == '"v1"'
    assert req.headers["if-modified-since"] == "Mon, 19 Oct 2026 12:00:00 GMT"
    assert response.not_modified and response.body == b"" and response.etag == '"v2"'


def test_an_unrequested_304_is_an_upstream_error() -> None:
    rec = Recorder(httpx.Response(304))
    refused(lambda: fetcher(rec).fetch(FEED, allowed_hosts=ALLOWED), FailureKind.UPSTREAM_ERROR)


# --------------------------------------------------------------------------- #
# Redirects: bounded, each hop re-validated and re-resolved
# --------------------------------------------------------------------------- #
def redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"location": location})


def test_a_redirect_to_an_approved_host_is_followed_and_re_resolved() -> None:
    calls: list[str] = []
    rec = Recorder(redirect("https://cdn.publisher.test/feed.xml", 301), ok())
    f = fetcher(rec, table={HOST: [PUBLIC_V4], "cdn.publisher.test": ["8.8.8.8"]}, calls=calls)
    response = f.fetch(FEED, allowed_hosts=ALLOWED, validators=Validators(etag='"v1"'))
    assert calls == [HOST, "cdn.publisher.test"]
    assert [r.url.host for r in rec.requests] == [PUBLIC_V4, "8.8.8.8"]
    assert rec.requests[1].headers["host"] == "cdn.publisher.test"
    assert "if-none-match" not in rec.requests[1].headers  # validators never cross a hop
    assert response.final_url == "https://cdn.publisher.test/feed.xml"
    assert response.redirects == 1


def test_a_relative_redirect_resolves_against_the_logical_url() -> None:
    rec = Recorder(redirect("/v2/rss.xml"), ok())
    response = fetcher(rec).fetch(FEED, allowed_hosts=ALLOWED)
    assert response.final_url == f"https://{HOST}/v2/rss.xml"
    assert rec.requests[1].url.host == PUBLIC_V4 and rec.requests[1].url.path == "/v2/rss.xml"


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.test/rss.xml",  # unlisted host
        f"http://{HOST}/rss.xml",  # downgrade
        "https://169.254.169.254/latest/meta-data/",  # metadata by literal
        "http://127.0.0.1:8080/admin",
        "https://localhost/",
        "file:///etc/passwd",
    ],
)
def test_a_redirect_off_the_allow_list_is_refused_before_the_second_connection(location) -> None:
    rec = Recorder(redirect(location), ok())
    refused(lambda: fetcher(rec).fetch(FEED, allowed_hosts=ALLOWED), FailureKind.EGRESS_BLOCKED)
    assert len(rec.requests) == 1


def test_dns_rebinding_on_a_redirect_hop_is_refused() -> None:
    # The approved redirect host now resolves to the metadata service.
    rec = Recorder(redirect("https://cdn.publisher.test/feed.xml"), ok())
    f = fetcher(rec, table={HOST: [PUBLIC_V4], "cdn.publisher.test": ["169.254.169.254"]})
    refused(lambda: f.fetch(FEED, allowed_hosts=ALLOWED), FailureKind.EGRESS_BLOCKED)
    assert len(rec.requests) == 1


def test_redirects_are_bounded() -> None:
    rec = Recorder(*(redirect(FEED) for _ in range(5)))
    f = fetcher(rec, limits=FetchLimits(max_redirects=3))
    err = refused(lambda: f.fetch(FEED, allowed_hosts=ALLOWED), FailureKind.EGRESS_BLOCKED)
    assert "redirect limit" in err.failure.detail
    assert len(rec.requests) == 4  # the first request plus three followed hops


def test_a_redirect_without_location_is_an_upstream_error() -> None:
    rec = Recorder(httpx.Response(302))
    refused(lambda: fetcher(rec).fetch(FEED, allowed_hosts=ALLOWED), FailureKind.UPSTREAM_ERROR)


# --------------------------------------------------------------------------- #
# Status classes
# --------------------------------------------------------------------------- #
def test_429_is_rate_limited_and_carries_retry_after() -> None:
    rec = Recorder(httpx.Response(429, headers={"retry-after": "120"}))
    err = refused(lambda: fetcher(rec).fetch(FEED, allowed_hosts=ALLOWED), FailureKind.RATE_LIMITED)
    assert err.retry_after_seconds == 120.0


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 451])
def test_permanent_client_errors_are_not_retryable(status: int) -> None:
    err = refused(
        lambda: fetcher(Recorder(httpx.Response(status))).fetch(FEED, allowed_hosts=ALLOWED),
        FailureKind.HTTP_CLIENT_ERROR,
    )
    assert err.failure.retryable is False


@pytest.mark.parametrize("status", [500, 502, 503, 504, 204, 206])
def test_other_statuses_are_upstream_errors(status: int) -> None:
    refused(
        lambda: fetcher(Recorder(httpx.Response(status))).fetch(FEED, allowed_hosts=ALLOWED),
        FailureKind.UPSTREAM_ERROR,
    )


# --------------------------------------------------------------------------- #
# Response bounds
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "content_type",
    ["text/html", "application/json", "application/octet-stream", "image/svg+xml", ""],
)
def test_non_feed_media_types_are_refused(content_type: str) -> None:
    rec = Recorder(httpx.Response(200, content=RSS, headers={"content-type": content_type}))
    refused(lambda: fetcher(rec).fetch(FEED, allowed_hosts=ALLOWED), FailureKind.UNSAFE_CONTENT)


@pytest.mark.parametrize(
    "content_type",
    ["application/rss+xml", "application/atom+xml", "application/xml; charset=utf-8", "TEXT/XML"],
)
def test_feed_media_types_are_accepted(content_type: str) -> None:
    rec = Recorder(httpx.Response(200, content=RSS, headers={"content-type": content_type}))
    assert fetcher(rec).fetch(FEED, allowed_hosts=ALLOWED).body == RSS


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "x-anything"])
def test_an_encoded_body_is_refused_so_nothing_is_decompressed(encoding: str) -> None:
    # A streamed body, as a real transport delivers it: nothing is decoded up front.
    rec = Recorder(
        httpx.Response(
            200,
            content=iter([b"\x1f\x8b not really gzip"]),
            headers={"content-type": "application/rss+xml", "content-encoding": encoding},
        )
    )
    refused(lambda: fetcher(rec).fetch(FEED, allowed_hosts=ALLOWED), FailureKind.UNSAFE_CONTENT)


def test_a_declared_oversize_body_is_refused_before_reading() -> None:
    rec = Recorder(ok(b"x" * 100))
    f = fetcher(rec, limits=FetchLimits(max_response_bytes=99))
    refused(lambda: f.fetch(FEED, allowed_hosts=ALLOWED), FailureKind.UNSAFE_CONTENT)


def test_an_undeclared_oversize_body_is_cut_off_while_streaming() -> None:
    def chunks():
        for _ in range(64):
            yield b"y" * 1024

    rec = Recorder(
        httpx.Response(200, content=chunks(), headers={"content-type": "application/xml"})
    )
    f = fetcher(rec, limits=FetchLimits(max_response_bytes=10_000))
    refused(lambda: f.fetch(FEED, allowed_hosts=ALLOWED), FailureKind.UNSAFE_CONTENT)


def test_the_overall_deadline_bounds_a_trickling_body() -> None:
    now = [0.0]

    def clock() -> float:
        return now[0]

    def trickle():
        for _ in range(10):
            now[0] += 5.0  # each chunk "takes" five seconds
            yield b"z"

    rec = Recorder(httpx.Response(200, content=trickle(), headers={"content-type": "text/xml"}))
    f = fetcher(rec, limits=FetchLimits(total_timeout_seconds=12.0), clock=clock)
    refused(lambda: f.fetch(FEED, allowed_hosts=ALLOWED), FailureKind.TIMEOUT)


def test_transport_timeouts_and_connection_errors_are_classified() -> None:
    def slow(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    def reset(request):
        raise httpx.ConnectError("connection refused", request=request)

    refused(
        lambda: fetcher(transport=httpx.MockTransport(slow)).fetch(FEED, allowed_hosts=ALLOWED),
        FailureKind.TIMEOUT,
    )
    refused(
        lambda: fetcher(transport=httpx.MockTransport(reset)).fetch(FEED, allowed_hosts=ALLOWED),
        FailureKind.NETWORK,
    )


def test_ambient_proxy_settings_are_ignored(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.9:3128")
    monkeypatch.setenv("ALL_PROXY", "http://10.0.0.9:3128")
    rec = Recorder(ok())
    fetcher(rec).fetch(FEED, allowed_hosts=ALLOWED)
    assert rec.requests[0].url.host == PUBLIC_V4


def test_user_agent_names_the_contact_page() -> None:
    assert build_user_agent("1.2.3", "https://op.test/bot") == "SignalNestBot/1.2.3 (+https://op.test/bot)"
    assert "contact-not-configured" in build_user_agent("1.2.3", None)


# --------------------------------------------------------------------------- #
# Replay origin (development/test only) and real sockets
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("http://127.0.0.1:8765", ("127.0.0.1", 8765)),
        ("http://[::1]:9000", ("::1", 9000)),
        ("http://127.0.0.1:8765/", ("127.0.0.1", 8765)),
        ("https://127.0.0.1:8765", None),
        ("http://127.0.0.1", None),
        ("http://localhost:8765", None),
        ("http://10.0.0.1:8765", None),
        ("http://127.0.0.2:8765", None),
        ("http://127.0.0.1:8765/path", None),
        ("http://u:p@127.0.0.1:8765", None),
        ("", None),
        (None, None),
    ],
)
def test_replay_origin_must_be_an_exact_loopback_http_origin(origin, expected) -> None:
    assert parse_replay_origin(origin) == expected


class _Server:
    """A one-route HTTP server on 127.0.0.1 driven by ``handler(request_handler)``."""

    def __init__(self, handler) -> None:
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.hits += 1
                handler(self)

            def log_message(self, *args):
                return

        self.hits = 0
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def _serve(body: bytes, *, delay: float = 0.0, content_type="application/rss+xml", length=True):
    def handle(h: BaseHTTPRequestHandler) -> None:
        if delay:
            time.sleep(delay)
        h.send_response(200)
        h.send_header("Content-Type", content_type)
        if length:
            h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    return handle


def test_replay_mode_fetches_over_a_real_loopback_socket() -> None:
    with _Server(_serve(RSS)) as server:
        f = fetcher(replay_origin=parse_replay_origin(server.origin))
        response = f.fetch(f"{server.origin}/feed", allowed_hosts=frozenset())
    assert response.body == RSS and server.hits == 1


def test_without_replay_mode_the_loopback_server_is_unreachable() -> None:
    with _Server(_serve(RSS)) as server:
        f = fetcher()
        url = f"{server.origin}/feed"
        refused(lambda: f.fetch(url, allowed_hosts=ALLOWED), FailureKind.EGRESS_BLOCKED)
        # Even a name on the allow-list that resolves to loopback is never dialled.
        g = fetcher(table={HOST: ["127.0.0.1"]})
        refused(lambda: g.fetch(FEED, allowed_hosts=ALLOWED), FailureKind.EGRESS_BLOCKED)
    assert server.hits == 0


def test_replay_mode_admits_only_its_exact_origin() -> None:
    with _Server(_serve(RSS)) as server:
        host, port = parse_replay_origin(server.origin)
        f = fetcher(replay_origin=(host, port + 1))  # a different loopback port
        url = f"{server.origin}/feed"
        refused(lambda: f.fetch(url, allowed_hosts=frozenset()), FailureKind.EGRESS_BLOCKED)
        g = fetcher(replay_origin=(host, port), table={HOST: ["10.0.0.7"]})
        refused(lambda: g.fetch(FEED, allowed_hosts=ALLOWED), FailureKind.EGRESS_BLOCKED)
    assert server.hits == 0


def test_a_slow_source_times_out_on_the_socket() -> None:
    with _Server(_serve(RSS, delay=1.5)) as server:
        f = fetcher(
            replay_origin=parse_replay_origin(server.origin),
            limits=FetchLimits(read_timeout_seconds=0.3, total_timeout_seconds=5.0),
        )
        url = f"{server.origin}/feed"
        refused(lambda: f.fetch(url, allowed_hosts=frozenset()), FailureKind.TIMEOUT)


def test_an_unbounded_body_is_cut_at_the_cap_on_the_socket() -> None:
    with _Server(_serve(b"<rss>" + b"a" * 300_000, length=False)) as server:
        f = fetcher(
            replay_origin=parse_replay_origin(server.origin),
            limits=FetchLimits(max_response_bytes=64_000),
        )
        url = f"{server.origin}/feed"
        refused(lambda: f.fetch(url, allowed_hosts=frozenset()), FailureKind.UNSAFE_CONTENT)


def test_replay_mode_never_leaves_the_loopback_origin() -> None:
    # The replay server redirects to an allow-listed public host: refused, never resolved.
    def handle(h: BaseHTTPRequestHandler) -> None:
        h.send_response(302)
        h.send_header("Location", FEED)
        h.send_header("Content-Length", "0")
        h.end_headers()

    calls: list[str] = []
    with _Server(handle) as server:
        f = fetcher(replay_origin=parse_replay_origin(server.origin), calls=calls)
        url = f"{server.origin}/feed"
        err = refused(lambda: f.fetch(url, allowed_hosts=ALLOWED), FailureKind.EGRESS_BLOCKED)
    assert "replay" in err.failure.detail
    assert calls == [] and server.hits == 1


def test_a_resolver_that_never_answers_times_out() -> None:
    release = threading.Event()

    def hang(host: str, port: int) -> list[str]:
        release.wait(5)
        return [PUBLIC_V4]

    rec = Recorder(ok())
    f = SafeFeedFetcher(
        user_agent="x",
        limits=FetchLimits(connect_timeout_seconds=0.2),
        resolver=hang,
        transport=httpx.MockTransport(rec),
    )
    started = time.monotonic()
    try:
        refused(lambda: f.fetch(FEED, allowed_hosts=ALLOWED), FailureKind.TIMEOUT)
    finally:
        release.set()
    assert time.monotonic() - started < 2.0
    assert rec.requests == []
