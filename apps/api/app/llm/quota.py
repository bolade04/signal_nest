"""Daily cost and call ceilings for generation calls (Project Phase 5C, W21 lane P5C).

The four ceiling settings (``Settings.CREATIVE_GENERATION_CEILINGS``, W21 CF-2) bound text
generation per UTC day:

* ``llm_daily_cost_ceiling_usd_global`` — model spend across every workspace;
* ``llm_daily_cost_ceiling_usd_per_workspace`` — model spend of one workspace;
* ``llm_max_calls_per_workspace_per_day`` — generation calls (provider attempts) of one
  workspace;
* ``llm_max_output_tokens_per_call`` — applied by the caller as the request's output limit,
  ``min(llm_max_output_tokens, llm_max_output_tokens_per_call)``
  (:meth:`app.llm.service.LLMService.run`, ``max_output_tokens``).

**Fail closed while unset.** None of the four has an approved value; each defaults to
``None``. While any is unset every generation attempt is refused (``ceilings_unset``) —
there is no default that lets a call through.

**What is counted — the ledger, never a draft's snapshot** (coordinator decision I-4 / R5).
Spend is read from the Phase 6 usage ledger: the day's ``llm_usage_events.cost_usd`` (every
task's, legacy scouting calls included, since the cost ceilings bound model spend) plus the
charge of each of the day's ``llm_budget_reservations`` that has no usage row yet (a call in
flight at its worst case, or one released or expired by recovery at its worst case). Calls are
the day's ``llm_usage_events`` rows of the generation task. The attempts this meter has made
but not yet written (the same-session path keeps them in memory until ``flush``) are added.

**Would breach → refused, before the call.** :class:`CeilingMeter` checks every provider
attempt in :meth:`~CeilingMeter.before_attempt`, with that attempt's exact worst case
(:func:`app.llm.bound.worst_case_cost` of the request bytes; the mock costs nothing): a
ceiling already reached, or one the attempt could cross, raises
:class:`LLMCeilingExceededError` and no call is made. It is an ``LLMBudgetExceededError``, so
the service propagates it before any request is built for the provider and never falls back
to the mock (the same path as a budget refusal).

**Under a lock, per workspace and globally.** The caller holds :func:`lock_for_generation`
for the whole read-check-call-record sequence, so two concurrent generation jobs can never
both pass a check that only one of them fits: on PostgreSQL a transaction-scoped advisory lock
serializes generation globally (there is no global row to lock), then the workspace row is
locked with ``SELECT … FOR NO KEY UPDATE`` (the ``capabilities/service.py``
``_workspace_lock_select`` pattern). ``NO KEY UPDATE`` rather than ``UPDATE``: the meter's own
ledger transactions insert rows referencing the workspace from a second session while this
lock is held, and those foreign-key checks take ``KEY SHARE``, which ``NO KEY UPDATE`` does not
block (``FOR UPDATE`` would). Lock order is always global, then workspace. On SQLite both
compile to nothing: its single writer serializes writers, the degradation every Phase 6 lock
documents.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import BigInteger, cast, func, literal, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.llm import budget as ledger
from app.llm.bound import Unbounded
from app.llm.metering import LLMBudgetExceededError, LLMMeter
from app.llm.models import LLMBudgetReservation, LLMUsageEvent
from app.organizations.models import Workspace

#: The refusal reasons (``LLMCeilingExceededError.reason``).
CEILINGS_UNSET = "ceilings_unset"
DAILY_CALLS_WORKSPACE = "daily_calls_workspace"
DAILY_COST_WORKSPACE = "daily_cost_workspace"
DAILY_COST_GLOBAL = "daily_cost_global"
CEILING_REASONS: tuple[str, ...] = (
    CEILINGS_UNSET,
    DAILY_CALLS_WORKSPACE,
    DAILY_COST_WORKSPACE,
    DAILY_COST_GLOBAL,
)

_GLOBAL_LOCK_ID = int.from_bytes(
    hashlib.sha256(b"signalnest.generation-ceilings:global").digest()[:8], "big", signed=True
)


class LLMCeilingExceededError(LLMBudgetExceededError):
    """A generation attempt refused by a daily ceiling, or because a ceiling is unset.

    Typed: ``reason`` is one of :data:`CEILING_REASONS`. No call was made."""


def day_window(now: datetime) -> tuple[datetime, datetime]:
    """``[start, end)`` of ``now``'s UTC calendar day."""
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def global_lock_statement():
    """The transaction-scoped advisory lock serializing generation across workspaces."""
    return select(func.pg_advisory_xact_lock(cast(literal(_GLOBAL_LOCK_ID), BigInteger)))


def workspace_lock_select(workspace_id: str):
    """``SELECT … FOR NO KEY UPDATE`` on the workspace row (see the module docstring)."""
    return (
        select(Workspace.id)
        .where(Workspace.id == workspace_id)
        .with_for_update(key_share=True)
    )


def lock_for_generation(db: Session, workspace_id: str) -> bool:
    """Take the global then the workspace generation lock, held until ``db``'s transaction
    ends. Returns False when the workspace row does not exist."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(global_lock_statement())
    return db.execute(workspace_lock_select(workspace_id)).first() is not None


@dataclass(frozen=True)
class DailyUsage:
    """One UTC day's ledger reading for a ceiling check."""

    workspace_calls: int
    workspace_cost: Decimal
    global_cost: Decimal


