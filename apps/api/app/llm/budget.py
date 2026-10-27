"""Shared budget ledger: reserve before a provider call, settle after it, recover leaks.

Implements D-LLM-1 option B (operator decision 2026-10-27, ledger entry (5)): the
PostgreSQL-backed reservation behind the per-workspace monthly budget. The guarantee
it gives, its cost basis and its limits are stated in :mod:`app.llm.metering`; this
module holds the ledger primitives. Every function writes through the session it is
given and never commits: the caller owns the transaction, and on the shared-ledger
path that transaction is short and separate from the run's own.

**Lock order.** Every writer that touches the period and its reservations takes the period
row's lock first (:func:`_lock_period`), then claims reservation rows (a release: periods
in key order, rows in id order, one period update per period). One order everywhere, so a
settlement and a multi-row recovery release cannot deadlock on PostgreSQL; a deadlock or
serialization failure that still occurs is retried by the meter on a fresh transaction.

Tables (:mod:`app.llm.models`): ``llm_budget_periods`` (one row per workspace and UTC
month: ``reserved_usd``, ``settled_usd``) and ``llm_budget_reservations`` (one row per
reserved call, keyed by attempt).

* :func:`reserve` - ``UPDATE llm_budget_periods SET reserved_usd = reserved_usd + w
  WHERE workspace_id = :ws AND period_start = :month AND reserved_usd + w <= :budget``
  (the period row is created first with ``INSERT ... ON CONFLICT DO NOTHING``). One row
  updated means the worst case ``w`` is reserved and a ``reserved`` reservation row is
  inserted; zero rows means the budget would be crossed and nothing changes. On
  PostgreSQL the conditional update takes the period's row lock, and under READ
  COMMITTED a waiter re-evaluates the condition after the lock is released, so any
  number of processes reserving against one workspace serialise correctly.
* :func:`settle` - records a ``reserved`` row's settled amount (``settled_usd +=
  charge``) and writes the usage row; keyed by ``attempt_key`` and conditional on the
  row still being ``reserved``, so a second or concurrent settlement of the same key is
  a no-op. A settlement that arrives after recovery already released or expired the row
  changes no money (the worst case stands) but still writes the call's usage row - once,
  claimed by ``usage_recorded_at`` however many processes settle late - logs
  ``llm.settlement_after_release`` at WARNING and counts
  ``llm_settlements_after_release_total``, so reconciliation sees the actual. The
  budget stays consumed by the call's **worst-case bound**: settlement never lowers
  ``reserved_usd``, and never raises it either: the charge is ``min(reported,
  reservation)``. The bound is a true upper bound (:mod:`app.llm.bound`), so a report
  above it means its premise failed: ERROR ``llm.cost_bound_violated``, counter
  ``llm_cost_bound_violations_total``, ``bound_violations`` on the period; the bound
  stays the charge, so ``reserved_usd`` never exceeds the budget. A settlement for a
  reservation whose workspace or organization was deleted only logs and counts
  (``llm_settlements_orphaned_total``); it never recreates a row.
* :func:`record_settled` - the same-session path (SQLite, or no session factory):
  writes a reservation row that is already settled, plus the period and usage rows,
  idempotently per attempt key. Nothing precedes the call on this path.
* :func:`release_for_job`, :func:`release_expired`, :func:`release_terminal` and
  :func:`sweep` - recovery. The expiry sweep leaves a reservation alone while its job
  still holds a live lease (``jobs.lease_expires_at`` in the future on a claimed or
  running job): the process that reserved it is heartbeating, so the call may still be
  in flight however long it has taken. A ``reserved`` row that is released is charged its worst
  case: **an unknown outcome is charged its worst case**, because the process that
  reserved it may have made the call before it died. Releasing, like settling, never
  lowers ``reserved_usd``; it only adds the amount to ``settled_usd``. A leaked
  reservation under-spends the budget, never over-spends it.

Cost basis (conservative): the worst case ``w`` is :func:`app.llm.bound.worst_case_cost`
of the exact request body (bytes bound tokens, plus stated overhead constants, plus
``max_tokens`` at the model's price, rounded up at 1e-6), and an attempt whose cost cannot
be bounded is not made. A timeout, a connection failure after the request was sent, an
undecodable or usage-less reply settle at ``w`` (the undecodable-reply charge is a
conservative basis pending D-LLM-3, not an accepted rule).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import get_logger, log_event
from app.core.metrics import (
    LLM_COST_BOUND_VIOLATIONS_TOTAL,
    LLM_RESERVATIONS_RELEASED_TOTAL,
    LLM_SETTLEMENTS_AFTER_RELEASE_TOTAL,
    LLM_SETTLEMENTS_ORPHANED_TOTAL,
    get_metrics,
)
from app.jobs.models import Job
from app.jobs.status import TERMINAL_STATUSES, JobStatus
from app.llm.bound import Unbounded, worst_case_cost
from app.llm.models import LLMBudgetPeriod, LLMBudgetReservation, LLMUsageEvent
from app.organizations.models import Workspace

__all__ = ["Unbounded", "worst_case_cost"]  # the bound lives in app.llm.bound

logger = get_logger("signalnest.llm")

RESERVED = "reserved"
SETTLED = "settled"
RELEASED = "released"
EXPIRED = "expired"

_MICRO = Decimal("0.000001")
#: ``httpx.Client(timeout=T)`` applies ``T`` to each phase of one request (connect,
#: write, read; pool acquisition besides), so one live provider call can legitimately
#: last about this many timeouts before the client gives up.
HTTP_TIMEOUT_PHASES = 3
#: Margin added to the longest a reserved call can legitimately stay unsettled.
RESERVATION_EXPIRY_MARGIN_SECONDS = 60.0


def usd(value: float | int | str | Decimal) -> Decimal:
    """An exact six-place USD amount from a price-table float or any numeric."""
    return Decimal(str(value)).quantize(_MICRO, rounding=ROUND_HALF_UP)


def period_start_for(now: datetime) -> date:
    """The first day of ``now``'s UTC calendar month."""
    return now.astimezone(UTC).date().replace(day=1)


