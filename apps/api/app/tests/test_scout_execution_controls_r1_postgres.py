"""D-25 execution controls (PROPOSED): the corrections after review r1 (contract §9), on PostgreSQL.

Several tests here are the independent reviewer's (W14-REV-EXECCTL,
``scripts/test_rev_execctl_postgres.py``), adopted with small adaptations: the two refusal-writer
tests (r1 F1) and the Y11 / Y12 / Y13 killers (r1 F4); and, from review r2
(``scripts/test_rev_execctl_r2_postgres.py``), the killers of its mutants Z1, Z2 and Z4: a raising
fenced writer is rolled back, the reconcile deadline carries its lease term, and the gate runs on
the database clock. The harness is ``test_scout_pause_postgres``'s (separate connections, the
real worker, the Alembic schema); offline fakes only.

* §9.1: the refusal-at-bound settlement commits if and only if the attempt still holds its job
  (it runs inside the worker's token-fenced failure transaction, with the full identity).
* §9.2: a model reply with status >= 500 (529 included) is an UNKNOWN end and blocks the Scout
  until reconciliation; a 4xx / 429 reply, ``httpx.ConnectError`` and a fetch 5xx are known.
* §9.3: reconciliation refuses an admission with no recorded end until its own deadline has
  passed by the database clock; the listing's three categories.

Gate: ``require_postgres`` (fails, never skips, under ``CI=true``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, text, update

from app.connectors import live as live_mod
from app.connectors.feedcache import FeedCache
from app.connectors.live import LiveRssConnector
from app.core.config import get_settings
from app.core.errors import ValidationDomainError
from app.infra.vector import SignalVolumeAboveBoundError
from app.jobs import pipeline
from app.jobs.admission import RunAdmission, RunStopped, admission_deadline_seconds, admitted_run
from app.jobs.context import ExecutionContext
from app.jobs.handlers import _record_refused_run
from app.jobs.models import Job
from app.jobs.reconcile import (
    CURRENT_OWNER,
    EARLIER_OWNERS_OUTSTANDING,
    UNKNOWN_END,
    ReconciliationRefused,
    list_unresolved,
    resolve,
)
from app.jobs.status import JobStatus
from app.llm.base import LLMError, LLMRequest, LLMUnavailableError, LLMUsage
from app.llm.metering import LLMMeter
from app.llm.mock import MockLLMProvider
from app.llm.providers_real import AnthropicProvider
from app.llm.service import LLMService
from app.scouting_requests.models import ScoutAdmission, ScoutRequest
from app.scouting_requests.routes import run_scout_request_endpoint
from app.tests import test_scout_pause_postgres as harness
from app.tests._auth2_support import require_postgres
from app.tests._llm_offline import offline_settings
from app.tests._llm_reserve_child import KEY, MODEL, wire_payload
from app.tests.test_live_rss_connector import Clock, Wire, connector, record, target
from app.tests.test_scout_pause_postgres import MARKET, Sources, _end_state, _runner, _start_worker

template_url = harness.template_url
world = harness.world


@pytest.fixture(autouse=True)
def _postgres_required() -> None:
    require_postgres()


# --------------------------------------------------------------------------- #
# helpers (the reviewer's, adapted)
# --------------------------------------------------------------------------- #
def _set_attempt(w, *, attempt, token, worker, live=True, owner=True, status="running"):
    """The job row as attempt ``attempt`` holds it; with ``owner`` the request names it."""
    expires = datetime.now(UTC) + (timedelta(minutes=10) if live else -timedelta(minutes=10))
    with w.factory() as s:
        s.execute(
            update(Job)
            .where(Job.id == w.job_id)
            .values(
                status=status,
                worker_id=worker,
                attempt_count=attempt,
                lease_token=token,
                lease_expires_at=expires,
            )
        )
        if owner:
            s.execute(
                update(ScoutRequest)
                .where(ScoutRequest.id == w.rid)
                .values(
                    status="running",
                    owner_job_id=w.job_id,
                    owner_attempt=attempt,
                    owner_lease_token=token,
                )
            )
        s.commit()


def _adm(w, *, attempt, token, worker, pre="queued") -> RunAdmission:
    return RunAdmission(
        scout_request_id=w.rid,
        organization_id=w.org,
        workspace_id=w.ws,
        fence=0,
        session_factory=w.factory,
        job_id=w.job_id,
        worker_id=worker,
        attempt=attempt,
        lease_token=token,
        claimed=True,
        pre_claim_status=pre,
    )


def _req(w) -> tuple:
    with w.factory() as s:
        r = s.get(ScoutRequest, w.rid)
        return (r.status, r.owner_job_id, r.owner_attempt, r.owner_lease_token, r.stats)


def _unresolved(w) -> int:
    with w.observer.connect() as c:
        return c.execute(
            text(
                "select count(*) from scout_admissions where scout_request_id=:r"
                " and resolved_at is null"
            ),
            {"r": w.rid},
        ).scalar_one()


def _rows(w) -> list:
    with w.factory() as s:
        return s.execute(
            select(
                ScoutAdmission.operation,
                ScoutAdmission.end_kind,
                ScoutAdmission.resolved_at.is_not(None),
            ).order_by(ScoutAdmission.admitted_at)
        ).all()


def _backdate(w, admission_id: str, *, hours: float = 1.0) -> None:
    """The passage of time: the admission's deadline (minutes) lies an hour in the past. Its
    persisted ``deadline_at`` (D-25 recovery R-D, lane W15) moves with ``admitted_at``."""
    with w.factory() as s:
        s.execute(
            update(ScoutAdmission)
            .where(ScoutAdmission.id == admission_id)
            .values(
                admitted_at=ScoutAdmission.admitted_at - timedelta(hours=hours),
                deadline_at=ScoutAdmission.deadline_at - timedelta(hours=hours),
            )
        )
        s.commit()


def _stale_and_replacement(w):
    _set_attempt(w, attempt=1, token="tok-a", worker="w-a")
    stale = _adm(w, attempt=1, token="tok-a", worker="w-a")
    # A lease recovery and a re-claim of the same job by another worker: attempt 2, a new
    # token; its committed claim made it the Scout's owner.
    _set_attempt(w, attempt=2, token="tok-b", worker="w-b")
    replacement = _adm(w, attempt=2, token="tok-b", worker="w-b", pre="running")
    return stale, replacement


def _bound_refusal(w, monkeypatch, *, replace=None) -> None:
    """The run passes its post-fetch admission and is then refused at the in-process bound
    (5 signals over a bound of 2). ``replace`` runs right after the post-fetch admission."""
    monkeypatch.setattr(get_settings(), "vector_inprocess_max_signals_per_run", 2)
    monkeypatch.setattr(pipeline, "fetch_signals", Sources(count=5))
    if replace is not None:
        real_recheck = pipeline.without_taken_down

        def recheck_then_replace(db, signals):
            replace()
            return real_recheck(db, signals)

        monkeypatch.setattr(pipeline, "without_taken_down", recheck_then_replace)


# --------------------------------------------------------------------------- #
# §9.1 (r1 F1): the refusal-at-bound settlement of a stale attempt
# --------------------------------------------------------------------------- #
def test_refusal_writer_of_a_stale_attempt_never_writes_over_its_replacement(world):
    """The reviewer's predicate-level test (adapted: the writer now carries the attempt). The
    stale attempt's writer leaves the replacement's ``running`` and ownership as they are; the
    replacement's own writer (control) settles the request ``failed``."""
    w = world
    stale, replacement = _stale_and_replacement(w)
    before = _req(w)
    ctx = ExecutionContext.for_scout_request(organization_id=w.org, workspace_id=w.ws)
    exc = SignalVolumeAboveBoundError(scanned=5, bound=3)
    with w.factory() as s:
        _record_refused_run(w.rid, ctx, exc, run_fence=0, owner=stale)(s)
        s.commit()
    assert _req(w)[:4] == before[:4]  # still running, still owned by attempt 2
    with w.factory() as s:
        _record_refused_run(w.rid, ctx, exc, run_fence=0, owner=replacement)(s)
        s.commit()
    status, owner_job, _attempt, _token, stats = _req(w)
    assert (status, owner_job, stats["refused"]) == ("failed", None, True)


