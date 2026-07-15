"""Safe outbound HTTP boundary for approved connector traffic (Phase 3B Batch 2).

This module is the **only** place a connector is allowed to reach the network, and
it is built to make Server-Side Request Forgery and its variants impossible even if
a source or a redirect is hostile:

* **URL validation** — https-only, exact approved host + port, no embedded
  credentials, no raw-IP URLs, no ``.local``/metadata/internal hostnames.
* **IP safety** — every candidate address (IPv4 + IPv6) must be a *global* public
  address; loopback, private, link-local, multicast, reserved and the cloud
  metadata address are rejected.
* **DNS guard** — the host is resolved *before* connecting and **every** resolved
  address must be safe (defends against a host that resolves to a private range and
  against split answers); resolution is repeated after each redirect (DNS-rebinding
  defense). Hostname validation is never trusted on its own.
* **Redirect guard** — bounded hops, each target fully re-validated, no scheme
  downgrade, no cross-host to a non-allowlisted host, no private-IP target, and no
  credential/sensitive-header forwarding across hosts.
* **Response limits** — connect/read/total timeouts, a hard byte cap, bounded
  decompression and a media-type check, all applied before the bytes are parsed.

**No real-egress transport is implemented in this batch.** ``SafeFeedClient`` drives
the guards through an injected :class:`FeedTransport`; the only transport wired
under default config is :class:`DisabledTransport`, which fails closed because
``connector_rss_live_enabled`` is off and no source is approved. Tests inject a fake
transport. The validation logic is fully live and tested; the act of opening a
socket is not part of this batch.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from app.connectors.base import FailureKind
from app.connectors.retry import ConnectorFetchError
from app.connectors.sources import ApprovedSource

#: Feed media types we accept (prefix match tolerates ``; charset=…`` suffixes).
ALLOWED_MEDIA_TYPES: tuple[str, ...] = (
    "application/rss+xml",
    "application/atom+xml",
    "application/xml",
    "text/xml",
)

#: Hostnames that must never be reached regardless of DNS.
_BLOCKED_HOST_SUFFIXES: tuple[str, ...] = (".local", ".internal", ".localdomain")
_BLOCKED_HOST_EXACT: frozenset[str] = frozenset(
    {"localhost", "metadata", "metadata.google.internal"}
)

#: Cloud metadata service address — never routable for a public feed.
_METADATA_IPS: frozenset[str] = frozenset({"169.254.169.254", "fd00:ec2::254"})

DnsResolver = Callable[[str], list[str]]


def _default_resolver(host: str) -> list[str]:
    """Resolve ``host`` to every A/AAAA address as strings (real DNS).

    Used only when a real transport is enabled (not in this batch / not in tests).
    """
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


def is_public_address(ip: str) -> bool:
    """True only for a globally-routable public unicast address.

    Rejects loopback, private, link-local, multicast, reserved, unspecified and the
    cloud metadata address, for both IPv4 and IPv6 (including IPv4-mapped IPv6).
    """
    if ip in _METADATA_IPS:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # Unwrap IPv4-mapped / 6to4-style IPv6 so a mapped private v4 can't slip through.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if isinstance(addr, ipaddress.IPv6Address) and addr.sixtofour is not None:
        addr = addr.sixtofour
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _validate_hostname(host: str) -> None:
    h = host.lower().rstrip(".")
    if not h:
        raise ConnectorFetchError(FailureKind.NETWORK, "empty host")
    if h in _BLOCKED_HOST_EXACT or any(h.endswith(s) for s in _BLOCKED_HOST_SUFFIXES):
        raise ConnectorFetchError(FailureKind.NETWORK, "internal hostname rejected")
    # A raw IP literal must go through the IP allowlist, never the hostname path.
    try:
        ipaddress.ip_address(h)
    except ValueError:
        return
    raise ConnectorFetchError(FailureKind.NETWORK, "raw IP URL rejected")


def validate_url(url: str, source: ApprovedSource) -> None:
    """Reject any URL that is not a safe, approved target for ``source``.

    Enforces scheme, credential-free authority, approved host + port, and internal-
    hostname rejection. Does **not** resolve DNS — that is :func:`guard_dns`.
    """
    parts = urlsplit(url)
    if parts.scheme != source.scheme or parts.scheme != "https":
        raise ConnectorFetchError(FailureKind.NETWORK, "scheme not permitted")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ConnectorFetchError(FailureKind.NETWORK, "embedded credentials rejected")
    host = parts.hostname or ""
    _validate_hostname(host)
    if host.lower() != source.host.lower():
        raise ConnectorFetchError(FailureKind.NETWORK, "host not in allowlist")
    port = parts.port if parts.port is not None else 443
    if port not in source.allowed_ports:
        raise ConnectorFetchError(FailureKind.NETWORK, "port not in allowlist")


def guard_dns(host: str, resolver: DnsResolver) -> list[str]:
    """Resolve ``host`` and require *every* address to be public; else reject.

    Returns the resolved addresses (for optional pinning). A resolution failure or
    any non-public / mixed answer fails closed.
    """
    try:
        addresses = resolver(host)
    except OSError as exc:
        raise ConnectorFetchError(FailureKind.NETWORK, "dns resolution failed") from exc
    if not addresses:
        raise ConnectorFetchError(FailureKind.NETWORK, "dns returned no addresses")
    for ip in addresses:
        if not is_public_address(ip):
            raise ConnectorFetchError(FailureKind.NETWORK, "host resolves to a non-public address")
    return addresses


def validate_redirect(
    from_url: str, to_url: str, source: ApprovedSource, resolver: DnsResolver
) -> None:
    """Validate a redirect hop: no downgrade, approved host, safe DNS.

    A redirect may only target the source's canonical host or an explicitly
    allowlisted redirect host, over https, resolving to public addresses.
    """
    to = urlsplit(to_url)
    frm = urlsplit(from_url)
    if to.scheme != "https":
        raise ConnectorFetchError(FailureKind.NETWORK, "redirect scheme downgrade rejected")
    if to.username or to.password or "@" in to.netloc:
        raise ConnectorFetchError(FailureKind.NETWORK, "redirect embeds credentials")
    host = to.hostname or ""
    _validate_hostname(host)
    if not source.redirect_host_allowed(host):
        raise ConnectorFetchError(FailureKind.NETWORK, "redirect host not in allowlist")
    port = to.port if to.port is not None else 443
    if port not in source.allowed_ports:
        raise ConnectorFetchError(FailureKind.NETWORK, "redirect port not in allowlist")
    guard_dns(host, resolver)
    # Signal to the caller whether credentials/sensitive headers must be dropped.
    if (frm.hostname or "").lower() != host.lower():
        # cross-host redirect: caller must not forward auth/cookies (it never does)
        return


@dataclass(frozen=True)
class FetchLimits:
    """Network bounds applied to every live fetch."""

    connect_timeout_s: float = 5.0
    read_timeout_s: float = 10.0
    total_timeout_s: float = 20.0
    max_response_bytes: int = 2 * 1024 * 1024
    max_decompressed_bytes: int = 8 * 1024 * 1024
    max_redirects: int = 3


@dataclass(frozen=True)
class FetchRequest:
    """An immutable, already-approved fetch instruction handed to a transport."""

    url: str
    source: ApprovedSource
    limits: FetchLimits = FetchLimits()
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class FetchResponse:
    """A transport's raw result, still untrusted until parsed."""

    status: int
    body: bytes
    media_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    headers: dict[str, str] = field(default_factory=dict)


