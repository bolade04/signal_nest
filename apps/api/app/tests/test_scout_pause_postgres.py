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
* D-25 cooperative-pause candidate (PROPOSED, contract §6; these replace the option-(1)
  pins of the refresh): the claim is committed in a transaction of its own, so a pause
  issued while a run is in flight COMMITS WITHOUT WAITING for the run; the run stops at
  its next admission (``stopped_request_paused``), its findings are discarded and the
  request stays ``paused``. On the #268 refusal path and the failure path neither the
  refusal's writer nor the terminal-failure hook (nor the status restore) overwrites it;
* work admitted before the pause commits may finish: with a priced fake provider on the
  shared ledger, the in-flight call's reservation, settlement and usage row stand (one
  of each) and nothing undoes them, not even the run's own rollback; no further attempt
  is admitted.

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
from sqlalchemy import create_engine, event, func, select, text, update
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


def _hold_uncommitted_pause(session, w) -> None:
    """The pause route's write (``paused`` and a new run fence), left UNCOMMITTED in
    ``session``: the route itself now commits before answering, so a test that needs the
    pause in flight issues the same statement and commits it later itself."""
    session.execute(
        update(ScoutRequest)
        .where(ScoutRequest.id == w.rid, ScoutRequest.workspace_id == w.ws)
        .values(status=PAUSED, run_fence=ScoutRequest.run_fence + 1)
        .execution_options(synchronize_session=False)
    )


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
        _hold_uncommitted_pause(pause_session, w)
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
    # The committed claim (D-25 candidate) reads the row FOR NO KEY UPDATE before its
    # compare-and-set; that read is what waits for the uncommitted pause.
    assert waiter["statement"].startswith("SELECT scout_requests.status, scout_req")
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
# A pause issued while a run is in flight (D-25 cooperative-pause candidate, PROPOSED)
# --------------------------------------------------------------------------- #
def _pause_mid_run(w: World, sources: Sources, worker: Background) -> tuple[float, list]:
    """Park the run at its fetch (after its committed claim), pause it from another
    session and require the pause to COMMIT while the run is still parked, i.e. without
    waiting for the run (contract §1.1). Returns the pause latency and every lock wait
    seen on the pause's backend (expected: none blocked by the worker). Then releases the
    run."""
    assert sources.started.wait(JOIN_SECONDS)
    seen: list = []
    pause = _start_pause(w)
    deadline = time.monotonic() + JOIN_SECONDS
    while pause.is_alive() and time.monotonic() < deadline:
        wait = _lock_wait(w, w.pause_pid)
        if wait is not None:
            seen.append(wait)
        time.sleep(0.002)
    pause_s = pause.join_ok()
    assert worker.is_alive(), "the run finished before the pause; the scenario did not hold"
    assert "pause_committed" in w.marks  # committed while the run was still parked
    sources.release.set()
    return pause_s, seen


def test_pause_mid_run_commits_without_waiting_because_the_claim_is_not_in_the_run_transaction(
    world, monkeypatch
):
    """Replaces the option-(1) pin "the pause waits for the run's commit" (contract §6.2
    Q-7). The hard constraint of the candidate: the claim is committed in a transaction of
    its own, so the run's long transaction holds no update lock on the request row while
    it fetches. A pause issued during the parked fetch therefore commits without waiting on
    the run's transaction id (it never shows a lock wait blocked by the worker), the run
    stops at its next admission (the post-fetch boundary) and makes no model call; nothing
    of the run is stored and nothing is reserved. The request stays ``paused``."""
    w = world
    sources = Sources(gate=True)
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    models = ModelCalls(monkeypatch)
    with offline_llm():
        worker = _start_worker(w)
        pause_s, waits = _pause_mid_run(w, sources, worker)
        assert worker.join_ok() is True

    end = _end_state(w)
    assert [x for x in waits if w.worker_pid in (x["blockers"] or [])] == []
    assert end.status == PAUSED and end.paused_audits == 1
    assert end.job_status == JobStatus.SUCCEEDED.value
    assert end.job_summary == {"scout_request_id": w.rid, "outcome": "stopped_request_paused"}
    assert len(sources.calls) == 1 and models.calls == []
    assert end.opportunities == 0 and end.raw_signals == 0 and end.last_run_at is None
    assert _ledger(w)["reservations"] == _ledger(w)["usage_events"] == 0
    _measure(
        "pause_mid_run_cooperative",
        pause_latency_ms=_ms(pause_s),
        lock_waits_seen=len(waits),
        job=end.job_status,
        job_outcome=end.job_summary["outcome"],
        end_status=end.status,
    )


