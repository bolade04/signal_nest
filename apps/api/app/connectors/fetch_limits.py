"""Per-source fetch-limit facts (P6-PLAT-1 boundary behaviour, #268).

A connector reads at most ``FetchScope.max_items`` entries from one source per run (the
*fetch limit*). That limit is independent of the per-run in-process bound
(``vector_inprocess_max_signals_per_run``): the fetch limit applies first, per source;
the bound applies afterwards, per run, to the total over all sources.

Without a record, a source that held more than its limit looks exactly like a source that
held exactly that many. Each connector therefore looks **one entry past the limit** (never
parsing more than ``max_items + 1`` entries, and never reading more bytes than its existing
size cap) and reports a :class:`SourceFetchReport`. The run's stats expose those reports as
``fetch_limits`` and, when any source was clipped, ``partial_fetch = true`` with the reason
:data:`FETCH_LIMIT_REACHED` - a new explicit outcome, never ``covered`` by silence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The per-source fetch limit (``FetchScope.max_items`` default): entries read per source
#: per run, independent of the per-run in-process bound.
DEFAULT_MAX_ITEMS_PER_SOURCE = 100

#: Stable reason code of a run whose input was clipped by a per-source fetch limit.
FETCH_LIMIT_REACHED = "fetch_limit_reached"
#: Stable reason code of a run in which at least one source failed (and so contributed
#: nothing): a failed source is never presented as an empty one.
SOURCE_FAILED = "source_failed"

#: Per-source failure reasons (``failure_reason``); closed, static, secret-free.
FEED_TOO_LARGE = "feed_too_large"
XML_UNSAFE = "xml_unsafe"
XML_MALFORMED = "xml_malformed"
RATE_LIMITED = "rate_limited"
RETRIES_EXHAUSTED = "retries_exhausted"
FETCH_ERROR = "fetch_error"


@dataclass(frozen=True)
class SourceFetchReport:
    """What one source delivered against its fetch limit in one run.

    ``fetched`` is the number of entries read within the limit (never above it) and
    ``eligible_seen`` how many of those matched the request's scope (keywords) and became
    signals. ``clipped_by_fetch_limit`` is true when the source held at least one more
    entry beyond the limit (the one-item look-ahead). ``max_items`` is ``None`` for a
    source that declares no fetch limit (the demo fixture library). Safe aggregates only.
    """

    source_key: str
    max_items: int | None
    fetched: int
    eligible_seen: int
    clipped_by_fetch_limit: bool
    #: True when the source contributed nothing because it failed (byte cap, unsafe or
    #: malformed XML, rate limit, exhausted retries): ``fetched`` is then 0 and says
    #: nothing about what the source held. ``failure_reason`` is one of the closed codes.
    failed: bool = False
    failure_reason: str | None = None

    def as_stats(self) -> dict:
        """The JSON object stored under ``stats["fetch_limits"]``."""
        return {
            "source_key": self.source_key,
            "max_items": self.max_items,
            "clipped": self.clipped_by_fetch_limit,
            "fetched": self.fetched,
            "eligible_seen": self.eligible_seen,
            "failed": self.failed,
            "failure_reason": self.failure_reason,
        }


@dataclass
class FetchOutcome:
    """A run's signals together with the per-source fetch-limit reports."""

    signals: list = field(default_factory=list)
    reports: list[SourceFetchReport] = field(default_factory=list)


def fetch_limits_stats(reports: list[SourceFetchReport]) -> list[dict]:
    return [r.as_stats() for r in reports]


def partial_fetch_facts(fetch_limits: list[dict]) -> dict:
    """``partial_fetch`` and ``partial_fetch_reasons`` from per-source stats entries.

    The run did not read everything its sources held when any source was clipped by its
    fetch limit (``fetch_limit_reached``) or failed (``source_failed``). Both facts are
    kept: the reasons list names each that applies and never collapses one into the
    other. ``partial_fetch_reasons`` is present only when ``partial_fetch`` is true.
    """
    reasons = []
    if any(f.get("failed") for f in fetch_limits):
        reasons.append(SOURCE_FAILED)
    if any(f.get("clipped") for f in fetch_limits):
        reasons.append(FETCH_LIMIT_REACHED)
    out: dict = {"partial_fetch": bool(reasons)}
    if reasons:
        out["partial_fetch_reasons"] = reasons
    return out


def is_partial_fetch(reports: list[SourceFetchReport]) -> bool:
    return partial_fetch_facts(fetch_limits_stats(reports))["partial_fetch"]


def fetch_stats(reports: list[SourceFetchReport]) -> dict:
    """The fetch facts a run's ``stats`` carry: ``fetch_limits``, ``partial_fetch`` and,
    only when a source was clipped or failed, ``partial_fetch_reasons``."""
    limits = fetch_limits_stats(reports)
    return {"fetch_limits": limits, **partial_fetch_facts(limits)}
