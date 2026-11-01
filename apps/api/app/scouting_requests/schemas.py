from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.connectors import fetch_limits
from app.connectors.coverage import CoverageReason, CoverageStatus
from app.core.enums import ScheduleInterval, ScheduleState, SourceType
from app.infra.vector import SIGNAL_VOLUME_ABOVE_BOUND
from app.jobs.outcomes import RunOutcome, RunOutcomeReason


class ScoutRequestCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    location_id: str | None = None
    campaign_id: str | None = None
    source_types: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    product_profile_id: str | None = None
    resolved_market: str | None = None
    notes: str | None = None


#: Fields an edit may not set to ``null``: a Scout always has a name, and a null source list or
#: keyword list would silently change what every later run reads (all source types; the
#: business context's keywords).
_NOT_NULLABLE = ("name", "source_types", "keywords")


class ScoutRequestUpdate(BaseModel):
    """An edit of an existing Scout (W16 customer repairs, PROPOSED; create is unchanged).

    Only the fields sent change. ``name`` is required to be non-blank (1-200 characters after
    trimming); ``source_types`` are the supported source types (repeats dropped); ``keywords`` are
    trimmed, emptied ones dropped and repeats (ignoring case) dropped. ``name``, ``source_types``
    and ``keywords`` cannot be ``null``; ``resolved_market`` and ``notes`` can (clearing them).
    Unknown fields are refused, so status, location, campaign, product profile, run fence and
    owner can never be set here."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    source_types: list[SourceType] | None = None
    keywords: list[str] | None = None
    resolved_market: str | None = Field(default=None, max_length=200)
    notes: str | None = None

    model_config = {"extra": "forbid"}

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be blank.")
        return value

    @field_validator("source_types")
    @classmethod
    def _distinct_sources(cls, value: list[SourceType] | None) -> list[SourceType] | None:
        if value is None:
            return value
        return list(dict.fromkeys(value))

    @field_validator("keywords")
    @classmethod
    def _clean_keywords(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        seen: set[str] = set()
        out: list[str] = []
        for raw in value:
            word = " ".join(raw.split())
            if word and word.casefold() not in seen:
                seen.add(word.casefold())
                out.append(word)
        return out

    @model_validator(mode="after")
    def _no_null_where_required(self) -> ScoutRequestUpdate:
        for field in _NOT_NULLABLE:
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null.")
        return self

    def changes(self) -> dict:
        """The sent fields as column values (source types as their string values)."""
        out = {}
        for field in self.model_fields_set:
            value = getattr(self, field)
            if field == "source_types" and value is not None:
                value = [s.value for s in value]
            out[field] = value
        return out


class ScoutCoverage(BaseModel):
    """Why a completed run holds the signals it holds (P6-DATA-2).

    ``status`` is ``covered`` (a source serves the market; zero opportunities means
    nothing relevant was found), ``not_covered`` (no configured source serves the
    market — running again will not help) or ``unavailable`` (live sources serve the
    market but all failed this run). ``reason`` is the closed code behind it.
    ``is_simulated`` is true when the run consumed simulated data. ``sources`` and
    ``sources_failed`` count the live sources consulted (0 on simulated paths)."""

    status: CoverageStatus
    reason: CoverageReason
    is_simulated: bool
    sources: int
    sources_failed: int


def coverage_from_stats(stats: object) -> ScoutCoverage | None:
    """The run's coverage outcome from its stats, or ``None`` when it has none
    (a run that predates coverage recording, or one that has not completed)."""
    value = stats.get("coverage") if isinstance(stats, dict) else None
    if not isinstance(value, dict):
        return None
    try:
        return ScoutCoverage.model_validate(value)
    except ValidationError:
        return None


# --------------------------------------------------------------------------- #
# The latest run's outcome (Q1 (a+), W21 FU-Q1-1, PROPOSED): what ran, what did not, and one
# next action, lifted from what the run recorded in ``stats``. The UI never reads those untyped
# keys itself (``apps/web/src/lib/scout-stats.ts``).
# --------------------------------------------------------------------------- #
class ScoutRunOutcomeKind(StrEnum):
    """How completely the latest recorded run read its sources.

    ``complete``: the run completed and every source it consulted was read in full.
    ``partial``: the run completed, but at least one source was clipped by its fetch limit or
    failed. ``refused``: the run was refused at the per-run bound before anything was analysed."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    REFUSED = "refused"


