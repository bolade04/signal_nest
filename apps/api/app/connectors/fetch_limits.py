"""Per-source fetch-limit facts (P6-PLAT-1 boundary behaviour, #268).

A connector reads at most ``FetchScope.max_items`` entries from one source per run (the
*fetch limit*). That limit is independent of the per-run in-process bound
(``vector_inprocess_max_signals_per_run``): the fetch limit applies first, per source;
the bound applies afterwards, per run, to the total over all sources.

Without a record, a source that held more than its limit looks exactly like a source that
held exactly that many. Each connector therefore looks **one entry past the limit** and reports a
:class:`SourceFetchReport`. What is bounded, measured (review r3): the XML parser builds the
whole document tree, bounded only by the existing 2 MiB size cap (at maximum density, about
72,000 entries under the cap: ~0.16 s and ~13 MiB extra peak memory); entry **extraction** is
bounded to ``max_items + 1`` and **normalisation** to ``max_items``. A source that fails (size
cap, unsafe or malformed XML, rate limit, exhausted retries) is reported ``failed`` with a
closed ``failure_reason``, never as an empty source. The run's stats expose the reports as
``fetch_limits`` and ``partial_fetch = true`` when any source was clipped
(:data:`FETCH_LIMIT_REACHED`) or failed (:data:`SOURCE_FAILED`), listing every applicable
reason in ``partial_fetch_reasons`` - an explicit outcome, never ``covered`` by silence. The
demo fixture library declares no per-source limit (``max_items`` null) and is never clipped.
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
