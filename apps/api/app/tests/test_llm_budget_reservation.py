"""D-LLM-1 option B on SQLite: the budget ledger's primitives and the meter's two paths.

The hard ceiling is a PostgreSQL guarantee and its proofs (multi-process concurrency,
crash, retry, duplicate settlement, reservation recovery) live in
``test_postgres_llm_accounting``. This module pins, on SQLite and offline:

* the ledger primitives (``app.llm.budget``): reserve until refused, settle exactly once
  (duplicate settlement is a no-op), release at the worst case for a job that ended, for
  an expired reservation and for a terminal job, tenant isolation, month rollover;
* the meter's shared-ledger path (forced on SQLite, where the run's session holds no
  write lock): reservation before the call, settlement with the usage row at
  settlement, refusal across two meters sharing one ledger, nothing left to flush;
* the meter's same-session path (the default on SQLite): the ledger rows are written
  at flush, and the **degraded per-session guarantee** - two runs bound before either
  commits can together exceed the budget - is pinned as the documented behaviour;
* the conservative cost basis: timeout, undecodable reply and usage-less reply settle
  at the worst case; a rejected or unavailable reply costs nothing;
* the run endpoint's gate reads the period's ``reserved_usd``;
* the worker's recovery slot sweeps expired reservations, the terminal-failure hook
  and the attempt's end release a job's reservations, and the metric counts them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.metrics import (
    LLM_COST_BOUND_VIOLATIONS_TOTAL,
    LLM_RESERVATIONS_RELEASED_TOTAL,
    LLM_SETTLEMENTS_AFTER_RELEASE_TOTAL,
    LLM_SETTLEMENTS_ORPHANED_TOTAL,
    InMemoryMetrics,
    configure_metrics,
)
from app.db.models import Base
from app.jobs import handlers as job_handlers
from app.jobs.context import ExecutionContext
from app.jobs.registry import TerminalFailureContext
from app.jobs.status import JobErrorCode, JobStatus
from app.jobs.store import DurableJobStore
from app.jobs.worker import JobRunner
from app.llm import budget as ledger
from app.llm.base import LLMMalformedError, LLMTimeoutError, LLMUnavailableError
from app.llm.metering import (
    LLMBudgetExceededError,
    LLMBudgetExhaustedError,
    LLMMeter,
    ensure_run_allowed,
)
from app.llm.models import LLMBudgetPeriod, LLMBudgetReservation, LLMUsageEvent
from app.llm.providers_real import AnthropicProvider
from app.llm.service import LLMService
from app.organizations.models import Organization, Workspace
from app.tests._llm_offline import offline_llm, offline_settings, wire_payload

KEY = "fixture-key"
MODEL = "claude-sonnet-4-6"
ORG = "org-llmb-0001"
WS = "ws-llmb-00001"
WS2 = "ws-llmb-00002"
T0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
CLASSIFIED = {"signal_type": "complaint", "pain_point_dna": "speed_complaint"}
#: 1000 in + 500 out at the sonnet-4-6 table price.
CALL_COST = Decimal("0.010500")


@pytest.fixture(scope="module", autouse=True)
def _offline():
    with offline_llm():
        yield


def _settings(**overrides) -> Settings:
    return offline_settings(llm_provider="anthropic", llm_api_key=KEY, llm_model=MODEL, **overrides)


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'llmb.db'}", connect_args={"check_same_thread": False}, future=True
    )
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    with make() as db:
        db.add(Organization(id=ORG, name="LLMB", slug="llmb"))
        db.add(Workspace(id=WS, organization_id=ORG, name="LLMB WS", slug="llmb-ws"))
        db.add(Workspace(id=WS2, organization_id=ORG, name="LLMB WS2", slug="llmb-ws2"))
        db.commit()
    yield make
    engine.dispose()


def _reserve(db, key: str, worst: Decimal, budget: Decimal, *, now=T0, job_id=None, ws=WS):
    return ledger.reserve(
        db,
        workspace_id=ws,
        organization_id=ORG,
        job_id=job_id,
        attempt_key=key,
        worst_usd=worst,
        budget_usd=budget,
        expires_at=now + timedelta(seconds=90),
        now=now,
    )


def _usage(cost: Decimal) -> dict:
    return {
        "scout_request_id": None,
        "task": "classify_signal",
        "provider": "anthropic",
        "model": MODEL,
        "input_tokens": 1000,
        "output_tokens": 500,
        "cost_usd": float(cost),
        "outcome": "ok",
    }


def _period(db, ws=WS, now=T0) -> LLMBudgetPeriod | None:
    return db.scalar(
        select(LLMBudgetPeriod).where(
            LLMBudgetPeriod.workspace_id == ws,
            LLMBudgetPeriod.period_start == ledger.period_start_for(now),
        )
    )


def _reservation(db, key: str) -> LLMBudgetReservation:
    row = db.scalar(select(LLMBudgetReservation).where(LLMBudgetReservation.attempt_key == key))
    assert row is not None, key
    return row


def _enqueue(db, job_id_hint: str) -> str:
    job = DurableJobStore().enqueue(
        db,
        organization_id=ORG,
        workspace_id=WS,
        job_type="test.ok",
        payload={"k": job_id_hint},
        payload_hash=job_id_hint,
        now=T0,
    )
    db.commit()
    return job.id


# --------------------------------------------------------------------------- #
# Ledger primitives
# --------------------------------------------------------------------------- #
def test_reserve_until_the_budget_refuses_and_the_period_counts_exactly(factory):
    w, budget = Decimal("0.0105"), Decimal("0.05")
    with factory() as db:
        outcomes = [_reserve(db, f"k:{i}", w, budget) for i in range(6)]
        db.commit()
        assert outcomes == [True, True, True, True, False, False]
        period = _period(db)
        assert period.reserved_usd == Decimal("0.042000") and period.settled_usd == 0
        assert period.organization_id == ORG
        rows = db.scalars(select(LLMBudgetReservation)).all()
        assert len(rows) == 4 and {r.state for r in rows} == {"reserved"}
        assert sum((r.reserved_usd for r in rows), Decimal(0)) == period.reserved_usd


def test_settle_records_the_actual_once_keeps_the_worst_case_and_writes_the_usage_row(factory):
    w, budget, actual = Decimal("0.02"), Decimal("1"), CALL_COST
    with factory() as db:
        assert _reserve(db, "k:0", w, budget)
        db.commit()
        assert ledger.settle(
            db, attempt_key="k:0", settled_usd=actual, now=T0, usage=_usage(actual)
        )
        db.commit()
        # P4: a second settlement of the same key is a no-op.
        assert not ledger.settle(
            db, attempt_key="k:0", settled_usd=actual, now=T0, usage=_usage(actual)
        )
        assert not ledger.settle(db, attempt_key="k:0", settled_usd=Decimal("9"), now=T0)
        db.commit()
        period = _period(db)
        assert (period.reserved_usd, period.settled_usd) == (w, actual)  # worst case kept
        row = _reservation(db, "k:0")
        assert (row.state, row.settled_usd, row.reserved_usd) == ("settled", actual, w)
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == 1
        usage = db.scalar(select(LLMUsageEvent))
        assert (usage.workspace_id, usage.organization_id, usage.cost_usd) == (WS, ORG, 0.0105)


def test_a_report_above_the_bound_charges_the_bound_and_counts_a_violation(factory, caplog):
    """The bound is a true upper bound: usage above it means its premise failed. The
    bound stays the charge (reserved_usd never exceeds the budget), the usage row keeps
    what the provider reported, and the violation is logged, counted and on the period."""
    w, budget, actual = Decimal("0.01"), Decimal("0.05"), Decimal("0.03")
    metrics = InMemoryMetrics()
    configure_metrics(metrics)
    try:
        with factory() as db, caplog.at_level("ERROR", logger="signalnest.llm"):
            assert _reserve(db, "k:0", w, budget)
            assert ledger.settle(
                db, attempt_key="k:0", settled_usd=actual, now=T0, usage=_usage(actual)
            )
            db.commit()
            period = _period(db)
            assert (period.reserved_usd, period.settled_usd) == (w, w)  # the bound, kept
            assert period.bound_violations == 1
            assert _reservation(db, "k:0").settled_usd == w
            assert db.scalar(select(LLMUsageEvent.cost_usd)) == 0.03  # the report, recorded
            assert [r.message for r in caplog.records if r.levelname == "ERROR"] == [
                "llm.cost_bound_violated"
            ]
    finally:
        configure_metrics(None)
    assert metrics.counter_value(LLM_COST_BOUND_VIOLATIONS_TOTAL, outcome="settle") == 1


def test_settling_an_unknown_key_changes_nothing(factory):
    with factory() as db:
        assert not ledger.settle(db, attempt_key="never", settled_usd=Decimal(1), now=T0)
        assert db.scalar(select(func.count()).select_from(LLMBudgetPeriod)) == 0


def test_release_charges_a_leaked_reservation_its_worst_case(factory):
    """An unknown outcome is charged its worst case: releasing never lowers reserved_usd."""
    w, budget = Decimal("0.02"), Decimal("0.05")
    with factory() as db:
        job = _enqueue(db, "j1")
        assert _reserve(db, "a:1:0", w, budget, job_id=job)
        assert _reserve(db, "a:1:1", w, budget, job_id=job)
        db.commit()
        assert ledger.release_for_job(db, job_id=job, now=T0, outcome="terminal") == 2
        db.commit()
        period = _period(db)
        assert (period.reserved_usd, period.settled_usd) == (Decimal("0.04"), Decimal("0.04"))
        for key in ("a:1:0", "a:1:1"):
            row = _reservation(db, key)
            assert (row.state, row.settled_usd) == ("released", w)
        # A later settlement of a released row is a no-op: its worst case stands.
        assert not ledger.settle(db, attempt_key="a:1:0", settled_usd=Decimal("0.001"), now=T0)
        # Releasing again releases nothing.
        assert ledger.release_for_job(db, job_id=job, now=T0, outcome="terminal") == 0
        # The remainder is still spendable, exactly budget - 2w.
        assert _reserve(db, "b:1:0", Decimal("0.01"), budget)
        assert not _reserve(db, "b:1:1", Decimal("0.001"), budget)


@pytest.mark.parametrize("after", ["released", "expired"])
def test_repeated_late_settlements_write_one_usage_row_per_attempt(factory, after):
    """Review r2 follow-up: the late path claims the attempt's usage row, so a repeat
    (the after-rollback writer, a retry of the settlement) adds nothing."""
    w, budget = Decimal("0.02"), Decimal("1")
    with factory() as db:
        if after == "released":
            job = _enqueue(db, "rep")
            assert _reserve(db, f"{job}:1:0", w, budget, job_id=job)
            assert ledger.release_for_job(db, job_id=job, now=T0, outcome="terminal") == 1
            key = f"{job}:1:0"
        else:
            assert _reserve(db, "rep:1:0", w, budget)
            assert ledger.release_expired(db, now=T0 + timedelta(hours=1)) == 1
            key = "rep:1:0"
        db.commit()
        for _ in range(4):
            assert not ledger.settle(
                db, attempt_key=key, settled_usd=CALL_COST, now=T0, usage=_usage(CALL_COST)
            )
            db.commit()
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == 1
        assert _reservation(db, key).usage_recorded_at is not None
        period = _period(db)
        assert (period.reserved_usd, period.settled_usd, period.bound_violations) == (w, w, 0)


def test_a_settlement_that_loses_the_race_to_recovery_still_records_the_call(factory):
    """The settlement read ``reserved``; recovery commits its release before the
    settlement's own update: that update matches nothing. The usage row is not dropped."""
    w, budget = Decimal("0.02"), Decimal("1")
    engine = factory.kw["bind"]
    with factory() as db:
        job = _enqueue(db, "lost")
        assert _reserve(db, f"{job}:1:0", w, budget, job_id=job)
        db.commit()
    fired: list[int] = []

    @event.listens_for(engine, "before_cursor_execute")
    def release_first(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE llm_budget_reservations") and not fired:
            fired.append(1)
            with factory() as other:
                ledger.release_for_job(other, job_id=job, now=T0, outcome="terminal")
                other.commit()

    try:
        with factory() as db:
            assert not ledger.settle(
                db, attempt_key=f"{job}:1:0", settled_usd=CALL_COST, now=T0, usage=_usage(CALL_COST)
            )
            db.commit()
    finally:
        event.remove(engine, "before_cursor_execute", release_first)
    assert fired == [1]
    with factory() as db:
        assert _reservation(db, f"{job}:1:0").state == "released"
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == 1
        period = _period(db)
        assert (period.reserved_usd, period.settled_usd) == (w, w)


def test_a_settlement_after_release_moves_no_money_but_records_the_call(factory, caplog):
    """MINOR-1 (review r1): recovery won the race against a live call. The worst case
    stands as the charge; the call's usage row is still written, with a WARNING and a
    metric, so reconciliation sees the actual."""
    w, budget = Decimal("0.02"), Decimal("1")
    with factory() as db:
        job = _enqueue(db, "late")
        assert _reserve(db, f"{job}:1:0", w, budget, job_id=job)
        assert ledger.release_for_job(db, job_id=job, now=T0, outcome="terminal") == 1
        db.commit()
        metrics = InMemoryMetrics()
        configure_metrics(metrics)
        try:
            late = ledger.settle(
                db, attempt_key=f"{job}:1:0", settled_usd=CALL_COST, now=T0, usage=_usage(CALL_COST)
            )
            db.commit()
        finally:
            configure_metrics(None)
        assert late is False  # no money moved ...
        period = _period(db)
        assert (period.reserved_usd, period.settled_usd) == (w, w)
        row = _reservation(db, f"{job}:1:0")
        assert (row.state, row.settled_usd) == ("released", w)
        usage = db.scalar(select(LLMUsageEvent))  # ... but the call is recorded
        assert (usage.job_id, usage.cost_usd, usage.outcome) == (job, 0.0105, "ok")
        assert metrics.counter_value(LLM_SETTLEMENTS_AFTER_RELEASE_TOTAL, outcome="released") == 1
        record = next(r for r in caplog.records if r.getMessage() == "llm.settlement_after_release")
        assert record.levelname == "WARNING"
        # A usage-less repeat (no call data) writes nothing more.
        assert not ledger.settle(db, attempt_key=f"{job}:1:0", settled_usd=CALL_COST, now=T0)
        db.commit()
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == 1


def test_the_expiry_sweep_defers_to_a_live_job_lease(factory):
    """MINOR-1 (review r1): a reservation past its expiry whose job still holds a live
    lease is left alone; once the lease has lapsed (or the job is no longer running) it
    expires by the clock."""
    from app.jobs.models import Job

    w, budget = Decimal("0.01"), Decimal("1")
    with factory() as db:
        heartbeating = _enqueue(db, "heartbeating")
        lapsed = _enqueue(db, "lapsed")
        for key, job in ((f"{heartbeating}:1:0", heartbeating), (f"{lapsed}:1:0", lapsed)):
            assert _reserve(db, key, w, budget, now=T0 - timedelta(hours=1), job_id=job)
        for job, lease in (
            (heartbeating, T0 + timedelta(days=1)),
            (lapsed, T0 - timedelta(seconds=1)),
        ):
            row = db.get(Job, job)
            row.status = JobStatus.RUNNING.value
            row.lease_expires_at = lease
        db.commit()
        assert ledger.release_expired(db, now=T0) == 1
        db.commit()
        states = {r.attempt_key: r.state for r in db.scalars(select(LLMBudgetReservation))}
        assert states == {f"{heartbeating}:1:0": "reserved", f"{lapsed}:1:0": "expired"}
        # The lease lapses: the next sweep expires it.
        db.get(Job, heartbeating).lease_expires_at = T0 - timedelta(seconds=1)
        db.commit()
        assert ledger.release_expired(db, now=T0) == 1
        db.commit()
        assert _reservation(db, f"{heartbeating}:1:0").state == "expired"


def test_the_expiry_window_covers_the_clients_per_phase_timeouts():
    s = _settings(llm_timeout_seconds=30, worker_lease_seconds=30.0)
    assert ledger.reservation_expiry(s, T0) == T0 + timedelta(seconds=3 * 30 + 30 + 60)


def test_sweep_releases_expired_and_terminal_reservations_only(factory):
    """P5 on SQLite: expired rows and rows of terminal jobs are released; others stay."""
    w, budget = Decimal("0.01"), Decimal("1")
    with factory() as db:
        live_job = _enqueue(db, "live")
        done_job = _enqueue(db, "done")
        assert _reserve(db, "old:1:0", w, budget, now=T0 - timedelta(hours=1), job_id=live_job)
        assert _reserve(db, "fresh:1:0", w, budget, now=T0, job_id=live_job)
        assert _reserve(db, "done:1:0", w, budget, now=T0, job_id=done_job)
        assert _reserve(db, "nojob:0:0", w, budget, now=T0)
        from app.jobs.models import Job

        db.get(Job, done_job).status = JobStatus.SUCCEEDED.value
        db.commit()
        metrics = InMemoryMetrics()
        configure_metrics(metrics)
        try:
            assert ledger.sweep(db, now=T0) == {"expired": 1, "terminal": 1}
            db.commit()
        finally:
            configure_metrics(None)
        states = {r.attempt_key: r.state for r in db.scalars(select(LLMBudgetReservation))}
        assert states == {
            "old:1:0": "expired",
            "fresh:1:0": "reserved",
            "done:1:0": "released",
            "nojob:0:0": "reserved",
        }
        period = _period(db)
        assert (period.reserved_usd, period.settled_usd) == (Decimal("0.04"), Decimal("0.02"))
        assert metrics.counter_value(LLM_RESERVATIONS_RELEASED_TOTAL, outcome="expired") == 1
        assert metrics.counter_value(LLM_RESERVATIONS_RELEASED_TOTAL, outcome="terminal") == 1
        # Idempotent: a second sweep finds nothing.
        assert ledger.sweep(db, now=T0) == {"expired": 0, "terminal": 0}


def test_two_workspaces_periods_are_independent(factory):
    w, budget = Decimal("0.03"), Decimal("0.05")
    with factory() as db:
        assert _reserve(db, "a:0", w, budget, ws=WS)
        assert not _reserve(db, "a:1", w, budget, ws=WS)  # WS is full
        assert _reserve(db, "b:0", w, budget, ws=WS2)  # WS2 is untouched
        db.commit()
        assert _period(db, WS).reserved_usd == w and _period(db, WS2).reserved_usd == w
        assert ledger.reserved_month_to_date_usd(db, WS, T0) == w


def test_the_month_rolls_over_on_the_period_key(factory):
    w, budget = Decimal("0.05"), Decimal("0.05")
    last_second = datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)
    first_second = datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)
    with factory() as db:
        assert _reserve(db, "oct:0", w, budget, now=last_second)
        assert not _reserve(db, "oct:1", w, budget, now=last_second)  # October is full
        assert _reserve(db, "nov:0", w, budget, now=first_second)  # November is fresh
        db.commit()
        assert ledger.reserved_month_to_date_usd(db, WS, last_second) == w
        assert ledger.reserved_month_to_date_usd(db, WS, first_second) == w
        assert {p.period_start.isoformat() for p in db.scalars(select(LLMBudgetPeriod))} == {
            "2026-10-01",
            "2026-11-01",
        }