def reservation_expiry(settings: Settings, now: datetime) -> datetime:
    """When a reservation made at ``now`` counts as leaked if still unsettled.

    ``expires_at = now + HTTP_TIMEOUT_PHASES x llm_timeout_seconds + worker_lease_seconds
    + RESERVATION_EXPIRY_MARGIN_SECONDS``: one reserved call is bounded by the client's
    real worst case (the per-phase timeout, three phases); the lease window covers the
    worker's own bookkeeping around it; the margin absorbs clock differences between
    hosts. The sweep additionally defers to a live job lease (:func:`release_expired`).
    A reservation past this instant whose process is in fact alive is released at its
    worst case, which is conservative, never an over-spend; its late settlement still
    records the call (:func:`settle`).
    """
    seconds = (
        HTTP_TIMEOUT_PHASES * float(settings.llm_timeout_seconds)
        + float(settings.worker_lease_seconds)
        + RESERVATION_EXPIRY_MARGIN_SECONDS
    )
    return now + timedelta(seconds=seconds)


def _parent_exists(db: Session, workspace_id: str, organization_id: str) -> bool:
    """Whether the workspace (of that organization) still exists.

    A late callback for a deleted tenant must never recreate that tenant's period or any
    other row (no ``INSERT ... ON CONFLICT`` resurrection), whether or not the database
    enforces the foreign keys: the writers that insert check this first.
    """
    return (
        db.scalar(
            select(Workspace.id).where(
                Workspace.id == workspace_id, Workspace.organization_id == organization_id
            )
        )
        is not None
    )


def _lock_period(db: Session, workspace_id: str, period_start: date) -> None:
    """Take the period row's lock (``SELECT ... FOR UPDATE``; a no-op on SQLite).

    **The one lock order of the ledger: the period row first, then reservation rows.**
    Every writer that touches both (reserve, settle, the late settlement, every release)
    takes the period lock before it claims any reservation, and a release works through
    periods in key order and its reservations in id order. A settlement and a multi-row
    recovery release therefore cannot wait on each other's rows in opposite order (the
    PostgreSQL deadlock review r3 reproduced). No row of a deleted period exists to lock:
    the statement then matches nothing and the caller's conditional updates do too.
    """
    db.execute(
        select(LLMBudgetPeriod.id)
        .where(
            LLMBudgetPeriod.workspace_id == workspace_id,
            LLMBudgetPeriod.period_start == period_start,
        )
        .with_for_update()
    )


#: SQLSTATEs a transaction may simply be retried after: deadlock_detected and
#: serialization_failure.
RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})


def is_retryable_conflict(exc: BaseException) -> bool:
    """Whether ``exc`` is a database deadlock or serialization failure (retry the transaction)."""
    if not isinstance(exc, DBAPIError):
        return False
    return getattr(exc.orig, "sqlstate", None) in RETRYABLE_SQLSTATES


