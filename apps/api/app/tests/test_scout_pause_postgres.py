"""The Scout pause promise on PostgreSQL: separate sessions, real row locks (D-25 half 1).

``test_scout_pause_promise.py`` pins the clauses on SQLite, where a second writer cannot
run while the worker's attempt holds the database. This module runs the real worker
(``JobRunner.poll_once`` -> ``execute_scout_request`` -> ``pipeline._run``) and the real
pause route body (``routes.pause_scout_request``, committed like ``get_db`` does) in
**separate threads, each with its own connection and server backend**, on a schema the
real Alembic chain produced, and records what PostgreSQL actually does:

* a pause committed before the worker's claim, or between the worker's load and its
  claim, wins: the claim (a compare-and-set) matches no row, the job is skipped before
  any fetch or model call and the #269 ledger gains nothing;
* a claim issued while a pause is uncommitted WAITS for the pause, then re-checks the
  row and skips;
* a pause issued while a run is in flight WAITS for the run's transaction (the claim's
  row lock on ``scout_requests``): it is serialised after the run's commit, not an
  interruption. The run finishes with ``outcome=completed``; the request then ends
  ``paused``. On the #268 refusal path and the failure path the pause lands as soon as
  the attempt rolls back, and neither the refusal's ``failed`` write nor the
  terminal-failure hook overwrites it;
* work the in-flight run dispatches after the pause was issued still happens: its
  fetch, its model calls, and (with a priced fake provider on the shared ledger) every
  call's reservation and charge, each committed in a transaction of its own. A pause
  does not undo them, and neither does the run's own rollback.

Everything is offline: the sources are fakes of ``pipeline.fetch_signals`` serving the
demo fixture library, the model is the deterministic mock (or the mock's output priced
as an Anthropic model for the ledger proofs; nothing is sent). Every connection carries
``statement_timeout`` / ``lock_timeout`` and every join is bounded. Measured timings are
printed (``PAUSE-PG-MEASURE``) for the evidence record; assertions never depend on a
timing threshold other than "still waiting while the holder is held".

Gate: :func:`require_postgres` -- with ``TEST_POSTGRES_URL`` unset these tests FAIL in CI
(``CI=true``) and skip only on a developer machine.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401 - registers every ORM model and job handler
from app.audit.models import AuditLog
from app.brands.models import Brand
from app.connectors.coverage import Coverage, CoverageReason
from app.core.config import Settings, get_settings
from app.core.enums import ScoutRequestStatus
from app.infra.vector import SIGNAL_VOLUME_ABOVE_BOUND
from app.jobs import pipeline
from app.jobs.models import Job
from app.jobs.service import enqueue_scout_request
from app.jobs.status import JobErrorCode, JobStatus
from app.jobs.store import DurableJobStore
from app.jobs.worker import JobRunner
from app.llm.base import LLMUsage
from app.llm.metering import cost_usd
from app.llm.mock import MockLLMProvider
from app.llm.service import LLMService
from app.locations.models import BusinessLocation
from app.opportunities.models import Opportunity
from app.organizations.models import Organization, User, Workspace
from app.scouting_requests.connectors import SignalBatch, fetch_with_reports
from app.scouting_requests.fixtures import fixtures_for_market
from app.scouting_requests.models import ScoutRequest
from app.scouting_requests.routes import pause_scout_request
from app.signals.models import RawSignal
from app.tests._auth2_support import pg_database, require_postgres
from app.tests._llm_offline import offline_llm, offline_settings
from app.tests._llm_reserve_child import KEY, MODEL

API_DIR = Path(__file__).resolve().parents[2]
MARKET = "Dallas, TX"
PAUSED = ScoutRequestStatus.PAUSED.value
#: Bound on every join and every wait for another session (seconds).
JOIN_SECONDS = 60.0
#: How long a holder is kept holding once the waiter is seen waiting (seconds).
HOLD_SECONDS = 1.0
DB_TIMEOUT_MS = 30_000
#: The priced fake's fixed usage per call (100 input + 50 output tokens).
FAKE_USAGE = (100, 50)


@pytest.fixture(autouse=True)
def _postgres_required() -> None:
    require_postgres()


# --------------------------------------------------------------------------- #
# Databases: one Alembic-migrated template per module, one clone per test
# --------------------------------------------------------------------------- #
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


@pytest.fixture(scope="module")
def template_url() -> Iterator[str]:
    require_postgres()
    with pg_database("sn_pausetpl") as url:
        _migrate(url)
        yield url


@contextmanager
def _clone(template: str) -> Iterator[str]:
    """A database of this test's own, copied from the migrated template, dropped after."""
    base = make_url(os.environ["TEST_POSTGRES_URL"])
    name = f"sn_pause_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{make_url(template).database}"'))
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _engine(url: str, **kwargs) -> Engine:
    engine = create_engine(url, future=True, pool_size=8, max_overflow=8, **kwargs)

    @event.listens_for(engine, "connect")
    def _timeouts(dbapi_conn, _record):  # pragma: no cover - trivial hook
        cur = dbapi_conn.cursor()
        cur.execute(f"SET statement_timeout = {DB_TIMEOUT_MS}")
        cur.execute(f"SET lock_timeout = {DB_TIMEOUT_MS}")
        cur.close()
        if not dbapi_conn.autocommit:
            dbapi_conn.commit()

    return engine