def test_record_settled_is_idempotent_per_attempt_key(factory):
    with factory() as db:
        kwargs = dict(
            workspace_id=WS,
            organization_id=ORG,
            job_id=None,
            attempt_key="r:0:0",
            settled_usd=CALL_COST,
            now=T0,
            usage=_usage(CALL_COST),
        )
        assert ledger.record_settled(db, **kwargs)
        assert not ledger.record_settled(db, **kwargs)
        db.commit()
        assert _period(db).reserved_usd == CALL_COST
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == 1
        assert _reservation(db, "r:0:0").state == "settled"


# --------------------------------------------------------------------------- #
# Late callbacks after the tenant was deleted (privacy: never resurrect a tenant row)
# --------------------------------------------------------------------------- #
@pytest.fixture(params=["fk-on", "fk-off"])
def tenant_db(request, tmp_path):
    """Like ``factory``, with and without foreign-key enforcement (the production SQLite
    pragma is ON; with it OFF the guard against resurrection cannot lean on the schema)."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'del.db'}", connect_args={"check_same_thread": False}, future=True
    )
    if request.param == "fk-on":

        @event.listens_for(engine, "connect")
        def _fk(dbapi_connection, _record):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    with make() as db:
        db.add(Organization(id=ORG, name="LLMB", slug="llmb"))
        db.add(Workspace(id=WS, organization_id=ORG, name="LLMB WS", slug="llmb-ws"))
        db.commit()
    yield make, request.param
    engine.dispose()


def _row_counts(db) -> tuple[int, int, int]:
    return tuple(
        db.scalar(select(func.count()).select_from(model))
        for model in (LLMBudgetPeriod, LLMBudgetReservation, LLMUsageEvent)
    )


@pytest.mark.parametrize("parent", ["workspace", "organization"])
def test_late_callbacks_after_the_tenant_was_deleted_recreate_nothing(tenant_db, parent):
    make, fk = tenant_db
    w, budget = Decimal("0.02"), Decimal("1")
    with make() as db:
        job = _enqueue(db, "gone")
        assert _reserve(db, f"{job}:1:0", w, budget, job_id=job)  # released later by the sweep
        assert _reserve(db, "x:1:0", w, budget, now=T0)  # in flight when the tenant goes
        assert _reserve(db, "x:1:1", w, budget, now=T0)  # settles normally before
        assert ledger.settle(
            db, attempt_key="x:1:1", settled_usd=CALL_COST, now=T0, usage=_usage(CALL_COST)
        )
        db.commit()
        assert _row_counts(db) == (1, 3, 1)
        # The tenant is erased (jobs go with it; a bare SQL delete mirrors the erasure walk).
        if fk == "fk-on":
            db.execute(delete(LLMUsageEvent))
            db.execute(delete(Workspace if parent == "workspace" else Organization))
        else:
            # Without enforcement nothing cascades: remove the tenant's rows as an erasure would.
            db.execute(delete(LLMUsageEvent))
            db.execute(delete(LLMBudgetReservation))
            db.execute(delete(LLMBudgetPeriod))
            db.execute(delete(Workspace))
            if parent == "organization":
                db.execute(delete(Organization))
        db.commit()
        assert _row_counts(db) == (0, 0, 0)

    metrics = InMemoryMetrics()
    configure_metrics(metrics)
    try:
        with make() as db:
            # a settlement (the call finished after the tenant was erased)
            assert not ledger.settle(
                db, attempt_key="x:1:0", settled_usd=CALL_COST, now=T0, usage=_usage(CALL_COST)
            )
            assert not ledger.settle(db, attempt_key=f"{job}:1:0", settled_usd=CALL_COST, now=T0)
            # recovery of every kind
            assert ledger.release_for_job(db, job_id=job, now=T0, outcome="terminal") == 0
            assert ledger.release_expired(db, now=T0 + timedelta(days=1)) == 0
            assert ledger.release_terminal(db, now=T0) == 0
            assert ledger.sweep(db, now=T0 + timedelta(days=1)) == {"expired": 0, "terminal": 0}
            # the writers that insert: a reservation and a same-session record
            assert not _reserve(db, "y:1:0", w, budget)
            assert not ledger.record_settled(
                db,
                workspace_id=WS,
                organization_id=ORG,
                job_id=None,
                attempt_key="z:1:0",
                settled_usd=CALL_COST,
                now=T0,
                usage=_usage(CALL_COST),
            )
            db.commit()
            assert _row_counts(db) == (0, 0, 0)  # no period, no reservation, no usage row
            assert db.scalar(select(func.count()).select_from(Workspace)) == 0
    finally:
        configure_metrics(None)
    assert metrics.counter_value(LLM_SETTLEMENTS_ORPHANED_TOTAL, outcome="settle") == 2
    assert metrics.counter_value(LLM_SETTLEMENTS_ORPHANED_TOTAL, outcome="reserve") == 1
    assert metrics.counter_value(LLM_SETTLEMENTS_ORPHANED_TOTAL, outcome="record_settled") == 1


def test_a_call_that_outlives_its_tenant_finishes_without_error_or_rows(tenant_db):
    """The meter path end to end: the reservation is made, the tenant is erased while the
    provider call is in flight, the call returns. Settlement must neither raise into the
    run nor write anything."""
    make, _fk = tenant_db
    s = _settings(llm_workspace_monthly_budget_usd=1.0)
    meter = LLMMeter(s, session_factory=make, shared_ledger=True)
    with make() as db:
        meter.bind(db, organization_id=ORG, workspace_id=WS, scout_request_id=None)

    def handler(request):
        with make() as peek:  # the erasure lands while the call is in flight
            peek.execute(delete(LLMBudgetReservation))
            peek.execute(delete(LLMBudgetPeriod))
            peek.execute(delete(Workspace))
            peek.commit()
        return _reply(CLASSIFIED)

    _service(s, handler).run("classify_signal", {"content": "x"}, meter=meter)
    with make() as db:
        assert _row_counts(db) == (0, 0, 0)
        meter.flush(db)  # nothing to add on the shared path
        db.commit()
        assert _row_counts(db) == (0, 0, 0)


# --------------------------------------------------------------------------- #
# The meter on both paths
# --------------------------------------------------------------------------- #
def _reply(output: dict, *, tokens=(1000, 500), stop_reason="end_turn") -> httpx.Response:
    body = {"content": [{"type": "text", "text": json.dumps(output)}], "stop_reason": stop_reason}
    if tokens is not None:
        body["usage"] = {"input_tokens": tokens[0], "output_tokens": tokens[1]}
    return httpx.Response(200, json=body)


def _service(settings: Settings, handler) -> LLMService:
    provider = AnthropicProvider(
        KEY, MODEL, 5, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    return LLMService(provider, sleep=lambda _s: None, settings=settings)


def _bound(factory, settings: Settings, *, shared: bool | None, job_id=None, attempt=0):
    meter = LLMMeter(
        settings, job_id=job_id, attempt=attempt, session_factory=factory, shared_ledger=shared
    )
    with factory() as db:
        meter.bind(db, organization_id=ORG, workspace_id=WS, scout_request_id=None)
    return meter


def _worst(meter: LLMMeter, content: str = "x") -> Decimal:
    """The bound the meter reserves for one ``classify_signal`` call carrying ``content``."""
    bound = meter.worst_case(
        "anthropic", MODEL, wire_payload(MODEL, content=content, tenant_ref=meter.tenant_ref)
    )
    assert isinstance(bound, Decimal)
    return bound


def test_sqlite_defaults_to_the_same_session_path_even_with_a_session_factory(factory):
    meter = _bound(factory, _settings(), shared=None)
    assert meter.shared_ledger is False
    assert (
        LLMMeter(_settings())
        .bind(factory(), organization_id=ORG, workspace_id=WS, scout_request_id=None)
        .shared_ledger
        is False
    )


def test_a_shared_ledger_meter_needs_a_session_factory(factory):
    meter = LLMMeter(_settings(), shared_ledger=True)
    with factory() as db, pytest.raises(RuntimeError, match="session factory"):
        meter.bind(db, organization_id=ORG, workspace_id=WS, scout_request_id=None)
    assert meter.workspace_id is None  # nothing changed


def test_shared_path_reserves_before_the_call_and_settles_with_the_usage_row(factory):
    s = _settings(llm_workspace_monthly_budget_usd=1.0)
    seen: list[dict] = []

    def handler(request):
        # At the moment the provider is called the reservation is already committed.
        with factory() as peek:
            period = _period(peek)
            rows = peek.scalars(select(LLMBudgetReservation)).all()
            seen.append(
                {
                    "reserved": period.reserved_usd,
                    "states": [r.state for r in rows],
                    "usage_rows": peek.scalar(select(func.count()).select_from(LLMUsageEvent)),
                }
            )
        return _reply(CLASSIFIED)

    meter = _bound(factory, s, shared=True, job_id="job-shared", attempt=3)
    assert meter.shared_ledger
    worst = _worst(meter)
    _service(s, handler).run("classify_signal", {"content": "x"}, meter=meter)
    assert len(seen) == 1 and seen[0]["states"] == ["reserved"] and seen[0]["usage_rows"] == 0
    assert seen[0]["reserved"] == worst  # the full bound of the call, before it was made
    with factory() as db:
        period = _period(db)
        row = db.scalar(select(LLMBudgetReservation))
        assert (period.reserved_usd, period.settled_usd) == (row.reserved_usd, CALL_COST)
        assert row.reserved_usd > CALL_COST  # the worst case stays consumed
        assert (row.state, row.settled_usd, row.job_id) == ("settled", CALL_COST, "job-shared")
        assert row.attempt_key == "job-shared:3:0"
        usage = db.scalar(select(LLMUsageEvent))
        assert (usage.job_id, usage.cost_usd, usage.outcome) == ("job-shared", 0.0105, "ok")
        # Nothing is left for the run-end flush.
        meter.flush(db)
        db.commit()
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == 1
    assert meter.spent == pytest.approx(0.0105) and meter.attempts == 1


def test_shared_path_refuses_across_meters_once_the_ledger_is_full(factory):
    """Two runs sharing one ledger: the second sees the first's reservation at once."""
    probe = _bound(factory, _settings(), shared=True)
    w = _worst(probe)
    s = _settings(llm_workspace_monthly_budget_usd=float(w * Decimal("1.5")))
    first = _bound(factory, s, shared=True)
    second = _bound(factory, s, shared=True)
    assert w <= Decimal(str(s.llm_workspace_monthly_budget_usd)) < 2 * w  # exactly one fits
    calls = []
    _service(s, lambda r: calls.append(1) or _reply(CLASSIFIED)).run(
        "classify_signal", {"content": "x"}, meter=first
    )
    with pytest.raises(LLMBudgetExceededError) as exc:
        _service(s, lambda r: calls.append(1) or _reply(CLASSIFIED)).run(
            "classify_signal", {"content": "x"}, meter=second
        )
    assert exc.value.reason == "workspace_budget" and len(calls) == 1
    assert second.refusals == {"workspace_budget": 1} and second.spent == 0.0
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(LLMBudgetReservation)) == 1


