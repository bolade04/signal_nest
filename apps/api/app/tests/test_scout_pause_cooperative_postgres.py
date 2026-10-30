"""The D-25 cooperative-pause candidate on PostgreSQL (PROPOSED, contract §6).

Nothing here accepts D-25: it pins the candidate behaviour so that it can be measured and
ruled on. It reuses the harness of ``test_scout_pause_postgres.py`` (separate threads, each
with its own connection and server backend; the real worker, the real route bodies; a
schema the real Alembic chain produced; offline fakes only, nothing is sent anywhere).

* a pause during a parked **live fetch attempt** commits without waiting; after the
  release no retry and no further source is fetched (the next attempt's admission is
  refused);
* an **admission waits for an uncommitted pause** (``FOR SHARE``) and then refuses; this
  is what makes "nothing is admitted after the pause commits" exact;
* a pause that lands before the run's **terminal write** discards the run's findings
  (§6.2 Q-1): a run's findings become visible only if its terminal write commits first;
* on the **mock / same-session path** the admitted call is recorded exactly once
  (``record_settled`` with #276's ``preserve_rowcount``); nothing for the refused attempt;
* **Pause -> Resume -> Run now** while the old run is parked: the old run admits nothing
  more, the new run proceeds under its own fence;
* **lease recovery** after a pause admits nothing and does not overwrite the pause;
* **repeated pauses**, concurrent and sequential, are idempotent;
* **earlier findings and configuration** survive a stopped run;
* **organization erasure** is refused while a run is blocked, and late callbacks after an
  erasure recreate nothing (requests, findings, reservations, usage, periods). There is no
  workspace-level deletion path in the application (``app.privacy.erasure`` erases
  organizations only).

Gate: ``require_postgres`` -- with ``TEST_POSTGRES_URL`` unset these tests FAIL in CI
(``CI=true``) and skip only on a developer machine.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update

from app.connectors import live as live_mod
from app.connectors.base import FailureKind
from app.connectors.feedcache import FeedCache
from app.connectors.live import LiveRssConnector
from app.connectors.retry import ConnectorFetchError
from app.core.errors import ConflictError
from app.core.metrics import (
    LLM_SETTLEMENTS_ORPHANED_TOTAL,
    InMemoryMetrics,
    configure_metrics,
)
from app.infra.vector import SignalVolumeAboveBoundError
from app.jobs import pipeline
from app.jobs.admission import REQUEST_GONE, RunAdmission, RunStopped
from app.jobs.context import ExecutionContext
from app.jobs.handlers import _record_refused_run, settle_failed_scout_request
from app.jobs.models import Job
from app.jobs.registry import TerminalFailureContext
from app.jobs.status import JobErrorCode, JobStatus, JobType
from app.llm.base import LLMTimeoutError
from app.llm.metering import LLMMeter
from app.llm.mock import MockLLMProvider
from app.llm.models import LLMBudgetPeriod, LLMBudgetReservation, LLMUsageEvent
from app.opportunities.models import Opportunity
from app.organizations.models import Organization, Workspace
from app.privacy.erasure import erase_organization
from app.scouting_requests.models import ScoutRequest
from app.scouting_requests.routes import (
    pause_scout_request,
    resume_scout_request,
    run_scout_request_endpoint,
)
from app.signals.models import RawSignal
from app.tests import test_scout_pause_postgres as harness
from app.tests._auth2_support import require_postgres
from app.tests._llm_offline import offline_llm, offline_settings, wire_payload
from app.tests._llm_reserve_child import MODEL
from app.tests.test_live_rss_connector import record, target
from app.tests.test_scout_pause_postgres import (
    JOIN_SECONDS,
    MARKET,
    PAUSED,
    Background,
    ModelCalls,
    Sources,
    World,
    _end_state,
    _ledger,
    _measure,
    _ms,
    _pause,
    _runner,
    _start_worker,
    _waiter_blocked_by,
)

#: The harness's fixtures (a migrated template database per module, a clone per test).
template_url = harness.template_url
world = harness.world

STOPPED = "stopped_request_paused"
#: A short bound for waits that only a defect would make long (keeps mutant runs short).
SHORT_SECONDS = 10.0


@pytest.fixture(autouse=True)
def _postgres_required() -> None:
    require_postgres()


class FirstCallGate(Sources):
    """``Sources`` that parks only its first call (later runs fetch freely)."""

    def __call__(self, **kwargs):
        if len(self.calls) >= 1:
            gate, self.gate = self.gate, False
            try:
                return super().__call__(**kwargs)
            finally:
                self.gate = gate
        return super().__call__(**kwargs)


def _await_short(predicate, what: str):
    deadline = time.monotonic() + SHORT_SECONDS
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def _job(w: World, job_id: str) -> Job:
    with w.factory() as s:
        return s.get(Job, job_id)


def _count(w: World, model, **where) -> int:
    with w.factory() as s:
        stmt = select(func.count()).select_from(model)
        for column, value in where.items():
            stmt = stmt.where(getattr(model, column) == value)
        return s.scalar(stmt)


def _run_now(w: World) -> str:
    """The manual run route's body, committed (it commits itself)."""
    with w.factory() as s:
        out = run_scout_request_endpoint(workspace_id=w.ws, request_id=w.rid, db=s, ctx=w.ctx)
        return out.stats["job_id"]