def test_refusal_writer_end_to_end_through_the_worker(world, monkeypatch):
    """The reviewer's end-to-end test: the attempt passes its post-fetch admission; its lease is
    then lost and the job re-claimed (attempt 2, a new token, the Scout's owner); the bound then
    refuses it. The replacement keeps ``running`` and its ownership."""
    w = world
    replaced: dict = {}

    def replace() -> None:
        with w.factory() as s:
            job = s.get(Job, w.job_id)
            replaced["attempt"] = job.attempt_count + 1
            s.execute(
                update(Job)
                .where(Job.id == w.job_id)
                .values(
                    attempt_count=replaced["attempt"],
                    worker_id="w-replacement",
                    lease_token="tok-replacement",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
                )
            )
            s.execute(
                update(ScoutRequest)
                .where(ScoutRequest.id == w.rid)
                .values(
                    owner_job_id=w.job_id,
                    owner_attempt=replaced["attempt"],
                    owner_lease_token="tok-replacement",
                )
            )
            s.commit()

    _bound_refusal(w, monkeypatch, replace=replace)
    assert _start_worker(w).join_ok() is not None
    status, owner_job, owner_attempt, owner_token, stats = _req(w)
    assert (status, owner_job, owner_attempt, owner_token) == (
        "running",
        w.job_id,
        replaced["attempt"],
        "tok-replacement",
    ), (status, owner_job, stats)


