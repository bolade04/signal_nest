"""LLM usage ledger (P6-LLM-4): one row per provider attempt, per workspace.

Holds counts and money only - never prompt text, scraped content or model output -
so it carries no customer content into retention scope beyond the workspace link.
Rows cascade with their workspace and organization.

Budget ledger (D-LLM-1 option B, ``app.llm.budget``): ``llm_budget_periods`` holds
one row per workspace and UTC calendar month with the money reserved before provider
calls and the money settled after them; ``llm_budget_reservations`` holds one row per
reserved provider call (keyed by attempt) so settlement is idempotent and a
reservation whose process died can be recovered. Money and keys only, like the usage
ledger; same cascade.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: Money columns: exact decimal USD with six places (never a float, so sums are exact
#: on PostgreSQL; SQLite stores them as REAL and the module says what that costs).
USD = Numeric(12, 6)


class LLMUsageEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "llm_usage_events"
    __table_args__ = (Index("ix_llm_usage_events_workspace_created", "workspace_id", "created_at"),)

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    scout_request_id: Mapped[str | None] = mapped_column(
        ForeignKey("scout_requests.id", ondelete="SET NULL"), index=True
    )
    #: The durable job whose attempt made the call (NULL off the worker path). A run's
    #: call cap counts this job's rows only.
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    task: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: ``ok`` or the normalized error class name of a failed attempt.
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)


class LLMBudgetPeriod(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One workspace's reserved and settled model spend for one UTC calendar month.

    ``reserved_usd`` is the budget consumed: the sum, over this month's reservations,
    of each call's worst-case bound (:func:`app.llm.bound.worst_case_cost`) - the number
    the budget check compares and the run endpoint reads; on the shared-ledger path it
    is never lowered and never raised by a settlement. ``settled_usd`` is what the
    finished calls were charged (actual usage, or the bound for an unknown outcome,
    never more than the bound); ``bound_violations`` counts the calls that reported more
    than their bound. The hot row is updated by short transactions only. One row per
    ``(workspace_id, period_start)`` (unique); the surrogate ``id`` is what the privacy
    export, erasure walk and verification address rows by, like every other table.
    """

    __tablename__ = "llm_budget_periods"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "period_start", name="uq_llm_budget_periods_workspace_period"
        ),
        Index("ix_llm_budget_periods_organization_id", "organization_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    #: First day of the UTC calendar month this row accounts for.
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    reserved_usd: Mapped[Decimal] = mapped_column(USD, nullable=False, default=Decimal(0))
    settled_usd: Mapped[Decimal] = mapped_column(USD, nullable=False, default=Decimal(0))
    #: How many settled calls reported usage above their reserved bound
    #: (:func:`app.llm.bound.worst_case_cost` is violated: its premise failed). The
    #: call is still charged its bound; this counter is what reconciliation reads.
    bound_violations: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


#: Reservation states. ``reserved``: the call may be in flight; ``settled``: charged
#: its actual (or worst-case) cost by the process that made it; ``released``: charged
#: its worst case by recovery because its job ended or a later attempt started without
#: it being settled; ``expired``: charged its worst case by the sweep after
#: ``expires_at``.
RESERVATION_STATES: tuple[str, ...] = ("reserved", "settled", "released", "expired")


class LLMBudgetReservation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One reserved provider call: its worst case before the call, its settlement after.

    ``attempt_key`` (job id, worker attempt ordinal, call ordinal) makes settlement
    idempotent: a second settlement of the same key is a no-op. A row that is still
    ``reserved`` after its process died is the record of that call until recovery
    charges it its worst case.
    """

    __tablename__ = "llm_budget_reservations"
    __table_args__ = (
        Index("ix_llm_budget_reservations_state_expires", "state", "expires_at"),
        Index("ix_llm_budget_reservations_workspace_period", "workspace_id", "period_start"),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    #: The durable job whose attempt reserved the call (NULL off the worker path).
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    attempt_key: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    #: The worst case reserved before the call (the same-session path records the
    #: settled amount here, since nothing precedes the call there).
    reserved_usd: Mapped[Decimal] = mapped_column(USD, nullable=False)
    #: What the call was charged once settled, released or expired; NULL while reserved.
    settled_usd: Mapped[Decimal | None] = mapped_column(USD, nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    #: After this instant a row still ``reserved`` is treated as leaked by the sweep.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set by the one writer that claims this attempt's usage row (settlement, or the
    #: late settlement after recovery): a conditional update on NULL, so exactly one
    #: ``llm_usage_events`` row exists per attempt however often or concurrently the
    #: attempt is settled.
    usage_recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