class World(SimpleNamespace):
    """One test's database, sessions, tenant, queued job and instrumentation."""


@pytest.fixture
def world(template_url, monkeypatch) -> Iterator[World]:
    with _clone(template_url) as url:
        engine = _engine(url)
        observer = _engine(url, isolation_level="AUTOCOMMIT")
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        w = World(
            engine=engine,
            observer=observer,
            factory=factory,
            worker_ident=None,
            worker_pid=None,
            worker_commits=[],
            marks={},
        )
        _seed(w)

        @event.listens_for(factory, "after_commit")
        def _after_commit(_session):  # pragma: no cover - trivial hook
            if threading.get_ident() == w.worker_ident:
                w.worker_commits.append(time.monotonic())

        real_rss_enabled_for = pipeline._rss_enabled_for

        def rss_enabled_for(db, *, organization_id, workspace_id):
            # Called by ``_run`` right before it selects the run's sources: after the
            # claim, inside the run's transaction. Record that backend.
            w.worker_pid = db.execute(text("select pg_backend_pid()")).scalar_one()
            return real_rss_enabled_for(
                db, organization_id=organization_id, workspace_id=workspace_id
            )

        monkeypatch.setattr(pipeline, "_rss_enabled_for", rss_enabled_for)
        try:
            yield w
        finally:
            engine.dispose()
            observer.dispose()


def _seed(w: World) -> None:
    slug = f"pause-{uuid.uuid4().hex[:8]}"
    with w.factory() as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        s.flush()
        user = User(email=f"{slug}@example.test", full_name=slug, hashed_password="x")
        ws = Workspace(organization_id=org.id, name="Main", slug="main")
        s.add_all([user, ws])
        s.flush()
        brand = Brand(organization_id=org.id, workspace_id=ws.id, name=slug, industry="coffee")
        s.add(brand)
        s.flush()
        loc = BusinessLocation(
            organization_id=org.id,
            workspace_id=ws.id,
            brand_id=brand.id,
            name=f"{slug} Dallas",
            city="Dallas",
            state_province="TX",
            country="US",
            is_active=True,
        )
        s.add(loc)
        s.flush()
        req = ScoutRequest(
            organization_id=org.id,
            workspace_id=ws.id,
            brand_id=brand.id,
            location_id=loc.id,
            name="Dallas",
            status=ScoutRequestStatus.QUEUED.value,
            source_types=[],
            keywords=["coffee", "cafe"],
            resolved_market=MARKET,
        )
        s.add(req)
        s.flush()
        job = enqueue_scout_request(
            s,
            organization_id=org.id,
            workspace_id=ws.id,
            scout_request_id=req.id,
            location_id=loc.id,
            request_id=req.id,
        )
        s.commit()
        w.org, w.ws, w.user, w.rid, w.job_id = org.id, ws.id, user.id, req.id, job.id
        w.ctx = SimpleNamespace(
            organization=SimpleNamespace(id=org.id), user=SimpleNamespace(id=user.id)
        )