def _resume(w: World) -> None:
    with w.factory() as s:
        resume_scout_request(workspace_id=w.ws, request_id=w.rid, db=s, ctx=w.ctx)
        s.commit()


# --------------------------------------------------------------------------- #
# Connector fetch attempts
# --------------------------------------------------------------------------- #
def test_a_pause_during_a_parked_live_fetch_admits_no_retry_and_no_further_source(
    world, monkeypatch
):
    w = world
    fetches: list[str] = []
    started, release = threading.Event(), threading.Event()

    class ParkedFetcher:
        """The egress fetcher's place: parks the first request, then times out (a
        retryable failure, so the connector would retry if admitted). Nothing is sent."""

        def fetch(self, url, *, allowed_hosts, validators=None):
            fetches.append(url)
            started.set()
            assert release.wait(JOIN_SECONDS)
            raise ConnectorFetchError(FailureKind.TIMEOUT, "fake timeout")

    connector = LiveRssConnector(
        [target(record("src-a", markets=(MARKET,))), target(record("src-b", markets=(MARKET,)))],
        fetcher=ParkedFetcher(),
        cache=FeedCache(),
        sleep=lambda _s: None,
        max_attempts=2,
    )
    monkeypatch.setattr(live_mod, "live_connector_for", lambda **_kw: connector)
    models = ModelCalls(monkeypatch)
    with offline_llm():
        worker = _start_worker(w)
        assert started.wait(JOIN_SECONDS)
        pause_s = _pause(w)  # synchronous: returns once committed, the fetch still parked
        assert worker.is_alive() and not release.is_set()
        release.set()
        assert worker.join_ok() is True

    end = _end_state(w)
    assert fetches == [record("src-a").feed_url]  # no retry of src-a, src-b never fetched
    assert models.calls == []
    assert end.status == PAUSED
    assert end.job_status == JobStatus.SUCCEEDED.value
    assert end.job_summary == {"scout_request_id": w.rid, "outcome": STOPPED}
    assert end.raw_signals == 0 and end.opportunities == 0
    _measure("pause_during_live_fetch", pause_latency_ms=_ms(pause_s), fetch_requests=len(fetches))


# --------------------------------------------------------------------------- #
# The admission's lock mode
# --------------------------------------------------------------------------- #
def test_an_admission_waits_for_an_uncommitted_pause_and_then_refuses(world, monkeypatch):
    """The pause has written the row but not committed when the run reaches an admission
    (the post-fetch boundary). The admission's ``SELECT ... FOR SHARE`` waits on the
    pause's row lock and, once the pause commits, reads ``paused``: refused. With a lock
    that does not conflict with the pause's UPDATE (none, or ``FOR KEY SHARE``) it would
    read the old ``running`` row and admit work after the cut-off."""
    w = world
    sources = Sources(gate=True)
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    models = ModelCalls(monkeypatch)
    with w.factory() as pause_session, offline_llm():
        worker = _start_worker(w)
        assert sources.started.wait(JOIN_SECONDS)
        pause_pid = pause_session.execute(text("select pg_backend_pid()")).scalar_one()
        pause_scout_request(workspace_id=w.ws, request_id=w.rid, db=pause_session, ctx=w.ctx)
        sources.release.set()
        waiter = _await_short(lambda: _waiter_blocked_by(w, pause_pid), "the admission to wait")
        time.sleep(0.5)
        assert worker.is_alive() and models.calls == []
        pause_session.commit()
        assert worker.join_ok() is True

    end = _end_state(w)
    assert waiter["wait_event_type"] == "Lock" and waiter["blockers"] == [pause_pid]
    assert waiter["statement"].startswith("SELECT scout_requests.status, scout_req")
    assert models.calls == []
    assert end.status == PAUSED
    assert end.job_summary == {"scout_request_id": w.rid, "outcome": STOPPED}
    _measure("admission_waits_for_uncommitted_pause", wait_event=waiter["wait_event"])