class PartialFetchReason(StrEnum):
    """Why a run did not read everything its sources held (``app.connectors.fetch_limits``)."""

    SOURCE_FAILED = fetch_limits.SOURCE_FAILED
    FETCH_LIMIT_REACHED = fetch_limits.FETCH_LIMIT_REACHED


class SourceFailureReason(StrEnum):
    """Why one source failed and contributed nothing (``app.connectors.fetch_limits``)."""

    FEED_TOO_LARGE = fetch_limits.FEED_TOO_LARGE
    XML_UNSAFE = fetch_limits.XML_UNSAFE
    XML_MALFORMED = fetch_limits.XML_MALFORMED
    RATE_LIMITED = fetch_limits.RATE_LIMITED
    RETRIES_EXHAUSTED = fetch_limits.RETRIES_EXHAUSTED
    RESPONSE_REFUSED = fetch_limits.RESPONSE_REFUSED
    EGRESS_BLOCKED = fetch_limits.EGRESS_BLOCKED
    FETCH_ERROR = fetch_limits.FETCH_ERROR


class RunRefusalReason(StrEnum):
    """Why a run was refused before analysis (P6-PLAT-1 strategy B)."""

    SIGNAL_VOLUME_ABOVE_BOUND = SIGNAL_VOLUME_ABOVE_BOUND


class ScoutRunNextAction(StrEnum):
    """The one next step a customer can take in the product today.

    ``narrow_request``: edit the Scout to use fewer source types or keywords, then run it again
    (a refusal). ``run_again_later``: run the Scout again later (a source failed).
    ``no_action``: nothing the customer can do changes the result (only a fetch limit clipped a
    source: no setting raises it, and a re-run reads the same limit)."""

    NARROW_REQUEST = "narrow_request"
    RUN_AGAIN_LATER = "run_again_later"
    NO_ACTION = "no_action"


class ScoutRunSource(BaseModel):
    """What one consulted source delivered in the run (one ``stats.fetch_limits`` entry).

    ``fetched`` entries were read within the source's limit, ``eligible_seen`` of them matched the
    Scout's keywords. ``clipped`` is true when the source held more than ``max_items`` (``null``:
    the source declares no limit). ``failed`` is true when the source contributed nothing because
    it failed; ``failure_reason`` is then its closed code (``null`` for a code this API does not
    know, so the failure itself is never dropped). Counts and codes only."""

    source_key: str
    max_items: int | None
    fetched: int
    eligible_seen: int
    clipped: bool
    failed: bool
    failure_reason: SourceFailureReason | None

    @field_validator("failure_reason", mode="before")
    @classmethod
    def _known_reason(cls, value: object) -> object:
        known = {r.value for r in SourceFailureReason}
        return value if value is None or value in known else None


class ScoutRunOutcome(BaseModel):
    """The latest recorded run's outcome (Q1 (a+), W21 FU-Q1-1, PROPOSED).

    Lifted read-only from ``stats``, which hold the result of the latest run that completed or
    was refused at the bound (a cancelled, stopped, skipped or otherwise failed run writes no
    stats; its outcome is on the job). **What ran:** ``sources`` (every consulted source, ``[]``
    when none covered the market, ``null`` when the run recorded no per-source facts) and the
    counts in ``stats`` (``RunStats`` keys). **What did not:** ``refusal_reason`` and ``bound``
    for a refusal, ``partial_reasons`` and the clipped or failed ``sources``. **One next
    action:** ``next_action`` (``null`` for a complete run)."""

    kind: ScoutRunOutcomeKind
    sources: list[ScoutRunSource] | None
    partial_reasons: list[PartialFetchReason]
    refusal_reason: RunRefusalReason | None
    bound: int | None
    next_action: ScoutRunNextAction | None

    model_config = {"extra": "forbid"}


def _next_action(
    kind: ScoutRunOutcomeKind, reasons: list[PartialFetchReason]
) -> ScoutRunNextAction | None:
    if kind is ScoutRunOutcomeKind.REFUSED:
        return ScoutRunNextAction.NARROW_REQUEST
    if PartialFetchReason.SOURCE_FAILED in reasons:
        return ScoutRunNextAction.RUN_AGAIN_LATER
    if kind is ScoutRunOutcomeKind.PARTIAL:
        return ScoutRunNextAction.NO_ACTION
    return None


