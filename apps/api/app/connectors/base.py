"""Connector contract — the shape every scouting source produces and obeys.

A ``SourceConnector`` turns a per-request :class:`FetchScope` into a
:class:`ConnectorResult`: a list of :class:`ConnectorSignal` items plus
connector-level metadata (attribution, retrieval time, degraded flag, classified
failures). The public :meth:`SourceConnector.fetch` returns just the signal list
and keeps the historical ``fetch(market=, keywords=, source_types=)`` signature,
so a real connector is a drop-in replacement for the fixture connector at the
existing seam with **no pipeline change**.

``ConnectorSignal`` is deliberately a superset of every attribute the scout
pipeline reads (``app/jobs/pipeline.py``), with neutral defaults for the
scoring hints a public source cannot supply. This lets the pipeline consume a
connector's output exactly as it consumes fixtures today.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class FailureKind(StrEnum):
    """Coarse, secret-free classification of a single fetch failure.

    ``retryable`` is derived from the kind so a connector never retries a
    permanent client/parse error and never gives up on a transient one.
    """

    TIMEOUT = "timeout"
    NETWORK = "network"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_ERROR = "upstream_error"
    PARSE_ERROR = "parse_error"
    UNSAFE_CONTENT = "unsafe_content"
    NOT_CONFIGURED = "not_configured"

    @property
    def retryable(self) -> bool:
        return self in {
            FailureKind.TIMEOUT,
            FailureKind.NETWORK,
            FailureKind.RATE_LIMITED,
            FailureKind.UPSTREAM_ERROR,
        }


@dataclass(frozen=True)
class FetchFailure:
    """One classified failure. ``detail`` is a static, secret-free summary."""

    kind: FailureKind
    detail: str

    @property
    def retryable(self) -> bool:
        return self.kind.retryable


@dataclass(frozen=True)
class FetchScope:
    """The per-request boundary a connector must stay inside.

    Carries only what a connector needs to scope its results to one market /
    location and never blend across tenants: the resolved market, the request's
    keywords, the enabled source types and a hard item cap. A connector must not
    return signals outside ``market`` when a market is set.
    """

    market: str | None
    keywords: tuple[str, ...] = ()
    source_types: tuple[str, ...] = ()
    max_items: int = 100


@dataclass
class ConnectorSignal:
    """A single normalized item emitted by a connector.

    Superset of the pipeline's read surface. Fields a public source cannot
    supply (engagement, buying intent, ad activity, ...) default to neutral so a
    connector never fabricates commercial signal it did not observe.
    """

    source_type: str
    content: str
    market: str | None
    source_url: str | None = None
    author: str | None = None
    language: str = "en"
    topics: list[str] = field(default_factory=list)
    published_days_ago: float = 0.0
    engagement: int = 0
    distinct_source_types: int = 1
    duplicate_count: int = 1
    has_buying_intent: bool = False
    has_active_ads: bool = False
    news_coverage: bool = False
    search_trend_up: bool = False
    geo_evidence: list[tuple[str, str]] = field(default_factory=list)
    # Real connectors set this False for live data; the sandbox keeps it True so
    # nothing simulated is ever presented as observed production data.
    is_simulated: bool = True
    # Non-secret provenance stamped onto every signal: which connector produced
    # it, the source's own title, and when it was retrieved. Never a credential.
    attribution: dict = field(default_factory=dict)


@dataclass
class ConnectorResult:
    """The full outcome of one connector run for one request."""

    connector: str
    source_type: str
    signals: list[ConnectorSignal] = field(default_factory=list)
    failures: list[FetchFailure] = field(default_factory=list)
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def degraded(self) -> bool:
        """True when at least one source failed but the run still returned."""
        return bool(self.failures)


class SourceConnector(abc.ABC):
    """Base class for every scouting connector.

    Subclasses implement :meth:`collect` (the source-specific fetch + parse +
    normalize) and declare their identity. The base :meth:`fetch` adapts the
    rich result to the historical list-returning seam so connectors are
    interchangeable with the fixture connector.
    """

    #: Stable, non-secret connector identifier (e.g. ``"rss_news"``).
    name: str
    #: The :class:`app.core.enums.SourceType` value this connector produces.
    source_type: str
    #: Whether this connector's data is simulated (sandbox) rather than live.
    is_simulated: bool = True

    @abc.abstractmethod
    def collect(self, scope: FetchScope) -> ConnectorResult:
        """Fetch, parse and normalize source data within ``scope``."""

    def fetch(
        self, *, market: str | None, keywords: list[str], source_types: list[str]
    ) -> list[ConnectorSignal]:
        """Drop-in seam: return only the normalized signals for a request."""
        scope = FetchScope(
            market=market,
            keywords=tuple(keywords or ()),
            source_types=tuple(source_types or ()),
        )
        return self.collect(scope).signals