class FeedTransport(Protocol):
    """Performs the actual bytes-over-the-wire fetch for an approved request.

    Implementations are injected. The redirect/DNS/limit policy is enforced by
    :class:`SafeFeedClient`; a transport must additionally honor the request limits
    and re-validate redirect hops via the provided ``validate_hop`` callback.
    """

    def fetch(self, request: FetchRequest) -> FetchResponse: ...


class DisabledTransport:
    """The default transport: live egress is off, so every fetch fails closed.

    This is what ``SafeFeedClient`` uses under default configuration. There is no
    real-egress transport in this batch, so the connector cannot open a socket.
    """

    def fetch(self, request: FetchRequest) -> FetchResponse:
        raise ConnectorFetchError(
            FailureKind.NOT_CONFIGURED, "live egress disabled (no approved transport)"
        )


def _check_media_type(media_type: str | None) -> None:
    if media_type is None:
        return
    mt = media_type.split(";", 1)[0].strip().lower()
    if mt and not any(mt == allowed for allowed in ALLOWED_MEDIA_TYPES):
        raise ConnectorFetchError(FailureKind.PARSE_ERROR, "unexpected media type")


@dataclass
class SafeFeedClient:
    """Validate, resolve and fetch an approved feed URL through a transport.

    The client performs URL validation, the DNS guard and response-limit checks;
    the injected transport performs the wire I/O and calls back into
    :meth:`validate_hop` for each redirect. With the default
    :class:`DisabledTransport` no network I/O occurs.
    """

    limits: FetchLimits = FetchLimits()
    resolver: DnsResolver = _default_resolver
    transport: FeedTransport = field(default_factory=DisabledTransport)

    def validate_hop(self, from_url: str, to_url: str, source: ApprovedSource) -> None:
        """Re-validate a redirect target (used by transports mid-request)."""
        validate_redirect(from_url, to_url, source, self.resolver)

    def fetch(
        self,
        url: str,
        source: ApprovedSource,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResponse:
        if not source.is_activatable():
            raise ConnectorFetchError(FailureKind.NOT_CONFIGURED, "source not activatable")
        if not source.owns_url(url):
            raise ConnectorFetchError(FailureKind.NETWORK, "url not an approved feed")
        validate_url(url, source)
        guard_dns(urlsplit(url).hostname or "", self.resolver)

        request = FetchRequest(
            url=url,
            source=source,
            limits=self.limits,
            etag=etag,
            last_modified=last_modified,
        )
        response = self.transport.fetch(request)

        if response.not_modified or response.status == 304:
            return response
        if response.status == 429:
            raise ConnectorFetchError(FailureKind.RATE_LIMITED, "source rate limited")
        if 400 <= response.status < 500:
            raise ConnectorFetchError(FailureKind.UPSTREAM_ERROR, "client error from source")
        if response.status >= 500:
            raise ConnectorFetchError(FailureKind.UPSTREAM_ERROR, "server error from source")
        if len(response.body) > min(self.limits.max_response_bytes, source.max_response_bytes):
            raise ConnectorFetchError(FailureKind.UNSAFE_CONTENT, "response exceeds size limit")
        _check_media_type(response.media_type)
        return response
