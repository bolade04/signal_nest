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

How each source's content was obtained (W21 FU-HELD-BODY-1, PROPOSED): every report a
connector writes says so in ``read_mode`` (:class:`SourceReadMode`). The live connector can
answer a run without contacting a source: inside the politeness interval after a good read
(``reused``), or during the cool-down after a failed fetch, from the copy it holds of the last
good read (``held``). A held copy is not a read of the source: the run is partial
(:data:`SOURCE_HELD`), and the report carries when that copy was last read successfully
(``last_read_at``), why the last fetch failed (``last_failure_reason``) and when the source may
be contacted again (``next_retry_at``). Facts a run did not record are left out of its stats,
never invented: a row written before they existed has none of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

#: The per-source fetch limit (``FetchScope.max_items`` default): entries read per source
#: per run, independent of the per-run in-process bound.
DEFAULT_MAX_ITEMS_PER_SOURCE = 100

#: Stable reason code of a run whose input was clipped by a per-source fetch limit.
FETCH_LIMIT_REACHED = "fetch_limit_reached"
#: Stable reason code of a run in which at least one source failed (and so contributed
#: nothing): a failed source is never presented as an empty one.
SOURCE_FAILED = "source_failed"
#: Stable reason code of a run in which at least one source was not contacted because its last
#: fetch failed and it was cooling down, and the copy held from its last good read was used
#: instead (``read_mode`` ``held``; W21 FU-HELD-BODY-1, PROPOSED). Such a source was not read in
#: this run, so the run is never complete.
SOURCE_HELD = "source_held"

#: Per-source failure reasons (``failure_reason``); closed, static, secret-free.
#: A byte cap refused the feed (the parser's ``MAX_FEED_BYTES`` or, on the live path, the
#: egress guard's ``max_response_bytes``).
FEED_TOO_LARGE = "feed_too_large"
#: The document declares a DOCTYPE/entity (byte screen or ``defusedxml``).
XML_UNSAFE = "xml_unsafe"
#: The document is not a readable feed (malformed XML, no channel, unknown format or
#: encoding) - including a malformation past the fetch limit: the document is rejected whole.
XML_MALFORMED = "xml_malformed"
#: The source is rate limited (a 429, a cool-down it asked for, or no token left).
RATE_LIMITED = "rate_limited"
#: A transient failure (timeout, network, upstream 5xx) outlasted the run's attempts.
RETRIES_EXHAUSTED = "retries_exhausted"
#: Live path (FU-268-2): the egress guard refused the response itself (an encoded body or
#: a media type that is not a feed).
RESPONSE_REFUSED = "response_refused"
#: Live path (FU-268-2): the egress guard refused the target (scheme, host allow-list, a
#: non-public address, a disallowed redirect).
EGRESS_BLOCKED = "egress_blocked"
#: Any other permanent fetch failure (a 4xx other than 429, a source not configured).
FETCH_ERROR = "fetch_error"


class SourceReadMode(StrEnum):
    """How a source that was not failed provided the content a run used (``read_mode``).

    ``fetched``: the source sent its content in this run (a live ``200``, or a simulated source
    read in this run). ``revalidated``: the source answered this run's conditional request with
    ``304 Not Modified``, so the copy held from its last good read is current. ``reused``: the
    source was not contacted, because its last read succeeded less than one politeness interval
    ago. ``held``: the source was not contacted, because its last fetch failed and it is cooling
    down; the copy held from its last good read was used (its age is that read's age).
    """

    FETCHED = "fetched"
    REVALIDATED = "revalidated"
    REUSED = "reused"
    HELD = "held"


def lookahead_entries(max_items: int) -> int:
    """How many entries a connector extracts from one source: one past its fetch limit,
    never more. The extra entry is what tells a clipped source (it held more than
    ``max_items``) from one that held exactly ``max_items``."""
    return max(0, max_items) + 1


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
    #: W21 FU-HELD-BODY-1 (PROPOSED). How the content was obtained (a :class:`SourceReadMode`
    #: value); ``None`` for a failed source, and where the writer does not know (a connector
    #: double without reports).
    read_mode: str | None = None
    #: When the source last answered successfully (``200`` or ``304``) for the copy used;
    #: ``None`` where not known (a simulated source, a failed one).
    last_read_at: datetime | None = None
    #: While the source cools down after a failed fetch: the earliest time the worker that ran
    #: this run will contact it again. ``None`` when it is not cooling down.
    next_retry_at: datetime | None = None
    #: For a ``held`` copy: the closed reason of the failed fetch that started the cool-down.
    last_failure_reason: str | None = None

    def as_stats(self) -> dict:
        """The JSON object stored under ``stats["fetch_limits"]``. The W21 FU-HELD-BODY-1 facts
        are written only when known, so an unknown one is absent (as in a row written before
        they existed), never a guessed value."""
        out: dict = {
            "source_key": self.source_key,
            "max_items": self.max_items,
            "clipped": self.clipped_by_fetch_limit,
            "fetched": self.fetched,
            "eligible_seen": self.eligible_seen,
            "failed": self.failed,
            "failure_reason": self.failure_reason,
        }
        if self.read_mode is not None:
            out["read_mode"] = str(self.read_mode)
        if self.last_read_at is not None:
            out["last_read_at"] = self.last_read_at.isoformat()
        if self.next_retry_at is not None:
            out["next_retry_at"] = self.next_retry_at.isoformat()
        if self.last_failure_reason is not None:
            out["last_failure_reason"] = self.last_failure_reason
        return out


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
    fetch limit (``fetch_limit_reached``), failed (``source_failed``) or was not contacted
    during a failure cool-down, its held copy used instead (``source_held``). Every fact is
    kept: the reasons list names each that applies and never collapses one into another.
    ``partial_fetch_reasons`` is present only when ``partial_fetch`` is true.
    """
    reasons = []
    if any(f.get("failed") for f in fetch_limits):
        reasons.append(SOURCE_FAILED)
    if any(f.get("read_mode") == SourceReadMode.HELD for f in fetch_limits):
        reasons.append(SOURCE_HELD)
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