# --------------------------------------------------------------------------- #
# Instruments
# --------------------------------------------------------------------------- #
class Sources:
    """Stand-in for ``pipeline.fetch_signals``: fake sources serving the demo library.

    ``gate=True`` parks the fetch (inside the run's transaction, after the claim) until
    the test releases it; ``count`` serves exactly that many signals; ``fail`` raises in
    place of a fetch."""

    def __init__(self, *, gate: bool = False, count: int | None = None, fail=None):
        self.gate, self.count, self.fail = gate, count, fail
        self.calls: list[float] = []
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(
        self, *, market, keywords, source_types, rss_enabled, takedowns=frozenset(), settings=None
    ):
        self.calls.append(time.monotonic())
        self.started.set()
        if self.gate:
            assert self.release.wait(JOIN_SECONDS), "the test never released the fetch"
        if self.fail is not None:
            raise self.fail
        signals = fixtures_for_market(MARKET)
        if self.count is not None:
            signals = [signals[i % len(signals)] for i in range(self.count)]

        class _FakeSource:
            name = "fake-source"

            def fetch(self, **_kw):
                return signals

        outcome = fetch_with_reports(
            _FakeSource(), market=market, keywords=list(keywords), source_types=list(source_types)
        )
        return SignalBatch(
            signals=outcome.signals,
            coverage=Coverage(CoverageReason.FIXTURE_LIBRARY, is_simulated=True),
            reports=outcome.reports,
        )


class ModelCalls:
    """Counts and timestamps every model call the mock provider serves."""

    def __init__(self, monkeypatch):
        self.calls: list[float] = []
        real = MockLLMProvider.generate

        def generate(provider, *args, **kwargs):
            self.calls.append(time.monotonic())
            return real(provider, *args, **kwargs)

        monkeypatch.setattr(MockLLMProvider, "generate", generate)


class PricedMock:
    """The deterministic mock's outputs, reported as a priced Anthropic model: the meter
    bounds the exact bytes the Anthropic adapter would send and charges the fixed usage.
    Nothing is sent anywhere. The first call can be parked until the test releases it."""

    name = "anthropic"
    model = MODEL

    def __init__(self, *, park_first: bool = False):
        self._mock = MockLLMProvider(model="mock-1", seed=offline_settings().llm_mock_seed)
        self.calls: list[float] = []
        self.park_first = park_first
        self.first_call = threading.Event()
        self.release = threading.Event()

    def prepare(self, request):
        from app.llm.providers_real import AnthropicProvider

        return AnthropicProvider(KEY, MODEL, 5).prepare(request)

    def generate(self, request, prompt_name, prompt_version):
        self.calls.append(time.monotonic())
        if len(self.calls) == 1 and self.park_first:
            self.first_call.set()
            assert self.release.wait(JOIN_SECONDS), "the test never released the model call"
        response = self._mock.generate(request, prompt_name, prompt_version)
        return response.model_copy(
            update={
                "provider": self.name,
                "model": self.model,
                "usage": LLMUsage(input_tokens=FAKE_USAGE[0], output_tokens=FAKE_USAGE[1]),
                "is_simulated": True,
            }
        )


class Background(threading.Thread):
    """A daemon thread whose result or exception is handed back on a bounded join."""

    def __init__(self, fn: Callable[[], object]):
        super().__init__(daemon=True)
        self._fn, self.result, self.error = fn, None, None

    def run(self) -> None:
        try:
            self.result = self._fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised by join_ok
            self.error = exc

    def join_ok(self):
        self.join(JOIN_SECONDS)
        assert not self.is_alive(), "background session did not finish in time"
        if self.error is not None:
            raise self.error
        return self.result


def _runner(w: World, clock: Callable[[], datetime] | None = None) -> JobRunner:
    return JobRunner(
        settings=Settings(_env_file=None),
        session_factory=w.factory,
        store=DurableJobStore(),
        **({"clock": clock} if clock is not None else {}),
    )


def _start_worker(w: World, runner: JobRunner | None = None) -> Background:
    runner = runner or _runner(w)

    def poll() -> bool:
        w.worker_ident = threading.get_ident()
        return runner.poll_once(worker_id="w-pg-pause")

    thread = Background(poll)
    thread.start()
    return thread