# --------------------------------------------------------------------------- #
# The commit boundary and the mock / same-session meter
# --------------------------------------------------------------------------- #
def test_pause_before_the_terminal_write_discards_the_runs_findings(world, monkeypatch):
    """§6.2 Q-1 (replaces, on the durable path, #267's ``paused_during_run`` that kept the
    findings): every operation of the run was admitted and completed, but the pause
    commits before the run's terminal write. The guarded terminal UPDATE matches nothing,
    the run stops at its commit boundary and ALL its uncommitted writes are discarded. The
    request stays ``paused`` with its earlier stats; the accounting of the calls stands
    (mock path: one settled reservation and one usage row per call)."""
    w = world
    sources = Sources()
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    models = ModelCalls(monkeypatch)
    real_build = pipeline._build_opportunities

    def build_then_pause(*args, **kwargs):
        built = real_build(*args, **kwargs)
        _pause(w)  # commits: the run's transaction holds only FK key-share locks
        return built

    monkeypatch.setattr(pipeline, "_build_opportunities", build_then_pause)
    with offline_llm():
        assert _start_worker(w).join_ok() is True

    end, ledger = _end_state(w), _ledger(w)
    assert models.calls and all(t < w.marks["pause_committed"] for t in models.calls)
    assert end.status == PAUSED and end.last_run_at is None and not end.stats
    assert end.job_summary == {"scout_request_id": w.rid, "outcome": STOPPED}
    assert end.opportunities == 0 and end.raw_signals == 0
    assert ledger["reservations"] == ledger["usage_events"] == len(models.calls)
    assert ledger["reservations_reserved"] == 0 and ledger["periods"] == 1
    _measure("pause_before_terminal_write", model_calls=len(models.calls), ledger_end=ledger)


def test_a_mid_run_pause_on_the_mock_path_records_exactly_the_admitted_call(world, monkeypatch):
    """The mock provider is never reserved; its calls are recorded by the same-session
    ``record_settled`` (#276: ``preserve_rowcount``, so the usage row follows the
    reservation row on PostgreSQL). The pause commits while the first mock call is in
    flight: that call was admitted, so exactly one settled reservation row, one usage row
    and the period row are written; the next attempt is refused and records nothing. The
    run's findings are discarded."""
    w = world
    monkeypatch.setattr(pipeline, "fetch_signals", Sources())
    first, release = threading.Event(), threading.Event()
    calls: list[float] = []
    real_generate = MockLLMProvider.generate

    def generate(provider, *args, **kwargs):
        calls.append(time.monotonic())
        if len(calls) == 1:
            first.set()
            assert release.wait(JOIN_SECONDS)
        return real_generate(provider, *args, **kwargs)

    monkeypatch.setattr(MockLLMProvider, "generate", generate)
    with offline_llm():
        worker = _start_worker(w)
        assert first.wait(JOIN_SECONDS)
        pause_s = _pause(w)
        release.set()
        assert worker.join_ok() is True

    end, ledger = _end_state(w), _ledger(w)
    assert len(calls) == 1
    assert ledger == {
        "reservations": 1,
        "reservations_reserved": 0,
        "usage_events": 1,
        "periods": 1,
        "reserved_usd": Decimal(0),
        "settled_usd": Decimal(0),
    }
    assert end.status == PAUSED
    assert end.job_summary == {"scout_request_id": w.rid, "outcome": STOPPED}
    assert end.opportunities == 0 and end.raw_signals == 0
    _measure("mock_path_admitted_call", pause_latency_ms=_ms(pause_s), ledger_end=ledger)