def test_shared_path_releases_an_earlier_attempts_leak_at_rebind(factory):
    """P3 shape on SQLite: the retried attempt starts by releasing what attempt 1 left."""
    s = _settings(llm_workspace_monthly_budget_usd=1.0)
    with factory() as db:
        job = _enqueue(db, "retry")
        assert _reserve(db, f"{job}:1:0", Decimal("0.02"), Decimal("1"), job_id=job)
        db.commit()
    meter = _bound(factory, s, shared=True, job_id=job, attempt=2)
    with factory() as db:
        row = _reservation(db, f"{job}:1:0")
        assert (row.state, row.settled_usd) == ("released", Decimal("0.02"))
    _service(s, lambda r: _reply(CLASSIFIED)).run("classify_signal", {"content": "x"}, meter=meter)
    with factory() as db:
        rows = {r.attempt_key: r for r in db.scalars(select(LLMBudgetReservation))}
        assert {k: r.state for k, r in rows.items()} == {
            f"{job}:1:0": "released",
            f"{job}:2:0": "settled",
        }
        period = _period(db)
        assert period.reserved_usd == Decimal("0.02") + rows[f"{job}:2:0"].reserved_usd
        assert period.settled_usd == Decimal("0.02") + CALL_COST


def test_same_session_path_writes_the_ledger_at_flush(factory):
    s = _settings(llm_workspace_monthly_budget_usd=1.0)
    meter = _bound(factory, s, shared=False, job_id="job-same", attempt=1)
    _service(s, lambda r: _reply(CLASSIFIED)).run("classify_signal", {"content": "x"}, meter=meter)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(LLMBudgetReservation)) == 0  # not yet
        meter.flush(db)
        db.commit()
        row = db.scalar(select(LLMBudgetReservation))
        assert (row.state, row.attempt_key, row.reserved_usd, row.settled_usd) == (
            "settled",
            "job-same:1:0",
            CALL_COST,
            CALL_COST,
        )
        period = _period(db)
        assert (period.reserved_usd, period.settled_usd) == (CALL_COST, CALL_COST)
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == 1
        meter.flush(db)  # a second flush adds nothing (idempotent per attempt key)
        db.commit()
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == 1