def _pause(w: World) -> float:
    """The pause route's body in a session of its own, committed as ``get_db`` does."""
    with w.factory() as s:
        w.pause_pid = s.execute(text("select pg_backend_pid()")).scalar_one()
        w.marks["pause_issued"] = time.monotonic()
        pause_scout_request(workspace_id=w.ws, request_id=w.rid, db=s, ctx=w.ctx)
        s.commit()
        w.marks["pause_committed"] = time.monotonic()
    return w.marks["pause_committed"] - w.marks["pause_issued"]


def _start_pause(w: World) -> Background:
    w.pause_pid = None
    thread = Background(lambda: _pause(w))
    thread.start()
    return thread


def _await(predicate: Callable[[], object], what: str) -> object:
    deadline = time.monotonic() + JOIN_SECONDS
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def _lock_wait(w: World, pid: int | None) -> dict | None:
    """``pid``'s lock wait as PostgreSQL reports it, or ``None`` if it is not waiting."""
    if pid is None:
        return None
    with w.observer.connect() as conn:
        row = (
            conn.execute(
                text(
                    "select a.wait_event_type, a.wait_event, pg_blocking_pids(a.pid) as blockers,"
                    " (select string_agg(l.locktype || ':' || l.mode, ',') from pg_locks l"
                    "   where l.pid = a.pid and not l.granted) as waiting_for"
                    " from pg_stat_activity a where a.pid = :pid"
                ),
                {"pid": pid},
            )
            .mappings()
            .first()
        )
    if row is None or row["wait_event_type"] != "Lock":
        return None
    return {**dict(row), "seen_at": time.monotonic()}


def _waiter_blocked_by(w: World, holder: int) -> dict | None:
    """Any backend of this database waiting on a lock that ``holder`` holds."""
    with w.observer.connect() as conn:
        row = (
            conn.execute(
                text(
                    "select pid, wait_event_type, wait_event, pg_blocking_pids(pid) as blockers,"
                    " left(query, 40) as statement from pg_stat_activity"
                    " where datname = current_database() and :holder = any(pg_blocking_pids(pid))"
                ),
                {"holder": holder},
            )
            .mappings()
            .first()
        )
    return {**dict(row), "seen_at": time.monotonic()} if row is not None else None


def _ledger(w: World) -> dict:
    with w.observer.connect() as conn:

        def scalar(sql: str):
            return conn.execute(text(sql), {"ws": w.ws}).scalar_one()

        return {
            "reservations": scalar(
                "select count(*) from llm_budget_reservations where workspace_id = :ws"
            ),
            "reservations_reserved": scalar(
                "select count(*) from llm_budget_reservations"
                " where workspace_id = :ws and state = 'reserved'"
            ),
            "usage_events": scalar(
                "select count(*) from llm_usage_events where workspace_id = :ws"
            ),
            "periods": scalar("select count(*) from llm_budget_periods where workspace_id = :ws"),
            "reserved_usd": Decimal(
                scalar(
                    "select coalesce(sum(reserved_usd), 0) from llm_budget_periods"
                    " where workspace_id = :ws"
                )
            ),
            "settled_usd": Decimal(
                scalar(
                    "select coalesce(sum(settled_usd), 0) from llm_budget_periods"
                    " where workspace_id = :ws"
                )
            ),
        }


def _end_state(w: World) -> SimpleNamespace:
    with w.factory() as s:
        req = s.get(ScoutRequest, w.rid)
        job = s.get(Job, w.job_id)
        return SimpleNamespace(
            status=req.status,
            stats=req.stats,
            last_run_at=req.last_run_at,
            job_status=job.status,
            job_summary=job.result_summary,
            job_error=(job.last_error_code, job.last_error_summary),
            opportunities=s.scalar(
                select(func.count())
                .select_from(Opportunity)
                .where(Opportunity.scout_request_id == w.rid)
            ),
            raw_signals=s.scalar(
                select(func.count())
                .select_from(RawSignal)
                .where(RawSignal.scout_request_id == w.rid)
            ),
            paused_audits=s.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.entity_id == w.rid, AuditLog.action == "scout_request.paused")
            ),
        )