def _dialect_insert(db: Session):
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


def ensure_period(
    db: Session, *, workspace_id: str, organization_id: str, period_start: date, now: datetime
) -> None:
    """Create the workspace's period row if it does not exist (never an error if it does)."""
    insert = _dialect_insert(db)
    db.execute(
        insert(LLMBudgetPeriod)
        .values(
            workspace_id=workspace_id,
            organization_id=organization_id,
            period_start=period_start,
            reserved_usd=Decimal(0),
            settled_usd=Decimal(0),
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["workspace_id", "period_start"])
    )


def reserved_month_to_date_usd(
    db: Session, workspace_id: str, now: datetime | None = None
) -> Decimal:
    """The workspace's ``reserved_usd`` for the current UTC month (0 without a row)."""
    period_start = period_start_for(now or datetime.now(UTC))
    value = db.scalar(
        select(LLMBudgetPeriod.reserved_usd).where(
            LLMBudgetPeriod.workspace_id == workspace_id,
            LLMBudgetPeriod.period_start == period_start,
        )
    )
    return usd(value) if value is not None else Decimal(0)


def reserve(
    db: Session,
    *,
    workspace_id: str,
    organization_id: str,
    job_id: str | None,
    attempt_key: str,
    worst_usd: Decimal,
    budget_usd: Decimal,
    expires_at: datetime,
    now: datetime,
) -> bool:
    """Reserve ``worst_usd`` for one call if the month's budget allows it.

    Returns ``True`` with the reservation row added (``reserved``) when the period's
    ``reserved_usd + worst_usd <= budget_usd`` held at the moment of the update;
    ``False``, with nothing changed, otherwise. The caller commits (or rolls back).
    """
    period_start = period_start_for(now)
    if not _parent_exists(db, workspace_id, organization_id):
        _orphaned(attempt_key, "reserve")
        return False
    ensure_period(
        db,
        workspace_id=workspace_id,
        organization_id=organization_id,
        period_start=period_start,
        now=now,
    )
    result = db.execute(
        update(LLMBudgetPeriod)
        .where(
            LLMBudgetPeriod.workspace_id == workspace_id,
            LLMBudgetPeriod.period_start == period_start,
            LLMBudgetPeriod.reserved_usd + worst_usd <= budget_usd,
        )
        .values(reserved_usd=LLMBudgetPeriod.reserved_usd + worst_usd, updated_at=now),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        return False
    db.add(
        LLMBudgetReservation(
            organization_id=organization_id,
            workspace_id=workspace_id,
            period_start=period_start,
            job_id=job_id,
            attempt_key=attempt_key,
            reserved_usd=worst_usd,
            state=RESERVED,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()
    return True


def _usage_row(reservation: LLMBudgetReservation, usage: dict[str, Any]) -> LLMUsageEvent:
    return LLMUsageEvent(
        organization_id=reservation.organization_id,
        workspace_id=reservation.workspace_id,
        job_id=reservation.job_id,
        **usage,
    )


def settle(
    db: Session,
    *,
    attempt_key: str,
    settled_usd: Decimal,
    now: datetime,
    usage: dict[str, Any] | None = None,
) -> bool:
    """Record the reservation ``attempt_key``'s settled amount; write its usage row.

    Idempotent and race-free. Only a row still ``reserved`` is settled (one conditional
    update that also claims the attempt's usage row, ``usage_recorded_at``), so a second
    or concurrent settlement of the same key changes nothing and returns ``False``. A
    call whose reservation recovery already ``released`` or ``expired`` (the sweep or a
    rebind won the race against a live call, including a race lost between our read and
    our update) also returns ``False`` and moves no money - the worst case stands as the
    charge - but its usage row IS written, once however many times or from however many
    processes the late settlement arrives (:func:`_record_late_settlement`), with a
    WARNING and a metric, so the call's actual usage and outcome reach reconciliation.

    The charge is ``min(settled_usd, reservation)``: the reservation is a true upper bound
    (:func:`app.llm.bound.worst_case_cost`), so a larger reported usage means the bound's
    premise failed. That is logged at ERROR, counted (``llm_cost_bound_violations_total``)
    and recorded on the period (``bound_violations``); the bound stays the charge, the
    usage row keeps what the provider reported, and ``reserved_usd`` never exceeds the
    budget by it. Settlement never lowers ``reserved_usd``. ``usage`` holds the usage
    row's own columns; the tenant and job columns come from the reservation.

    A reservation that no longer exists (its workspace or organization was deleted)
    writes nothing: it is logged and counted (``llm_settlements_orphaned_total``) only.
    """
    reservation = db.scalar(
        select(LLMBudgetReservation).where(LLMBudgetReservation.attempt_key == attempt_key)
    )
    if reservation is None:
        _orphaned(attempt_key, "settle")
        return False
    # Period lock first (the ledger's one lock order), then decide on the row as it is now.
    _lock_period(db, reservation.workspace_id, reservation.period_start)
    reservation = db.scalar(
        select(LLMBudgetReservation)
        .where(LLMBudgetReservation.attempt_key == attempt_key)
        .execution_options(populate_existing=True)
    )
    if reservation is None:
        _orphaned(attempt_key, "settle")
        return False
    if reservation.state != RESERVED:
        _late_settlement(db, reservation, usage, now=now)
        return False
    worst = usd(reservation.reserved_usd)
    charge = min(settled_usd, worst)
    claimed = db.execute(
        update(LLMBudgetReservation)
        .where(
            LLMBudgetReservation.id == reservation.id,
            LLMBudgetReservation.state == RESERVED,
        )
        .values(
            state=SETTLED,
            settled_usd=charge,
            settled_at=now,
            usage_recorded_at=now if usage is not None else None,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    if claimed.rowcount != 1:
        # Recovery (or another settlement) won between our read and our update: look at
        # the row again and take the late path - never drop the call's usage.
        db.expire(reservation)
        current = db.scalar(
            select(LLMBudgetReservation)
            .where(LLMBudgetReservation.attempt_key == attempt_key)
            .execution_options(populate_existing=True)
        )
        if current is None:
            _orphaned(attempt_key, "settle")
        elif current.state != RESERVED:
            _late_settlement(db, current, usage, now=now)
        return False
    violated = settled_usd > worst
    period_claimed = db.execute(
        update(LLMBudgetPeriod)
        .where(
            LLMBudgetPeriod.workspace_id == reservation.workspace_id,
            LLMBudgetPeriod.period_start == reservation.period_start,
        )
        .values(
            settled_usd=LLMBudgetPeriod.settled_usd + charge,
            bound_violations=LLMBudgetPeriod.bound_violations + (1 if violated else 0),
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    if violated:
        _report_violation(reservation, reported=settled_usd, bound=worst, stage="settle")
    if usage is not None:
        _add_usage(db, reservation, usage, attempt_key)
    elif period_claimed.rowcount != 1:
        _orphaned(attempt_key, "settle")
    db.flush()
    return True


def _add_usage(
    db: Session, reservation: LLMBudgetReservation, usage: dict[str, Any], attempt_key: str
) -> None:
    """Insert the usage row; if its parent vanished meanwhile, log and count instead."""
    try:
        with db.begin_nested():
            db.add(_usage_row(reservation, usage))
            db.flush()
    except IntegrityError:
        _orphaned(attempt_key, "usage_row")


def _late_settlement(
    db: Session,
    reservation: LLMBudgetReservation,
    usage: dict[str, Any] | None,
    *,
    now: datetime,
) -> None:
    """A settlement for a reservation that is no longer ``reserved``.

    ``settled``: a duplicate - nothing to do. ``released``/``expired``: record the
    usage row exactly once (the ``usage_recorded_at`` claim), count it, and flag a
    bound violation if the call reported more than the reservation.
    """
    if usage is None or reservation.state not in (RELEASED, EXPIRED):
        return
    state = reservation.state
    claimed = db.execute(
        update(LLMBudgetReservation)
        .where(
            LLMBudgetReservation.id == reservation.id,
            LLMBudgetReservation.usage_recorded_at.is_(None),
        )
        .values(usage_recorded_at=now, updated_at=now),
        execution_options={"synchronize_session": False},
    )
    if claimed.rowcount != 1:
        return  # the usage row of this attempt is already written (or the row is gone)
    worst = usd(reservation.reserved_usd)
    reported = usd(usage.get("cost_usd", 0))
    violated = reported > worst
    if violated:
        db.execute(
            update(LLMBudgetPeriod)
            .where(
                LLMBudgetPeriod.workspace_id == reservation.workspace_id,
                LLMBudgetPeriod.period_start == reservation.period_start,
            )
            .values(
                bound_violations=LLMBudgetPeriod.bound_violations + 1,
                updated_at=now,
            ),
            execution_options={"synchronize_session": False},
        )
        _report_violation(reservation, reported=reported, bound=worst, stage="late_settle")
    _add_usage(db, reservation, usage, reservation.attempt_key)
    get_metrics().increment(LLM_SETTLEMENTS_AFTER_RELEASE_TOTAL, outcome=state)
    log_event(
        logger,
        "llm.settlement_after_release",
        level=logging.WARNING,
        component="llm",
        outcome=state,
        job_id=reservation.job_id,
        attempt_key=reservation.attempt_key,
        reservation_state=state,
        call_outcome=usage.get("outcome"),
    )


def _report_violation(
    reservation: LLMBudgetReservation, *, reported: Decimal, bound: Decimal, stage: str
) -> None:
    get_metrics().increment(LLM_COST_BOUND_VIOLATIONS_TOTAL, outcome=stage)
    log_event(
        logger,
        "llm.cost_bound_violated",
        level=logging.ERROR,
        component="llm",
        outcome="bound_violated",
        job_id=reservation.job_id,
        attempt_key=reservation.attempt_key,
        reported_usd=str(reported),
        bound_usd=str(bound),
    )


def _orphaned(attempt_key: str, stage: str) -> None:
    """A late callback for a reservation whose tenant was deleted: log and count only."""
    get_metrics().increment(LLM_SETTLEMENTS_ORPHANED_TOTAL, outcome=stage)
    log_event(
        logger,
        "llm.settlement_orphaned",
        level=logging.WARNING,
        component="llm",
        outcome=stage,
        attempt_key=attempt_key,
    )


def record_settled(
    db: Session,
    *,
    workspace_id: str,
    organization_id: str,
    job_id: str | None,
    attempt_key: str,
    settled_usd: Decimal,
    now: datetime,
    usage: dict[str, Any] | None = None,
    worst_usd: Decimal | None = None,
) -> bool:
    """The same-session path: record one finished call as an already-settled reservation.

    Writes the reservation row (``settled``, reserved = settled = the charge), adds the
    charge to the period's ``reserved_usd`` and ``settled_usd``, and writes the usage
    row - all in the caller's transaction, once per attempt key: a key already present
    changes nothing and returns ``False``. When ``worst_usd`` (the call's bound) is given
    and the reported charge exceeds it, the charge is the bound and the violation is
    logged, counted and recorded on the period, as in :func:`settle`.
    """
    violated = worst_usd is not None and settled_usd > worst_usd
    reported = settled_usd
    if violated:
        settled_usd = worst_usd
    period_start = period_start_for(now)
    if not _parent_exists(db, workspace_id, organization_id):
        _orphaned(attempt_key, "record_settled")
        return False
    insert = _dialect_insert(db)
    inserted = db.execute(
        insert(LLMBudgetReservation)
        .values(
            organization_id=organization_id,
            workspace_id=workspace_id,
            period_start=period_start,
            job_id=job_id,
            attempt_key=attempt_key,
            reserved_usd=settled_usd,
            settled_usd=settled_usd,
            state=SETTLED,
            expires_at=now,
            settled_at=now,
            usage_recorded_at=now if usage is not None else None,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["attempt_key"])
    )
    if inserted.rowcount != 1:
        return False
    ensure_period(
        db,
        workspace_id=workspace_id,
        organization_id=organization_id,
        period_start=period_start,
        now=now,
    )
    db.execute(
        update(LLMBudgetPeriod)
        .where(
            LLMBudgetPeriod.workspace_id == workspace_id,
            LLMBudgetPeriod.period_start == period_start,
        )
        .values(
            reserved_usd=LLMBudgetPeriod.reserved_usd + settled_usd,
            settled_usd=LLMBudgetPeriod.settled_usd + settled_usd,
            bound_violations=LLMBudgetPeriod.bound_violations + (1 if violated else 0),
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    if violated:
        get_metrics().increment(LLM_COST_BOUND_VIOLATIONS_TOTAL, outcome="same_session")
        log_event(
            logger,
            "llm.cost_bound_violated",
            level=logging.ERROR,
            component="llm",
            outcome="bound_violated",
            job_id=job_id,
            attempt_key=attempt_key,
            reported_usd=str(reported),
            bound_usd=str(worst_usd),
        )
    if usage is not None:
        db.add(
            LLMUsageEvent(
                organization_id=organization_id,
                workspace_id=workspace_id,
                job_id=job_id,
                **usage,
            )
        )
    db.flush()
    return True


def _release_rows(
    db: Session, rows: list[LLMBudgetReservation], *, now: datetime, state: str
) -> int:
    """Charge each still-``reserved`` row its worst case; returns how many were released.

    One lock order (:func:`_lock_period`): the rows are grouped by period; for each period,
    in key order, the period row is locked first, then its rows are claimed in id order,
    and the period is updated once with the sum of what this call released.
    """
    by_period: dict[tuple[str, date], list[LLMBudgetReservation]] = {}
    for row in rows:
        by_period.setdefault((row.workspace_id, row.period_start), []).append(row)
    released = 0
    for (workspace_id, period_start), group in sorted(by_period.items()):
        _lock_period(db, workspace_id, period_start)
        charged = Decimal(0)
        for row in sorted(group, key=lambda r: r.id):
            worst = usd(row.reserved_usd)
            claimed = db.execute(
                update(LLMBudgetReservation)
                .where(LLMBudgetReservation.id == row.id, LLMBudgetReservation.state == RESERVED)
                .values(state=state, settled_usd=worst, settled_at=now, updated_at=now),
                execution_options={"synchronize_session": False},
            )
            if claimed.rowcount != 1:
                continue  # settled or released by someone else first: its charge stands
            charged += worst
            released += 1
        if charged:
            db.execute(
                update(LLMBudgetPeriod)
                .where(
                    LLMBudgetPeriod.workspace_id == workspace_id,
                    LLMBudgetPeriod.period_start == period_start,
                )
                .values(settled_usd=LLMBudgetPeriod.settled_usd + charged, updated_at=now),
                execution_options={"synchronize_session": False},
            )
    if released:
        db.flush()
    return released


def release_for_job(db: Session, *, job_id: str, now: datetime, outcome: str) -> int:
    """Release every ``reserved`` reservation of ``job_id`` at its worst case.

    Called when the job reaches a terminal state, when one of its attempts ends, and
    when a later attempt starts (a lost lease's in-flight reservations). ``outcome``
    labels the metric (``terminal`` | ``attempt_end`` | ``rebind``).
    """
    rows = db.scalars(
        select(LLMBudgetReservation).where(
            LLMBudgetReservation.job_id == job_id, LLMBudgetReservation.state == RESERVED
        )
    ).all()
    released = _release_rows(db, list(rows), now=now, state=RELEASED)
    _report(released, outcome=outcome)
    return released


def release_expired(db: Session, *, now: datetime) -> int:
    """Charge every ``reserved`` row past its ``expires_at`` its worst case (``expired``).

    A row whose job still holds a live lease (claimed or running, ``lease_expires_at``
    at or after ``now``) is left alone whatever its ``expires_at``: its process is
    heartbeating and the call may still be in flight. Rows with no job, with a job
    that is no longer claimed/running, or whose lease has lapsed expire by the clock.
    """
    live_lease = (
        Job.status.in_([JobStatus.CLAIMED.value, JobStatus.RUNNING.value])
        & Job.lease_expires_at.is_not(None)
        & (Job.lease_expires_at >= now)
    )
    rows = db.scalars(
        select(LLMBudgetReservation)
        .outerjoin(Job, Job.id == LLMBudgetReservation.job_id)
        .where(
            LLMBudgetReservation.state == RESERVED,
            LLMBudgetReservation.expires_at < now,
            or_(Job.id.is_(None), ~live_lease),
        )
    ).all()
    released = _release_rows(db, list(rows), now=now, state=EXPIRED)
    _report(released, outcome="expired")
    return released


def release_terminal(db: Session, *, now: datetime) -> int:
    """Release every ``reserved`` row whose job is in a terminal state (``released``)."""
    rows = db.scalars(
        select(LLMBudgetReservation)
        .join(Job, Job.id == LLMBudgetReservation.job_id)
        .where(
            LLMBudgetReservation.state == RESERVED,
            Job.status.in_([s.value for s in TERMINAL_STATUSES]),
        )
    ).all()
    released = _release_rows(db, list(rows), now=now, state=RELEASED)
    _report(released, outcome="terminal")
    return released


def sweep(db: Session, *, now: datetime) -> dict[str, int]:
    """Recovery: expired reservations, then reservations of terminal jobs."""
    return {
        "expired": release_expired(db, now=now),
        "terminal": release_terminal(db, now=now),
    }


def _report(released: int, *, outcome: str) -> None:
    if not released:
        return
    get_metrics().increment(LLM_RESERVATIONS_RELEASED_TOTAL, outcome=outcome, amount=released)
    log_event(
        logger,
        "llm.reservations_released",
        level=logging.WARNING,
        component="llm",
        outcome=outcome,
        released=released,
    )