def test_same_session_path_is_only_a_per_session_guarantee(factory):
    """The documented SQLite degradation: two runs bound before either commits both spend."""
    w = _worst(_bound(factory, _settings(), shared=False))
    budget = w * Decimal("1.2")  # one call fits; two together (at 0.018 each) do not
    s = _settings(llm_workspace_monthly_budget_usd=float(budget))
    first = _bound(factory, s, shared=False)
    second = _bound(factory, s, shared=False)
    calls = []
    for meter in (first, second):
        _service(s, lambda r: calls.append(1) or _reply(CLASSIFIED, tokens=(1000, 1000))).run(
            "classify_signal", {"content": "x"}, meter=meter
        )
    assert len(calls) == 2  # together above the budget that admits a single call
    with factory() as db:
        first.flush(db)
        second.flush(db)
        db.commit()
        assert _period(db).reserved_usd == Decimal("0.036") > budget
    # A run bound after those commits sees them and is refused.
    third = _bound(factory, s, shared=False)
    with pytest.raises(LLMBudgetExceededError):
        _service(s, lambda r: _reply(CLASSIFIED)).run(
            "classify_signal", {"content": "x"}, meter=third
        )


@pytest.mark.parametrize("shared", [True, False], ids=["shared-ledger", "same-session"])
def test_conservative_cost_basis(factory, shared):
    s = _settings(llm_max_retries=0, llm_workspace_monthly_budget_usd=5.0)
    cases = {
        "timeout": (httpx.ReadTimeout("slow"), LLMTimeoutError, "worst"),
        "undecodable": (
            httpx.Response(200, content=b"\x1f\x8b not json"),
            LLMMalformedError,
            "worst",
        ),
        "no_usage_block": (_reply(CLASSIFIED, tokens=None), None, "worst"),
        # Step 2: a connection that fails AFTER the request was sent is unknown, not free.
        "connection_reset_after_send": (httpx.ReadError("reset"), LLMUnavailableError, "worst"),
        "write_error_during_send": (httpx.WriteError("broken pipe"), LLMUnavailableError, "worst"),
        "remote_protocol_error": (
            httpx.RemoteProtocolError("closed"),
            LLMUnavailableError,
            "worst",
        ),
        # Provably not dispatched (nothing can have left): free as before. A pool or
        # connect timeout is a timeout, and a timeout keeps its worst-case rule.
        "connect_error": (httpx.ConnectError("refused"), LLMUnavailableError, 0.0),
        "pool_timeout": (httpx.PoolTimeout("no connection"), LLMTimeoutError, "worst"),
        "unavailable": (
            httpx.Response(529, json={"type": "error", "error": {}}),
            LLMUnavailableError,
            0.0,
        ),
        "reported": (_reply(CLASSIFIED), None, 0.0105),
    }
    for name, (step, error, expected) in cases.items():
        meter = _bound(factory, s, shared=shared)
        w = _worst(meter, "")

        def handler(request, step=step):
            if isinstance(step, Exception):
                raise step
            return step

        if error is None:
            _service(s, handler).run("classify_signal", {"content": ""}, meter=meter)
        else:
            with pytest.raises(error):
                _service(s, handler).run("classify_signal", {"content": ""}, meter=meter)
        charged = meter.spent
        if expected == "worst":
            assert charged == pytest.approx(float(w)), name
        else:
            assert charged == pytest.approx(expected), name
        if shared:
            with factory() as db:
                row = db.scalar(
                    select(LLMBudgetReservation).order_by(LLMBudgetReservation.created_at.desc())
                )
                assert row.state == "settled" and float(row.settled_usd) == pytest.approx(charged)


