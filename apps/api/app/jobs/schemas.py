"""Serialization contracts for durable-job API responses.

Two disclosure tiers, mirroring the rest of the platform:

* :class:`JobOut` / :class:`JobEventOut` — the **customer** view. Lifecycle and
  outcome only. It deliberately omits every operational/infrastructure field
  (worker id, lease/heartbeat/claim timestamps, raw payload, payload hash,
  idempotency key, raw error summary).
* :class:`JobOperatorOut` — the **operator** diagnostics view. It may surface
  safe worker/lease diagnostics but still never a raw payload or any secret.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class JobOut(BaseModel):
    """Customer-safe job view: lifecycle + outcome, no infrastructure detail."""

    id: str
    job_type: str
    status: str
    scout_request_id: str | None
    location_id: str | None
    attempt_count: int
    max_attempts: int
    priority: int
    last_error_code: str | None
    result_summary: dict | None
    scheduled_for: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    cancel_requested_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class JobListOut(BaseModel):
    """A paginated page of customer-safe jobs."""

    items: list[JobOut]
    total: int
    limit: int
    offset: int


class JobEventOut(BaseModel):
    """Customer-safe audit event: what happened, never how/where."""

    id: str
    event_type: str
    previous_status: str | None
    new_status: str | None
    attempt: int | None
    error_code: str | None
    event_metadata: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class JobOperatorOut(BaseModel):
    """Operator diagnostics view: safe worker/lease detail, never a raw payload."""

    id: str
    organization_id: str
    workspace_id: str
    location_id: str | None
    scout_request_id: str | None
    job_type: str
    contract_version: str
    status: str
    priority: int
    attempt_count: int
    max_attempts: int
    payload_hash: str
    worker_id: str | None
    available_at: datetime | None
    scheduled_for: datetime | None
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    cancel_requested_at: datetime | None
    cancelled_at: datetime | None
    last_error_code: str | None
    last_error_summary: str | None
    result_summary: dict | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class JobDiagnosticsOut(BaseModel):
    """Operator queue snapshot: status counts + a page of recent jobs."""

    status_counts: dict[str, int]
    recent: list[JobOperatorOut]


class JobPageOut(BaseModel):
    """A bounded, cross-tenant operator page of jobs (filterable listing)."""

    items: list[JobOperatorOut]
    total: int
    limit: int
    offset: int


class StuckJobsOut(BaseModel):
    """Operator stuck-job summary: a live count plus a bounded page.

    ``stale_after_seconds`` is the heartbeat-staleness threshold used to classify
    (the configured worker stale bound); ``as_of`` is the injected evaluation
    clock the count/page was computed against, so the read is self-describing.
    """

    stuck_count: int
    stale_after_seconds: float
    as_of: datetime
    limit: int
    offset: int
    items: list[JobOperatorOut]


class DeadLetterJobsOut(BaseModel):
    """Operator dead-letter summary: total count plus a bounded recent page."""

    total: int
    limit: int
    offset: int
    items: list[JobOperatorOut]