def run_outcome_from_stats(stats: object) -> ScoutRunOutcome | None:
    """The latest recorded run's outcome from its stats, or ``None`` when they record none.

    ``None`` for a Scout that has not produced a result (``{}``), for a completed run recorded
    before the per-source fetch facts existed (#268: without ``partial_fetch`` "complete" cannot
    be told from "partial"), and for stats that do not validate (never a guess). A refusal
    recorded before #268 is still a refusal, with ``sources`` null."""
    if not isinstance(stats, dict):
        return None
    refused = stats.get("refused") is True
    partial = stats.get("partial_fetch")
    if not refused and not isinstance(partial, bool):
        return None
    try:
        limits = stats.get("fetch_limits")
        sources = (
            [ScoutRunSource.model_validate(entry) for entry in limits]
            if isinstance(limits, list)
            else None
        )
        reasons = (
            [PartialFetchReason(r) for r in stats.get("partial_fetch_reasons") or []]
            if partial is True
            else []
        )
        if refused:
            kind = ScoutRunOutcomeKind.REFUSED
            refusal_reason = RunRefusalReason(stats.get("refusal_reason"))
            bound = stats.get("bound")
        else:
            kind = ScoutRunOutcomeKind.PARTIAL if partial else ScoutRunOutcomeKind.COMPLETE
            refusal_reason, bound = None, None
        return ScoutRunOutcome(
            kind=kind,
            sources=sources,
            partial_reasons=reasons,
            refusal_reason=refusal_reason,
            bound=bound,
            next_action=_next_action(kind, reasons),
        )
    except (ValidationError, ValueError, TypeError):
        return None


class ScoutExecutionState(StrEnum):
    """A Scout's derived execution state (D-25 recovery contract §4, PROPOSED)."""

    IDLE = "idle"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    STOPPED = "stopped"
    NEEDS_ATTENTION = "needs_attention"


class ScoutExecutionReason(StrEnum):
    """Why the Scout is in its execution state (a closed, customer-safe code)."""

    OPERATION_OUTCOME_UNKNOWN = "operation_outcome_unknown"
    OPERATION_OVERDUE = "operation_overdue"
    PAUSED_BY_USER = "paused_by_user"
    RUN_ACTIVE = "run_active"
    OPERATION_FINISHING = "operation_finishing"
    RUN_CANCELLED = "run_cancelled"
    RUN_FAILED = "run_failed"
    ADMISSION_UNRESOLVED = "admission_unresolved"
    RUN_INTERRUPTED = "run_interrupted"
    NONE = "none"


class ScoutExecutionNextAction(StrEnum):
    """What can move the Scout on: wait, resume it, run it, or contact support (a customer
    can never clear an unresolved operation)."""

    WAIT = "wait"
    RESUME = "resume"
    RUN_NOW = "run_now"
    CONTACT_SUPPORT = "contact_support"


class ScoutExecutionOut(BaseModel):
    """Derived, read-only execution state of one Scout (D-25 recovery contract §4,
    PROPOSED): computed from committed rows and the database clock on each read; never
    stored. ``needs_attention`` means an earlier operation did not report an outcome and
    SignalNest must confirm it has ended before the Scout can run again. ``since`` is set
    only where a stable timestamp exists."""

    state: ScoutExecutionState
    reason: ScoutExecutionReason
    next_action: ScoutExecutionNextAction
    since: datetime | None = None

    model_config = {"extra": "forbid"}


class ScoutRequestOut(BaseModel):
    id: str
    organization_id: str
    workspace_id: str
    brand_id: str
    location_id: str | None
    campaign_id: str | None
    name: str
    status: str
    source_types: list[str]
    keywords: list[str]
    product_profile_id: str | None
    resolved_market: str | None
    notes: str | None
    last_run_at: datetime | None
    stats: dict
    #: The latest completed run's coverage outcome, lifted from ``stats``.
    coverage: ScoutCoverage | None = None
    #: W21 FU-Q1-1 (PROPOSED): the latest recorded run's outcome, lifted from ``stats``.
    run_outcome: ScoutRunOutcome | None = Field(
        default=None,
        description=(
            "The latest recorded run's outcome (PROPOSED, Q1 (a+)): what ran, what did not and "
            "one next action, lifted from stats. Null when the stats record none (no result yet, "
            "or a completed run recorded before per-source fetch facts existed)."
        ),
    )
    #: D-25 recovery (PROPOSED): the derived execution state; W16 (PROPOSED): on the list too.
    execution: ScoutExecutionOut | None = Field(
        default=None,
        description=(
            "Derived execution state (PROPOSED). Computed on every response that returns the "
            "Scout itself, the list included (W16, PROPOSED); null only where a response does "
            "not compute it."
        ),
    )
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def _lift_coverage(self) -> ScoutRequestOut:
        if self.coverage is None:
            self.coverage = coverage_from_stats(self.stats)
        if self.run_outcome is None:
            self.run_outcome = run_outcome_from_stats(self.stats)
        return self