@pytest.mark.parametrize("shared", [True, False], ids=["shared-ledger", "same-session"])
def test_a_provider_reporting_more_than_the_bound_is_charged_the_bound_and_flagged(
    factory, shared, caplog
):
    """The invariant behind the guarantee: actual <= reserved. If a provider ever reports
    usage above the bound (its premise failed) the period is NOT pushed over the budget:
    the bound is the charge, ERROR + metric + ``bound_violations`` make it visible."""
    probe = _bound(factory, _settings(), shared=shared)
    w = _worst(probe)
    s = _settings(llm_max_retries=0, llm_workspace_monthly_budget_usd=float(w * Decimal("1.5")))
    meter = _bound(factory, s, shared=shared)
    metrics = InMemoryMetrics()
    configure_metrics(metrics)
    try:
        with caplog.at_level("ERROR", logger="signalnest.llm"):
            # 200 000 input tokens at USD 3/Mtok alone is 0.6 > w (the r1 measurement)
            _service(s, lambda r: _reply(CLASSIFIED, tokens=(200_000, 1_000))).run(
                "classify_signal", {"content": "x"}, meter=meter
            )
            with factory() as db:
                meter.flush(db)
                db.commit()
    finally:
        configure_metrics(None)
    with factory() as db:
        period = _period(db)
        assert period.reserved_usd == w <= Decimal(str(s.llm_workspace_monthly_budget_usd))
        assert (period.settled_usd, period.bound_violations) == (w, 1)
        usage = db.scalar(select(LLMUsageEvent))
        assert usage.cost_usd == pytest.approx(0.615)  # the provider's report, on record
        assert usage.input_tokens == 200_000
    assert [r.message for r in caplog.records if r.levelname == "ERROR"] == [
        "llm.cost_bound_violated"
    ]
    stage = "settle" if shared else "same_session"
    assert metrics.counter_value(LLM_COST_BOUND_VIOLATIONS_TOTAL, outcome=stage) == 1
    # the next call still sees a ledger within budget and is judged on the bound alone
    again = _bound(factory, s, shared=shared)
    with pytest.raises(LLMBudgetExceededError):  # w + w > 1.5 w: refused by the ledger
        _service(s, lambda r: _reply(CLASSIFIED)).run(
            "classify_signal", {"content": "x"}, meter=again
        )