def _commit_order(w: World) -> dict | None:
    """Server-side commit times of the job row's and the request row's last writers, when
    the cluster tracks commit timestamps (a measurement aid; CI's service does not)."""
    with w.observer.connect() as conn:
        if conn.execute(text("show track_commit_timestamp")).scalar_one() != "on":
            return None
        job_ts = conn.execute(
            text("select pg_xact_commit_timestamp(xmin) from jobs where id = :id"), {"id": w.job_id}
        ).scalar_one()
        req_ts = conn.execute(
            text("select pg_xact_commit_timestamp(xmin) from scout_requests where id = :id"),
            {"id": w.rid},
        ).scalar_one()
    return {
        "job_row_last_commit": job_ts.isoformat(),
        "request_row_last_commit": req_ts.isoformat(),
        "request_after_job_ms": round((req_ts - job_ts).total_seconds() * 1000, 3),
    }


def _measure(name: str, **values) -> None:
    print("PAUSE-PG-MEASURE", json.dumps({"scenario": name, **values}, default=str))


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


# --------------------------------------------------------------------------- #
# Pause before the claim, and the pause/claim race
# --------------------------------------------------------------------------- #
def test_pause_committed_before_the_claim_skips_with_no_fetch_model_call_or_ledger_row(
    world, monkeypatch
):
    w = world
    sources = Sources()
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    models = ModelCalls(monkeypatch)
    pause_ms = _ms(_pause(w))
    before = _ledger(w)

    with offline_llm():
        assert _start_worker(w).join_ok() is True

    end = _end_state(w)
    assert (sources.calls, models.calls) == ([], [])
    assert end.status == PAUSED and end.last_run_at is None
    assert end.job_status == JobStatus.SUCCEEDED.value
    assert end.job_summary == {"scout_request_id": w.rid, "outcome": "skipped_request_paused"}
    assert (
        _ledger(w)
        == before
        == {
            "reservations": 0,
            "reservations_reserved": 0,
            "usage_events": 0,
            "periods": 0,
            "reserved_usd": Decimal(0),
            "settled_usd": Decimal(0),
        }
    )
    _measure("pause_before_claim", pause_ms=pause_ms, job=end.job_status, status=end.status)


def test_pause_committed_between_the_load_and_the_claim_wins_the_race(world, monkeypatch):
    """The worker has loaded the request (it says ``queued``) and is parked before its
    claim; the pause commits from another session meanwhile. The worker holds no lock on
    the row yet, so the pause does not wait; the claim then matches no row."""
    w = world
    sources = Sources()
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    models = ModelCalls(monkeypatch)
    loaded, go = threading.Event(), threading.Event()
    real_scope_matches = pipeline.scope_matches

    def park_after_load(*args, **kwargs):
        loaded.set()
        assert go.wait(JOIN_SECONDS)
        return real_scope_matches(*args, **kwargs)

    monkeypatch.setattr(pipeline, "scope_matches", park_after_load)
    with offline_llm():
        worker = _start_worker(w)
        assert loaded.wait(JOIN_SECONDS)
        pause_ms = _ms(_pause(w))  # returns while the worker is still parked
        assert worker.is_alive()
        go.set()
        assert worker.join_ok() is True

    end = _end_state(w)
    assert (sources.calls, models.calls) == ([], [])
    assert end.status == PAUSED
    assert end.job_summary == {"scout_request_id": w.rid, "outcome": "skipped_request_paused"}
    assert _ledger(w)["reservations"] == _ledger(w)["usage_events"] == 0
    _measure("pause_between_load_and_claim", pause_ms=pause_ms, waited=False, job=end.job_status)


def test_claim_issued_while_a_pause_is_uncommitted_waits_for_it_then_skips(world, monkeypatch):
    """The other order: the pause has written the row but not committed when the worker
    claims. The claim's compare-and-set waits on the pause's row lock; once the pause
    commits, PostgreSQL re-checks the claim's predicate against the committed row
    (``status != 'paused'`` is now false), so the claim matches nothing and the job skips."""
    w = world
    sources = Sources()
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    models = ModelCalls(monkeypatch)
    with w.factory() as pause_session, offline_llm():
        pause_pid = pause_session.execute(text("select pg_backend_pid()")).scalar_one()
        pause_scout_request(workspace_id=w.ws, request_id=w.rid, db=pause_session, ctx=w.ctx)
        worker = _start_worker(w)
        waiter = _await(lambda: _waiter_blocked_by(w, pause_pid), "the claim to wait")
        time.sleep(HOLD_SECONDS)
        assert worker.is_alive() and _waiter_blocked_by(w, pause_pid) is not None
        pause_session.commit()
        committed_at = time.monotonic()
        assert worker.join_ok() is True
        worker_done = time.monotonic()

    end = _end_state(w)
    assert waiter["wait_event_type"] == "Lock" and waiter["blockers"] == [pause_pid]
    assert waiter["statement"].startswith("UPDATE scout_requests SET status")
    assert (sources.calls, models.calls) == ([], [])
    assert end.status == PAUSED
    assert end.job_summary == {"scout_request_id": w.rid, "outcome": "skipped_request_paused"}
    _measure(
        "claim_waits_for_uncommitted_pause",
        wait_event=waiter["wait_event"],
        claim_waited_ms_at_least=_ms(committed_at - waiter["seen_at"]),
        worker_done_after_pause_commit_ms=_ms(worker_done - committed_at),
        job=end.job_status,
    )


