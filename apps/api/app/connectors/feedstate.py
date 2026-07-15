"""Feed state and deduplication for the live connector path (Phase 3B Batch 2).

Holds the *minimum* state a live fetch needs — conditional-request validators
(ETag / Last-Modified), a content fingerprint, failure counting and a backoff
timestamp — plus a correctly-**scoped** deduplication key.

The dedup key deliberately includes the isolation dimensions
(source + tenant/workspace + location + market + item id + normalized URL /
fingerprint) so that a duplicate seen in one market can **never** suppress the same
headline legitimately surfacing in another market. Deduplication is per-scope, not
global.

State is in-memory here (a small, provider-neutral structure). Durable persistence
is intentionally deferred to the enablement change — no schema/migration is added
while live egress is unapproved.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit


def normalize_url(url: str | None) -> str:
    """Canonicalize a URL for fingerprinting: lowercase host, drop fragment/query.

    Returns ``""`` for a missing URL. This is used only for dedup identity, never
    for fetching.
    """
    if not url:
        return ""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), host, path, "", ""))


def content_fingerprint(*parts: str) -> str:
    """Stable SHA-256 fingerprint over the given content parts."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()


@dataclass(frozen=True)
class DedupScope:
    """The isolation frame a dedup decision is made within.

    Two items with the same item identity but different scopes are **not**
    duplicates — this is what keeps markets/tenants from suppressing each other.
    """

    source_id: str
    tenant_id: str | None = None
    workspace_id: str | None = None
    location_id: str | None = None
    market: str | None = None


def dedup_key(scope: DedupScope, *, item_id: str | None, url: str | None, fingerprint: str) -> str:
    """Compute the scoped dedup key for one feed item."""
    identity = item_id or normalize_url(url) or fingerprint
    return content_fingerprint(
        scope.source_id,
        scope.tenant_id or "",
        scope.workspace_id or "",
        scope.location_id or "",
        scope.market or "",
        identity,
    )


@dataclass
class FeedState:
    """Mutable per-source fetch bookkeeping (in-memory)."""

    source_id: str
    last_success_at: float | None = None
    last_attempt_at: float | None = None
    etag: str | None = None
    last_modified: str | None = None
    last_fingerprint: str | None = None
    failure_count: int = 0
    backoff_until: float | None = None

    def record_success(
        self, *, at: float, etag: str | None, last_modified: str | None, fingerprint: str
    ) -> None:
        self.last_attempt_at = at
        self.last_success_at = at
        self.etag = etag
        self.last_modified = last_modified
        self.last_fingerprint = fingerprint
        self.failure_count = 0
        self.backoff_until = None

    def record_failure(self, *, at: float, backoff_seconds: float) -> None:
        self.last_attempt_at = at
        self.failure_count += 1
        self.backoff_until = at + max(0.0, backoff_seconds)

    def is_backing_off(self, *, now: float) -> bool:
        return self.backoff_until is not None and now < self.backoff_until


@dataclass
class DedupIndex:
    """Tracks seen dedup keys so replayed items are dropped within a scope."""

    _seen: set[str] = field(default_factory=set)

    def is_new(self, key: str) -> bool:
        return key not in self._seen

    def add(self, key: str) -> None:
        self._seen.add(key)

    def mark_if_new(self, key: str) -> bool:
        """Return True and remember the key when it is new; False if a duplicate."""
        if key in self._seen:
            return False
        self._seen.add(key)
        return True