def _cost(db: Session, start: datetime, end: datetime, workspace_id: str | None) -> Decimal:
    usage = select(func.coalesce(func.sum(LLMUsageEvent.cost_usd), 0.0)).where(
        LLMUsageEvent.created_at >= start, LLMUsageEvent.created_at < end
    )
    unrecorded = select(
        func.coalesce(
            func.sum(
                func.coalesce(LLMBudgetReservation.settled_usd, LLMBudgetReservation.reserved_usd)
            ),
            0,
        )
    ).where(
        LLMBudgetReservation.created_at >= start,
        LLMBudgetReservation.created_at < end,
        LLMBudgetReservation.usage_recorded_at.is_(None),
    )
    if workspace_id is not None:
        usage = usage.where(LLMUsageEvent.workspace_id == workspace_id)
        unrecorded = unrecorded.where(LLMBudgetReservation.workspace_id == workspace_id)
    return ledger.usd(db.scalar(usage) or 0) + ledger.usd(db.scalar(unrecorded) or 0)


def daily_usage(db: Session, *, workspace_id: str, task: str, now: datetime) -> DailyUsage:
    """The ledger's reading of ``now``'s UTC day for ``workspace_id`` and globally."""
    start, end = day_window(now)
    calls = db.scalar(
        select(func.count())
        .select_from(LLMUsageEvent)
        .where(
            LLMUsageEvent.workspace_id == workspace_id,
            LLMUsageEvent.task == task,
            LLMUsageEvent.created_at >= start,
            LLMUsageEvent.created_at < end,
        )
    )
    return DailyUsage(
        workspace_calls=int(calls or 0),
        workspace_cost=_cost(db, start, end, workspace_id),
        global_cost=_cost(db, start, end, None),
    )


def _refuse(reason: str, detail: str | None = None) -> None:
    error = LLMCeilingExceededError(
        f"generation refused: {reason}" + (f" ({detail})" if detail else ""), reason
    )
    error.detail = detail
    raise error


def check_ceilings(
    db: Session,
    settings: Settings,
    *,
    workspace_id: str,
    task: str,
    now: datetime,
    worst: Decimal = Decimal(0),
    pending_calls: int = 0,
    pending_cost: Decimal = Decimal(0),
) -> DailyUsage:
    """Raise :class:`LLMCeilingExceededError` unless one more attempt (worst case ``worst``)
    fits every ceiling; ``pending_*`` are this caller's attempts not yet in the ledger.

    A ceiling is refused when it is already reached or the attempt could cross it."""
    unset = settings.creative_generation_ceilings_unset()
    if unset:
        _refuse(CEILINGS_UNSET, ", ".join(unset))
    usage = daily_usage(db, workspace_id=workspace_id, task=task, now=now)
    max_calls = int(settings.llm_max_calls_per_workspace_per_day)
    if usage.workspace_calls + pending_calls + 1 > max_calls:
        _refuse(DAILY_CALLS_WORKSPACE)
    workspace_ceiling = settings.llm_daily_cost_ceiling_usd_per_workspace
    for reason, spent, ceiling in (
        (DAILY_COST_WORKSPACE, usage.workspace_cost, workspace_ceiling),
        (DAILY_COST_GLOBAL, usage.global_cost, settings.llm_daily_cost_ceiling_usd_global),
    ):
        limit = ledger.usd(ceiling)
        total = spent + pending_cost
        if total >= limit or total + worst > limit:
            _refuse(reason)
    return usage


class CeilingMeter(LLMMeter):
    """An :class:`~app.llm.metering.LLMMeter` that also enforces the daily generation
    ceilings before every provider attempt (see the module docstring).

    ``db`` is the session holding :func:`lock_for_generation`; the checks read the ledger
    through it. Everything else — the run cap, the monthly budget, the usage rows — is the
    base meter's, unchanged.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        db: Session,
        task: str,
        clock: Callable[[], datetime] | None = None,
        **kwargs,
    ):
        super().__init__(settings, **kwargs)
        self._ceiling_db = db
        self._ceiling_task = task
        self._ceiling_clock = clock or (lambda: datetime.now(UTC))

    def _unwritten(self) -> tuple[int, Decimal]:
        """This meter's attempts not yet written to the ledger (same-session path)."""
        records = self._records
        calls = sum(1 for r in records if r["task"] == self._ceiling_task)
        cost = sum((ledger.usd(r["cost_usd"]) for r in records), Decimal(0))
        return calls, cost

    def before_attempt(self, provider: str, model: str, payload: bytes | None):
        if self.workspace_id is None:
            raise LLMBudgetExceededError("LLM call not made: unbound meter", "unbound")
        worst = self.worst_case(provider, model, payload)
        calls, cost = self._unwritten()
        check_ceilings(
            self._ceiling_db,
            self.settings,
            workspace_id=self.workspace_id,
            task=self._ceiling_task,
            now=self._ceiling_clock(),
            # An unbounded attempt is refused by the base meter (``cost_unbounded``).
            worst=Decimal(0) if isinstance(worst, Unbounded) else worst,
            pending_calls=calls,
            pending_cost=cost,
        )
        return super().before_attempt(provider, model, payload)


__all__ = [
    "CEILINGS_UNSET",
    "CEILING_REASONS",
    "DAILY_CALLS_WORKSPACE",
    "DAILY_COST_GLOBAL",
    "DAILY_COST_WORKSPACE",
    "CeilingMeter",
    "DailyUsage",
    "LLMCeilingExceededError",
    "check_ceilings",
    "daily_usage",
    "day_window",
    "global_lock_statement",
    "lock_for_generation",
    "workspace_lock_select",
]