# --------------------------------------------------------------------------- #
# A pause issued while a run is in flight
# --------------------------------------------------------------------------- #
def _pause_mid_run(w: World, sources: Sources, worker: Background) -> tuple[Background, dict]:
    """Park the run at its fetch (after its claim), issue the pause from another session,
    observe it waiting on the run's row lock, hold, then release the run."""
    assert sources.started.wait(JOIN_SECONDS)
    pause = _start_pause(w)
    wait = _await(lambda: _lock_wait(w, w.pause_pid), "the pause to wait on a lock")
    time.sleep(HOLD_SECONDS)
    still = _lock_wait(w, w.pause_pid)
    assert pause.is_alive() and still is not None, "the pause stopped waiting while the run held"
    assert "pause_committed" not in w.marks
    sources.release.set()
    return pause, wait


def test_pause_issued_mid_run_waits_for_the_run_commit_and_then_lands(world, monkeypatch):
    """The pause is serialised after the run, not an interruption: it waits on the
    ``scout_requests`` row lock the run's claim took, for as long as the run holds it.
    The run finishes with every fetch and model call it had left, records
    ``outcome=completed`` (its final re-read still saw ``running``), and the request
    ends ``paused`` once the pause commits after the run."""
    w = world
    sources = Sources(gate=True)
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    models = ModelCalls(monkeypatch)
    with offline_llm():
        worker = _start_worker(w)
        pause, wait = _pause_mid_run(w, sources, worker)
        assert worker.join_ok() is True
        pause_ms = _ms(pause.join_ok())

    end = _end_state(w)
    assert wait["blockers"] == [w.worker_pid]
    assert end.status == PAUSED and end.paused_audits == 1
    assert end.job_status == JobStatus.SUCCEEDED.value
    assert end.job_summary["outcome"] == "completed"  # not paused_during_run
    assert end.opportunities > 0 and end.last_run_at is not None
    # Everything the run did after its fetch happened after the pause was issued.
    assert len(sources.calls) == 1 and models.calls
    assert all(t > w.marks["pause_issued"] for t in models.calls)
    # Each mock call is recorded on the ledger with the run (a settled reservation row of
    # zero cost); the pause undid none of them. (The mock's usage rows are not asserted:
    # on PostgreSQL the same-session ``record_settled`` stops after the reservation row,
    # a #269 path the mock alone takes; reported in the lane evidence, not changed here.)
    ledger = _ledger(w)
    assert ledger["reservations"] == len(models.calls)
    order = _commit_order(w)
    if order is not None:
        assert order["request_after_job_ms"] >= 0
    _measure(
        "pause_mid_run_completed",
        wait_event=f"{wait['wait_event_type']}/{wait['wait_event']}",
        waiting_for=wait["waiting_for"],
        blocked_by_worker=True,
        pause_latency_ms=pause_ms,
        hold_ms=_ms(HOLD_SECONDS),
        client_pause_commit_after_worker_last_commit_ms=_ms(
            w.marks["pause_committed"] - w.worker_commits[-1]
        ),
        model_calls_after_pause_issued=len(models.calls),
        ledger_end=ledger,
        job=end.job_status,
        job_outcome=end.job_summary["outcome"],
        end_status=end.status,
        server_commit_order=order,
    )