# --------------------------------------------------------------------------- #
# No escape through Pause -> Resume (-> Run now); lease recovery; repeated pauses
# --------------------------------------------------------------------------- #
def test_pause_resume_run_now_the_old_run_admits_nothing_and_the_new_run_proceeds(
    world, monkeypatch
):
    w = world
    sources = FirstCallGate(gate=True)
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    models = ModelCalls(monkeypatch)
    with offline_llm():
        old = _start_worker(w)
        assert sources.started.wait(JOIN_SECONDS)
        _pause(w)
        _resume(w)
        new_job = _run_now(w)
        assert _start_worker(w, _runner(w)).join_ok() is True  # the new run, to completion
        new_calls = len(models.calls)
        assert old.is_alive()
        sources.release.set()
        assert old.join_ok() is True

    old_job, new = _job(w, w.job_id), _job(w, new_job)
    end = _end_state(w)
    assert old_job.result_summary == {"scout_request_id": w.rid, "outcome": STOPPED}
    assert new.status == JobStatus.SUCCEEDED.value
    assert new.payload["run_fence"] == 1 and "run_fence" not in (_job(w, w.job_id).payload)
    assert new.result_summary["outcome"] == "completed"
    assert end.status == "completed" and end.opportunities > 0
    assert len(models.calls) == new_calls  # the old run made no model call after release
    assert end.raw_signals == new.result_summary["scanned"]  # only the new run's signals
    _measure("pause_resume_run_now", old_outcome=STOPPED, new_outcome="completed")


def test_lease_recovery_after_a_pause_admits_nothing_and_does_not_overwrite_the_pause(
    world, monkeypatch
):
    """The run is parked; the customer pauses; the worker's lease then expires (a crash
    as far as the store can tell) and another worker recovers and re-claims the job. The
    recovered attempt's committed claim is refused (paused, fence moved): it skips without
    a fetch. The original attempt, released afterwards, is refused at its next admission
    and, having lost its lease, writes nothing; the pause stands."""
    w = world
    sources = FirstCallGate(gate=True)
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    models = ModelCalls(monkeypatch)
    with offline_llm():
        original = _start_worker(w)
        assert sources.started.wait(JOIN_SECONDS)
        _pause(w)
        with w.factory() as s:
            s.execute(
                update(Job)
                .where(Job.id == w.job_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(hours=1))
            )
            s.commit()
        assert _start_worker(w, _runner(w)).join_ok() is True  # recover + re-claim + skip
        recovered = _job(w, w.job_id)
        sources.release.set()
        assert original.join_ok() is True

    end = _end_state(w)
    assert recovered.status == JobStatus.SUCCEEDED.value
    assert recovered.result_summary == {
        "scout_request_id": w.rid,
        "outcome": "skipped_request_paused",
    }
    assert end.job_summary == recovered.result_summary  # not overwritten by the original
    assert len(sources.calls) == 1 and models.calls == []
    assert end.status == PAUSED and end.raw_signals == 0 and end.opportunities == 0
    _measure("lease_recovery_after_pause", recovered=recovered.result_summary["outcome"])


def test_repeated_pauses_concurrent_and_sequential_are_idempotent(world):
    w = world

    def pause_once() -> str:
        with w.factory() as s:
            out = pause_scout_request(workspace_id=w.ws, request_id=w.rid, db=s, ctx=w.ctx)
            s.commit()
            return out.status

    concurrent = [Background(pause_once) for _ in range(5)]
    for thread in concurrent:
        thread.start()
    statuses = [thread.join_ok() for thread in concurrent]
    statuses += [pause_once(), pause_once()]

    end = _end_state(w)
    with w.factory() as s:
        fence = s.get(ScoutRequest, w.rid).run_fence
    assert statuses == [PAUSED] * 7
    assert end.status == PAUSED and end.paused_audits == 7
    assert fence == 7  # every pause advanced the fence once; none was lost
    _measure("repeated_pauses", calls=7, fence=fence)


