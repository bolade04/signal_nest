"""Safe outbound HTTP for approved feeds (P6-DATA-1 / P6-DATA-4).

This module is the **only** place a connector reaches the network. It is built so a
hostile feed, DNS answer or redirect cannot turn a scout run into a request against
an internal service (SSRF):

* **Allow-list first.** A URL must be ``https``, carry no credentials, use port 443,
  name a host (never a raw IP literal or an internal-looking name) and that host
  must be the source's own host or one of its approved redirect hosts.
* **Resolve, check every address, then connect to the checked address.** The host is
  resolved once per hop; *every* returned address must be a global unicast address
  (loopback, private, link-local — including the cloud metadata address —
  carrier-grade NAT, documentation, benchmarking, reserved, multicast and
  unspecified ranges are refused, for IPv4 and IPv6, after unwrapping IPv4-mapped,
  6to4 and NAT64 forms; Teredo is refused outright). The connection is then made to
  that **pinned** address with the hostname carried in ``Host`` and TLS SNI, so the
  certificate is still verified against the hostname and a second DNS answer
  (rebinding) is never consulted.
* **Redirects are followed by hand**, at most ``max_redirects`` times, and every hop
  is re-validated and re-resolved from scratch; an ``https`` → ``http`` downgrade or
  a hop to an unlisted host is refused.
* **Bounded response.** Connect / read timeouts plus an overall deadline, a hard byte
  cap enforced while streaming (and on ``Content-Length`` up front), an allowed feed
  media-type list, and ``Accept-Encoding: identity`` with any encoded body refused —
  so there is no decompression step to bomb.
* **Polite.** A stable ``User-Agent`` naming a contact URL, and conditional requests
  (``If-None-Match`` / ``If-Modified-Since``) when the caller holds validators.

``trust_env`` is off, so an ambient ``HTTP(S)_PROXY`` can never re-route the request.

The one relaxation is ``replay_origin`` — a single ``http`` loopback origin used to
replay recorded feed captures in tests and in CI (:mod:`app.connectors.replay`).
Settings validation refuses it outside the development and test environments. A fetcher
in replay mode reaches **only** that exact loopback ``host:port`` — every other URL,
including an allow-listed public host named by a redirect, is refused — so replay can
never turn into third-party egress.

DNS resolution runs under the same overall deadline (bounded by the connect timeout), so
a resolver that never answers cannot hold a worker.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import httpx

from app.connectors.base import FailureKind
from app.connectors.retry import ConnectorFetchError

#: Feed media types accepted (compared after dropping ``; charset=…``).
ALLOWED_MEDIA_TYPES: frozenset[str] = frozenset(
    {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}
)
_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
#: Names that must never be dialled, whatever DNS says about them.
_BLOCKED_HOSTNAMES = frozenset(
    {"localhost", "localhost.localdomain", "metadata", "metadata.google.internal"}
)
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")

#: ``(host, port) -> addresses``; injected in tests, real DNS otherwise.
Resolver = Callable[[str, int], list[str]]


def system_resolver(host: str, port: int) -> list[str]:
    """Every A/AAAA address for ``host`` (order preserved, duplicates dropped)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise ConnectorFetchError(FailureKind.NETWORK, "dns resolution failed") from exc
    seen: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in seen:
            seen.append(address)
    return seen


def address_problem(address: str, *, loopback_ok: bool = False) -> str | None:
    """Why ``address`` must not be dialled, or ``None`` for a global unicast address."""
    try:
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(address)
    except ValueError:
        return "unparseable address"
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.teredo is not None:
            return "teredo address"
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        elif addr.sixtofour is not None:
            addr = addr.sixtofour
        elif addr in _NAT64_PREFIX:
            addr = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    if loopback_ok and addr.is_loopback:
        return None
    if (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
        or addr.is_reserved
        or (isinstance(addr, ipaddress.IPv6Address) and addr.is_site_local)
        or not addr.is_global
    ):
        return "non-public address"
    return None


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def parse_replay_origin(origin: str | None) -> tuple[str, int] | None:
    """``http://127.0.0.1:PORT`` / ``http://[::1]:PORT`` → ``(host, port)``; else None."""
    if not origin:
        return None
    parts = urlsplit(origin)
    try:
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "http"
        or port is None
        or parts.username
        or parts.password
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or host not in ("127.0.0.1", "::1")
    ):
        return None
    return host, port


@dataclass(frozen=True)
class FetchLimits:
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 10.0
    total_timeout_seconds: float = 20.0
    max_response_bytes: int = 2 * 1024 * 1024
    max_redirects: int = 3