def test_pause_issued_mid_run_is_not_overwritten_by_the_refusal(world, monkeypatch):
    """#268 refusal path: the run is refused at the per-run bound after its fetch. The
    attempt rolls back, which releases the row, and the waiting pause commits; the
    refusal's after-rollback writer then finds the request paused and leaves it so (its
    ``failed`` write is a compare-and-set excluding ``paused``). The job fails
    non-retryably with the stable reason; no model call was made; nothing reserved."""
    w = world
    monkeypatch.setattr(get_settings(), "vector_inprocess_max_signals_per_run", 3)
    sources = Sources(gate=True, count=5)
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    models = ModelCalls(monkeypatch)
    with offline_llm():
        worker = _start_worker(w)
        pause, wait = _pause_mid_run(w, sources, worker)
        assert worker.join_ok() is True
        pause_ms = _ms(pause.join_ok())

    end = _end_state(w)
    assert wait["blockers"] == [w.worker_pid]
    assert end.status == PAUSED
    assert end.stats["refused"] is True and end.stats["refusal_reason"] == SIGNAL_VOLUME_ABOVE_BOUND
    assert (end.stats["scanned"], end.stats["bound"]) == (5, 3)
    assert end.job_status == JobStatus.FAILED.value
    assert end.job_error == (JobErrorCode.NON_RETRYABLE.value, SIGNAL_VOLUME_ABOVE_BOUND)
    assert models.calls == [] and end.raw_signals == 0
    assert _ledger(w)["reservations"] == 0
    _measure(
        "pause_mid_run_refused",
        wait_event=f"{wait['wait_event_type']}/{wait['wait_event']}",
        pause_latency_ms=pause_ms,
        job=end.job_status,
        job_error=end.job_error,
        end_status=end.status,
        stats_refused=end.stats["refused"],
    )


def test_pause_issued_mid_run_is_not_overwritten_by_the_failure_hook(world, monkeypatch):
    """Failure path, final attempt: the run fails after its fetch, the attempt rolls
    back, the waiting pause commits, and ``settle_failed_scout_request`` (in the job's
    failure transaction) leaves the paused request alone."""
    w = world
    sources = Sources(gate=True, fail=RuntimeError("fake source failure"))
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    with w.factory() as s:
        s.get(Job, w.job_id).max_attempts = 1
        s.commit()
    with offline_llm():
        worker = _start_worker(w)
        pause, wait = _pause_mid_run(w, sources, worker)
        assert worker.join_ok() is True
        pause_ms = _ms(pause.join_ok())

    end = _end_state(w)
    assert wait["blockers"] == [w.worker_pid]
    assert end.status == PAUSED
    assert end.job_status == JobStatus.DEAD_LETTERED.value
    assert end.job_error[0] == JobErrorCode.TRANSIENT.value
    _measure(
        "pause_mid_run_failed_final",
        pause_latency_ms=pause_ms,
        job=end.job_status,
        end_status=end.status,
    )


def test_pause_issued_mid_run_is_honoured_by_the_retry(world, monkeypatch):
    """Failure path, attempts left: the attempt fails and rolls back (the request is
    ``queued`` again), the waiting pause commits, the job waits to retry -- and the retry
    meets a paused request at its entry guard and skips."""
    w = world
    sources = Sources(gate=True, fail=RuntimeError("fake source failure"))
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    shift = [timedelta(0)]
    runner = _runner(w, clock=lambda: datetime.now(UTC) + shift[0])
    with offline_llm():
        worker = _start_worker(w, runner)
        pause, _wait = _pause_mid_run(w, sources, worker)
        assert worker.join_ok() is True
        pause.join_ok()
        assert _end_state(w).job_status == JobStatus.RETRY_WAIT.value
        assert _end_state(w).status == PAUSED
        sources.fail, sources.gate = None, False
        shift[0] = timedelta(hours=1)  # past the retry backoff
        assert _start_worker(w, runner).join_ok() is True

    end = _end_state(w)
    assert len(sources.calls) == 1  # the retry fetched nothing
    assert end.status == PAUSED
    assert end.job_status == JobStatus.SUCCEEDED.value
    assert end.job_summary == {"scout_request_id": w.rid, "outcome": "skipped_request_paused"}
    _measure("pause_mid_run_then_retry", job=end.job_status, outcome=end.job_summary["outcome"])