def test_pause_landing_after_the_fetch_boundary_is_not_overwritten_by_the_refusal(
    world, monkeypatch
):
    """#268 refusal path with the pause committing after the post-fetch admission and
    before the bound check (injected in the takedown re-check, from another session). The
    attempt is refused and rolls back; the refusal's writer is a compare-and-set on the
    run's fence, which the pause advanced, so the request stays ``paused`` with its
    earlier stats (a run's outcome lands only if it commits before the pause, §6.2 Q-1;
    replaces "the paused request carries the refusal's stats"). The job fails
    non-retryably with the stable reason; no model call; nothing reserved."""
    w = world
    monkeypatch.setattr(get_settings(), "vector_inprocess_max_signals_per_run", 3)
    sources = Sources(count=5)
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    models = ModelCalls(monkeypatch)
    real_recheck = pipeline.without_taken_down

    def pause_then_recheck(db, signals):
        _pause(w)
        return real_recheck(db, signals)

    monkeypatch.setattr(pipeline, "without_taken_down", pause_then_recheck)
    with offline_llm():
        assert _start_worker(w).join_ok() is True

    end = _end_state(w)
    assert end.status == PAUSED
    assert not (end.stats or {}).get("refused")
    assert end.job_status == JobStatus.FAILED.value
    assert end.job_error == (JobErrorCode.NON_RETRYABLE.value, SIGNAL_VOLUME_ABOVE_BOUND)
    assert models.calls == [] and end.raw_signals == 0
    assert _ledger(w)["reservations"] == 0
    _measure("pause_then_refusal", job=end.job_status, end_status=end.status)


def test_pause_issued_mid_run_is_not_overwritten_by_the_failure_hook(world, monkeypatch):
    """Failure path, final attempt: the pause commits while the fetch is parked (without
    waiting), the fetch then fails, the attempt rolls back, and neither the lease-aware
    status restore nor ``settle_failed_scout_request`` overwrites the pause."""
    w = world
    sources = Sources(gate=True, fail=RuntimeError("fake source failure"))
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    with w.factory() as s:
        s.get(Job, w.job_id).max_attempts = 1
        s.commit()
    with offline_llm():
        worker = _start_worker(w)
        pause_s, _waits = _pause_mid_run(w, sources, worker)
        assert worker.join_ok() is True

    end = _end_state(w)
    assert end.status == PAUSED
    assert end.job_status == JobStatus.DEAD_LETTERED.value
    assert end.job_error[0] == JobErrorCode.TRANSIENT.value
    _measure(
        "pause_mid_run_failed_final",
        pause_latency_ms=_ms(pause_s),
        job=end.job_status,
        end_status=end.status,
    )


def test_pause_issued_mid_run_is_honoured_by_the_retry(world, monkeypatch):
    """Failure path, attempts left: the pause commits while the fetch is parked, the
    attempt fails and rolls back, the job waits to retry -- and the retry's committed
    claim meets a paused request with a moved fence and skips (no fetch)."""
    w = world
    sources = Sources(gate=True, fail=RuntimeError("fake source failure"))
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    shift = [timedelta(0)]
    runner = _runner(w, clock=lambda: datetime.now(UTC) + shift[0])
    with offline_llm():
        worker = _start_worker(w, runner)
        _pause_mid_run(w, sources, worker)
        assert worker.join_ok() is True
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
# Work already admitted: it may finish, it is reserved and charged, nothing undoes it;
# nothing new is admitted after the pause commits
# --------------------------------------------------------------------------- #
def _priced(monkeypatch) -> PricedMock:
    provider = PricedMock(park_first=True)
    monkeypatch.setattr("app.llm.service.llm_service",
        LLMService(provider, sleep=lambda _s: None, settings=offline_settings()),
    )
    return provider


