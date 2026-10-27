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

#: Stable reason code of a run whose input was clipped by a per-source fetch limit.
FETCH_LIMIT_REACHED = "fetch_limit_reached"


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

    def as_stats(self) -> dict:
        """The JSON object stored under ``stats["fetch_limits"]``."""
        return {
            "source_key": self.source_key,
            "max_items": self.max_items,
            "clipped": self.clipped_by_fetch_limit,
            "fetched": self.fetched,
            "eligible_seen": self.eligible_seen,
        }


@dataclass
class FetchOutcome:
    """A run's signals together with the per-source fetch-limit reports."""

    signals: list = field(default_factory=list)
    reports: list[SourceFetchReport] = field(default_factory=list)


def fetch_limits_stats(reports: list[SourceFetchReport]) -> list[dict]:
    return [r.as_stats() for r in reports]


def is_partial_fetch(reports: list[SourceFetchReport]) -> bool:
    return any(r.clipped_by_fetch_limit for r in reports)


def fetch_stats(reports: list[SourceFetchReport]) -> dict:
    """The fetch facts a run's ``stats`` carry: ``fetch_limits``, ``partial_fetch`` and,
    only when a source was clipped, ``partial_fetch_reason``."""
    out: dict = {
        "fetch_limits": fetch_limits_stats(reports),
        "partial_fetch": is_partial_fetch(reports),
    }
    if out["partial_fetch"]:
        out["partial_fetch_reason"] = FETCH_LIMIT_REACHED
    return out
