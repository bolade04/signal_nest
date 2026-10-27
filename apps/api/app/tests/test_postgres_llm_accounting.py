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

from app.jobs.models import Job
from app.jobs.status import JobStatus
from app.jobs.store import DurableJobStore
from app.jobs.worker import JobRunner
from app.llm import budget as ledger
from app.llm.metering import LLMBudgetExhaustedError, ensure_run_allowed
from app.llm.models import LLMBudgetPeriod, LLMBudgetReservation, LLMUsageEvent
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
    return child.worst_case(child.service_prompt_chars())


def _crash_worst_case() -> Decimal:
    """The worst case the crash body reserves (a fixed prompt size)."""
    return child.worst_case(child.CRASH_PROMPT_CHARS)


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