class ScoutRunResult(BaseModel):
    scout_request_id: str
    status: str
    stats: dict


class TriggerType(StrEnum):
    """How a scouting run came to be enqueued.

    ``manual`` — a user pressed run (the only path that exists today).
    ``scheduled`` — enqueued by a future recurrence tick (SB-B+); surfaced only when
    the source job carries an explicit, server-side trigger marker.
    ``unknown`` — the run predates trigger recording, or the marker is absent; we
    never *guess* a trigger from scheduling columns. Honest-unknown over mislabelling.
    """

    MANUAL = "manual"
    SCHEDULED = "scheduled"
    UNKNOWN = "unknown"


class RunStats(BaseModel):
    """Aggregate per-run counts, sourced verbatim from the durable job's
    ``result_summary``. Present only once a run has produced a summary; a run that
    has not completed carries ``stats: null`` rather than fabricated zeros."""

    scanned: int
    noise_filtered: int
    signals_analyzed: int
    opportunities: int
    #: W16 (PROPOSED): of ``opportunities`` (the findings this run produced), how many were new
    #: cards and how many were already known (seen again). Absent on runs recorded before.
    opportunities_new: int | None = None
    opportunities_seen_again: int | None = None
    coverage: ScoutCoverage | None = None


class RunItem(BaseModel):
    """One scouting run in the request's history — a bounded, customer-safe
    projection of a durable ``Job``. It exposes only fields already published by the
    customer-safe ``JobOut`` boundary (never payloads, hashes, idempotency/lease
    tokens, worker/host/trace identifiers, or raw error text)."""

    id: str
    status: str
    trigger: TriggerType
    is_simulated: bool | None
    attempt_count: int
    max_attempts: int
    last_error_code: str | None
    scheduled_for: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    cancel_requested_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    stats: RunStats | None
    #: W16 (PROPOSED): as ``JobOut.outcome`` / ``JobOut.outcome_reason``.
    outcome: RunOutcome | None = None
    outcome_reason: RunOutcomeReason | None = None


class RunHistoryOut(BaseModel):
    items: list[RunItem]
    total: int
    limit: int
    offset: int


class ScoutScheduleCreate(BaseModel):
    """Request body to attach the single recurring schedule to a scout request.

    Only the cadence is customer-supplied; everything else (market scope, next run,
    audit) is derived server-side. ``interval`` is a bounded enum, so an unsupported
    value (hourly, monthly, cron, …) is rejected at the request boundary as 422.
    """

    interval: ScheduleInterval


class ScoutScheduleOut(BaseModel):
    """Customer-safe projection of a :class:`ScoutSchedule`.

    Exposes only what a customer needs to reason about the cadence — never the
    organization id, idempotency/lease tokens, tick job ids, payloads, contract
    version or any audit/worker internal. ``state`` is derived from the live job
    state (see :func:`app.scouting_requests.schedules.derive_schedule_state`) so the
    UI can distinguish a genuinely running schedule from an enabled-but-inert one
    that still needs activation.
    """

    id: str
    scout_request_id: str
    location_id: str | None
    interval: ScheduleInterval
    state: ScheduleState
    enabled: bool
    next_run_at: datetime | None
    last_tick_at: datetime | None
    #: When the schedule's most recently finished tick *failed* (P6-PLAT-6), else
    #: ``None``. Derived from the tick jobs, never persisted: it clears on its own once
    #: a later tick succeeds. With ``state="active"`` it means one occurrence was lost
    #: and recurrence continued; with ``state="stalled"`` the chain stopped.
    last_tick_failed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SchedulingCapabilityOut(BaseModel):
    """Customer-visible scout-scheduling availability for one workspace (P6-UI-006).

    Exactly one field, decided by the same resolver call the schedule mutation gate
    enforces with. The operator endpoint's ``decided_by`` / ``global_flag`` /
    ``has_override`` / ``override_value`` are governance metadata and are deliberately
    absent: a customer needs only whether the feature is available to them.
    """

    enabled: bool