def test_the_mock_provider_reserves_nothing(factory):
    from app.llm.mock import MockLLMProvider

    s = _settings(llm_workspace_monthly_budget_usd=0.0)
    meter = _bound(factory, s, shared=True)
    LLMService(MockLLMProvider(), settings=s).run("classify_signal", {"content": "x"}, meter=meter)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(LLMBudgetReservation)) == 0


# --------------------------------------------------------------------------- #
# The run endpoint's gate and the worker's recovery
# --------------------------------------------------------------------------- #
def test_ensure_run_allowed_reads_the_periods_reserved_amount(factory):
    s = _settings(llm_workspace_monthly_budget_usd=0.05)
    with factory() as db:
        ensure_run_allowed(db, s, WS)  # nothing reserved
        assert _reserve(db, "k:0", Decimal("0.05"), Decimal("0.05"), now=datetime.now(UTC))
        db.commit()
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == 0  # no usage row yet
        with pytest.raises(LLMBudgetExhaustedError, match="USD 0.05 of 0.05"):
            ensure_run_allowed(db, s, WS)
        ensure_run_allowed(db, s, WS2)  # another workspace is unaffected


def test_the_worker_poll_sweeps_expired_reservations(factory):
    with factory() as db:
        assert _reserve(db, "leak:1:0", Decimal("0.01"), Decimal("1"), now=T0 - timedelta(days=1))
        db.commit()
    runner = JobRunner(settings=offline_settings(), session_factory=factory, clock=lambda: T0)
    metrics = InMemoryMetrics()
    configure_metrics(metrics)
    try:
        assert runner.poll_once(worker_id="w-llmb") is False  # nothing to claim ...
    finally:
        configure_metrics(None)
    with factory() as db:  # ... but the leak was charged its worst case
        row = _reservation(db, "leak:1:0")
        assert (row.state, row.settled_usd) == ("expired", Decimal("0.01"))
    assert metrics.counter_value(LLM_RESERVATIONS_RELEASED_TOTAL, outcome="expired") == 1