def test_earlier_findings_and_configuration_survive_a_stopped_run(world, monkeypatch):
    w = world
    monkeypatch.setattr(pipeline, "fetch_signals", Sources())
    with offline_llm():
        assert _start_worker(w).join_ok() is True
    with w.factory() as s:
        req = s.get(ScoutRequest, w.rid)
        before = (
            req.status,
            req.stats,
            req.last_run_at,
            req.keywords,
            req.source_types,
            req.name,
            req.resolved_market,
            req.location_id,
        )
    opportunities = _count(w, Opportunity, scout_request_id=w.rid)
    raw = _count(w, RawSignal, scout_request_id=w.rid)
    assert before[0] == "completed" and opportunities > 0

    sources = Sources(gate=True)
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    second = _run_now(w)
    with offline_llm():
        worker = _start_worker(w)
        assert sources.started.wait(JOIN_SECONDS)
        _pause(w)
        sources.release.set()
        assert worker.join_ok() is True

    with w.factory() as s:
        req = s.get(ScoutRequest, w.rid)
        after = (
            req.status,
            req.stats,
            req.last_run_at,
            req.keywords,
            req.source_types,
            req.name,
            req.resolved_market,
            req.location_id,
        )
    assert _job(w, second).result_summary["outcome"] == STOPPED
    assert after == (PAUSED, *before[1:])
    assert _count(w, Opportunity, scout_request_id=w.rid) == opportunities
    assert _count(w, RawSignal, scout_request_id=w.rid) == raw


# --------------------------------------------------------------------------- #
# Organization erasure (scenario 9)
# --------------------------------------------------------------------------- #
def _tenant_rows(w: World) -> dict:
    return {
        "organizations": _count(w, Organization, id=w.org),
        "workspaces": _count(w, Workspace, id=w.ws),
        "scout_requests": _count(w, ScoutRequest, workspace_id=w.ws),
        "raw_signals": _count(w, RawSignal, workspace_id=w.ws),
        "opportunities": _count(w, Opportunity, workspace_id=w.ws),
        "jobs": _count(w, Job, workspace_id=w.ws),
        "reservations": _count(w, LLMBudgetReservation, workspace_id=w.ws),
        "usage_events": _count(w, LLMUsageEvent, workspace_id=w.ws),
        "periods": _count(w, LLMBudgetPeriod, workspace_id=w.ws),
    }


def test_erasure_is_refused_while_a_run_is_blocked_and_deletes_nothing(world, monkeypatch):
    """9(a): the organization's job is running (parked mid-operation), so erasure answers
    409 ``organization_jobs_active`` and deletes nothing; the run then finishes normally."""
    w = world
    sources = Sources(gate=True)
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    with offline_llm():
        worker = _start_worker(w)
        assert sources.started.wait(JOIN_SECONDS)
        before = _tenant_rows(w)
        with w.factory() as s, pytest.raises(ConflictError) as refused:
            erase_organization(s, organization_id=w.org)
        assert refused.value.code == "organization_jobs_active"
        assert _tenant_rows(w) == before
        sources.release.set()
        assert worker.join_ok() is True

    end = _end_state(w)
    assert end.status == "completed" and end.opportunities > 0
    assert _tenant_rows(w)["organizations"] == 1


def test_a_late_attempt_after_erasure_recreates_nothing(world, monkeypatch):
    """9(b), the job path: the run is parked in its fetch; its lease expires and lease
    recovery dead-letters the job (no attempt left), so erasure is allowed and erases the
    organization. The stale attempt, released afterwards, is refused at its next admission
    (``request_gone``), loses its lease at completion and writes nothing: no request,
    finding, job, reservation, usage or period row is recreated."""
    w = world
    sources = Sources(gate=True)
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    models = ModelCalls(monkeypatch)
    with w.factory() as s:
        s.get(Job, w.job_id).max_attempts = 1
        s.commit()
    with offline_llm():
        stale = _start_worker(w)
        assert sources.started.wait(JOIN_SECONDS)
        with w.factory() as s:
            s.execute(
                update(Job)
                .where(Job.id == w.job_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(hours=1))
            )
            s.commit()
        assert _start_worker(w, _runner(w)).join_ok() is False  # recovery only: dead-letters
        assert _job(w, w.job_id).status == JobStatus.DEAD_LETTERED.value
        with w.factory() as s:
            erase_organization(s, organization_id=w.org)
            s.commit()
        sources.release.set()
        assert stale.join_ok() is True

    assert models.calls == []
    assert _tenant_rows(w) == dict.fromkeys(_tenant_rows(w), 0)