@dataclass(frozen=True)
class Validators:
    """Conditional-request validators from a previous 200 for the same feed."""

    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class FeedResponse:
    """A fetched feed, still untrusted until parsed."""

    final_url: str
    status: int
    body: bytes
    media_type: str | None
    etag: str | None
    last_modified: str | None
    retrieved_at: datetime
    redirects: int

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _blocked(detail: str) -> ConnectorFetchError:
    return ConnectorFetchError(FailureKind.EGRESS_BLOCKED, detail)


class SafeFeedFetcher:
    """Fetch one approved feed URL through the guard described in the module doc."""

    def __init__(
        self,
        *,
        user_agent: str,
        limits: FetchLimits | None = None,
        resolver: Resolver | None = None,
        transport: httpx.BaseTransport | None = None,
        replay_origin: tuple[str, int] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._user_agent = user_agent
        self._limits = limits or FetchLimits()
        self._resolve = resolver or system_resolver
        self._transport = transport
        self._replay_origin = replay_origin
        self._clock = clock

    # --- validation ---------------------------------------------------------

    def _is_replay(self, parts: SplitResult) -> bool:
        if self._replay_origin is None or parts.scheme != "http":
            return False
        try:
            port = parts.port
        except ValueError:
            return False
        return ((parts.hostname or "").lower(), port) == self._replay_origin

    def check_url(self, url: str, allowed_hosts: frozenset[str]) -> SplitResult:
        """Refuse any URL that is not an allow-listed https target (or the replay origin)."""
        parts = urlsplit(url)
        if parts.username or parts.password or "@" in parts.netloc:
            raise _blocked("embedded credentials refused")
        try:
            port = parts.port
        except ValueError as exc:
            raise _blocked("malformed port") from exc
        if self._is_replay(parts):
            return parts
        if self._replay_origin is not None:
            raise _blocked("replay mode reaches only the replay origin")
        if parts.scheme != "https":
            raise _blocked("scheme not permitted")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            raise _blocked("missing host")
        if _is_ip_literal(host):
            raise _blocked("raw IP address refused")
        if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
            raise _blocked("internal hostname refused")
        if host not in allowed_hosts:
            raise _blocked("host not in the source allow-list")
        if port not in (None, 443):
            raise _blocked("port not permitted")
        return parts

    def _resolve_within(self, host: str, port: int, seconds: float) -> list[str]:
        """Run the resolver with a deadline; a resolver that never answers times out."""
        outcome: dict[str, object] = {}

        def run() -> None:
            try:
                outcome["addresses"] = self._resolve(host, port)
            except BaseException as exc:  # handed back to the caller's thread
                outcome["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(max(seconds, 0.0))
        if worker.is_alive():
            raise ConnectorFetchError(FailureKind.TIMEOUT, "dns resolution timed out")
        if "error" in outcome:
            raise outcome["error"]  # type: ignore[misc]
        return list(outcome["addresses"])  # type: ignore[arg-type]

    def _pin(self, parts: SplitResult, deadline: float) -> str:
        """Resolve the hop's host and return the one checked address to dial."""
        host = (parts.hostname or "").lower()
        if self._is_replay(parts):
            problem = address_problem(host, loopback_ok=True)
            if problem:
                raise _blocked(problem)
            return host
        budget = min(self._limits.connect_timeout_seconds, deadline - self._clock())
        addresses = self._resolve_within(host, parts.port or 443, budget)
        if not addresses:
            raise ConnectorFetchError(FailureKind.NETWORK, "dns returned no addresses")
        for address in addresses:
            if address_problem(address):
                raise _blocked("host resolves to a non-public address")
        return addresses[0]

    # --- fetch --------------------------------------------------------------

    def fetch(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        validators: Validators | None = None,
    ) -> FeedResponse:
        limits = self._limits
        deadline = self._clock() + limits.total_timeout_seconds
        current = url
        for hop in range(limits.max_redirects + 1):
            parts = self.check_url(current, allowed_hosts)
            pinned = self._pin(parts, deadline)
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ConnectorFetchError(FailureKind.TIMEOUT, "fetch deadline exceeded")
            outcome = self._request(
                parts,
                pinned,
                validators=validators if hop == 0 else None,
                remaining=remaining,
                deadline=deadline,
                redirects=hop,
            )
            if isinstance(outcome, FeedResponse):
                return outcome
            current = urljoin(current, outcome)
        raise _blocked("redirect limit exceeded")

    def _request(
        self,
        parts: SplitResult,
        pinned: str,
        *,
        validators: Validators | None,
        remaining: float,
        deadline: float,
        redirects: int,
    ) -> FeedResponse | str:
        """One hop. Returns the response, or the raw ``Location`` of a redirect."""
        limits = self._limits
        host = (parts.hostname or "").lower()
        dial = f"[{pinned}]" if ":" in pinned else pinned
        if parts.port is not None:
            dial = f"{dial}:{parts.port}"
        target = urlunsplit((parts.scheme, dial, parts.path or "/", parts.query, ""))
        host_header = host if ":" not in host else f"[{host}]"
        if parts.port is not None:
            host_header = f"{host_header}:{parts.port}"
        headers = {
            "Host": host_header,
            "User-Agent": self._user_agent,
            "Accept": _ACCEPT,
            "Accept-Encoding": "identity",
        }
        if validators and validators.etag:
            headers["If-None-Match"] = validators.etag
        if validators and validators.last_modified:
            headers["If-Modified-Since"] = validators.last_modified
        extensions = {"sni_hostname": host} if parts.scheme == "https" else {}
        timeout = httpx.Timeout(
            connect=min(limits.connect_timeout_seconds, remaining),
            read=min(limits.read_timeout_seconds, remaining),
            write=min(limits.read_timeout_seconds, remaining),
            pool=min(limits.connect_timeout_seconds, remaining),
        )
        try:
            with httpx.Client(
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
                timeout=timeout,
            ) as client:
                with client.stream("GET", target, headers=headers, extensions=extensions) as resp:
                    return self._read(resp, parts, validators, deadline, redirects)
        except httpx.TimeoutException as exc:
            raise ConnectorFetchError(FailureKind.TIMEOUT, "source timed out") from exc
        except httpx.TransportError as exc:
            raise ConnectorFetchError(FailureKind.NETWORK, "connection to source failed") from exc

    def _read(
        self,
        resp: httpx.Response,
        parts: SplitResult,
        validators: Validators | None,
        deadline: float,
        redirects: int,
    ) -> FeedResponse | str:
        status = resp.status_code
        if status in _REDIRECT_STATUSES:
            location = resp.headers.get("location")
            if not location:
                raise ConnectorFetchError(FailureKind.UPSTREAM_ERROR, "redirect without location")
            return location
        final_url = urlunsplit(parts._replace(fragment=""))
        retrieved_at = datetime.now(UTC)
        if status == 304:
            if validators is None:
                raise ConnectorFetchError(FailureKind.UPSTREAM_ERROR, "unexpected 304")
            return FeedResponse(
                final_url=final_url,
                status=304,
                body=b"",
                media_type=None,
                etag=resp.headers.get("etag") or validators.etag,
                last_modified=resp.headers.get("last-modified") or validators.last_modified,
                retrieved_at=retrieved_at,
                redirects=redirects,
            )
        if status == 429:
            raise ConnectorFetchError(
                FailureKind.RATE_LIMITED,
                "source rate limited",
                retry_after_seconds=_retry_after(resp.headers.get("retry-after")),
            )
        if 400 <= status < 500:
            raise ConnectorFetchError(FailureKind.HTTP_CLIENT_ERROR, f"source answered {status}")
        if status != 200:
            raise ConnectorFetchError(
                FailureKind.UPSTREAM_ERROR,
                f"source answered {status}",
                retry_after_seconds=_retry_after(resp.headers.get("retry-after")),
            )
        encoding = (resp.headers.get("content-encoding") or "identity").strip().lower()
        if encoding not in ("", "identity"):
            raise ConnectorFetchError(FailureKind.UNSAFE_CONTENT, "encoded response refused")
        media_type = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if media_type not in ALLOWED_MEDIA_TYPES:
            raise ConnectorFetchError(FailureKind.UNSAFE_CONTENT, "unexpected media type")
        cap = self._limits.max_response_bytes
        declared = resp.headers.get("content-length")
        if declared and declared.strip().isdigit() and int(declared) > cap:
            raise ConnectorFetchError(FailureKind.UNSAFE_CONTENT, "response exceeds size limit")
        body = bytearray()
        # A transport may hand over an already-buffered body; it is capped the same way.
        chunks = [resp.content] if resp.is_stream_consumed else resp.iter_raw()
        for chunk in chunks:
            body.extend(chunk)
            if len(body) > cap:
                raise ConnectorFetchError(FailureKind.UNSAFE_CONTENT, "response exceeds size limit")
            if self._clock() > deadline:
                raise ConnectorFetchError(FailureKind.TIMEOUT, "fetch deadline exceeded")
        return FeedResponse(
            final_url=final_url,
            status=200,
            body=bytes(body),
            media_type=media_type,
            etag=resp.headers.get("etag"),
            last_modified=resp.headers.get("last-modified"),
            retrieved_at=retrieved_at,
            redirects=redirects,
        )


def build_user_agent(version: str, contact_url: str | None) -> str:
    """``SignalNestBot/<version> (+<contact>)`` — identifies the fetcher to publishers."""
    contact = contact_url or "contact-not-configured"
    return f"SignalNestBot/{version} (+{contact})"