def test_a_failing_sweep_never_breaks_the_poll(factory, monkeypatch, caplog):
    def boom(_db, *, now):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(ledger, "sweep", boom)
    runner = JobRunner(settings=offline_settings(), session_factory=factory, clock=lambda: T0)
    assert runner.poll_once(worker_id="w-llmb") is False
    assert any(r.getMessage() == "worker.llm_reservation_sweep_failed" for r in caplog.records)


def test_the_terminal_failure_hook_releases_the_jobs_reservations(factory):
    with factory() as db:
        job = _enqueue(db, "final")
        assert _reserve(db, f"{job}:2:0", Decimal("0.03"), Decimal("1"), job_id=job)
        db.commit()
        job_handlers.settle_failed_scout_request(
            TerminalFailureContext(
                db=db,
                context=ExecutionContext.for_scout_request(
                    organization_id=ORG, workspace_id=WS, location_id=None
                ),
                payload={},  # no request to settle: the release still runs
                job_id=job,
                job_type="scout_request.execute",
                status=JobStatus.FAILED.value,
                error_code=JobErrorCode.NON_RETRYABLE,
                attempt=2,
                now=T0,
            )
        )
        db.commit()
        row = _reservation(db, f"{job}:2:0")
        assert (row.state, row.settled_usd) == ("released", Decimal("0.03"))