def test_late_ledger_and_terminal_callbacks_after_erasure_recreate_nothing(world):
    """9(b), the callbacks: a priced call was reserved for the tenant (committed, as the
    shared ledger does before a provider call); the organization is then erased. Its late
    settlement, a late admission, the commit-boundary write, the refusal writer, the
    terminal-failure hook and the status restore all find no row and write nothing; the
    #269 orphan counter records the settlement."""
    w = world
    metrics = InMemoryMetrics()
    configure_metrics(metrics)
    try:
        meter = LLMMeter(offline_settings(), session_factory=w.factory)
        with w.factory() as s:
            meter.bind(s, organization_id=w.org, workspace_id=w.ws, scout_request_id=w.rid)
        meter.before_attempt("anthropic", MODEL, wire_payload(MODEL))
        assert _tenant_rows(w)["reservations"] == 1
        with w.factory() as s:
            erase_organization(s, organization_id=w.org)
            s.commit()
        assert _tenant_rows(w) == dict.fromkeys(_tenant_rows(w), 0)

        meter.after_attempt(
            task="classify_signal", provider="anthropic", model=MODEL, error=LLMTimeoutError("x")
        )
        admission = RunAdmission(
            scout_request_id=w.rid,
            organization_id=w.org,
            workspace_id=w.ws,
            fence=0,
            session_factory=w.factory,
            job_id=w.job_id,
            worker_id="w-gone",
            attempt=1,
        )
        with pytest.raises(RunStopped) as gone:
            admission.admit("model")
        assert gone.value.outcome == REQUEST_GONE
        context = ExecutionContext.for_scout_request(organization_id=w.org, workspace_id=w.ws)
        now = datetime.now(UTC)
        with w.factory() as s:
            with pytest.raises(RunStopped) as gone_at_commit:
                admission.finish(s, stats={"scanned": 1}, now=now)
            assert gone_at_commit.value.outcome == REQUEST_GONE
            _record_refused_run(
                w.rid, context, SignalVolumeAboveBoundError(scanned=5, bound=3), run_fence=0
            )(s)
            settle_failed_scout_request(
                TerminalFailureContext(
                    db=s,
                    context=context,
                    payload={"scout_request_id": w.rid, "run_fence": 0},
                    job_id=w.job_id,
                    job_type=JobType.SCOUT_REQUEST_EXECUTE.value,
                    status=JobStatus.DEAD_LETTERED.value,
                    error_code=JobErrorCode.TRANSIENT,
                    attempt=1,
                    now=now,
                )
            )
            admission.claimed, admission.pre_claim_status = True, "queued"
            admission.restore_writer(now=now)(s)
            meter.flush(s)
            s.commit()

        assert _tenant_rows(w) == dict.fromkeys(_tenant_rows(w), 0)
        assert metrics.counter_value(LLM_SETTLEMENTS_ORPHANED_TOTAL, outcome="settle") == 1
    finally:
        configure_metrics(None)


# --------------------------------------------------------------------------- #
# S4: schedule continuity after Pause -> Resume (no catch-up is added)
# --------------------------------------------------------------------------- #
def _tick_outcomes(w: World) -> list[tuple[str, str]]:
    with w.factory() as s:
        rows = s.execute(
            select(Job.payload, Job.result_summary)
            .where(Job.scout_request_id == w.rid, Job.job_type == JobType.SCOUT_SCHEDULE_TICK.value)
            .order_by(Job.scheduled_for)
        ).all()
    return [(p["occurrence_at"], (r or {}).get("outcome", "pending")) for p, r in rows]


def _executions(w: World) -> list[tuple[int | None, str, str | None]]:
    with w.factory() as s:
        rows = s.execute(
            select(Job.payload, Job.status, Job.result_summary)
            .where(
                Job.scout_request_id == w.rid,
                Job.job_type == JobType.SCOUT_REQUEST_EXECUTE.value,
                Job.id != w.job_id,
            )
            .order_by(Job.created_at)
        ).all()
    return [(p.get("run_fence"), st, (r or {}).get("outcome")) for p, st, r in rows]


