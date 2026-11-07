"""D-25 execution controls (O1 ownership at admission, O2 owner + K = 1) on PostgreSQL.

PROPOSED (contract D-25-EXECUTION-CONTROL-CONTRACT-01 §3/§8); nothing here is a ruling. The
harness of ``test_scout_pause_postgres`` (separate threads and connections, the real worker,
routes and Alembic schema; offline fakes only). A sampler asserts the bound at every sample:
at most ONE unresolved admission and at most ONE transmission in progress per Scout.

Gate: ``require_postgres`` (fails, never skips, under ``CI=true``).
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.connectors import live as live_mod
from app.connectors.base import FailureKind
from app.connectors.feedcache import FeedCache
from app.connectors.live import LiveRssConnector
from app.connectors.retry import ConnectorFetchError
from app.core.config import Settings
from app.jobs import pipeline
from app.jobs.admission import RunAdmission
from app.jobs.models import Job, JobEvent
from app.jobs.reconcile import UNKNOWN_END, list_unresolved, resolve
from app.jobs.schemas import JobOut
from app.jobs.status import JobError, JobErrorCode, JobStatus
from app.jobs.store import DurableJobStore, JobLeaseLostError
from app.jobs.worker import JobRunner
from app.llm.base import LLMTimeoutError, LLMUsage
from app.llm.mock import MockLLMProvider
from app.llm.models import LLMBudgetReservation, LLMUsageEvent
from app.llm.service import LLMService
from app.privacy.erasure import erase_organization
from app.scouting_requests.models import ScoutAdmission, ScoutRequest
from app.scouting_requests.routes import (
    get_scout_request,
    resume_scout_request,
    run_scout_request_endpoint,
)
from app.tests import test_scout_pause_postgres as harness
from app.tests._auth2_support import require_postgres
from app.tests._llm_offline import offline_settings
from app.tests._llm_reserve_child import KEY, MODEL
from app.tests.test_live_rss_connector import record, target
from app.tests.test_scout_pause_postgres import (
    JOIN_SECONDS,
    MARKET,
    Background,
    Sources,
    World,
    _end_state,
    _pause,
    _runner,
    _start_worker,
)

template_url = harness.template_url
world = harness.world


@pytest.fixture(autouse=True)
def _postgres_required() -> None:
    require_postgres()


class Provider:
    """A priced fake (the mock's output reported as a priced Anthropic call; nothing is
    sent). ``park``: call numbers that wait for ``release``; ``timeout``: call numbers that
    raise ``LLMTimeoutError`` (an unknown outcome). Tracks sends in flight."""

    name = "anthropic"
    model = MODEL

    def __init__(self, *, park=(), timeout=()):
        self._mock = MockLLMProvider(model="mock-1", seed=offline_settings().llm_mock_seed)
        self.park, self.timeout = set(park), set(timeout)
        self.parked, self.release = threading.Event(), threading.Event()
        self.calls: list[float] = []
        self.inflight = self.max_inflight = 0
        self._lock = threading.Lock()

    def prepare(self, request):
        from app.llm.providers_real import AnthropicProvider

        return AnthropicProvider(KEY, MODEL, 5).prepare(request)

    def generate(self, request, prompt_name, prompt_version):
        with self._lock:
            self.calls.append(time.monotonic())
            n = len(self.calls)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if n in self.park:
                self.parked.set()
                assert self.release.wait(JOIN_SECONDS)
            if n in self.timeout:
                raise LLMTimeoutError("fake read timeout after the request was sent")
            out = self._mock.generate(request, prompt_name, prompt_version)
            return out.model_copy(
                update={
                    "provider": self.name,
                    "model": self.model,
                    "usage": LLMUsage(input_tokens=100, output_tokens=50),
                }
            )
        finally:
            with self._lock:
                self.inflight -= 1


def _use(monkeypatch, provider) -> None:
    monkeypatch.setattr(pipeline, "fetch_signals", Sources())
    monkeypatch.setattr("app.llm.service.llm_service",
        LLMService(provider, sleep=lambda _s: None, settings=offline_settings()),
    )


def _unresolved(w: World) -> int:
    with w.observer.connect() as c:
        return c.execute(
            text(
                "select count(*) from scout_admissions where scout_request_id=:r"
                " and resolved_at is null"
            ),
            {"r": w.rid},
        ).scalar_one()


class Sampler(threading.Thread):
    """Samples the bound every 5 ms: unresolved admissions and sends in flight per Scout."""

    def __init__(self, w: World, provider: Provider):
        super().__init__(daemon=True)
        self.w, self.provider = w, provider
        self.stop, self.max_unresolved, self.samples = threading.Event(), 0, 0
        self.start()

    def run(self):
        while not self.stop.is_set():
            self.max_unresolved = max(self.max_unresolved, _unresolved(self.w))
            self.samples += 1
            time.sleep(0.005)

    def result(self):
        self.stop.set()
        self.join(5)
        return {
            "max_unresolved": self.max_unresolved,
            "max_inflight": self.provider.max_inflight,
            "samples": self.samples,
        }


def _expire_lease(w: World, job_id: str) -> None:
    with w.factory() as s:
        s.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(hours=1))
        )
        s.commit()


def _ledger(w: World) -> dict:
    with w.factory() as s:
        return {
            "admissions": s.scalar(
                select(func.count())
                .select_from(ScoutAdmission)
                .where(ScoutAdmission.scout_request_id == w.rid)
            ),
            "reservations": s.scalar(
                select(func.count())
                .select_from(LLMBudgetReservation)
                .where(LLMBudgetReservation.workspace_id == w.ws)
            ),
            "usage": s.scalar(
                select(func.count())
                .select_from(LLMUsageEvent)
                .where(LLMUsageEvent.workspace_id == w.ws)
            ),
        }


def _job_row(w: World) -> dict:
    """The job row as the assertions and the deadline diagnostics report it."""
    with w.factory() as s:
        job = s.get(Job, w.job_id)
        return {
            "status": job.status,
            "attempt_count": job.attempt_count,
            "worker_id": job.worker_id,
            "lease_expires_at": job.lease_expires_at,
            "available_at": job.available_at,
            "last_error": (job.last_error_code, job.last_error_summary),
        }


# --------------------------------------------------------------------------- #
# §6.1-3: a zombie after lease expiry, repeated recoveries, the bound
# --------------------------------------------------------------------------- #
#: Interval of the predicate polls below (seconds). Every wait ends on an observed
#: condition or on its deadline, which fails the test; none is a timed pause.
POLL_SECONDS = 0.02


def _expire_then_heartbeat(w: World) -> dict:
    """A test-local trigger: the test's expiry write, then the current owner's heartbeat
    exactly as its heartbeat thread issues it (``DurableJobStore.heartbeat``, fenced by the
    credential the row carries: the owner's own). Both commit in ONE transaction, so nothing
    reads the lapsed lease in between: for the recovery poll that follows, a heartbeat landed
    between the expiry and its scan. Returns the lease after the expiry and after the beat, and
    the time just after the commit."""
    with w.factory() as s:
        s.execute(
            update(Job)
            .where(Job.id == w.job_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(hours=1))
        )
        job = s.get(Job, w.job_id)
        expired = job.lease_expires_at
        DurableJobStore().heartbeat(
            s, job, worker_id=job.worker_id, lease_token=job.lease_token, lease_seconds=30.0
        )
        s.commit()
        return {
            "expired": expired,
            "renewed": job.lease_expires_at,
            "status": job.status,
            "at": datetime.now(UTC),
        }


class ShareHeldStore(DurableJobStore):
    """A test-local trigger: holds ``FOR SHARE`` on the job row, from a session of its own,
    across this poll's lease recovery. That is the lock a waiting attempt's admission read
    (``RunAdmission.check``) takes on every try, so the recovery's ``FOR UPDATE SKIP LOCKED``
    skips the row and the poll finds nothing to run."""

    def __init__(self, w: World):
        self.w = w
        self.recovered: list[int] = []

    def recover_expired_leases(self, db, **kwargs):
        with self.w.engine.connect() as conn, conn.begin():
            conn.execute(text("select id from jobs where id = :j for share"), {"j": self.w.job_id})
            recovered = super().recover_expired_leases(db, **kwargs)
        self.recovered.append(recovered)
        return recovered


class AdmissionWaits:
    """Records each attempt an external admission refused on K = 1.

    ``RunAdmission._blocking`` runs only after such a refusal, right before the attempt
    waits (holding no lock) behind the Scout's outstanding admission. "Attempt N was refused
    at its model admission" therefore means attempt N claimed the Scout and is waiting."""

    def __init__(self, monkeypatch):
        self.refused: list[tuple[int, str]] = []
        real = RunAdmission._blocking

        def blocking(admission, operation):
            self.refused.append((admission.attempt, operation))
            return real(admission, operation)

        monkeypatch.setattr(RunAdmission, "_blocking", blocking)

    def waiting(self, attempt: int) -> bool:
        return (attempt, "model") in self.refused


def _recover_to(
    w: World, waits: AdmissionWaits, *, attempt: int, runner, expire
) -> tuple[Background, int]:
    """One recovery: expire the lease, then poll until attempt ``attempt`` is running and
    waiting at its model admission. Returns that attempt's poll and the idle polls' count.

    One poll is not always enough (FU-ZOMBIE-POLL). ``poll_once`` returns False, having done
    nothing, when its ``FOR UPDATE SKIP LOCKED`` recovery meets a waiting attempt's
    ``FOR SHARE`` on the job row, when the running attempt's heartbeat renewed the lease
    between the expiry and the scan, or when that attempt saw its own lease lapse first and
    failed itself to ``retry_wait`` (due again after its backoff). Such a poll is re-issued
    after a fresh expiry. A poll that claims the job keeps running: it is the new attempt.
    The deadline fails the test with what it saw; nothing falls through."""
    deadline = time.monotonic() + JOIN_SECONDS
    polls, idle, poll = 0, 0, None
    while True:
        row = _job_row(w)
        if poll is not None and row["attempt_count"] == attempt and waits.waiting(attempt):
            return poll, idle
        if poll is not None and not poll.is_alive():
            if poll.join_ok() is not False:  # re-raises the poll's error, if any
                raise AssertionError(
                    f"recovery to attempt {attempt}: a poll ran a job to completion while the"
                    f" original operation was outstanding; job {row}; refused {waits.refused}"
                )
            idle, poll = idle + 1, None
        if time.monotonic() > deadline:
            raise AssertionError(
                f"recovery to attempt {attempt}: not running and waiting at its model"
                f" admission after {JOIN_SECONDS} s ({polls} polls, {idle} idle);"
                f" job {row}; refused {waits.refused}"
            )
        if poll is None:
            polls += 1
            expire(attempt - 1, polls)
            poll = _start_worker(w, runner(attempt - 1, polls))
        time.sleep(POLL_SECONDS)


def _zombie_recoveries(w: World, monkeypatch, *, runner=None, expire=None) -> dict:
    """The §6.1-3 choreography. ``runner(recovery, poll)`` builds each poll's runner and
    ``expire(recovery, poll)`` writes each expiry (trigger seams). Returns the idle polls per
    recovery."""
    runner = runner or (lambda _recovery, _poll: _runner(w))
    expire = expire or (lambda _recovery, _poll: _expire_lease(w, w.job_id))
    provider = Provider(park={1})
    _use(monkeypatch, provider)
    waits = AdmissionWaits(monkeypatch)
    sampler = Sampler(w, provider)
    workers = [_start_worker(w)]
    assert provider.parked.wait(JOIN_SECONDS)
    idle = {}
    for recovery in (1, 2, 3):  # three recoveries while the original operation is outstanding
        poll, idle[recovery] = _recover_to(
            w, waits, attempt=recovery + 1, runner=runner, expire=expire
        )
        workers.append(poll)
    assert len(provider.calls) == 1  # no recovery admitted a model call
    provider.release.set()
    for worker in workers:
        worker.join_ok()
    bound = sampler.result()

    with w.factory() as s:
        job = s.get(Job, w.job_id)
        assert job.attempt_count == 4  # the original plus three recoveries
        assert job.status == JobStatus.SUCCEEDED.value
        assert job.result_summary["outcome"] == "completed"
    assert bound["max_unresolved"] <= 1 and bound["max_inflight"] <= 1
    assert _unresolved(w) == 0
    ledger = _ledger(w)
    calls = len(provider.calls)
    assert ledger["admissions"] == calls and ledger["usage"] == calls
    assert ledger["reservations"] == calls
    return idle


def test_a_zombie_admits_nothing_and_repeated_recoveries_keep_one_unresolved_admission(
    world, monkeypatch
):
    """The original attempt is parked inside an admitted model call when its lease lapses.
    Three successive recoveries re-claim the job; each takes ownership from the lapsed owner
    and waits (no lock held) at its first model admission behind the zombie's outstanding
    admission (K = 1), and becomes a stale zombie itself at the next recovery. At every
    sample: unresolved <= 1 and sends in flight <= 1. Released, the original records its KNOWN
    end (resolving its admission) and admits nothing more; the stale waiters stop; the newest
    attempt runs the Scout to completion. The original's reservation was released at the
    first re-claim; its late settlement still writes exactly one usage row."""
    _zombie_recoveries(world, monkeypatch)


def test_the_zombie_recoveries_survive_a_skipped_row_and_a_renewed_lease(world, monkeypatch):
    """FU-ZOMBIE-POLL: the choreography above with both triggers of a poll that finds nothing
    to run, injected once each. The second recovery's first poll runs while another session
    holds ``FOR SHARE`` on the job row (as a waiting attempt's admission read does); the
    third recovery's lease is renewed by the running attempt's heartbeat right after the
    expiry. Each such poll returns False, the recovery expires the lease again and polls
    again, and the run ends exactly as above."""
    w = world
    share = ShareHeldStore(w)
    beats: list[dict] = []

    def runner(recovery: int, poll: int) -> JobRunner:
        if (recovery, poll) == (2, 1):
            return JobRunner(
                settings=Settings(_env_file=None), session_factory=w.factory, store=share
            )
        return _runner(w)

    def expire(recovery: int, poll: int) -> None:
        if (recovery, poll) == (3, 1):
            beats.append(_expire_then_heartbeat(w))
        else:
            _expire_lease(w, w.job_id)

    idle = _zombie_recoveries(w, monkeypatch, runner=runner, expire=expire)
    assert share.recovered == [0]  # the injected poll's recovery recovered nothing
    assert len(beats) == 1 and beats[0]["status"] == JobStatus.RUNNING.value
    assert beats[0]["expired"] < beats[0]["at"] < beats[0]["renewed"]  # the beat landed
    assert idle[2] >= 1 and idle[3] >= 1  # each injected trigger cost one idle poll


# --------------------------------------------------------------------------- #
# W21-REV-INTIND-A O-5: a stale attempt's failure is fenced before its transition is checked
# --------------------------------------------------------------------------- #
def _stale_attempt_then(w: World, then: JobStatus) -> tuple[str, str]:
    """Attempt 1 claims and runs; its lease expires and is recovered; attempt 2 claims and
    runs, then fails retryably (``retry_wait``) or completes (``succeeded``). Returns attempt
    1's credential (its worker id and lease token). ``running``: attempt 2 is still running."""
    store = DurableJobStore()
    with w.factory() as s:
        job = store.claim_one(s, worker_id="w-stale", lease_seconds=30)
        store.mark_running(s, job, worker_id="w-stale", lease_token=job.lease_token)
        s.commit()
        stale = (job.worker_id, job.lease_token)
    _expire_lease(w, w.job_id)
    with w.factory() as s:
        assert store.recover_expired_leases(s) == 1
        s.commit()
    with w.factory() as s:
        job = store.claim_one(s, worker_id="w-new", lease_seconds=30)
        assert job is not None and job.id == w.job_id
        store.mark_running(s, job, worker_id="w-new", lease_token=job.lease_token)
        if then is JobStatus.RETRY_WAIT:
            store.fail(
                s, job, worker_id="w-new", lease_token=job.lease_token,
                error=JobError(JobErrorCode.TRANSIENT, "x"), base_seconds=2, max_seconds=300,
            )
        elif then is JobStatus.SUCCEEDED:
            store.complete(s, job, worker_id="w-new", lease_token=job.lease_token)
        s.commit()
    return stale


@pytest.mark.parametrize("then", [JobStatus.RETRY_WAIT, JobStatus.SUCCEEDED, JobStatus.RUNNING])
@pytest.mark.parametrize("retryable", [True, False])
def test_a_stale_attempts_failure_raises_lease_lost_and_writes_nothing(world, then, retryable):
    """O-5: the stale attempt re-reads the job after its rollback and finds it moved on
    (``retry_wait`` after another attempt failed, ``succeeded``, or ``running`` under the new
    attempt, the one case the old order already fenced). Its ``fail`` must stop on
    the ownership fence (``JobLeaseLostError``, which the worker logs as a lost lease), never
    on a transition check of a status it no longer owns (``InvalidJobTransition``, which
    escaped ``poll_once``: the FU-ZOMBIE-POLL signature). Nothing is written."""
    w = world
    worker_id, token = _stale_attempt_then(w, then)
    before = _job_row(w)
    count_events = select(func.count()).select_from(JobEvent).where(JobEvent.job_id == w.job_id)
    with w.factory() as s:
        events = s.scalar(count_events)
    error = JobError(JobErrorCode.TRANSIENT if retryable else JobErrorCode.NON_RETRYABLE, "late")
    with w.factory() as s:
        job = s.get(Job, w.job_id)  # what the stale attempt re-reads after its rollback
        assert job.status == then.value
        with pytest.raises(JobLeaseLostError):
            DurableJobStore().fail(
                s, job, worker_id=worker_id, lease_token=token, error=error,
                base_seconds=2, max_seconds=300,
            )
        s.rollback()
    assert _job_row(w) == before
    with w.factory() as s:
        assert s.scalar(count_events) == events


def test_the_migrated_schema_enforces_k1_with_the_partial_unique_index(world):
    """The Alembic schema itself (not the ORM metadata): a resolved and an unresolved admission
    coexist for one Scout; a second unresolved one is refused by the partial unique index
    ``uq_scout_admission_unresolved``."""
    w = world
    now = datetime.now(UTC)

    def row(**values) -> ScoutAdmission:
        base = dict(
            scout_request_id=w.rid,
            organization_id=w.org,
            workspace_id=w.ws,
            job_id=None,
            attempt=1,
            worker_id="w-1",
            operation="model",
            admitted_at=now,
        )
        base.update(values)
        return ScoutAdmission(**base)

    with w.factory() as s:
        s.add_all([row(ended_at=now, end_kind="known", resolved_at=now), row()])
        s.commit()
    with w.factory() as s:
        s.add(row(operation="fetch"))
        with pytest.raises(IntegrityError) as refused:
            s.commit()
    assert "uq_scout_admission_unresolved" in str(refused.value)


# --------------------------------------------------------------------------- #
# §6.4: Cancel during an admitted operation (a PROPOSED cancellation change)
# --------------------------------------------------------------------------- #
def test_cancel_during_an_admitted_operation_stops_at_the_next_admission(world, monkeypatch):
    w = world
    provider = Provider(park={1})
    _use(monkeypatch, provider)
    worker = _start_worker(w)
    assert provider.parked.wait(JOIN_SECONDS)
    with w.factory() as s:
        DurableJobStore().request_cancel(s, s.get(Job, w.job_id))
        s.commit()
    assert _unresolved(w) == 1  # Cancel resolves nothing (G4)
    provider.release.set()
    assert worker.join_ok() is True

    end = _end_state(w)
    assert end.job_status == JobStatus.CANCELLED.value
    # D-25 recovery R-C (PROPOSED, lane W15) replaces the pre-claim ``queued`` (the F-03 shape)
    # with the status the Scout had before the run was requested; this seeded legacy job
    # records none, so ``draft``.
    assert end.status == "draft"
    assert len(provider.calls) == 1  # the in-flight call finished; nothing further admitted
    assert _unresolved(w) == 0  # resolved by its own known end, never by the Cancel
    with w.factory() as s:
        assert s.get(ScoutRequest, w.rid).owner_job_id is None


# --------------------------------------------------------------------------- #
# §6.5: a manual run overlapping a scheduled run
# --------------------------------------------------------------------------- #
def test_an_overlapping_claim_is_refused_while_the_owner_is_valid(world, monkeypatch):
    w = world
    provider = Provider(park={1})
    _use(monkeypatch, provider)
    from app.jobs.service import enqueue_scout_request

    with w.factory() as s:
        req = s.get(ScoutRequest, w.rid)
        rival = enqueue_scout_request(
            s,
            organization_id=w.org,
            workspace_id=w.ws,
            scout_request_id=w.rid,
            location_id=req.location_id,
            request_id=w.rid,
            idempotency_key="schedule:overlap",
            trigger="scheduled",
            run_fence=req.run_fence,
        ).id
        s.commit()
    first = _start_worker(w)
    assert provider.parked.wait(JOIN_SECONDS)
    assert _start_worker(w, _runner(w)).join_ok() is True
    provider.release.set()
    assert first.join_ok() is True
    with w.factory() as s:
        outcomes = {
            s.get(Job, w.job_id).result_summary["outcome"],
            s.get(Job, rival).result_summary["outcome"],
        }
    assert outcomes == {"completed", "skipped_owner_active"}
    assert provider.max_inflight == 1


# --------------------------------------------------------------------------- #
# §6.8 / §8.2.1: unknown outcomes block the Scout; wait expiry; reconciliation; ticks
# --------------------------------------------------------------------------- #
def test_a_model_timeout_blocks_the_scout_until_reconciliation(world, monkeypatch):
    """A model attempt times out after it may have been sent: an UNKNOWN end. Its retry
    cannot be admitted (K = 1, nothing to wait for), so the run ends ``failed``
    (``admission_unresolved``) and the Scout admits nothing until an operator reconciles the
    admission; the next run then proceeds."""
    w = world
    provider = Provider(timeout={1})
    _use(monkeypatch, provider)
    assert _start_worker(w).join_ok() is True
    end = _end_state(w)
    assert end.job_status == JobStatus.FAILED.value
    assert end.job_error == ("non_retryable", "admission_unresolved")
    # W21 FU-Q1-2 (PROPOSED): the retry was refused at once (an unknown end, nothing to wait
    # for), and the failed job says so; it is never worded as a wait that timed out.
    assert end.job_summary == {"admission_wait": "none"}
    assert end.status == "failed" and len(provider.calls) == 1
    assert _unresolved(w) == 1
    with w.factory() as s:
        listing = list_unresolved(s, w.rid)
        assert [a["operation"] for a in listing[UNKNOWN_END]] == ["model"]
        assert (
            resolve(
                s,
                admission_id=listing[UNKNOWN_END][0]["admission_id"],
                operator="ops",
                reason="outcome_confirmed_ended",
            )
            is True
        )
        assert (
            resolve(
                s,
                admission_id=listing[UNKNOWN_END][0]["admission_id"],
                operator="ops",
                reason="outcome_confirmed_ended",
            )
            is False
        )  # idempotent
        s.commit()
    assert _unresolved(w) == 0
    provider.timeout.clear()
    with w.factory() as s:
        job = run_scout_request_endpoint(workspace_id=w.ws, request_id=w.rid, db=s, ctx=w.ctx)
    assert _start_worker(w, _runner(w)).join_ok() is True
    with w.factory() as s:
        assert s.get(Job, job.stats["job_id"]).result_summary["outcome"] == "completed"


def test_a_fetch_read_timeout_blocks_the_scout(world, monkeypatch):
    import httpx

    w = world
    fetches: list[str] = []

    class TimingOut:
        def fetch(self, url, *, allowed_hosts, validators=None):
            fetches.append(url)
            try:
                raise httpx.ReadTimeout("fake read timeout")
            except httpx.ReadTimeout as exc:
                raise ConnectorFetchError(FailureKind.TIMEOUT, "source timed out") from exc

    connector = LiveRssConnector(
        [target(record("src-a", markets=(MARKET,)))],
        fetcher=TimingOut(),
        cache=FeedCache(),
        sleep=lambda _s: None,
        max_attempts=2,
    )
    monkeypatch.setattr(live_mod, "live_connector_for", lambda **_kw: connector)
    assert _start_worker(w).join_ok() is True
    end = _end_state(w)
    assert len(fetches) == 1  # the retry was not admitted
    assert end.job_error == ("non_retryable", "admission_unresolved")
    assert end.job_summary == {"admission_wait": "none"}  # W21 FU-Q1-2: refused at once
    assert _unresolved(w) == 1


# --------------------------------------------------------------------------- #
# §6.6: rapid Pause -> Resume -> Run while the old attempt is in flight
# --------------------------------------------------------------------------- #
def test_pause_resume_run_new_owner_waits_for_the_old_admission(world, monkeypatch):
    w = world
    provider = Provider(park={1})
    _use(monkeypatch, provider)
    sampler = Sampler(w, provider)
    old = _start_worker(w)
    assert provider.parked.wait(JOIN_SECONDS)
    _pause(w)  # also clears the owner
    with w.factory() as s:
        assert s.get(ScoutRequest, w.rid).owner_job_id is None
        resume_scout_request(workspace_id=w.ws, request_id=w.rid, db=s, ctx=w.ctx)
        s.commit()
    with w.factory() as s:
        new_job = run_scout_request_endpoint(
            workspace_id=w.ws, request_id=w.rid, db=s, ctx=w.ctx
        ).stats["job_id"]
    new = _start_worker(w, _runner(w))
    time.sleep(0.5)  # the new owner is waiting behind the old outstanding admission
    assert new.is_alive() and len(provider.calls) == 1
    provider.release.set()
    assert old.join_ok() is True and new.join_ok() is True
    bound = sampler.result()

    with w.factory() as s:
        assert s.get(Job, w.job_id).result_summary["outcome"] in (
            "stopped_request_paused",
            "stopped_owner_superseded",
        )
        assert s.get(Job, new_job).result_summary["outcome"] == "completed"
    assert bound["max_unresolved"] <= 1 and bound["max_inflight"] <= 1


# --------------------------------------------------------------------------- #
# §8: erasure is not blocked by an unresolved admission; late ends write nothing
# --------------------------------------------------------------------------- #
def test_erasure_proceeds_over_an_unresolved_admission_and_late_ends_write_nothing(
    world, monkeypatch
):
    w = world
    provider = Provider(timeout={1})
    _use(monkeypatch, provider)
    assert _start_worker(w).join_ok() is True
    assert _unresolved(w) == 1
    with w.factory() as s:
        row = s.scalars(
            select(ScoutAdmission).where(ScoutAdmission.scout_request_id == w.rid)
        ).one()
        admission_id, job_id, attempt = row.id, row.job_id, row.attempt
        erase_organization(s, organization_id=w.org)
        s.commit()
    with w.factory() as s:
        late = s.execute(
            update(ScoutAdmission)
            .where(
                ScoutAdmission.id == admission_id,
                ScoutAdmission.job_id == job_id,
                ScoutAdmission.attempt == attempt,
                ScoutAdmission.ended_at.is_(None),
            )
            .values(ended_at=datetime.now(UTC), end_kind="known")
        )
        s.commit()
        assert late.rowcount == 0
        assert s.scalar(select(func.count()).select_from(ScoutAdmission)) == 0
        assert (
            s.scalar(select(func.count()).select_from(ScoutRequest).where(ScoutRequest.id == w.rid))
            == 0
        )


# --------------------------------------------------------------------------- #
# §8.2.3: a lapsed lease alone loses no run; §8.1.8: wait expiry is definite
# --------------------------------------------------------------------------- #
def test_a_lapsed_lease_stops_the_run_retryably_and_loses_nothing(world, monkeypatch):
    """The run's lease lapses while it is parked in its fetch, and nobody has recovered the
    job yet. Its next admission is refused (``lease_lapsed``): a retryable failure, so the
    job waits to retry (not terminal), the owner is cleared, and the retry runs the Scout."""
    w = world
    sources = Sources(gate=True)
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    _use_mock_llm = LLMService(
        MockLLMProvider(model="mock-1", seed=offline_settings().llm_mock_seed),
        settings=offline_settings(),
    )
    monkeypatch.setattr("app.llm.service.llm_service", _use_mock_llm)
    shift = [timedelta(0)]
    runner = _runner(w, clock=lambda: datetime.now(UTC) + shift[0])
    worker = _start_worker(w, runner)
    assert sources.started.wait(JOIN_SECONDS)
    _expire_lease(w, w.job_id)
    sources.release.set()
    assert worker.join_ok() is True
    first = _end_state(w)
    with w.factory() as s:
        owner = s.get(ScoutRequest, w.rid).owner_job_id
    sources.gate = False
    shift[0] = timedelta(hours=1)  # past the retry backoff
    assert _start_worker(w, runner).join_ok() is True
    end = _end_state(w)
    assert first.job_status == JobStatus.RETRY_WAIT.value
    assert first.job_error == ("transient", "authority_lapsed")
    assert owner is None
    assert end.job_status == JobStatus.SUCCEEDED.value
    assert end.job_summary["outcome"] == "completed" and end.status == "completed"


class Looks:
    """W21 review r1 MINOR-4(a): every ``RunAdmission._blocking`` look - what a waiter saw of the
    blocking admission, ``(ended, deadline_passed)`` - with its thread, so a test decides the
    expected ``admission_wait`` from what the waiter actually saw, never from the host's speed.
    (Template: the W21-REV-Q1 reviewer's ``test_zz_rev_q1_postgres.Looks``.)"""

    def __init__(self, monkeypatch):
        self.items: list[tuple[int, float, tuple[bool, bool] | None]] = []
        real = RunAdmission._blocking
        looks = self

        def spy(self, operation):
            got = real(self, operation)
            looks.items.append(
                (threading.get_ident(), time.monotonic(), None if got is None else (got[1], got[2]))
            )
            return got

        monkeypatch.setattr(RunAdmission, "_blocking", spy)

    def of(self, ident: int) -> list[tuple[bool, bool] | None]:
        return [v for i, _t, v in self.items if i == ident]


def _set_deadline(w: World, seconds: float) -> None:
    """Move the zombie's outstanding admission's PERSISTED deadline to ``seconds`` from now
    (database clock), keeping ``admitted_at`` consistent with its basis (the CHECK)."""
    with w.observer.connect() as c:
        c.execute(
            text(
                "update scout_admissions a set deadline_at = t.ts + make_interval(secs => :s),"
                " admitted_at = t.ts + make_interval(secs => :s)"
                " - make_interval(secs => a.deadline_client_seconds + a.deadline_lease_seconds)"
                " from (select clock_timestamp() as ts) t"
                " where a.scout_request_id = :r and a.resolved_at is null"
            ),
            {"r": w.rid, "s": seconds},
        )


def _job_out(w: World) -> dict:
    with w.factory() as s:
        return JobOut.model_validate(s.get(Job, w.job_id)).model_dump(mode="json")


def test_a_wait_behind_an_outstanding_admission_expires_definitely(world, monkeypatch):
    """A re-claim waits behind the zombie's outstanding admission; once that admission's
    deadline has passed (database time) the run ends ``failed`` (``admission_unresolved``),
    the request ``failed``, the owner cleared, and the admission is NOT resolved by it."""
    w = world
    provider = Provider(park={1})
    _use(monkeypatch, provider)
    zombie = _start_worker(w)
    assert provider.parked.wait(JOIN_SECONDS)
    _expire_lease(w, w.job_id)
    # The zombie's admission is outstanding; its PERSISTED deadline (D-25 recovery R-D, lane
    # W15) is moved to 0.2 s from now, with ``admitted_at`` kept consistent with its basis (the
    # waiter's own settings no longer decide it, contract §3.2).
    with w.observer.connect() as c:
        c.execute(
            text(
                "update scout_admissions a set deadline_at = t.ts + interval '0.2 second',"
                " admitted_at = t.ts + interval '0.2 second'"
                " - make_interval(secs => a.deadline_client_seconds + a.deadline_lease_seconds)"
                " from (select clock_timestamp() as ts) t"
                " where a.scout_request_id = :r and a.resolved_at is null"
            ),
            {"r": w.rid},
        )
    looks = Looks(monkeypatch)
    waiter = _start_worker(w, _runner(w))
    assert waiter.join_ok() is True
    seen = looks.of(waiter.ident)
    expired = _end_state(w)
    still_unresolved = _unresolved(w)
    with w.factory() as s:
        owner = s.get(ScoutRequest, w.rid).owner_job_id
    provider.release.set()
    zombie.join_ok()
    assert expired.job_status == JobStatus.FAILED.value
    assert expired.job_error == ("non_retryable", "admission_unresolved")
    # W21 FU-Q1-2 (PROPOSED; review r1 MINOR-4(a)): the record is decided by what the waiter saw.
    # Whether it reached its admission before the 0.2 s deadline depends on the host's speed: if
    # it looked more than once, it waited until the deadline; if its first look already found the
    # deadline passed, nothing waited. The blocker never ends with an unknown end here.
    assert seen and seen[-1] == (False, True)
    waited = len(seen) >= 2
    assert expired.job_summary == {"admission_wait": "until_deadline" if waited else "none"}
    assert expired.status == "failed" and owner is None
    assert still_unresolved == 1 and len(provider.calls) == 1


def test_a_wait_until_a_deadline_two_seconds_ahead_reads_timed_out(world, monkeypatch):
    """W21 FU-Q1-2 (PROPOSED; review r1 MINOR-4(a)): the outstanding admission's deadline is 2 s
    ahead, so the re-claim waits (it looks, sleeps, looks again) until the deadline passes. The
    failed job records exactly ``until_deadline`` and is the one real path to the "timed out"
    wording (``earlier_operation_timed_out``). (Template: the reviewer's R-PG-DEADLINE.)"""
    w = world
    provider = Provider(park={1})
    _use(monkeypatch, provider)
    zombie = _start_worker(w)
    assert provider.parked.wait(JOIN_SECONDS)
    _expire_lease(w, w.job_id)
    looks = Looks(monkeypatch)
    _set_deadline(w, 2.0)
    waiter = _start_worker(w, _runner(w))
    assert waiter.join_ok() is True
    seen = looks.of(waiter.ident)
    end = _end_state(w)
    provider.release.set()
    zombie.join_ok()
    assert seen[0] == (False, False) and seen[-1] == (False, True) and len(seen) >= 2
    assert end.job_error == ("non_retryable", "admission_unresolved")
    assert end.job_summary == {"admission_wait": "until_deadline"}
    job = _job_out(w)
    assert (job["outcome"], job["outcome_reason"]) == ("failed", "earlier_operation_timed_out")
    assert end.status == "failed"


def test_a_wait_that_ends_with_an_unknown_end_reads_unresolved(world, monkeypatch):
    """W21 FU-Q1-2 (PROPOSED; review r1 MINOR-4(a)): the re-claim is waiting when the zombie's
    parked model call times out (an unknown end). The failed job records exactly
    ``until_unknown_end``, which is never worded as a timeout; the Scout needs attention (D-25
    unchanged). (Template: the reviewer's R-PG-UNKNOWN.)"""
    w = world
    provider = Provider(park={1}, timeout={1})
    _use(monkeypatch, provider)
    zombie = _start_worker(w)
    assert provider.parked.wait(JOIN_SECONDS)
    _expire_lease(w, w.job_id)
    looks = Looks(monkeypatch)
    waiter = _start_worker(w, _runner(w))
    deadline = time.monotonic() + JOIN_SECONDS
    while not looks.of(waiter.ident) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert looks.of(waiter.ident)[0] == (False, False)  # it is waiting
    provider.release.set()  # the zombie's parked call now times out: an unknown end
    zombie.join_ok()
    assert waiter.join_ok() is True
    seen = looks.of(waiter.ident)
    end = _end_state(w)
    assert seen[-1][0] is True and len(seen) >= 2
    assert end.job_error == ("non_retryable", "admission_unresolved")
    assert end.job_summary == {"admission_wait": "until_unknown_end"}
    job = _job_out(w)
    assert (job["outcome"], job["outcome_reason"]) == ("failed", "earlier_operation_unresolved")
    with w.factory() as s:
        scout = get_scout_request(workspace_id=w.ws, request_id=w.rid, db=s, ctx=w.ctx)
        assert scout.status == "failed"
        assert scout.execution.state == "needs_attention"
