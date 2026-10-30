"""The D-25 cooperative-pause candidate on SQLite (PROPOSED, contract §6).

What SQLite can pin of the candidate (the prompt pause itself is a PostgreSQL property: on
SQLite a pause during a run's write phase waits on the database write lock, up to
``busy_timeout``; see ``test_scout_pause_cooperative_postgres.py`` for the PostgreSQL
behaviour). Here, with the real worker, the real handler and the real routes:

* the production handler always runs ``pipeline._run`` under ``admitted_run``, with the
  job's fence and the job row's tenant;
* the claim is committed on its own: another connection sees ``running`` while the run
  is still fetching, and a pause committed then stops the run at the post-fetch boundary;
* the **model admission**: a pause that lands after the fetch boundary makes the first
  model attempt be refused -- no model call, no usage row, findings discarded;
* the pause route advances the fence (idempotent); the run route and the schedule tick
  record the fence on the job; a legacy job without one is fence 0; a malformed one is a
  validation failure;
* a failed attempt puts back the pre-claim status for its retry (§6.2 Q-2 (B)), and that
  restore is a no-op once another attempt owns the job;
* a final failure of an old job does not settle a newer run's request (fenced hook).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.core.enums import ScoutRequestStatus
from app.db import seed as seed_mod
from app.infra.vector import SignalVolumeAboveBoundError
from app.jobs import admission as admission_mod
from app.jobs import handlers, pipeline
from app.jobs.admission import RunAdmission
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.registry import TerminalFailureContext
from app.jobs.service import enqueue_job
from app.jobs.status import JobErrorCode, JobStatus, JobType
from app.llm.base import LLMTimeoutError as promise_llm_timeout
from app.llm.models import LLMUsageEvent
from app.scouting_requests import schedules as sched
from app.scouting_requests.models import ScoutRequest
from app.signals.models import RawSignal
from app.tests import test_scout_pause_promise as promise
from app.tests._auth2_support import live_bearer
from app.tests._llm_offline import offline_llm
from app.tests.test_scout_pause_promise import (
    _NOW,
    API,
    PAUSED,
    QUEUED,
    _fixture_fetch,
    _queue,
    _runner,
    _scheduled_request,
    _tick,
)

#: The harness's fixtures (a seeded, file-backed SQLite per module; restore after commits).
factory = promise.factory
s = promise.s
committed = promise.committed
client = promise.client
counted = promise.counted
flag_on = promise.flag_on

STOPPED = "stopped_request_paused"
RUNNING = ScoutRequestStatus.RUNNING.value


def _commit_pause(factory, rid: str) -> None:
    """What the pause route writes (status and fence), from a session of its own."""
    with factory() as other:
        other.execute(
            update(ScoutRequest)
            .where(ScoutRequest.id == rid)
            .values(status=PAUSED, run_fence=ScoutRequest.run_fence + 1)
        )
        other.commit()


def _raw_signals(factory, rid: str) -> int:
    with factory() as session:
        return session.scalar(
            select(func.count()).select_from(RawSignal).where(RawSignal.scout_request_id == rid)
        )


def _job(factory, job_id: str) -> Job:
    with factory() as session:
        return session.get(Job, job_id)


def _poll(factory, worker_id: str = "w-coop") -> bool:
    with offline_llm():
        return _runner(factory, clock=lambda: _NOW + timedelta(minutes=1)).poll_once(
            worker_id=worker_id
        )


# --------------------------------------------------------------------------- #
# The opt-in
# --------------------------------------------------------------------------- #
def test_the_production_handler_always_runs_the_pipeline_under_admitted_run(
    factory, committed, monkeypatch
):
    rid = seed_mod.sid("scout", "dallas")
    job_id = _queue(factory, rid)
    seen = {}

    def spy(db, scout_request_id, context=None):
        seen["admission"] = admission_mod.current_admission()
        return {"scout_request_id": scout_request_id, "outcome": "completed"}

    monkeypatch.setattr(handlers, "_run", spy)
    assert _poll(factory)

    admission = seen["admission"]
    job = _job(factory, job_id)
    assert isinstance(admission, RunAdmission)
    assert admission.scout_request_id == rid and admission.job_id == job_id
    assert admission.fence == job.payload["run_fence"]
    assert (admission.organization_id, admission.workspace_id) == (
        job.organization_id,
        job.workspace_id,
    )
    assert admission_mod.current_admission() is None  # scoped to the run


# --------------------------------------------------------------------------- #
# The committed claim, the post-fetch boundary and the model admission
# --------------------------------------------------------------------------- #
def test_the_claim_is_committed_and_a_pause_during_the_fetch_stops_the_run(
    factory, committed, monkeypatch
):
    rid = seed_mod.sid("scout", "london")
    job_id = _queue(factory, rid)
    raw_before = _raw_signals(factory, rid)
    seen = {}
    real_fetch = _fixture_fetch()

    def fetch_then_pause(**kwargs):
        with factory() as other:  # another connection: sees only what is committed
            seen["status_during_fetch"] = other.get(ScoutRequest, rid).status
        _commit_pause(factory, rid)  # the run holds no lock yet: this commits
        return real_fetch(**kwargs)

    monkeypatch.setattr(pipeline, "fetch_signals", fetch_then_pause)
    model_calls = []
    monkeypatch.setattr(promise.MockLLMProvider, "generate", lambda *a, **k: model_calls.append(1))
    assert _poll(factory)

    job = _job(factory, job_id)
    assert seen["status_during_fetch"] == RUNNING
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.result_summary == {"scout_request_id": rid, "outcome": STOPPED}
    assert model_calls == []
    with factory() as session:
        assert session.get(ScoutRequest, rid).status == PAUSED
        assert _raw_signals(factory, rid) == raw_before  # the run's writes were discarded


def test_the_first_model_attempt_is_refused_after_a_pause_that_lands_after_the_fetch(
    factory, committed, counted, monkeypatch
):
    """The model admission on SQLite: the pause commits after the post-fetch boundary
    (in the takedown re-check, before the run's first write). The run then stores its
    first signal and reaches its first model attempt, whose admission is refused: no model
    call is made, no usage row is written, and the run's writes are discarded."""
    rid = seed_mod.sid("scout", "lagos")
    job_id = _queue(factory, rid)
    raw_before = _raw_signals(factory, rid)
    real_recheck = pipeline.without_taken_down

    def pause_then_recheck(db, signals):
        _commit_pause(factory, rid)
        return real_recheck(db, signals)

    monkeypatch.setattr(pipeline, "without_taken_down", pause_then_recheck)
    assert _poll(factory)

    job = _job(factory, job_id)
    assert counted["fetch"] == 1 and counted["model"] == 0
    assert job.result_summary == {"scout_request_id": rid, "outcome": STOPPED}
    with factory() as session:
        assert session.get(ScoutRequest, rid).status == PAUSED
        assert _raw_signals(factory, rid) == raw_before  # the run's writes were discarded
        assert (
            session.scalar(
                select(func.count())
                .select_from(LLMUsageEvent)
                .where(LLMUsageEvent.job_id == job_id)
            )
            == 0
        )


# --------------------------------------------------------------------------- #
# The fence: pause route, run route, tick, legacy and malformed payloads
# --------------------------------------------------------------------------- #
def test_the_pause_route_advances_the_fence_and_is_idempotent(client, factory):
    rid = seed_mod.sid("scout", "nairobi")
    auth = {"Authorization": f"Bearer {live_bearer(seed_mod.sid('user'))}"}
    base = f"{API}/workspaces/{seed_mod.sid('ws')}/scout-requests/{rid}"
    with factory() as session:
        before = session.get(ScoutRequest, rid).run_fence
    replies = [client.post(base + "/pause", headers=auth) for _ in range(3)]
    assert [r.status_code for r in replies] == [200, 200, 200]
    assert [r.json()["status"] for r in replies] == [PAUSED] * 3
    assert "run_fence" not in replies[0].json()  # the response schema is unchanged
    with factory() as session:
        assert session.get(ScoutRequest, rid).run_fence == before + 3


def test_the_run_route_records_the_request_fence_on_its_job(client, factory):
    rid = seed_mod.sid("scout", "nairobi")
    auth = {"Authorization": f"Bearer {live_bearer(seed_mod.sid('user'))}"}
    base = f"{API}/workspaces/{seed_mod.sid('ws')}/scout-requests/{rid}"
    assert client.post(base + "/pause", headers=auth).status_code == 200
    assert client.post(base + "/resume", headers=auth).status_code == 200
    job_id = client.post(base + "/run", headers=auth).json()["stats"]["job_id"]
    with factory() as session:
        fence = session.get(ScoutRequest, rid).run_fence
        assert session.get(Job, job_id).payload == {"scout_request_id": rid, "run_fence": fence}
    assert fence >= 1


def test_the_schedule_tick_records_the_request_fence_on_its_run(s):
    req, schedule, first = _scheduled_request(s, "dallas")
    req.run_fence = 4
    s.flush()
    assert _tick(s, schedule, first)["run_enqueued"] is True
    run = s.scalar(
        select(Job).where(
            Job.scout_request_id == req.id, Job.job_type == JobType.SCOUT_REQUEST_EXECUTE.value
        )
    )
    assert run.payload["run_fence"] == 4 and run.payload["trigger"] == sched.SCHEDULED_TRIGGER


def _enqueue_raw(factory, rid: str, payload: dict) -> str:
    with factory() as session:
        req = session.get(ScoutRequest, rid)
        req.status = QUEUED
        job = enqueue_job(
            session,
            job_type=JobType.SCOUT_REQUEST_EXECUTE,
            context=ExecutionContext.for_scout_request(
                organization_id=req.organization_id,
                workspace_id=req.workspace_id,
                location_id=req.location_id,
                request_id=rid,
            ),
            payload=payload,
            location_id=req.location_id,
            scout_request_id=rid,
            now=_NOW,
        )
        session.commit()
        return job.id


def test_a_legacy_job_without_a_fence_runs_as_fence_zero(factory, committed):
    rid = seed_mod.sid("scout", "dallas")
    job_id = _enqueue_raw(factory, rid, {"scout_request_id": rid})
    assert _poll(factory)
    assert _job(factory, job_id).result_summary["outcome"] == "completed"


def test_a_legacy_job_is_fenced_once_its_request_has_been_paused(factory, committed):
    rid = seed_mod.sid("scout", "dallas")
    job_id = _enqueue_raw(factory, rid, {"scout_request_id": rid})
    _commit_pause(factory, rid)
    with factory() as session:  # resumed: only the fence remembers the pause
        session.get(ScoutRequest, rid).status = ScoutRequestStatus.DRAFT.value
        session.commit()
    assert _poll(factory)
    assert _job(factory, job_id).result_summary["outcome"] == "skipped_request_paused"


@pytest.mark.parametrize("bad", ["1", -1, True, 1.5])
def test_a_malformed_fence_is_a_validation_failure(factory, committed, bad):
    rid = seed_mod.sid("scout", "dallas")
    job_id = _enqueue_raw(factory, rid, {"scout_request_id": rid, "run_fence": bad})
    assert _poll(factory)
    job = _job(factory, job_id)
    assert job.status == JobStatus.FAILED.value
    assert job.last_error_code == JobErrorCode.VALIDATION.value


# --------------------------------------------------------------------------- #
# After a failed attempt (§6.2 Q-2 (B)) and the fenced terminal-failure hook
# --------------------------------------------------------------------------- #
def test_a_failed_attempt_puts_back_the_pre_claim_status_for_its_retry(
    factory, committed, monkeypatch
):
    rid = seed_mod.sid("scout", "nairobi")
    monkeypatch.setattr(pipeline, "fetch_signals", _fixture_fetch(fail=RuntimeError("down")))
    job_id = _queue(factory, rid)
    assert _poll(factory)
    assert _job(factory, job_id).status == JobStatus.RETRY_WAIT.value
    with factory() as session:
        # The claim had committed ``running``; the failed attempt's restore put back
        # ``queued`` (what the request showed before the claim), as before the candidate.
        assert session.get(ScoutRequest, rid).status == QUEUED


def test_the_status_restore_is_a_no_op_once_another_attempt_owns_the_job(s):
    req = s.get(ScoutRequest, seed_mod.sid("scout", "dallas"))
    req.status = RUNNING
    job = enqueue_job(
        s,
        job_type=JobType.SCOUT_REQUEST_EXECUTE,
        context=ExecutionContext.for_scout_request(
            organization_id=req.organization_id, workspace_id=req.workspace_id
        ),
        payload={"scout_request_id": req.id, "run_fence": req.run_fence},
        scout_request_id=req.id,
        now=_NOW,
    )
    job.status, job.worker_id, job.attempt_count = JobStatus.RUNNING.value, "w-new", 2
    s.flush()
    stale = RunAdmission(
        scout_request_id=req.id,
        organization_id=req.organization_id,
        workspace_id=req.workspace_id,
        fence=req.run_fence,
        session_factory=None,
        job_id=job.id,
        worker_id="w-old",
        attempt=1,
        claimed=True,
        pre_claim_status=QUEUED,
    )
    stale.restore_writer(now=_NOW)(s)
    s.refresh(req)
    assert req.status == RUNNING  # the lease moved on: the stale restore wrote nothing

    owner = RunAdmission(
        scout_request_id=req.id,
        organization_id=req.organization_id,
        workspace_id=req.workspace_id,
        fence=req.run_fence,
        session_factory=None,
        job_id=job.id,
        worker_id="w-new",
        attempt=2,
        claimed=True,
        pre_claim_status=QUEUED,
    )
    owner.restore_writer(now=_NOW)(s)
    s.refresh(req)
    assert req.status == QUEUED  # control: the owning attempt's restore applies


def test_a_final_failure_of_an_old_job_does_not_settle_a_newer_run(s):
    req = s.get(ScoutRequest, seed_mod.sid("scout", "london"))
    req.status, req.run_fence = QUEUED, 1  # Pause -> Resume -> Run now: the newer job's
    s.flush()

    def hook(payload_fence: int) -> None:
        handlers.settle_failed_scout_request(
            TerminalFailureContext(
                db=s,
                context=ExecutionContext.for_scout_request(
                    organization_id=req.organization_id, workspace_id=req.workspace_id
                ),
                payload={"scout_request_id": req.id, "run_fence": payload_fence},
                job_id="old-job",
                job_type=JobType.SCOUT_REQUEST_EXECUTE.value,
                status=JobStatus.DEAD_LETTERED.value,
                error_code=JobErrorCode.TRANSIENT,
                attempt=5,
                now=_NOW,
            )
        )
        s.refresh(req)

    hook(0)
    assert req.status == QUEUED  # the old job (fence 0) leaves the newer run's request
    hook(1)
    assert req.status == ScoutRequestStatus.FAILED.value  # control: its own fence settles


def _rival_case(s, case: str):
    """A request whose claim was made by THIS attempt (fence 0, ``queued`` before it) and
    then superseded as ``case`` says; returns the request and the stale attempt."""
    req = s.get(ScoutRequest, seed_mod.sid("scout", "lagos"))
    context = ExecutionContext.for_scout_request(
        organization_id=req.organization_id, workspace_id=req.workspace_id
    )
    mine = enqueue_job(
        s,
        job_type=JobType.SCOUT_REQUEST_EXECUTE,
        context=context,
        payload={"scout_request_id": req.id, "run_fence": 0},
        scout_request_id=req.id,
        now=_NOW,
    )
    mine.status, mine.worker_id, mine.attempt_count = JobStatus.RUNNING.value, "w-mine", 1
    req.run_fence = 0
    states = {
        "newer_pause": (PAUSED, 1),
        "resume": (ScoutRequestStatus.DRAFT.value, 1),
        "newer_claim_new_fence": (RUNNING, 1),
        "newer_claim_same_fence_other_job": (RUNNING, 0),
        "completed_by_another_run": (ScoutRequestStatus.COMPLETED.value, 0),
        "failed_by_another_run": (ScoutRequestStatus.FAILED.value, 0),
    }
    req.status, req.run_fence = states[case]
    if case == "newer_claim_same_fence_other_job":
        rival = enqueue_job(
            s,
            job_type=JobType.SCOUT_REQUEST_EXECUTE,
            context=context,
            payload={"scout_request_id": req.id, "run_fence": 0},
            scout_request_id=req.id,
            idempotency_key="rival",
            now=_NOW,
        )
        rival.status, rival.worker_id, rival.attempt_count = JobStatus.RUNNING.value, "w-rival", 1
    s.flush()
    stale = RunAdmission(
        scout_request_id=req.id,
        organization_id=req.organization_id,
        workspace_id=req.workspace_id,
        fence=0,
        session_factory=None,
        job_id=mine.id,
        worker_id="w-mine",
        attempt=1,
        claimed=True,
        pre_claim_status=QUEUED,
    )
    return req, stale


@pytest.mark.parametrize(
    "case",
    [
        "newer_pause",
        "resume",
        "newer_claim_new_fence",
        "newer_claim_same_fence_other_job",
        "completed_by_another_run",
        "failed_by_another_run",
    ],
)
def test_the_status_restore_never_overwrites_a_newer_state(s, case):
    """§6.2 Q-2 (B), PROPOSED: the restore after a failed attempt is conditional on run
    identity (job, worker, attempt), the run fence, lease ownership and no rival active
    execute job. It must leave every newer state exactly as it is."""
    req, stale = _rival_case(s, case)
    before = (req.status, req.run_fence)
    stale.restore_writer(now=_NOW)(s)
    s.refresh(req)
    assert (req.status, req.run_fence) == before


# --------------------------------------------------------------------------- #
# S1: one admission never authorizes the rest of a run
# --------------------------------------------------------------------------- #
def _record_checks(monkeypatch, events: list) -> None:
    real_check = RunAdmission.check

    def check(self, session, operation):
        events.append(("admit", operation))
        return real_check(self, session, operation)

    monkeypatch.setattr(RunAdmission, "check", check)


def test_every_model_attempt_and_retry_passes_its_own_admission(factory, committed, monkeypatch):
    rid = seed_mod.sid("scout", "dallas")
    job_id = _queue(factory, rid)
    events: list = []
    _record_checks(monkeypatch, events)
    real_generate = promise.MockLLMProvider.generate

    def generate(provider, *args, **kwargs):
        events.append(("send", "model"))
        if sum(1 for e in events if e == ("send", "model")) == 1:
            raise promise_llm_timeout("first attempt times out: a retry follows")
        return real_generate(provider, *args, **kwargs)

    monkeypatch.setattr(promise.MockLLMProvider, "generate", generate)
    assert _poll(factory)

    assert _job(factory, job_id).result_summary["outcome"] == "completed"
    sends = [i for i, e in enumerate(events) if e == ("send", "model")]
    assert len(sends) >= 2  # the timed-out attempt and its retry, at least
    # Every model send, the retry included, directly follows an admission of its own.
    assert all(events[i - 1] == ("admit", "model") for i in sends)
    assert sum(1 for e in events if e == ("admit", "model")) == len(sends)
    assert events.index(("admit", "post_fetch")) < sends[0]


def test_every_live_fetch_attempt_passes_its_own_admission(factory, committed, monkeypatch):
    from app.connectors import live as live_mod
    from app.connectors.base import FailureKind
    from app.connectors.feedcache import FeedCache
    from app.connectors.live import LiveRssConnector
    from app.connectors.retry import ConnectorFetchError
    from app.tests.test_live_rss_connector import record, target

    rid = seed_mod.sid("scout", "dallas")
    job_id = _queue(factory, rid)
    events: list = []
    _record_checks(monkeypatch, events)

    class FailingFetcher:
        def fetch(self, url, *, allowed_hosts, validators=None):
            events.append(("send", "fetch"))
            raise ConnectorFetchError(FailureKind.NETWORK, "fake network failure")

    connector = LiveRssConnector(
        [target(record("src-a")), target(record("src-b"))],
        fetcher=FailingFetcher(),
        cache=FeedCache(),
        sleep=lambda _s: None,
        max_attempts=2,
    )
    monkeypatch.setattr(live_mod, "live_connector_for", lambda **_kw: connector)
    assert _poll(factory)

    sends = [i for i, e in enumerate(events) if e == ("send", "fetch")]
    assert len(sends) == 4  # 2 sources x 2 attempts (a retry each)
    assert all(events[i - 1] == ("admit", "fetch") for i in sends)
    assert events[sends[-1] + 1] == ("admit", "post_fetch")
    assert _job(factory, job_id).result_summary["outcome"] == "completed"


def test_the_refusal_writer_of_an_old_run_leaves_a_newer_run_alone(s):
    """Pause -> Resume -> Run now: the newer run (fence 1) owns ``running``. The old run's
    refusal (fence 0) writes neither ``failed`` nor its stats."""
    req = s.get(ScoutRequest, seed_mod.sid("scout", "nairobi"))
    req.status, req.run_fence = RUNNING, 1
    s.flush()
    stats_before = req.stats
    context = ExecutionContext.for_scout_request(
        organization_id=req.organization_id, workspace_id=req.workspace_id
    )
    handlers._record_refused_run(
        req.id, context, SignalVolumeAboveBoundError(scanned=5, bound=3), run_fence=0
    )(s)
    s.refresh(req)
    assert (req.status, req.stats) == (RUNNING, stats_before)
    handlers._record_refused_run(
        req.id, context, SignalVolumeAboveBoundError(scanned=5, bound=3), run_fence=1
    )(s)
    s.refresh(req)
    assert req.status == ScoutRequestStatus.FAILED.value and req.stats["refused"] is True


def test_the_pause_is_committed_before_it_is_acknowledged(factory, committed):
    """``get_db`` commits in its teardown, which FastAPI runs after the response is sent.
    The pause route therefore commits before answering: with a database dependency whose
    teardown never commits, the acknowledged pause is still durable."""
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import app

    rid = seed_mod.sid("scout", "dallas")
    with factory() as session:
        token = live_bearer(seed_mod.sid("user"), db=session)
        fence = session.get(ScoutRequest, rid).run_fence
        session.commit()

    def never_commits():
        session = factory()
        try:
            yield session
        finally:
            session.rollback()
            session.close()

    app.dependency_overrides[get_db] = never_commits
    try:
        reply = TestClient(app).post(
            f"{API}/workspaces/{seed_mod.sid('ws')}/scout-requests/{rid}/pause",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert reply.status_code == 200 and reply.json()["status"] == PAUSED
    with factory() as session:
        request = session.get(ScoutRequest, rid)
        assert (request.status, request.run_fence) == (PAUSED, fence + 1)
