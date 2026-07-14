"""Worker-fleet registry ORM model.

One row per worker *process* records its identity, lifecycle status and last
heartbeat so operators can see which workers are alive, busy, draining or stale.

Deliberately **not** stored: credentials, environment variables, raw host
metadata, IP addresses, lease tokens, or job payloads. The only host signal is an
optional, non-reversible ``host_fingerprint`` (a truncated hash of the hostname)
used to correlate co-located workers without persisting the hostname itself.

Portable across SQLite (local) and PostgreSQL (full) via the shared column types.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON, DateTime

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.jobs.worker_status import WorkerStatus


class WorkerRegistration(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A registered worker process and its current fleet status."""

    __tablename__ = "worker_registrations"
    __table_args__ = (
        # A worker id identifies exactly one live registration row.
        UniqueConstraint("worker_id", name="uq_worker_registrations_worker_id"),
        # Operator filters: by status, by liveness (heartbeat age), by class.
        Index("ix_worker_registrations_status", "status"),
        Index("ix_worker_registrations_heartbeat", "last_heartbeat_at"),
        Index("ix_worker_registrations_type", "worker_type"),
    )

    #: Stable process identity (host-pid-token or operator-configured).
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Logical worker class (e.g. "durable-jobs").
    worker_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=WorkerStatus.STARTING.value
    )

    #: Lifecycle timestamps (UTC, timezone-aware).
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Capacity + capability advertisement.
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    supported_job_types: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    queue_backend: Mapped[str] = mapped_column(String(16), nullable=False, default="local")

    #: Non-secret build identifiers for support/correlation.
    application_version: Mapped[str] = mapped_column(String(32), nullable=False, default="0.0.0")
    build_revision: Mapped[str | None] = mapped_column(String(64))
    #: Optional non-reversible hostname hash; never an IP or raw hostname.
    host_fingerprint: Mapped[str | None] = mapped_column(String(64))