def test_the_refusal_commits_only_together_with_the_fenced_failure(world, monkeypatch):
    """§9.1 "if and only if": the attempt still holds its job when its refusal writer runs, and
    loses it (the job is re-claimed) just before the token-fenced ``fail``. The refusal must not
    survive the lost fence: the request keeps ``running`` and its stats."""
    w = world
    _bound_refusal(w, monkeypatch)
    from app.jobs.worker import JobRunner

    real_write_fenced = JobRunner._write_fenced

    def reclaimed_then_fenced(self, db, writers, **kwargs):
        # D-25 recovery §9.1 (lane W15): the fenced transaction now locks the job row before
        # its writers run, so a re-claim can no longer commit between the writer and ``fail``
        # (it would wait for the fenced transaction). The re-claim therefore commits at the
        # last instant it still can: just before the fenced transaction begins.
        with w.factory() as other:
            other.execute(
                update(Job)
                .where(Job.id == w.job_id)
                .values(
                    attempt_count=Job.attempt_count + 1,
                    worker_id="w-replacement",
                    lease_token="tok-replacement",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
                )
            )
            other.commit()
        return real_write_fenced(self, db, writers, **kwargs)

    monkeypatch.setattr(JobRunner, "_write_fenced", reclaimed_then_fenced)
    assert _start_worker(w).join_ok() is not None
    status, _owner, _attempt, _token, stats = _req(w)
    assert status == "running" and not (stats or {}).get("refused"), (status, stats)
    with w.factory() as s:
        assert s.get(Job, w.job_id).status == JobStatus.RUNNING.value  # the replacement's


def test_the_valid_attempts_refusal_settles_the_request_failed_with_its_stats(world, monkeypatch):
    """§9.1, the valid attempt: the job ends ``failed`` and the request ``failed`` with the
    refusal's stats, the owner cleared, in one transaction."""
    w = world
    _bound_refusal(w, monkeypatch)
    assert _start_worker(w).join_ok() is not None
    end = _end_state(w)
    status, owner_job, _attempt, _token, stats = _req(w)
    assert end.job_status == JobStatus.FAILED.value and end.job_error[0] == "non_retryable"
    assert (status, owner_job, stats["refused"]) == ("failed", None, True)