def test_the_handler_builds_a_per_attempt_meter_with_the_ledger_seam(factory, monkeypatch):
    """The durable handler passes the attempt ordinal and a session factory built from
    the attempt's bind, and releases the job's reservations when the attempt ends."""
    built: list[LLMMeter] = []
    real = job_handlers.LLMMeter

    def spy(settings, **kwargs):
        meter = real(settings, **kwargs)
        built.append(meter)
        return meter

    monkeypatch.setattr(job_handlers, "LLMMeter", spy)
    monkeypatch.setattr(job_handlers, "_run", lambda db, rid, ctx: {"scanned": 0})
    from app.jobs.registry import HandlerContext

    with factory() as db:
        job = _enqueue(db, "handler")
        assert _reserve(db, f"{job}:1:0", Decimal("0.02"), Decimal("1"), job_id=job)
        db.commit()
        result = job_handlers.execute_scout_request(
            HandlerContext(
                db=db,
                context=ExecutionContext.for_scout_request(
                    organization_id=ORG, workspace_id=WS, location_id=None
                ),
                payload={"scout_request_id": "req-x"},
                job_id=job,
                attempt=1,
                now=T0,
            )
        )
        db.commit()
        assert result == {"scanned": 0}
        (meter,) = built
        assert (meter.job_id, meter.attempt) == (job, 1)
        assert meter._session_factory is not None
        assert meter._session_factory().get_bind() is db.get_bind()
        assert _reservation(db, f"{job}:1:0").state == "released"


def test_the_durable_handler_is_the_only_production_caller_of_the_pipeline_run():
    """INFO-4 (review r1): ``pipeline._run``'s bare ``LLMMeter(get_settings())`` fallback
    has no session factory (the same-session path even on PostgreSQL). It must stay
    reachable only from tests: every production import or call of ``_run`` is the durable
    handler, which supplies a shared-ledger meter through ``metered_run``."""
    import re
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1]
    callers = set()
    for path in app_dir.rglob("*.py"):
        if "tests" in path.relative_to(app_dir).parts or path.name == "pipeline.py":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"from app\.jobs\.pipeline import[^\n]*\b_run\b|pipeline\._run\b", text):
            callers.add(path.relative_to(app_dir).as_posix())
    assert callers == {"jobs/handlers.py"}
    handler = (app_dir / "jobs" / "handlers.py").read_text(encoding="utf-8")
    assert "session_factory=sessionmaker(" in handler and "with metered_run(meter):" in handler
