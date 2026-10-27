"""D-LLM-1 option B: the per-workspace LLM budget as a hard ceiling on PostgreSQL.

The guarantee under proof (``app.llm.metering``): on PostgreSQL, the sum of worst-case
costs of all provider calls started by a workspace in a UTC calendar month never
exceeds the workspace budget as configured at reservation time, across any number of
processes. The required proofs, each on a schema produced by the real Alembic chain in a
database of the test's own, through **independent connections** and - where the proof
is about processes - **separate OS processes** (``multiprocessing`` ``spawn``, each with
an engine of its own, barrier-synchronised):

* P1 multi-process concurrency: eight processes make metered calls against one
  workspace until refused; the worst cases reserved never sum above the budget, the
  period's ``reserved_usd`` equals the settled sum exactly, every call made has its
  reservation and usage rows;
* P2 crash: a process reserves, (makes the call,) and dies before settling; the period
  shows the reservation; recovery (expiry sweep, or the job's terminal state) releases
  it at its worst case; the rest of the budget is still spendable;
* P3 retry: a job's attempt dies after reserving; the re-run attempt reserves again
  under a new key and the earlier reservation is released when it starts; the total
  never exceeds the budget;
* P4 duplicate settlement: the same key settled twice (two processes racing, then a
  repeat) is charged once with one usage row;
* P5 reservation recovery: the worker's sweep releases expired reservations and those
  of terminal jobs, leaves live ones alone, and is idempotent;
* P6 recovery racing a live call (review r1 MINOR-1): the sweep runs while a call is in
  flight past its expiry; with the job's lease live the reservation survives and the
  call settles normally; with the lease gone the reservation expires at its worst case
  and the late settlement still writes the usage row and counts the metric;
* P7 repeated and concurrent late settlements (after ``released`` and after
  ``expired``): four processes plus repeats settle one key after recovery charged it
  its worst case - exactly one usage row per attempt, no money moved, usage above the
  reservation charged at the bound and counted once; a settlement that loses its race
  to recovery between its read and its update still records the call; randomised
  settle-vs-recovery interleavings across processes keep the period's accounting
  consistent;
* tenant isolation, month rollover, and the run endpoint's gate: eight concurrent
  callers all pass the read-only gate, and the reservations behind them admit exactly
  the one call that still fits.

Everything is offline: the provider is a fixed-cost fake. Every connection carries a
``statement_timeout``/``lock_timeout``; every join is bounded. Gate:
:func:`require_postgres` - with ``TEST_POSTGRES_URL`` unset these tests FAIL in CI and
skip only on a developer machine.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.metrics import (
    LLM_SETTLEMENTS_AFTER_RELEASE_TOTAL,
    InMemoryMetrics,
    configure_metrics,
)
from app.jobs.models import Job
from app.jobs.status import JobStatus
from app.jobs.store import DurableJobStore
from app.jobs.worker import JobRunner
from app.llm import budget as ledger
from app.llm.metering import LLMBudgetExhaustedError, LLMMeter, ensure_run_allowed
from app.llm.models import LLMBudgetPeriod, LLMBudgetReservation, LLMUsageEvent
from app.llm.service import LLMService
from app.organizations.models import Organization, Workspace
from app.tests import _llm_reserve_child as child
from app.tests._auth2_support import pg_database, require_postgres
from app.tests._llm_offline import offline_settings

API_DIR = Path(__file__).resolve().parents[2]
ORG = "org-llmpg-001"
WS = "ws-llmpg-0001"
WS2 = "ws-llmpg-0002"
T0 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
JOIN_SECONDS = 90.0
DB_TIMEOUT_MS = 10_000
N_PROCESSES = 8


@pytest.fixture(autouse=True)
def _postgres_required() -> None:
    require_postgres()


def _migrate(url: str) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env.pop("SN_MIGRATION_MODE", None)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def pg_url() -> Iterator[str]:
    """A migrated database of this test's own, with the tenant scope seeded."""
    with pg_database("sn_llm") as url:
        _migrate(url)
        eng = child.make_engine(url)
        with Session(eng) as tenant:
            tenant.add(Organization(id=ORG, name="LLMPG", slug="llmpg"))
            tenant.add(Workspace(id=WS, organization_id=ORG, name="LLMPG WS", slug="llmpg-ws"))
            tenant.add(Workspace(id=WS2, organization_id=ORG, name="LLMPG WS2", slug="llmpg-ws2"))
            tenant.commit()
        eng.dispose()
        yield url


@pytest.fixture
def engine(pg_url: str) -> Iterator[Engine]:
    eng = child.make_engine(pg_url)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker:
    return child.make_factory(engine)


def _period(db: Session, ws: str = WS, now: datetime | None = None) -> LLMBudgetPeriod | None:
    return db.scalar(
        select(LLMBudgetPeriod).where(
            LLMBudgetPeriod.workspace_id == ws,
            LLMBudgetPeriod.period_start == ledger.period_start_for(now or datetime.now(UTC)),
        )
    )