# --------------------------------------------------------------------------- #
# Already-dispatched work: it happens, it is reserved and charged, nothing undoes it
# --------------------------------------------------------------------------- #
def _priced(monkeypatch) -> PricedMock:
    provider = PricedMock(park_first=True)
    monkeypatch.setattr(
        pipeline,
        "llm_service",
        LLMService(provider, sleep=lambda _s: None, settings=offline_settings()),
    )
    return provider


def _pause_at_first_model_call(w: World, provider: PricedMock) -> tuple[Background, dict, dict]:
    assert provider.first_call.wait(JOIN_SECONDS)
    pause = _start_pause(w)
    wait = _await(lambda: _lock_wait(w, w.pause_pid), "the pause to wait on a lock")
    # Seen from a third session while the run is still in flight: the first call's
    # reservation is already committed, outside the run's transaction.
    during = _ledger(w)
    time.sleep(HOLD_SECONDS)
    assert pause.is_alive() and _lock_wait(w, w.pause_pid) is not None
    provider.release.set()
    return pause, wait, during


def test_model_calls_dispatched_after_a_mid_run_pause_are_reserved_and_charged(world, monkeypatch):
    w = world
    sources = Sources()
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    provider = _priced(monkeypatch)
    worker = _start_worker(w)
    pause, wait, during = _pause_at_first_model_call(w, provider)
    assert worker.join_ok() is True
    pause_ms = _ms(pause.join_ok())

    end, ledger = _end_state(w), _ledger(w)
    calls = len(provider.calls)
    after_pause = sum(1 for t in provider.calls if t > w.marks["pause_issued"])
    per_call = Decimal(str(cost_usd(get_settings(), "anthropic", MODEL, *FAKE_USAGE)))
    assert wait["blockers"] == [w.worker_pid]
    assert during["reservations"] == during["reservations_reserved"] == 1
    assert during["usage_events"] == 0 and during["reserved_usd"] > 0
    assert calls >= 2 and after_pause == calls - 1
    assert ledger["reservations"] == ledger["usage_events"] == calls
    assert ledger["reservations_reserved"] == 0  # every call settled
    assert ledger["settled_usd"] == per_call * calls
    assert ledger["reserved_usd"] >= ledger["settled_usd"] > 0
    assert end.status == PAUSED and end.job_summary["outcome"] == "completed"
    _measure(
        "dispatched_work_after_pause",
        pause_latency_ms=pause_ms,
        model_calls=calls,
        model_calls_after_pause_issued=after_pause,
        ledger_seen_mid_run=during,
        ledger_end=ledger,
        per_call_usd=per_call,
        job_outcome=end.job_summary["outcome"],
        end_status=end.status,
    )


def test_charges_of_a_paused_run_are_not_undone_when_the_run_rolls_back(world, monkeypatch):
    """The run is paused mid-run and then fails after its model calls (final attempt).
    The run's own writes roll back -- no signal of it is stored -- but every model call
    it made stays reserved and charged: each reservation and settlement was committed in
    a transaction of its own. The request ends paused."""
    w = world
    sources = Sources()
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    provider = _priced(monkeypatch)

    def fail_after_the_calls(*args, **kwargs):
        raise RuntimeError("fake failure after the model calls")

    monkeypatch.setattr(pipeline, "_build_opportunities", fail_after_the_calls)
    with w.factory() as s:
        s.get(Job, w.job_id).max_attempts = 1
        s.commit()
    worker = _start_worker(w)
    pause, _wait, during = _pause_at_first_model_call(w, provider)
    assert worker.join_ok() is True
    pause.join_ok()

    end, ledger = _end_state(w), _ledger(w)
    calls = len(provider.calls)
    per_call = Decimal(str(cost_usd(get_settings(), "anthropic", MODEL, *FAKE_USAGE)))
    assert end.job_status == JobStatus.DEAD_LETTERED.value
    assert end.status == PAUSED
    assert end.raw_signals == 0 and end.opportunities == 0  # the run's writes rolled back
    assert calls >= 2
    assert ledger["reservations"] == ledger["usage_events"] == calls  # the charges did not
    assert ledger["settled_usd"] == per_call * calls
    _measure(
        "charges_survive_rollback",
        model_calls=calls,
        ledger_seen_mid_run=during,
        ledger_end=ledger,
        raw_signals=end.raw_signals,
        job=end.job_status,
        end_status=end.status,
    )