def _pause_at_first_model_call(w: World, provider: PricedMock) -> tuple[float, dict]:
    """Pause while the first (admitted, reserved) model call is in flight; the pause must
    commit before the call is released. Returns the pause latency and the ledger seen from
    a third session while the call was in flight."""
    assert provider.first_call.wait(JOIN_SECONDS)
    during = _ledger(w)
    pause_s = _pause(w)  # synchronous: returns only once committed
    assert not provider.release.is_set()
    provider.release.set()
    return pause_s, during


def test_a_mid_run_pause_lets_the_admitted_call_finish_and_admits_no_further_attempt(
    world, monkeypatch
):
    """Replaces the option-(1) pin "model calls dispatched after a mid-run pause are
    reserved and charged" (contract §6.2 Q-7), on the shared-ledger path (a priced fake
    provider; nothing is sent). The pause commits while the first call is in flight. That
    call was admitted before the cut-off: its reservation, settlement and usage row stand,
    exactly one of each. No further model attempt is admitted (the next one is refused in
    its reservation transaction, which reserves nothing). The run stops, its findings are
    discarded, and the request stays ``paused``."""
    w = world
    sources = Sources()
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    provider = _priced(monkeypatch)
    worker = _start_worker(w)
    pause_s, during = _pause_at_first_model_call(w, provider)
    assert worker.join_ok() is True

    end, ledger = _end_state(w), _ledger(w)
    per_call = Decimal(str(cost_usd(get_settings(), "anthropic", MODEL, *FAKE_USAGE)))
    assert during["reservations"] == during["reservations_reserved"] == 1
    assert during["usage_events"] == 0 and during["reserved_usd"] > 0
    assert len(provider.calls) == 1  # nothing admitted after the cut-off
    assert ledger["reservations"] == ledger["usage_events"] == 1
    assert ledger["reservations_reserved"] == 0  # the in-flight call settled
    assert ledger["settled_usd"] == per_call
    assert ledger["reserved_usd"] == during["reserved_usd"]  # the refused attempt: nothing
    assert end.status == PAUSED
    assert end.job_status == JobStatus.SUCCEEDED.value
    assert end.job_summary == {"scout_request_id": w.rid, "outcome": "stopped_request_paused"}
    assert end.opportunities == 0 and end.raw_signals == 0  # findings discarded
    _measure(
        "admitted_call_after_pause",
        pause_latency_ms=_ms(pause_s),
        model_calls=len(provider.calls),
        ledger_seen_mid_run=during,
        ledger_end=ledger,
        per_call_usd=per_call,
        job_outcome=end.job_summary["outcome"],
        end_status=end.status,
    )


def test_charges_of_a_paused_run_are_not_undone_when_the_run_rolls_back(world, monkeypatch):
    """The run is paused while its first call is in flight and then fails right after
    that call (final attempt), before its next admission. The run's own writes roll back
    -- no signal of it is stored -- but the call it made stays reserved and charged: its
    reservation and settlement were committed in transactions of their own. The request
    ends paused."""
    w = world
    sources = Sources()
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    provider = _priced(monkeypatch)

    def fail_after_the_call(*args, **kwargs):
        raise RuntimeError("fake failure after the model call")

    monkeypatch.setattr(pipeline, "screen_classification", fail_after_the_call)
    with w.factory() as s:
        s.get(Job, w.job_id).max_attempts = 1
        s.commit()
    worker = _start_worker(w)
    _pause_s, during = _pause_at_first_model_call(w, provider)
    assert worker.join_ok() is True

    end, ledger = _end_state(w), _ledger(w)
    per_call = Decimal(str(cost_usd(get_settings(), "anthropic", MODEL, *FAKE_USAGE)))
    assert end.job_status == JobStatus.DEAD_LETTERED.value
    assert end.status == PAUSED
    assert end.raw_signals == 0 and end.opportunities == 0  # the run's writes rolled back
    assert len(provider.calls) == 1
    assert ledger["reservations"] == ledger["usage_events"] == 1  # the charge did not
    assert ledger["settled_usd"] == per_call
    _measure(
        "charges_survive_rollback",
        model_calls=len(provider.calls),
        ledger_seen_mid_run=during,
        ledger_end=ledger,
        raw_signals=end.raw_signals,
        job=end.job_status,
        end_status=end.status,
    )
