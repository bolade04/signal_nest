"""Approved live sources — the egress allow-list and the per-source ToS record.

A live connector may fetch **only** a feed named by an immutable
:class:`SourceRecord` in :data:`APPROVED_SOURCES`. There is no customer-supplied
URL path anywhere: adding, approving or removing a source is a reviewed code change,
so every live source has a pull request, a reviewer and a dated approval reference.

Each record is also the source's provenance / terms-of-service record (P6-PRIV-4):
who publishes the feed, where its terms and robots policy live, which approval
covers it and when that approval was given. Every value is public and non-secret —
this repository is public, so an approval reference is a ticket or decision id,
never a person's address.

``APPROVED_SOURCES`` **ships empty.** No feed has product-owner approval or legal /
terms-of-service sign-off (``docs/project-phase-6-plan.md`` ``P6-D03``, external
prerequisite X1). A record becomes usable only when :meth:`SourceRecord.is_activatable`
holds, and even then nothing is fetched while ``connector_rss_live_enabled`` is off or
``connector_rss_kill_switch`` is on, or after an operator takedown
(:mod:`app.connectors.takedown`).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from urllib.parse import urlsplit


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


#: Stable source identifier: lowercase slug, safe for logs, URLs and a DB column.
SOURCE_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class LegalStatus(StrEnum):
    """Terms-of-service / legal review state. Anything but APPROVED blocks egress."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


def normalize_market(market: str | None) -> str:
    """Canonical form for an *exact* market comparison.

    Casefolded with whitespace collapsed. Deliberately not a substring test: a
    source cleared for ``"London, ON"`` must never serve ``"London, UK"``
    (``P6-DATA-6`` records the substring-matching defect elsewhere).
    """
    return " ".join((market or "").split()).casefold()


@dataclass(frozen=True)
class SourceRecord:
    """One approved (or awaiting approval) live feed and its terms record."""

    #: Stable identifier stamped on every signal this source produces.
    source_key: str
    #: The connector that reads it (only ``rss_news`` exists).
    connector: str
    display_name: str
    #: Who owns and publishes the feed.
    publisher: str
    #: The exact feed URL. The fetcher refuses any other URL for this source.
    feed_url: str
    #: Markets this source is cleared to serve, compared exactly.
    markets: tuple[str, ...]
    #: Where the publisher's terms of use live (public URL).
    tos_url: str
    #: The publisher's robots policy location and the recorded disposition for the
    #: feed path (e.g. "feed path not disallowed for generic agents, reviewed <date>").
    robots_url: str | None = None
    robots_disposition: str | None = None
    #: Reviewed decision covering this feed: a public ticket / decision id.
    approval_reference: str | None = None
    #: Date the approval was given.
    approved_on: date | None = None
    legal_status: LegalStatus = LegalStatus.PENDING
    #: A source never runs unless this is on as well as approved.
    enabled: bool = False
    #: Hosts a redirect from the feed URL may land on (exact, https only).
    allowed_redirect_hosts: tuple[str, ...] = ()
    #: Attribution text shown with the source's items.
    attribution: str = ""
    #: Politeness floor for this source: never fetched more often than this.
    min_fetch_interval_seconds: float | None = None

    @property
    def host(self) -> str:
        return (urlsplit(self.feed_url).hostname or "").lower()

    def serves_market(self, market: str | None) -> bool:
        wanted = normalize_market(market)
        return bool(wanted) and any(normalize_market(m) == wanted for m in self.markets)

    def problems(self) -> list[str]:
        """Structural defects that make the record unusable, whatever its approval."""
        out: list[str] = []
        if not SOURCE_KEY_PATTERN.match(self.source_key):
            out.append("source_key is not a lowercase slug")
        if self.connector != "rss_news":
            out.append("connector must be rss_news")
        parts = urlsplit(self.feed_url)
        if parts.scheme != "https" or not parts.hostname:
            out.append("feed_url must be an absolute https URL")
        if parts.username or parts.password:
            out.append("feed_url must not embed credentials")
        # The egress guard refuses both, so such a record could never be fetched.
        if parts.hostname and _is_ip_literal(parts.hostname):
            out.append("feed_url must name a host, not an IP address")
        try:
            port = parts.port
        except ValueError:
            port = -1
        if port not in (None, 443):
            out.append("feed_url must use the default https port")
        if not self.markets or not all(normalize_market(m) for m in self.markets):
            out.append("markets must name at least one market")
        if urlsplit(self.tos_url).scheme != "https":
            out.append("tos_url must be an https URL")
        return out

    def is_activatable(self) -> bool:
        """True only for an enabled, well-formed record with a dated approval."""
        return (
            self.enabled
            and self.legal_status is LegalStatus.APPROVED
            and bool(self.approval_reference)
            and self.approved_on is not None
            and not self.problems()
        )

    def terms_record(self) -> dict:
        """The public provenance / ToS record (P6-PRIV-4), as plain JSON values."""
        return {
            "source_key": self.source_key,
            "connector": self.connector,
            "display_name": self.display_name,
            "publisher": self.publisher,
            "feed_url": self.feed_url,
            "markets": list(self.markets),
            "tos_url": self.tos_url,
            "robots_url": self.robots_url,
            "robots_disposition": self.robots_disposition,
            "approval_reference": self.approval_reference,
            "approved_on": self.approved_on.isoformat() if self.approved_on else None,
            "legal_status": self.legal_status.value,
            "enabled": self.enabled,
        }


#: The live-source allow-list. **Intentionally empty** until the operator records the
#: first-source decision (``P6-D03``) and its terms sign-off (X1). Adding a record is a
#: reviewed change; see ``docs/operations/live-data-sources.md``.
APPROVED_SOURCES: tuple[SourceRecord, ...] = ()


def registry_problems(sources: tuple[SourceRecord, ...]) -> list[str]:
    """Defects across a registry: per-record structure plus duplicate keys."""
    out: list[str] = []
    seen: set[str] = set()
    for source in sources:
        out.extend(f"{source.source_key}: {p}" for p in source.problems())
        if source.source_key in seen:
            out.append(f"{source.source_key}: duplicate source_key")
        seen.add(source.source_key)
    return out