def _reservations(db: Session) -> list[LLMBudgetReservation]:
    return list(db.scalars(select(LLMBudgetReservation).order_by(LLMBudgetReservation.created_at)))


def _enqueue(db: Session, key: str) -> str:
    """A job row for the ledger to point at; scheduled far ahead so no poll claims it."""
    job = DurableJobStore().enqueue(
        db,
        organization_id=ORG,
        workspace_id=WS,
        job_type="test.ok",
        payload={"k": key},
        payload_hash=key,
        scheduled_for=T0 + timedelta(days=3650),
        now=T0,
    )
    db.commit()
    return job.id


def _drain(queue, count: int) -> list[dict]:
    results = []
    for _ in range(count):
        results.append(queue.get(timeout=JOIN_SECONDS))
    return results


def _spawn(target, kwargs_list: list[dict]) -> list[mp.Process]:
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=target, kwargs=kw, daemon=True) for kw in kwargs_list]
    for p in procs:
        p.start()
    return procs


def _join(procs: list[mp.Process]) -> list[int | None]:
    for p in procs:
        p.join(JOIN_SECONDS)
    alive = [p.pid for p in procs if p.is_alive()]
    for p in procs:
        if p.is_alive():
            p.kill()
    assert not alive, f"child processes did not finish in time: {alive}"
    return [p.exitcode for p in procs]


def _run_worst_case() -> Decimal:
    """The worst case each run body reserves per call (the service's prompt measure)."""
    return child.worst_case()


def _crash_worst_case() -> Decimal:
    """The worst case the crash body reserves (a fixed prompt size)."""
    return child.worst_case(child.CRASH_CONTENT, tenant=False)


