"""Scout request model. Each request is isolated by workspace/brand/location/campaign."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.core.enums import ScheduleInterval, ScoutRequestStatus
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ScoutRequest(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "scout_requests"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    brand_id: Mapped[str] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # location/campaign scoping — critical for request isolation.
    location_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_locations.id", ondelete="SET NULL"), index=True
    )
    campaign_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default=ScoutRequestStatus.DRAFT.value)
    source_types: Mapped[list] = mapped_column(JSON, default=list)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    product_profile_id: Mapped[str | None] = mapped_column(String(32))
    resolved_market: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict] = mapped_column(JSON, default=dict)  # scanned/filtered/opportunities
    #: D-25 run fence (PROPOSED, ``app.jobs.admission``): advanced by every pause; a run
    #: may continue only while it still equals the value its job was enqueued under.
    run_fence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: D-25 execution owner (O2, PROPOSED): the single attempt that may run this Scout.
    owner_job_id: Mapped[str | None] = mapped_column(String(32))
    owner_attempt: Mapped[int | None] = mapped_column(Integer)
    owner_lease_token: Mapped[str | None] = mapped_column(String(64))


class ScoutAdmission(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One admitted external operation of a Scout run (D-25 K = 1 ledger, PROPOSED).

    Committed before a connector fetch attempt or a model attempt is dispatched; resolved only
    by a known recorded end or by reconciliation. At most one unresolved row per Scout
    (``uq_scout_admission_unresolved``). Keys, states and timestamps only - no content.
    """

    __tablename__ = "scout_admissions"
    __table_args__ = (
        CheckConstraint("operation IN ('fetch', 'model')", name="ck_scout_admission_operation"),
        CheckConstraint(
            "end_kind IS NULL OR end_kind IN ('known', 'unknown')",
            name="ck_scout_admission_end_kind",
        ),
        Index(
            "uq_scout_admission_unresolved",
            "scout_request_id",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
            sqlite_where=text("resolved_at IS NULL"),
        ),
        # R-D (recovery contract §3, PROPOSED). PostgreSQL only: the ledger is written only
        # there, and SQLite cannot add a CHECK without rebuilding the table.
        CheckConstraint(
            "deadline_rule IN ('client_plus_lease', 'unrecorded')",
            name="ck_scout_admission_deadline_rule",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(deadline_rule = 'unrecorded' AND deadline_at IS NULL"
            " AND deadline_client_seconds IS NULL AND deadline_lease_seconds IS NULL)"
            " OR (deadline_rule = 'client_plus_lease' AND deadline_at IS NOT NULL"
            " AND deadline_client_seconds > 0 AND deadline_lease_seconds > 0"
            " AND deadline_at = admitted_at"
            " + make_interval(secs => deadline_client_seconds + deadline_lease_seconds))",
            name="ck_scout_admission_deadline_basis",
        ).ddl_if(dialect="postgresql"),
    )

    scout_request_id: Mapped[str] = mapped_column(
        ForeignKey("scout_requests.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(200), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_key: Mapped[str | None] = mapped_column(String(200))
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_kind: Mapped[str | None] = mapped_column(String(16))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(200))
    #: A reason code plus at most 200 characters; never secrets or customer content.
    resolution_note: Mapped[str | None] = mapped_column(String(240))
    #: R-D (recovery contract §3, PROPOSED): the instant, in database time, after which an
    #: admission with no recorded end becomes ELIGIBLE for reconciliation (a procedural gate,
    #: not evidence that the external party stopped). Persisted by the admitting INSERT from
    #: one ``clock_timestamp()`` reading shared with ``admitted_at``; NULL on rows created
    #: before it was persisted (``deadline_rule = 'unrecorded'``; never back-filled).
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The rule that combined the basis: ``client_plus_lease`` or ``unrecorded``.
    deadline_rule: Mapped[str] = mapped_column(
        String(24), nullable=False, default="unrecorded", server_default="unrecorded"
    )
    #: The basis, from the admitting process's settings: the operation's client deadline and
    #: the worker lease, in seconds.
    deadline_client_seconds: Mapped[float | None] = mapped_column(Float)
    deadline_lease_seconds: Mapped[float | None] = mapped_column(Float)


class ScoutSchedule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Recurring cadence attached to exactly one scout request + market (SB-B).

    Additive and dark-deployed: rows only ever drive work while scheduling resolves
    enabled for their workspace (the capability resolver — P6-CAP-1; with the shipped
    ``scout_scheduling_enabled=False`` and no override, never). A schedule owns one
    recurring cadence for one scout request; the uniqueness constraint enforces the
    product rule of at most one schedule per request. Recurrence is a pure fixed
    interval measured in UTC from the enable/resume moment — no clock-of-day,
    timezone or DST.
    """

    __tablename__ = "scout_schedules"
    __table_args__ = (
        # At most one schedule per scout request within a workspace. Delete is a
        # hard delete, so a request never accumulates stale schedule rows.
        UniqueConstraint(
            "workspace_id", "scout_request_id", name="uq_scout_schedule_request"
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Market scoping — mirrors ScoutRequest so a schedule inherits its request's
    # isolation. SET NULL on location removal, matching the request's own rule.
    location_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_locations.id", ondelete="SET NULL"), index=True
    )
    # The scheduled request. CASCADE: deleting the request removes its schedule.
    scout_request_id: Mapped[str] = mapped_column(
        ForeignKey("scout_requests.id", ondelete="CASCADE"), index=True, nullable=False
    )

    interval: Mapped[str] = mapped_column(String(20), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Next UTC instant a tick should fan out a scouting run; NULL when inert
    #: (paused/never enabled). Recomputed as enable/resume time + interval.
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Last UTC instant a tick actually fired (advanced the cadence). NULL until
    #: the first fan-out. Never guessed from scheduling columns.
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def interval_enum(self) -> ScheduleInterval:
        """The validated recurrence cadence for this row."""
        return ScheduleInterval(self.interval)