# --------------------------------------------------------------------------- #
# §9.2 (r1 F2): how a model reply is classified; a model 5xx blocks the Scout
# --------------------------------------------------------------------------- #
def _real_provider(outcome) -> AnthropicProvider:
    """The real adapter, answered offline by ``outcome``: an HTTP status, or a transport error
    (``connect_error`` before dispatch, ``read_error`` after it)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if outcome == "connect_error":
            raise httpx.ConnectError("refused", request=request)
        if outcome == "read_error":
            raise httpx.ReadError("reset after the request was sent", request=request)
        return httpx.Response(outcome, json={"error": {"type": "overloaded_error"}})

    return AnthropicProvider(
        KEY, MODEL, 5, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def _provider_error(outcome) -> LLMError:
    request = LLMRequest(task="classify_signal", inputs={"content": "x", "audiences": None})
    with pytest.raises(LLMError) as raised:
        _real_provider(outcome).generate(request, "classify_signal", "v1")
    return raised.value


@pytest.mark.parametrize(
    ("outcome", "end_kind"),
    [
        (500, "unknown"),
        (502, "unknown"),
        (503, "unknown"),
        (529, "unknown"),
        (400, "known"),
        (404, "known"),
        (408, "known"),
        (422, "known"),
        (429, "known"),
        ("connect_error", "known"),
        ("read_error", "unknown"),
    ],
)
def test_a_model_reply_is_classified_as_built(world, outcome, end_kind):
    """The conservative rule kept by §9.2: the real adapter's error, through the meter, to the
    admission's recorded end."""
    w = world
    _set_attempt(w, attempt=1, token="tok-a", worker="w-a")
    adm = _adm(w, attempt=1, token="tok-a", worker="w-a")
    meter = LLMMeter(offline_settings(), session_factory=w.factory, job_id=w.job_id, attempt=1)
    with w.factory() as s:
        meter.bind(s, organization_id=w.org, workspace_id=w.ws, scout_request_id=w.rid)
    error = _provider_error(outcome)
    with admitted_run(adm):
        meter.before_attempt("anthropic", MODEL, wire_payload())
        meter.after_attempt(task="classify_signal", provider="anthropic", model=MODEL, error=error)
    assert _rows(w) == [("model", end_kind, end_kind == "known")]


class _FiveXX:
    """A priced fake whose first call is answered by the real adapter with ``status`` (so the
    real classification applies) and whose other calls are the deterministic mock's."""

    name = "anthropic"
    model = MODEL

    def __init__(self, status: int):
        self.status, self.calls = status, 0
        self._mock = MockLLMProvider(model="mock-1", seed=offline_settings().llm_mock_seed)

    def prepare(self, request):
        return AnthropicProvider(KEY, MODEL, 5).prepare(request)

    def generate(self, request, prompt_name, prompt_version):
        self.calls += 1
        if self.calls == 1:
            return _real_provider(self.status).generate(request, prompt_name, prompt_version)
        out = self._mock.generate(request, prompt_name, prompt_version)
        return out.model_copy(
            update={
                "provider": self.name,
                "model": self.model,
                "usage": LLMUsage(input_tokens=100, output_tokens=50),
            }
        )


@pytest.mark.parametrize("status", [500, 529])
def test_a_model_5xx_blocks_the_scout_until_reconciliation(world, monkeypatch, status):
    """A model 5xx (529: overloaded) is an unknown end: the run's retry is refused at once, the
    job ends ``failed`` ``admission_unresolved``, the Scout is blocked (a second run admits
    nothing); reconciliation lets the next run complete."""
    w = world
    provider = _FiveXX(status)
    monkeypatch.setattr(pipeline, "fetch_signals", Sources())
    monkeypatch.setattr("app.llm.service.llm_service",
        LLMService(provider, sleep=lambda _s: None, settings=offline_settings()),
    )
    assert _start_worker(w).join_ok() is True
    end = _end_state(w)
    assert end.job_status == JobStatus.FAILED.value
    assert end.job_error == ("non_retryable", "admission_unresolved")
    assert end.status == "failed" and provider.calls == 1
    assert _rows(w) == [("model", "unknown", False)]
    # D-25 recovery §9.5.6 (PROPOSED, lane W15): a second Run now on the blocked Scout is now
    # refused at the route (``scout_needs_attention``) and creates no job; before, it was
    # accepted and its run failed ``admission_unresolved`` without admitting anything.
    with w.factory() as s:
        with pytest.raises(ValidationDomainError) as refused:
            run_scout_request_endpoint(workspace_id=w.ws, request_id=w.rid, db=s, ctx=w.ctx)
    assert refused.value.code == "scout_needs_attention"
    assert provider.calls == 1  # the blocked Scout admitted nothing
    with w.factory() as s:
        [row] = list_unresolved(s, w.rid)[UNKNOWN_END]
        assert resolve(
            s, admission_id=row["admission_id"], operator="ops", reason="provider_confirmed"
        )
        s.commit()
    with w.factory() as s:
        third = run_scout_request_endpoint(workspace_id=w.ws, request_id=w.rid, db=s, ctx=w.ctx)
    assert _start_worker(w, _runner(w)).join_ok() is True
    with w.factory() as s:
        assert s.get(Job, third.stats["job_id"]).result_summary["outcome"] == "completed"