# --------------------------------------------------------------------------- #
# P1 - multi-process concurrency: the sum of worst cases never exceeds the budget
# --------------------------------------------------------------------------- #
def test_p1_eight_processes_never_reserve_more_than_the_budget_together(pg_url, factory):
    budget = 0.10
    w = _run_worst_case()
    fits = int(Decimal(str(budget)) // w)
    assert 2 <= fits < N_PROCESSES * 3, (w, fits)  # several calls fit, far fewer than wanted
    assert child.FIXED_COST < w  # the fake's actual cost is below the estimate, as usual
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(N_PROCESSES)
    results = ctx.Queue()
    procs = _spawn(
        child.run_until_refused,
        [
            dict(
                url=pg_url,
                barrier=barrier,
                budget=budget,
                organization_id=ORG,
                workspace_id=WS,
                job_id=None,
                attempt=0,
                results=results,
            )
            for _ in range(N_PROCESSES)
        ],
    )
    reports = _drain(results, N_PROCESSES)
    assert _join(procs) == [0] * N_PROCESSES
    assert len({r["pid"] for r in reports}) == N_PROCESSES  # eight distinct OS processes
    assert all(r["reason"] == "workspace_budget" for r in reports)  # every one was refused
    calls = sum(r["calls"] for r in reports)
    assert calls == fits  # all the budget admits, and not one call more, across processes
    with factory() as db:
        rows = _reservations(db)
        assert len(rows) == calls and {r.state for r in rows} == {"settled"}
        worst_sum = sum((r.reserved_usd for r in rows), Decimal(0))
        assert worst_sum == w * calls <= Decimal(str(budget))  # the guarantee
        period = _period(db)
        assert period.reserved_usd == worst_sum  # consumed on the worst-case basis, exactly
        assert period.settled_usd == child.FIXED_COST * calls  # charged the actual, exactly
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == calls
        assert sum(r["spent"] for r in reports) == pytest.approx(float(child.FIXED_COST * calls))
        # Worst case plus one more call would have crossed the budget.
        assert w * (calls + 1) > Decimal(str(budget))


# --------------------------------------------------------------------------- #
# P2 - crash between reservation and settlement
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("make_call", [True, False], ids=["after-the-call", "before-the-call"])
def test_p2_a_crashed_process_leaves_its_reservation_which_recovery_charges_at_worst(
    pg_url, factory, make_call
):
    budget = 0.10
    w = _crash_worst_case()
    ctx = mp.get_context("spawn")
    reserved = ctx.Event()
    procs = _spawn(
        child.reserve_then_die,
        [
            dict(
                url=pg_url,
                budget=budget,
                organization_id=ORG,
                workspace_id=WS,
                job_id=None,
                attempt=0,
                reserved_event=reserved,
                make_call=make_call,
            )
        ],
    )
    assert reserved.wait(JOIN_SECONDS)
    assert _join(procs) == [1]  # died
    with factory() as db:
        (row,) = _reservations(db)
        assert row.state == "reserved" and row.settled_usd is None
        assert _period(db).reserved_usd == w  # the reservation is the record of the call
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == 0
        # Not yet expired: the sweep leaves it (the process might be alive and slow).
        assert ledger.sweep(db, now=datetime.now(UTC)) == {"expired": 0, "terminal": 0}
        # Past its expiry the sweep charges it its worst case.
        later = row.expires_at + timedelta(seconds=1)
        assert ledger.sweep(db, now=later) == {"expired": 1, "terminal": 0}
        db.commit()
        db.refresh(row)
        assert (row.state, row.settled_usd) == ("expired", w)
        period = _period(db)
        assert (period.reserved_usd, period.settled_usd) == (w, w)
        # A later run can spend the rest of the budget - and no more.
        rest = Decimal(str(budget)) - w
        assert ledger.reserve(
            db,
            workspace_id=WS,
            organization_id=ORG,
            job_id=None,
            attempt_key="later:0:0",
            worst_usd=rest,
            budget_usd=Decimal(str(budget)),
            expires_at=later,
            now=datetime.now(UTC),
        )
        assert not ledger.reserve(
            db,
            workspace_id=WS,
            organization_id=ORG,
            job_id=None,
            attempt_key="later:0:1",
            worst_usd=Decimal("0.000001"),
            budget_usd=Decimal(str(budget)),
            expires_at=later,
            now=datetime.now(UTC),
        )
        db.commit()


def test_p2_a_crashed_attempt_of_a_job_is_released_when_the_job_ends(pg_url, factory):
    budget = 0.10
    w = _crash_worst_case()
    with factory() as db:
        job_id = _enqueue(db, "crash-job")
    ctx = mp.get_context("spawn")
    reserved = ctx.Event()
    procs = _spawn(
        child.reserve_then_die,
        [
            dict(
                url=pg_url,
                budget=budget,
                organization_id=ORG,
                workspace_id=WS,
                job_id=job_id,
                attempt=1,
                reserved_event=reserved,
                make_call=True,
            )
        ],
    )
    assert reserved.wait(JOIN_SECONDS)
    assert _join(procs) == [1]
    with factory() as db:
        (row,) = _reservations(db)
        assert (row.state, row.job_id, row.attempt_key) == ("reserved", job_id, f"{job_id}:1:0")
        # The job reaches a terminal state (dead-lettered by the store, say): the
        # worker's recovery slot releases the reservation on its next poll.
        db.execute(update(Job).where(Job.id == job_id).values(status=JobStatus.DEAD_LETTERED.value))
        db.commit()
    runner = JobRunner(settings=offline_settings(), session_factory=factory, clock=lambda: T0)
    assert runner.poll_once(worker_id="w-llmpg") is False
    with factory() as db:
        (row,) = _reservations(db)
        assert (row.state, row.settled_usd) == ("released", w)
        assert _period(db).settled_usd == w


# --------------------------------------------------------------------------- #
# P3 - retry after a lost attempt
# --------------------------------------------------------------------------- #
def test_p3_a_retried_attempt_reserves_anew_and_the_lost_one_is_released(pg_url, factory):
    w_lost, w_run = _crash_worst_case(), _run_worst_case()
    # The lost attempt's worst case plus the re-run's first call fit; a second call of
    # the re-run would not.
    budget = float(w_lost + w_run + child.FIXED_COST)
    assert w_lost + 2 * w_run > Decimal(str(budget))
    with factory() as db:
        job_id = _enqueue(db, "retry-job")
    ctx = mp.get_context("spawn")
    reserved = ctx.Event()
    procs = _spawn(
        child.reserve_then_die,
        [
            dict(
                url=pg_url,
                budget=budget,
                organization_id=ORG,
                workspace_id=WS,
                job_id=job_id,
                attempt=1,
                reserved_event=reserved,
                make_call=True,
            )
        ],
    )
    assert reserved.wait(JOIN_SECONDS)
    assert _join(procs) == [1]
    # Attempt 2 of the same job, in this process (its lease was recovered).
    results = ctx.Queue()
    barrier = ctx.Barrier(1)
    procs = _spawn(
        child.run_until_refused,
        [
            dict(
                url=pg_url,
                barrier=barrier,
                budget=budget,
                organization_id=ORG,
                workspace_id=WS,
                job_id=job_id,
                attempt=2,
                results=results,
            )
        ],
    )
    (report,) = _drain(results, 1)
    assert _join(procs) == [0]
    assert report["calls"] == 1 and report["reason"] == "workspace_budget"
    with factory() as db:
        rows = {r.attempt_key: r for r in _reservations(db)}
        assert set(rows) == {f"{job_id}:1:0", f"{job_id}:2:0"}
        lost, retried = rows[f"{job_id}:1:0"], rows[f"{job_id}:2:0"]
        assert (lost.state, lost.settled_usd) == ("released", w_lost)  # released at rebind
        assert (retried.state, retried.settled_usd) == ("settled", child.FIXED_COST)
        worst_sum = sum((r.reserved_usd for r in rows.values()), Decimal(0))
        assert worst_sum == w_lost + w_run <= Decimal(str(budget))
        period = _period(db)
        assert (period.reserved_usd, period.settled_usd) == (worst_sum, w_lost + child.FIXED_COST)
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == 1


# --------------------------------------------------------------------------- #
# P4 - duplicate settlement
# --------------------------------------------------------------------------- #
def test_p4_the_same_attempt_key_settled_by_racing_processes_is_charged_once(pg_url, factory):
    budget = 1.0
    w = Decimal("0.05")
    with factory() as db:
        assert ledger.reserve(
            db,
            workspace_id=WS,
            organization_id=ORG,
            job_id=None,
            attempt_key="dup:1:0",
            worst_usd=w,
            budget_usd=Decimal(str(budget)),
            expires_at=T0 + timedelta(hours=1),
            now=T0,
        )
        db.commit()
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(4)
    results = ctx.Queue()
    procs = _spawn(
        child.settle_once,
        [
            dict(
                url=pg_url,
                attempt_key="dup:1:0",
                settled_usd=str(child.FIXED_COST),
                barrier=barrier,
                results=results,
            )
            for _ in range(4)
        ],
    )
    reports = _drain(results, 4)
    assert _join(procs) == [0] * 4
    assert sorted(r["settled"] for r in reports) == [False, False, False, True]
    with factory() as db:
        # And the after-rollback-style repeat, later, is a no-op too.
        assert not ledger.settle(
            db,
            attempt_key="dup:1:0",
            settled_usd=child.FIXED_COST,
            now=datetime.now(UTC),
            usage=None,
        )
        db.commit()
        period = _period(db)
        assert (period.reserved_usd, period.settled_usd) == (w, child.FIXED_COST)
        (row,) = _reservations(db)
        assert (row.state, row.settled_usd) == ("settled", child.FIXED_COST)
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == 1


# --------------------------------------------------------------------------- #
# P5 - reservation recovery by the worker's sweep
# --------------------------------------------------------------------------- #
def test_p5_the_sweep_releases_expired_and_terminal_reservations_and_nothing_else(pg_url, factory):
    budget = Decimal("1")
    w = Decimal("0.01")
    now = datetime.now(UTC)
    with factory() as db:
        live = _enqueue(db, "live")
        done = _enqueue(db, "done")
        for key, when, job in (
            ("old:1:0", now - timedelta(days=1), live),
            ("fresh:1:0", now, live),
            ("done:1:0", now, done),
            ("nojob:0:0", now, None),
        ):
            assert ledger.reserve(
                db,
                workspace_id=WS,
                organization_id=ORG,
                job_id=job,
                attempt_key=key,
                worst_usd=w,
                budget_usd=budget,
                expires_at=when + timedelta(seconds=90),
                now=when,
            )
        db.execute(update(Job).where(Job.id == done).values(status=JobStatus.SUCCEEDED.value))
        db.commit()
    runner = JobRunner(settings=offline_settings(), session_factory=factory, clock=lambda: now)
    assert runner.poll_once(worker_id="w-llmpg") is False
    with factory() as db:
        states = {r.attempt_key: r.state for r in _reservations(db)}
        assert states == {
            "old:1:0": "expired",
            "fresh:1:0": "reserved",
            "done:1:0": "released",
            "nojob:0:0": "reserved",
        }
        period = _period(db, now=now)
        assert (period.reserved_usd, period.settled_usd) == (4 * w, 2 * w)
        assert ledger.sweep(db, now=now) == {"expired": 0, "terminal": 0}  # idempotent


# --------------------------------------------------------------------------- #
# P6 - recovery racing a live call
# --------------------------------------------------------------------------- #
class _BlockingProvider(child.FixedCostProvider):
    """A fixed-cost fake whose call waits until the test lets it return."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.proceed = threading.Event()

    def generate(self, request, prompt_name, prompt_version):
        self.entered.set()
        assert self.proceed.wait(JOIN_SECONDS)
        return super().generate(request, prompt_name, prompt_version)


@pytest.mark.parametrize("lease_live", [True, False], ids=["live-lease", "lease-gone"])
def test_p6_the_sweep_defers_to_a_live_lease_and_a_late_settlement_still_records_the_call(
    pg_url, factory, lease_live
):
    settings = child.settings_for(1.0)
    now = datetime.now(UTC)
    with factory() as db:
        job_id = _enqueue(db, "inflight")
        db.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=JobStatus.RUNNING.value,
                lease_expires_at=now + timedelta(days=1)
                if lease_live
                else now - timedelta(seconds=1),
            )
        )
        db.commit()
    provider = _BlockingProvider()
    service = LLMService(provider, sleep=lambda _s: None, settings=settings)
    meter = LLMMeter(settings, job_id=job_id, attempt=1, session_factory=factory)
    with factory() as run_session:
        meter.bind(run_session, organization_id=ORG, workspace_id=WS, scout_request_id=None)
    errors: list[BaseException] = []

    def call() -> None:
        try:
            service.run("classify_signal", child.RUN_INPUTS, meter=meter)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            errors.append(exc)

    metrics = InMemoryMetrics()
    configure_metrics(metrics)
    try:
        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        assert provider.entered.wait(
            JOIN_SECONDS
        )  # the reservation is committed, the call is in flight
        with factory() as db:
            (row,) = _reservations(db)
            assert row.state == "reserved"
            w = row.reserved_usd
            # The sweep runs well past the reservation's expiry while the call is still in flight.
            swept = ledger.sweep(db, now=row.expires_at + timedelta(seconds=1))
            db.commit()
            assert swept == {"expired": 0 if lease_live else 1, "terminal": 0}
        provider.proceed.set()
        thread.join(JOIN_SECONDS)
        assert not thread.is_alive() and not errors, errors
    finally:
        configure_metrics(None)
    with factory() as db:
        (row,) = _reservations(db)
        usage = db.scalar(select(LLMUsageEvent))
        period = _period(db)
        assert (usage.job_id, usage.cost_usd, usage.outcome) == (
            job_id,
            0.0105,
            "ok",
        )  # always recorded
        if lease_live:
            assert (row.state, row.settled_usd) == ("settled", child.FIXED_COST)
            assert (period.reserved_usd, period.settled_usd) == (w, child.FIXED_COST)
            assert (
                metrics.counter_value(LLM_SETTLEMENTS_AFTER_RELEASE_TOTAL, outcome="expired") == 0
            )
        else:
            assert (row.state, row.settled_usd) == ("expired", w)  # the worst case stands
            assert (period.reserved_usd, period.settled_usd) == (w, w)
            assert (
                metrics.counter_value(LLM_SETTLEMENTS_AFTER_RELEASE_TOTAL, outcome="expired") == 1
            )
    assert meter.spent == pytest.approx(float(child.FIXED_COST))


# --------------------------------------------------------------------------- #
# Tenant isolation, rollover, and the run endpoint's gate under a race
# --------------------------------------------------------------------------- #
def test_two_workspaces_periods_are_independent_on_postgresql(pg_url, factory):
    budget = Decimal("0.05")
    with factory() as db:
        kw = dict(
            organization_id=ORG,
            job_id=None,
            budget_usd=budget,
            now=T0,
            expires_at=T0 + timedelta(hours=1),
            worst_usd=Decimal("0.03"),
        )
        assert ledger.reserve(db, workspace_id=WS, attempt_key="a:0", **kw)
        assert not ledger.reserve(db, workspace_id=WS, attempt_key="a:1", **kw)
        assert ledger.reserve(db, workspace_id=WS2, attempt_key="b:0", **kw)
        db.commit()
        assert _period(db, WS, T0).reserved_usd == Decimal("0.03")
        assert _period(db, WS2, T0).reserved_usd == Decimal("0.03")


def test_the_month_rolls_over_on_postgresql(pg_url, factory):
    budget = Decimal("0.05")
    last = datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)
    first = datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)
    with factory() as db:
        kw = dict(
            workspace_id=WS, organization_id=ORG, job_id=None, budget_usd=budget, worst_usd=budget
        )
        assert ledger.reserve(db, attempt_key="oct:0", now=last, expires_at=last, **kw)
        assert not ledger.reserve(db, attempt_key="oct:1", now=last, expires_at=last, **kw)
        assert ledger.reserve(db, attempt_key="nov:0", now=first, expires_at=first, **kw)
        db.commit()
        assert ledger.reserved_month_to_date_usd(db, WS, last) == budget
        assert ledger.reserved_month_to_date_usd(db, WS, first) == budget


def test_the_run_gate_is_a_read_and_the_reservation_is_the_enforcement(pg_url, factory):
    """Eight concurrent callers on independent connections, the month at budget - 1.5 w:
    the read-only API gate admits every one (it only refuses at or over budget), and
    the reservations behind them admit exactly the one call that still fits."""
    settings = child.settings_for(0.10)
    budget = Decimal("0.10")
    w = Decimal("0.02")
    with factory() as db:
        assert ledger.reserve(
            db,
            workspace_id=WS,
            organization_id=ORG,
            job_id=None,
            attempt_key="prior:0:0",
            worst_usd=budget - w - w / 2,
            budget_usd=budget,
            expires_at=T0,
            now=datetime.now(UTC),
        )
        db.commit()
    barrier = threading.Barrier(N_PROCESSES)
    gate: list[str] = []
    reserved: list[bool] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def caller(i: int) -> None:
        try:
            db = factory()
            try:
                barrier.wait(JOIN_SECONDS)
                try:
                    ensure_run_allowed(db, settings, WS)
                    outcome = "allowed"
                except LLMBudgetExhaustedError:
                    outcome = "refused"
                ok = ledger.reserve(
                    db,
                    workspace_id=WS,
                    organization_id=ORG,
                    job_id=None,
                    attempt_key=f"race:{i}",
                    worst_usd=w,
                    budget_usd=budget,
                    expires_at=T0,
                    now=datetime.now(UTC),
                )
                db.commit()
            finally:
                db.close()
            with lock:
                gate.append(outcome)
                reserved.append(ok)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=caller, args=(i,), daemon=True) for i in range(N_PROCESSES)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN_SECONDS)
    assert not any(t.is_alive() for t in threads) and not errors, errors
    assert gate == ["allowed"] * N_PROCESSES  # the gate alone would admit all eight
    assert sum(reserved) == 1  # the ledger admits exactly the one that fits
    with factory() as db:
        assert _period(db).reserved_usd == budget - w / 2
        ensure_run_allowed(db, settings, WS)  # still under budget: a new run may start ...
        assert not ledger.reserve(  # ... but no call of it can be made
            db,
            workspace_id=WS,
            organization_id=ORG,
            job_id=None,
            attempt_key="one-more",
            worst_usd=w,
            budget_usd=budget,
            expires_at=T0,
            now=datetime.now(UTC),
        )


# --------------------------------------------------------------------------- #
# P7 - repeated and concurrent LATE settlements, after RELEASED and after EXPIRED
# --------------------------------------------------------------------------- #
def _usage(cost: Decimal, outcome: str = "ok") -> dict:
    return {
        "scout_request_id": None,
        "task": "classify_signal",
        "provider": "anthropic",
        "model": child.MODEL,
        "input_tokens": child.FIXED_USAGE[0],
        "output_tokens": child.FIXED_USAGE[1],
        "cost_usd": float(cost),
        "outcome": outcome,
    }


def _reserve(db: Session, key: str, w: Decimal, *, job_id: str | None, expired: bool) -> None:
    """Reserve ``key``; ``expired`` makes it already past ``expires_at`` (sweepable)."""
    now = datetime.now(UTC)
    assert ledger.reserve(
        db,
        workspace_id=WS,
        organization_id=ORG,
        job_id=job_id,
        attempt_key=key,
        worst_usd=w,
        budget_usd=Decimal("1"),
        expires_at=now - timedelta(minutes=5) if expired else now + timedelta(hours=1),
        now=now,
    )
    db.commit()


def _assert_period_invariants(db: Session, budget: Decimal) -> None:
    """Accounting invariants that must hold after EVERY interleaving."""
    period = _period(db)
    rows = _reservations(db)
    if period is None:
        assert not rows
        return
    assert period.reserved_usd == sum((r.reserved_usd for r in rows), Decimal(0))
    charged = sum((r.settled_usd or Decimal(0) for r in rows if r.state != "reserved"), Decimal(0))
    assert period.settled_usd == charged  # every charge, once
    assert period.reserved_usd <= budget  # the budget is never exceeded
    assert all((r.settled_usd or 0) <= r.reserved_usd for r in rows)  # actual <= reserved
    usage = db.scalar(select(func.count()).select_from(LLMUsageEvent))
    assert usage <= len(rows)  # never more than one usage row per attempt


@pytest.mark.parametrize("after", ["released", "expired"])
def test_p7_racing_late_settlements_record_the_call_exactly_once(pg_url, factory, after):
    """Four processes settle the same key at once after recovery charged it its worst
    case, then the same process settles twice more: one usage row, no money moved."""
    w, budget = Decimal("0.05"), Decimal("1")
    with factory() as db:
        job_id = _enqueue(db, f"late-{after}") if after == "released" else None
        _reserve(db, f"late:{after}:0", w, job_id=job_id, expired=after == "expired")
        if after == "released":
            assert ledger.release_for_job(db, job_id=job_id, now=datetime.now(UTC), outcome="t")
        else:
            assert ledger.release_expired(db, now=datetime.now(UTC)) == 1
        db.commit()
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(4)
    results = ctx.Queue()
    procs = _spawn(
        child.settle_once,
        [
            dict(
                url=pg_url,
                attempt_key=f"late:{after}:0",
                settled_usd=str(child.FIXED_COST),
                barrier=barrier,
                results=results,
            )
            for _ in range(4)
        ],
    )
    reports = _drain(results, 4)
    assert _join(procs) == [0] * 4
    assert [r["settled"] for r in reports] == [False] * 4  # no money moves late
    with factory() as db:
        for _ in range(2):  # and repeated settlements from one process, later
            assert not ledger.settle(
                db,
                attempt_key=f"late:{after}:0",
                settled_usd=child.FIXED_COST,
                now=datetime.now(UTC),
                usage=_usage(child.FIXED_COST),
            )
            db.commit()
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == 1
        (row,) = _reservations(db)
        assert (row.state, row.settled_usd) == (after, w)  # the worst case stands
        period = _period(db)
        assert (period.reserved_usd, period.settled_usd, period.bound_violations) == (w, w, 0)
        _assert_period_invariants(db, budget)


def test_p7_a_settlement_that_loses_the_race_to_recovery_still_records_the_call(pg_url, engine):
    """Deterministic interleaving: the settlement has read the row as ``reserved`` when
    recovery commits its release (before the settlement's period lock); the settlement
    re-reads the row under the lock and takes the late path. Its usage row must still be
    written, once."""
    from sqlalchemy import event

    factory = child.make_factory(engine)
    w = Decimal("0.05")
    with factory() as db:
        job_id = _enqueue(db, "lost-race")
        _reserve(db, "race:1:0", w, job_id=job_id, expired=False)
    fired: list[int] = []

    @event.listens_for(engine, "before_cursor_execute")
    def release_first(conn, cursor, statement, parameters, context, executemany):
        # Recovery commits between the settlement's first read and its period lock (the
        # lock order makes the later window unreachable: the period lock serialises both).
        if "FOR UPDATE" in statement and not fired:
            fired.append(1)
            other = factory()
            try:
                ledger.release_for_job(other, job_id=job_id, now=datetime.now(UTC), outcome="t")
                other.commit()
            finally:
                other.close()

    try:
        with factory() as db:
            assert not ledger.settle(
                db,
                attempt_key="race:1:0",
                settled_usd=child.FIXED_COST,
                now=datetime.now(UTC),
                usage=_usage(child.FIXED_COST),
            )
            db.commit()
    finally:
        event.remove(engine, "before_cursor_execute", release_first)
    assert fired == [1]
    with factory() as db:
        (row,) = _reservations(db)
        assert (row.state, row.settled_usd) == ("released", w)
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == 1  # not dropped
        _assert_period_invariants(db, Decimal("1"))
        assert not ledger.settle(  # and a repeat adds nothing
            db,
            attempt_key="race:1:0",
            settled_usd=child.FIXED_COST,
            now=datetime.now(UTC),
            usage=_usage(child.FIXED_COST),
        )
        db.commit()
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == 1


@pytest.mark.parametrize("after", ["released", "expired"])
def test_p7_usage_above_the_reservation_after_recovery_keeps_the_bound_as_the_charge(
    pg_url, factory, after
):
    w, budget = Decimal("0.05"), Decimal("1")
    with factory() as db:
        job_id = _enqueue(db, f"over-{after}") if after == "released" else None
        _reserve(db, f"over:{after}:0", w, job_id=job_id, expired=after == "expired")
        if after == "released":
            ledger.release_for_job(db, job_id=job_id, now=datetime.now(UTC), outcome="t")
        else:
            ledger.release_expired(db, now=datetime.now(UTC))
        db.commit()
        for _ in range(3):
            ledger.settle(
                db,
                attempt_key=f"over:{after}:0",
                settled_usd=Decimal("0.9"),
                now=datetime.now(UTC),
                usage=_usage(Decimal("0.9")),
            )
            db.commit()
        period = _period(db)
        assert (period.reserved_usd, period.settled_usd) == (w, w)
        assert period.bound_violations == 1  # counted once, not once per repeat
        usage = db.scalars(select(LLMUsageEvent)).all()
        assert [u.cost_usd for u in usage] == [0.9]  # the report stays on record
        _assert_period_invariants(db, budget)


def test_p7_settlement_races_recovery_across_processes_and_the_invariants_hold(pg_url, factory):
    """Per attempt: one process settles (with a usage row) while another recovers the
    same key and two more settle it late. Whatever the interleaving, there is exactly one
    usage row per attempt and the period's accounting is consistent."""
    attempts = 12
    w, budget = Decimal("0.02"), Decimal("1")
    keys = []
    with factory() as db:
        for i in range(attempts):
            job_id = _enqueue(db, f"stress-{i}")
            key = f"stress:{i}:0"
            _reserve(db, key, w, job_id=job_id, expired=False)
            keys.append((key, job_id))
    ctx = mp.get_context("spawn")
    for key, job_id in keys:
        barrier = ctx.Barrier(4)
        results = ctx.Queue()
        procs = _spawn(
            child.settle_once,
            [
                dict(
                    url=pg_url,
                    attempt_key=key,
                    settled_usd=str(child.FIXED_COST),
                    barrier=barrier,
                    results=results,
                )
                for _ in range(3)
            ],
        ) + _spawn(
            child.recover_once,
            [dict(url=pg_url, job_id=job_id, mode="job", barrier=barrier, results=results)],
        )
        _drain(results, 4)
        assert _join(procs) == [0] * 4
    with factory() as db:
        rows = _reservations(db)
        assert len(rows) == attempts and {r.state for r in rows} <= {"settled", "released"}
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == attempts
        _assert_period_invariants(db, budget)


# --------------------------------------------------------------------------- #
# P8 - one lock order: a settlement never deadlocks with a multi-row recovery release
# --------------------------------------------------------------------------- #
def test_p8_a_settlement_and_a_multi_row_release_do_not_deadlock(pg_url):
    """Review r3 MAJOR-1, deterministic. Two overdue reservations of one workspace. The
    settlement of the second takes its first lock and pauses; the sweep then runs, taking
    the first reservation, the period and (on the old order) waiting for the second. When
    the settlement resumes it needs the period: the old order is a lock cycle and
    PostgreSQL aborts the settlement, whose usage row is then lost. With one lock order
    (the period first) the sweep waits for the settlement and both complete."""
    import time

    from sqlalchemy import event
    from sqlalchemy.exc import DBAPIError

    e_settle, e_sweep = child.make_engine(pg_url), child.make_engine(pg_url)
    f_settle, f_sweep = child.make_factory(e_settle), child.make_factory(e_sweep)
    w = Decimal("0.020000")
    now = datetime.now(UTC)
    with f_settle() as db:
        for key, age in (("dl:a:0", 20), ("dl:b:0", 10)):
            assert ledger.reserve(
                db,
                workspace_id=WS,
                organization_id=ORG,
                job_id=None,
                attempt_key=key,
                worst_usd=w,
                budget_usd=Decimal(1),
                expires_at=now - timedelta(minutes=age),
                now=now,
            )
        db.commit()
    first_lock = threading.Event()

    @event.listens_for(e_settle, "after_cursor_execute")
    def pause_after_the_first_lock(conn, cursor, statement, params, context, many):
        text = statement.lstrip()
        if not first_lock.is_set() and (
            text.startswith("UPDATE llm_budget_reservations") or "FOR UPDATE" in text
        ):
            first_lock.set()
            time.sleep(2.5)  # beyond deadlock_timeout: the sweep is queued before we resume

    outcome: dict[str, object] = {}

    def settle() -> None:
        session = f_settle()
        try:
            outcome["settle"] = ledger.settle(
                session,
                attempt_key="dl:b:0",
                settled_usd=Decimal("0.01"),
                now=datetime.now(UTC),
                usage=child.usage_for("dl:b:0", Decimal("0.01")),
            )
            session.commit()
        except DBAPIError as exc:
            session.rollback()
            outcome["settle"] = f"ERROR {type(exc.orig).__name__}"
        finally:
            session.close()

    def sweep() -> None:
        assert first_lock.wait(20)
        session = f_sweep()
        try:
            outcome["sweep"] = ledger.release_expired(session, now=datetime.now(UTC))
            session.commit()
        except DBAPIError as exc:
            session.rollback()
            outcome["sweep"] = f"ERROR {type(exc.orig).__name__}"
        finally:
            session.close()

    threads = [threading.Thread(target=settle), threading.Thread(target=sweep)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    event.remove(e_settle, "after_cursor_execute", pause_after_the_first_lock)
    with f_settle() as db:
        rows = {r.attempt_key: r.state for r in _reservations(db)}
        usage = db.scalar(select(func.count()).select_from(LLMUsageEvent))
    e_settle.dispose()
    e_sweep.dispose()
    assert outcome["settle"] is True, (outcome, rows, usage)  # not a deadlock victim
    assert outcome["sweep"] == 1, outcome  # the sweep released the other overdue row
    assert rows == {"dl:a:0": "expired", "dl:b:0": "settled"}
    assert usage == 1  # the call's usage record is written


@pytest.mark.parametrize("run", [1, 2, 3])
def test_p8_six_process_chaos_loses_no_usage_row_and_no_operation(pg_url, factory, run):
    """Six processes run 80 random ledger operations each against 30 shared reservations
    (settle, over-bound settle, release by job, expiry, sweep, reserve at a tight budget),
    then four processes late-settle every key twice. No operation may fail (no deadlock),
    every attempt has exactly one usage row, and the period's accounting is consistent."""
    w, budget = Decimal("0.020000"), Decimal("1.000000")
    keys = []
    with factory() as db:
        for i in range(30):
            job = _enqueue(db, f"chaos-{run}-{i}")
            key = f"chaos:{i}:0"
            now = datetime.now(UTC)
            assert ledger.reserve(
                db,
                workspace_id=WS,
                organization_id=ORG,
                job_id=job,
                attempt_key=key,
                worst_usd=w,
                budget_usd=budget,
                expires_at=now + (timedelta(hours=-1) if i % 3 == 0 else timedelta(hours=1)),
                now=now,
            )
            db.commit()
            keys.append((key, job, str(w)))
    ctx = mp.get_context("spawn")
    results = ctx.Queue()
    start = ctx.Barrier(6)
    procs = _spawn(
        child.chaos,
        [
            dict(
                url=pg_url,
                seed=seed + 100 * run,
                keys=keys,
                budget=str(budget),
                w=str(w),
                rounds=80,
                barrier=start,
                results=results,
            )
            for seed in range(6)
        ],
    )
    reports = _drain(results, 6)
    assert _join(procs) == [0] * 6
    errors = {op: n for r in reports for op, n in r["ops"].items() if op.startswith("error:")}
    assert errors == {}, errors
    barrier = ctx.Barrier(4)
    results = ctx.Queue()
    procs = _spawn(
        child.late_settle,
        [
            dict(
                url=pg_url,
                keys=[k for k, _j, _w in keys],
                repeats=2,
                cost="0.010000",
                barrier=barrier,
                results=results,
            )
            for _ in range(4)
        ],
    )
    _drain(results, 4)
    assert _join(procs) == [0] * 4
    with factory() as db:
        period, rows = _period(db), _reservations(db)
        per_key: dict[str, int] = {}
        for u in db.scalars(select(LLMUsageEvent)):
            per_key[u.task] = per_key.get(u.task, 0) + 1
        assert all(per_key.get(k, 0) == 1 for k, _j, _w in keys), per_key
        assert all(v == 1 for v in per_key.values()), per_key
        assert period.reserved_usd == sum((r.reserved_usd for r in rows), Decimal(0))
        assert period.reserved_usd <= budget
        assert period.settled_usd == sum(
            (r.settled_usd or Decimal(0) for r in rows if r.state != "reserved"), Decimal(0)
        )
        assert all((r.settled_usd or 0) <= r.reserved_usd for r in rows)
        assert all(
            (r.usage_recorded_at is not None) == (per_key.get(r.attempt_key, 0) == 1) for r in rows
        )
