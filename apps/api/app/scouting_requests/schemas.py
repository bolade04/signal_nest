from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, ValidationError, model_validator

from app.connectors.coverage import CoverageReason, CoverageStatus
from app.core.enums import ScheduleInterval, ScheduleState


class ScoutRequestCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    location_id: str | None = None
    campaign_id: str | None = None
    source_types: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    product_profile_id: str | None = None
    resolved_market: str | None = None
    notes: str | None = None


class ScoutRequestUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    source_types: list[str] | None = None
    keywords: list[str] | None = None
    resolved_market: str | None = None
    notes: str | None = None


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
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def _lift_coverage(self) -> ScoutRequestOut:
        if self.coverage is None:
            self.coverage = coverage_from_stats(self.stats)
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
    created_at: datetime
    updated_at: datetime