@pytest.mark.parametrize("order", ["fenced_job_first", "tick_first"])
def test_the_schedule_chain_survives_pause_resume_and_a_stale_fenced_job(world, monkeypatch, order):
    """Rapid Pause -> Resume leaves the occurrence's execute job fenced (stale) while the
    tick chain goes on. Two claim orders:

    * ``fenced_job_first`` (the store's FIFO order: the stale job became due first): the
      stale job is skipped, the next tick fans out a new run under the new fence; no
      occurrence is missed;
    * ``tick_first`` (the next tick is claimed while the stale job is still pending): the
      tick coalesces against it (that occurrence is missed, PROPOSED B7), the stale job is
      then skipped, and the following tick fans out normally.

    Either way the chain survives (no re-arming needed), the schedule stays enabled, and no
    catch-up run is created for a missed occurrence."""
    from app.core.config import get_settings
    from app.core.enums import ScheduleInterval
    from app.scouting_requests import schedules as sched
    from app.scouting_requests.models import ScoutSchedule

    w = world
    monkeypatch.setattr(get_settings(), "scout_scheduling_enabled", True)
    monkeypatch.setattr(pipeline, "fetch_signals", Sources())
    t0 = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)  # after the seed
    clock = [t0]
    runner = _runner(w, clock=lambda: clock[0])
    with offline_llm():
        assert _start_worker(w, runner).join_ok() is True  # the seeded run completes
        with w.factory() as s:
            req = s.get(ScoutRequest, w.rid)
            schedule = sched.create_schedule(
                s, request=req, interval=ScheduleInterval.DAILY, now=t0
            )
            s.commit()
            schedule_id = schedule.id
        day = timedelta(days=1)

        def poll_at(when) -> bool:
            clock[0] = when
            return _start_worker(w, runner).join_ok()

        assert poll_at(t0 + day) is True  # tick 1 fans out run E1 (fence 0)
        _pause(w)
        _resume(w)  # E1 is now stale: its fence (0) is not the request's (1)
        if order == "tick_first":
            with w.factory() as s:  # hold E1 back so tick 2 is claimed while it is pending
                s.execute(
                    update(Job)
                    .where(
                        Job.scout_request_id == w.rid,
                        Job.job_type == JobType.SCOUT_REQUEST_EXECUTE.value,
                        Job.status == JobStatus.PENDING.value,
                    )
                    .values(available_at=t0 + 2 * day + timedelta(hours=1))
                )
                s.commit()
        while poll_at(t0 + 2 * day + timedelta(hours=2)):
            pass  # drain everything due by then: tick 2 and E1 (in the order under test)
        while poll_at(t0 + 3 * day + timedelta(hours=2)):
            pass  # tick 3 and whatever it fanned out

    ticks, runs = _tick_outcomes(w), _executions(w)
    with w.factory() as s:
        schedule = s.get(ScoutSchedule, schedule_id)
        enabled, next_run = schedule.enabled, schedule.next_run_at
        status = s.get(ScoutRequest, w.rid).status
    outcomes = [o for _, o in ticks]
    assert enabled is True and next_run is not None and next_run > t0 + 3 * day  # alive
    assert outcomes[:3] == (
        ["fanned_out", "fanned_out", "fanned_out"]
        if order == "fenced_job_first"
        else ["fanned_out", "coalesced", "fanned_out"]
    )
    assert outcomes[3:] == ["pending"]  # the chain's next tick is seeded, nothing extra
    stale = runs[0]
    assert stale == (0, JobStatus.SUCCEEDED.value, "skipped_request_paused")
    fresh = runs[1:]
    assert fresh and all(f == (1, JobStatus.SUCCEEDED.value, "completed") for f in fresh)
    assert len(fresh) == (2 if order == "fenced_job_first" else 1)  # no catch-up run
    assert status == "completed"
    _measure(
        "schedule_continuity",
        order=order,
        tick_outcomes=outcomes,
        executions=runs,
        missed_occurrences=outcomes[:3].count("coalesced"),
    )