def test_a_fetch_5xx_reply_is_a_known_end(world, monkeypatch):
    """§9.2: for the fetch seam a received reply of any status, 5xx included, is known (an
    unbilled read-only GET under a 20 s deadline): the egress's UPSTREAM_ERROR resolves the
    attempt's admission, its retry is admitted, and the Scout is not blocked."""
    w = world
    src = record("src-5xx", markets=(MARKET,))
    wire = Wire({src.host: [httpx.Response(503), httpx.Response(503)]})
    conn = connector([target(src)], wire, Clock(), max_attempts=2)
    monkeypatch.setattr(live_mod, "live_connector_for", lambda **_kw: conn)
    monkeypatch.setattr("app.llm.service.llm_service",
        LLMService(
            MockLLMProvider(model="mock-1", seed=offline_settings().llm_mock_seed),
            settings=offline_settings(),
        ),
    )
    _start_worker(w).join_ok()
    assert len(wire.requests) == 2
    assert [r for r in _rows(w) if r[0] == "fetch"] == [("fetch", "known", True)] * 2
    assert _unresolved(w) == 0
    assert _end_state(w).job_error[1] != "admission_unresolved"


# --------------------------------------------------------------------------- #
# §9.3 (r1 F3): reconciliation's deadline gate and the listing
# --------------------------------------------------------------------------- #
def test_reconciliation_waits_for_the_deadline_of_an_admission_with_no_recorded_end(world):
    """The reviewer's case: the attempt's call is in flight (no recorded end) and Cancel has
    been requested, so the attempt no longer holds valid authority. Reconciliation is refused
    until the admission's own deadline has passed by the database clock, then permitted."""
    w = world
    _set_attempt(w, attempt=1, token="tok-a", worker="w-a")
    inflight, _ = _adm(w, attempt=1, token="tok-a", worker="w-a").admit_external("model")
    with w.factory() as s:
        s.execute(update(Job).where(Job.id == w.job_id).values(status="cancel_requested"))
        s.commit()
    with w.factory() as s:
        [item] = list_unresolved(s, w.rid)[CURRENT_OWNER]
        assert (item["job_status"], item["deadline_passed"]) == ("cancel_requested", False)
        assert item["attempt_holds_authority"] is False and item["lease_expires_at"]
        with pytest.raises(ReconciliationRefused, match="deadline"):
            resolve(s, admission_id=inflight, operator="ops", reason="operator_decision")
    assert _unresolved(w) == 1
    _backdate(w, inflight)
    with w.factory() as s:
        assert list_unresolved(s, w.rid)[CURRENT_OWNER][0]["deadline_passed"] is True
        assert resolve(s, admission_id=inflight, operator="ops", reason="operator_decision")
        s.commit()
    assert _unresolved(w) == 0


def test_an_earlier_attempts_admission_is_reconcilable_by_its_own_identity_and_deadline(world):
    """The Y8 killer: attempt 1 admitted a fetch and recorded no end; the job was re-claimed
    by attempt 2 (running, a live lease, the owner). Attempt 1's row is listed as an earlier
    owner's, refused before its own deadline and reconcilable after it, whatever attempt 2
    (which holds the job) is doing."""
    w = world
    _set_attempt(w, attempt=1, token="tok-a", worker="w-a")
    row, _ = _adm(w, attempt=1, token="tok-a", worker="w-a").admit_external("fetch")
    _set_attempt(w, attempt=2, token="tok-b", worker="w-b")
    with w.factory() as s:
        listing = list_unresolved(s, w.rid)
        assert listing[CURRENT_OWNER] == [] and listing[UNKNOWN_END] == []
        [item] = listing[EARLIER_OWNERS_OUTSTANDING]
        assert (item["attempt"], item["job_status"], item["attempt_holds_authority"]) == (
            1,
            "running",
            False,
        )
        with pytest.raises(ReconciliationRefused, match="deadline"):
            resolve(s, admission_id=row, operator="ops", reason="worker_crashed")
    _backdate(w, row)
    with w.factory() as s:
        assert resolve(s, admission_id=row, operator="ops", reason="worker_crashed") is True
        s.commit()
    assert _unresolved(w) == 0


