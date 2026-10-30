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

from app.connectors import live as live_mod
from app.connectors.base import FailureKind
from app.connectors.feedcache import FeedCache
from app.connectors.live import LiveRssConnector
from app.connectors.retry import ConnectorFetchError
from app.jobs import pipeline
from app.jobs.models import Job
from app.jobs.reconcile import list_unresolved, resolve
from app.jobs.status import JobStatus
from app.jobs.store import DurableJobStore
from app.llm.base import LLMTimeoutError, LLMUsage
from app.llm.mock import MockLLMProvider
from app.llm.models import LLMBudgetReservation, LLMUsageEvent
from app.llm.service import LLMService
from app.privacy.erasure import erase_organization
from app.scouting_requests.models import ScoutAdmission, ScoutRequest
from app.scouting_requests.routes import resume_scout_request, run_scout_request_endpoint
from app.tests import test_scout_pause_postgres as harness
from app.tests._auth2_support import require_postgres
from app.tests._llm_offline import offline_settings
from app.tests._llm_reserve_child import KEY, MODEL
from app.tests.test_live_rss_connector import record, target
from app.tests.test_scout_pause_postgres import (
    JOIN_SECONDS,
    MARKET,
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
    monkeypatch.setattr(
        pipeline,
        "llm_service",
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


# --------------------------------------------------------------------------- #
# §6.1-3: a zombie after lease expiry, repeated recoveries, the bound
# --------------------------------------------------------------------------- #
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
    w = world
    provider = Provider(park={1})
    _use(monkeypatch, provider)
    sampler = Sampler(w, provider)
    workers = [_start_worker(w)]
    assert provider.parked.wait(JOIN_SECONDS)
    for _ in range(3):  # three recoveries while the original operation is outstanding
        _expire_lease(w, w.job_id)
        workers.append(_start_worker(w, _runner(w)))
        deadline = time.monotonic() + JOIN_SECONDS
        while time.monotonic() < deadline:  # wait until the new attempt is running
            with w.factory() as s:
                if s.get(Job, w.job_id).attempt_count == len(workers):
                    break
            time.sleep(0.02)
        time.sleep(0.3)
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
    provider.release.set()
    assert worker.join_ok() is True

    end = _end_state(w)
    assert end.job_status == JobStatus.CANCELLED.value
    assert end.status == "queued"  # the pre-claim status (the F-03 shape, tracked separately)
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
    assert end.status == "failed" and len(provider.calls) == 1
    assert _unresolved(w) == 1
    with w.factory() as s:
        listing = list_unresolved(s, w.rid)
        assert [a["operation"] for a in listing["unknown_outcome"]] == ["model"]
        assert (
            resolve(
                s,
                admission_id=listing["unknown_outcome"][0]["admission_id"],
                operator="ops",
                reason="outcome_confirmed_ended",
            )
            is True
        )
        assert (
            resolve(
                s,
                admission_id=listing["unknown_outcome"][0]["admission_id"],
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
    monkeypatch.setattr(pipeline, "llm_service", _use_mock_llm)
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
    monkeypatch.setattr("app.jobs.admission.RunAdmission._deadline_seconds", lambda self, op: 0.2)
    assert _start_worker(w, _runner(w)).join_ok() is True
    expired = _end_state(w)
    still_unresolved = _unresolved(w)
    with w.factory() as s:
        owner = s.get(ScoutRequest, w.rid).owner_job_id
    provider.release.set()
    zombie.join_ok()
    assert expired.job_status == JobStatus.FAILED.value
    assert expired.job_error == ("non_retryable", "admission_unresolved")
    assert expired.status == "failed" and owner is None
    assert still_unresolved == 1 and len(provider.calls) == 1
