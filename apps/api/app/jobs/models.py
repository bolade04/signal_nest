"""Durable job + job-event ORM models.

Two tables back the durable queue:

* ``jobs`` — one row per unit of work, carrying its tenant scope, versioned
  payload, lifecycle status, attempt/lease bookkeeping and safe error/result
  summaries. Indexed for claiming, tenant lookup, availability, lease-expiry
  recovery, idempotency and scout-request lookup.
* ``job_events`` — an **append-only** audit trail of every lifecycle change.
  It records *what happened* (event type, status transition, attempt, worker id,
  safe metadata, error code) but **never** secrets, credentials, raw payloads,
  full customer content, or raw stack traces.

Both are written to be portable across SQLite (local) and PostgreSQL (full),
using the same portable column types as the rest of the schema.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON, DateTime

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.jobs.status import JobStatus


class Job(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A durable unit of work processed at-least-once by a worker."""

    __tablename__ = "jobs"
    __table_args__ = (
        # Tenant-scoped idempotency: a caller-supplied key is unique within a
        # tenant, never globally. A NULL key means "not idempotent" and both
        # SQLite and PostgreSQL treat NULLs as distinct, so unkeyed jobs coexist.
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "idempotency_key",
            name="uq_jobs_tenant_idempotency",
        ),
        # Claiming: eligible rows ordered by availability then priority.
        Index("ix_jobs_claim", "status", "available_at", "priority"),
        # Lease-expiration recovery sweep.
        Index("ix_jobs_lease", "status", "lease_expires_at"),
        # Tenant listing.
        Index("ix_jobs_tenant", "organization_id", "workspace_id"),
        # Idempotency lookup.
        Index("ix_jobs_idempotency", "organization_id", "workspace_id", "idempotency_key"),
    )

    # --- Tenant scope -------------------------------------------------------
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    location_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_locations.id", ondelete="SET NULL"), index=True
    )
    scout_request_id: Mapped[str | None] = mapped_column(
        ForeignKey("scout_requests.id", ondelete="SET NULL"), index=True
    )

    # --- Contract + payload -------------------------------------------------
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(8), nullable=False, default="1")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: Stable hash of the enveloped payload — lets callers detect an idempotency
    #: key reused with a *different* payload (a conflict, not a duplicate).
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))

    # --- Lifecycle ----------------------------------------------------------
    status: Mapped[str] = mapped_column(
        String(40), nullable=False, default=JobStatus.PENDING.value, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    # --- Scheduling + lease -------------------------------------------------
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    #: Opaque per-claim ownership token. Rotated to a fresh random value on every
    #: claim and cleared when ownership ends (terminal outcome or lease recovery).
    #: A worker captures it at claim time and must present it for every subsequent
    #: mutation, so a stale worker whose lease was reclaimed can never write back
    #: (the reclaim rotated the token). Internal-only: never exposed by any API.
    lease_token: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Outcome (safe, secret-free) ----------------------------------------
    last_error_code: Mapped[str | None] = mapped_column(String(40))
    last_error_summary: Mapped[str | None] = mapped_column(Text)
    result_summary: Mapped[dict | None] = mapped_column(JSON)


class JobEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Append-only audit record for a single job lifecycle event."""

    __tablename__ = "job_events"
    __table_args__ = (
        # Audit retrieval: a job's events in insertion order.
        Index("ix_job_events_job", "job_id", "created_at"),
        Index("ix_job_events_tenant", "organization_id", "workspace_id"),
    )

    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # Tenant context is denormalized onto each event so isolation queries and
    # retention policies never need to join back to a possibly-deleted job.
    organization_id: Mapped[str] = mapped_column(String(32), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    location_id: Mapped[str | None] = mapped_column(String(32))

    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(40))
    new_status: Mapped[str | None] = mapped_column(String(40))
    attempt: Mapped[int | None] = mapped_column(Integer)
    worker_id: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(40))
    #: Safe, bounded metadata only (no secrets/payloads/customer content).
    event_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