def test_an_unknown_end_is_reconcilable_before_its_deadline(world):
    """An admission whose end is recorded unknown: SignalNest's client is no longer in the
    call, so only the external party's side is unknown; reconciliation does not wait."""
    w = world
    _set_attempt(w, attempt=1, token="tok-a", worker="w-a")
    adm = _adm(w, attempt=1, token="tok-a", worker="w-a")
    row, _ = adm.admit_external("model")
    adm.record_end(row, known=False)
    # D-25 recovery §9.3 A2 (PROPOSED, lane W15): reconciliation also refuses an unknown end
    # while its attempt still holds valid authority; the run then ends (as its retry is refused,
    # the job fails ``admission_unresolved``), and the end is reconcilable before its deadline.
    with w.factory() as s:
        s.execute(update(Job).where(Job.id == w.job_id).values(status="failed"))
        s.commit()
    with w.factory() as s:
        [item] = list_unresolved(s, w.rid)[UNKNOWN_END]
        assert item["deadline_passed"] is False and item["end_kind"] == "unknown"
        assert resolve(s, admission_id=row, operator="ops", reason="provider_confirmed") is True
        s.commit()


def _admitted_ago(w, admission_id: str, seconds: float) -> None:
    """The reviewer's helper: ``admitted_at`` set ``seconds`` before the database clock. Its
    persisted ``deadline_at`` (D-25 recovery R-D, lane W15) follows from its basis, from the
    same clock reading."""
    with w.observer.connect() as c:
        c.execute(
            text(
                "update scout_admissions a set admitted_at = t.ts - make_interval(secs => :s),"
                " deadline_at = t.ts - make_interval(secs => :s)"
                " + make_interval(secs => a.deadline_client_seconds + a.deadline_lease_seconds)"
                " from (select clock_timestamp() as ts) t where a.id = :i"
            ),
            {"s": seconds, "i": admission_id},
        )


def test_k3_cancel_case_refused_before_the_deadline_then_permitted(world):
    """Review r2 (adopted; kills its Z2): the gate's deadline is the waiting rule's, lease
    included. A Cancel-requested attempt's call is in flight (no recorded end): refused at once,
    refused 3 s before the deadline, permitted 3 s after it (database clock)."""
    w = world
    _set_attempt(w, attempt=1, token="tok-a", worker="w-a")
    inflight, _ = _adm(w, attempt=1, token="tok-a", worker="w-a").admit_external("model")
    with w.factory() as s:
        s.execute(update(Job).where(Job.id == w.job_id).values(status="cancel_requested"))
        s.commit()
    with w.factory() as s:
        with pytest.raises(ReconciliationRefused):
            resolve(s, admission_id=inflight, operator="rev", reason="operator_decision")
    deadline = admission_deadline_seconds("model")
    _admitted_ago(w, inflight, deadline - 3)  # 3 s before the deadline
    with w.factory() as s:
        with pytest.raises(ReconciliationRefused):
            resolve(s, admission_id=inflight, operator="rev", reason="operator_decision")
    _admitted_ago(w, inflight, deadline + 3)  # 3 s after it
    with w.factory() as s:
        assert resolve(s, admission_id=inflight, operator="rev", reason="operator_decision") is True
        s.commit()
    assert _unresolved(w) == 0


def test_k3_the_gate_uses_the_database_clock_not_the_process_clock(world, monkeypatch):
    """Review r2 (adopted; kills its Z4): with the process clock a day ahead, an in-flight
    admission with no recorded end is still refused: the gate reads ``clock_timestamp()``."""
    from datetime import datetime as real_datetime

    from app.jobs import reconcile as rec

    class Future(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(days=1)

    w = world
    _set_attempt(w, attempt=1, token="tok-a", worker="w-a")
    inflight, _ = _adm(w, attempt=1, token="tok-a", worker="w-a").admit_external("fetch")
    with w.factory() as s:
        s.execute(update(Job).where(Job.id == w.job_id).values(status="cancel_requested"))
        s.commit()
    monkeypatch.setattr(rec, "datetime", Future)  # the process clock one day ahead
    with w.factory() as s:
        with pytest.raises(ReconciliationRefused):
            resolve(s, admission_id=inflight, operator="rev", reason="operator_decision")


def test_k1_a_raising_fenced_writer_leaves_job_and_request_consistent(world, monkeypatch):
    """Review r2 (adopted; kills its Z1): a valid attempt refused at the bound whose fenced
    writer writes and then raises. Its partial write is discarded, the job ends ``failed`` and
    the terminal hook settles the request ``failed`` with the owner cleared."""
    from app.jobs import handlers as hnd

    w = world
    monkeypatch.setattr(get_settings(), "vector_inprocess_max_signals_per_run", 2)
    monkeypatch.setattr(pipeline, "fetch_signals", Sources(count=5))

    def raising_writer(*_a, **_k):
        def write(db):
            db.execute(
                update(ScoutRequest).where(ScoutRequest.id == w.rid).values(stats={"partial": True})
            )
            raise RuntimeError("fenced writer fails")

        return write

    monkeypatch.setattr(hnd, "_record_refused_run", raising_writer)
    _start_worker(w).join_ok()
    with w.factory() as s:
        job = s.get(Job, w.job_id)
        req = s.get(ScoutRequest, w.rid)
        assert job.status == "failed", job.status
        assert req.status == "failed" and req.owner_job_id is None
        assert (req.stats or {}).get("partial") is None


# --------------------------------------------------------------------------- #
# r1 F4: the reviewer's Y11, Y12 and Y13 killers (adopted)
# --------------------------------------------------------------------------- #
def test_a_paused_waiter_admits_nothing_after_the_blocking_admission_ends(world):
    """Y11: the wait re-checks authority on every try. A waiter paused during its wait admits
    nothing once the blocking admission has ended."""
    w = world
    _set_attempt(w, attempt=1, token="tok-a", worker="w-a")
    old = _adm(w, attempt=1, token="tok-a", worker="w-a")
    blocking, _ = old.admit_external("fetch")  # outstanding (its call is "in flight")
    _set_attempt(w, attempt=2, token="tok-b", worker="w-b")
    waiter = _adm(w, attempt=2, token="tok-b", worker="w-b", pre="running")
    calls: list = []

    def sleep(delay):
        calls.append(delay)
        if len(calls) == 1:
            with w.factory() as s:  # the pause route's single UPDATE, committed
                s.execute(
                    update(ScoutRequest)
                    .where(ScoutRequest.id == w.rid)
                    .values(
                        status="paused",
                        run_fence=ScoutRequest.run_fence + 1,
                        owner_job_id=None,
                        owner_attempt=None,
                        owner_lease_token=None,
                    )
                )
                s.commit()
            old.record_end(blocking, known=True)  # the blocking operation ends: resolved

    with pytest.raises(RunStopped) as stopped:
        waiter.admit_external("fetch", sleep=sleep)
    assert stopped.value.cause == "request_paused"
    assert calls and _unresolved(w) == 0
    with w.factory() as s:
        assert s.scalar(select(ScoutAdmission.id).where(ScoutAdmission.attempt == 2)) is None


def test_a_raw_exception_from_the_fetcher_records_an_unknown_end(world, monkeypatch):
    """Y12: an exception that is not a ``ConnectorFetchError`` (here a raw read timeout after
    dispatch) records an unknown end."""
    w = world

    class RawTimeout:
        def fetch(self, url, *, allowed_hosts, validators=None):
            raise httpx.ReadTimeout("body read timed out after the request was sent")

    conn = LiveRssConnector(
        [target(record("rev-raw", markets=(MARKET,)))],
        fetcher=RawTimeout(),
        cache=FeedCache(),
        sleep=lambda _s: None,
        max_attempts=1,
    )
    monkeypatch.setattr(live_mod, "live_connector_for", lambda **_kw: conn)
    _start_worker(w).join_ok()
    rows = _rows(w)
    assert rows and rows[0] == ("fetch", "unknown", False)


def test_a_model_error_with_an_unknown_outcome_records_an_unknown_end(world):
    """Y13: a model error carrying ``outcome_unknown`` (a connection reset after the request
    was sent) records an unknown end."""
    w = world
    _set_attempt(w, attempt=1, token="tok-a", worker="w-a")
    adm = _adm(w, attempt=1, token="tok-a", worker="w-a")
    meter = LLMMeter(offline_settings(), session_factory=w.factory, job_id=w.job_id, attempt=1)
    with w.factory() as s:
        meter.bind(s, organization_id=w.org, workspace_id=w.ws, scout_request_id=w.rid)
    err = LLMUnavailableError("connection reset after the request was sent")
    err.outcome_unknown = True
    with admitted_run(adm):
        meter.before_attempt("mock", "mock-1", None)
        meter.after_attempt(task="classify_signal", provider="mock", model="mock-1", error=err)
    assert _rows(w) == [("model", "unknown", False)]
