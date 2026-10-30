"""D-25 execution controls (PROPOSED) on SQLite: units and SQLite-safe paths.

The ledger and K = 1 are PostgreSQL-only at run time (``test_scout_execution_controls_postgres``);
here: missing identity fails closed, the fetch-end classification, the K = 1 index itself,
reconciliation's refusals and idempotency, the tick outcome, the owner takeover rule.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.connectors.base import FailureKind
from app.connectors.live import fetch_end_known
from app.connectors.retry import ConnectorFetchError
from app.db import seed as seed_mod
from app.jobs.admission import SKIPPED_OWNER_ACTIVE, RunAdmission, RunStopped
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.reconcile import ReconciliationRefused, list_unresolved, resolve
from app.jobs.service import enqueue_job
from app.jobs.status import JobStatus, JobType
from app.scouting_requests import schedules as sched
from app.scouting_requests.models import ScoutAdmission, ScoutRequest
from app.tests import test_scout_pause_promise as promise
from app.tests.test_scout_pause_promise import _NOW, _scheduled_request, _tick

factory = promise.factory
s = promise.s
flag_on = promise.flag_on


def _error(kind: FailureKind, cause: BaseException | None = None) -> ConnectorFetchError:
    try:
        if cause is not None:
            raise cause
        raise ConnectorFetchError(kind, "x")
    except ConnectorFetchError as exc:
        return exc
    except BaseException as exc:  # noqa: BLE001 - build the chain egress builds
        try:
            raise ConnectorFetchError(kind, "x") from exc
        except ConnectorFetchError as chained:
            return chained


@pytest.mark.parametrize(
    ("kind", "cause", "known"),
    [
        (FailureKind.EGRESS_BLOCKED, None, True),
        (FailureKind.HTTP_CLIENT_ERROR, None, True),
        (FailureKind.UPSTREAM_ERROR, None, True),
        (FailureKind.RATE_LIMITED, None, True),
        (FailureKind.UNSAFE_CONTENT, None, True),
        (FailureKind.NETWORK, httpx.ConnectError("refused"), True),
        (FailureKind.NETWORK, OSError("dns"), True),
        (FailureKind.NETWORK, None, True),
        (FailureKind.NETWORK, httpx.ReadError("reset"), False),
        (FailureKind.NETWORK, httpx.RemoteProtocolError("bad"), False),
        (FailureKind.TIMEOUT, httpx.ReadTimeout("slow"), False),
        (FailureKind.TIMEOUT, httpx.ConnectTimeout("slow"), False),
        (FailureKind.TIMEOUT, None, False),
    ],
)
def test_the_fetch_end_is_classified_from_the_egress_exception_chain(kind, cause, known):
    assert fetch_end_known(_error(kind, cause)) is known


def _admission(req, **overrides) -> RunAdmission:
    values = dict(
        scout_request_id=req.id,
        organization_id=req.organization_id,
        workspace_id=req.workspace_id,
        fence=req.run_fence,
        session_factory=None,
        job_id="job-x",
        worker_id="w-1",
        attempt=1,
        lease_token="tok",
        process_now=_NOW,
    )
    values.update(overrides)
    return RunAdmission(**values)


@pytest.mark.parametrize("missing", ["lease_token", "worker_id", "job_id", "attempt"])
def test_a_missing_claim_identity_fails_closed(s, missing):
    req = s.get(ScoutRequest, seed_mod.sid("scout", "dallas"))
    admission = _admission(req, **{missing: 0 if missing == "attempt" else None})
    with pytest.raises(RunStopped) as stopped:
        admission.check(s, "model")
    assert stopped.value.cause == "identity_incomplete"
    with pytest.raises(RunStopped):
        admission.claim(now=_NOW)


def _row(req, **values) -> ScoutAdmission:
    base = dict(
        id=uuid.uuid4().hex,
        scout_request_id=req.id,
        organization_id=req.organization_id,
        workspace_id=req.workspace_id,
        job_id=None,
        attempt=1,
        worker_id="w-1",
        operation="model",
        admitted_at=_NOW,
    )
    base.update(values)
    return ScoutAdmission(**base)


def test_k1_is_enforced_by_the_partial_unique_index(s):
    req = s.get(ScoutRequest, seed_mod.sid("scout", "dallas"))
    s.add(_row(req, resolved_at=_NOW, end_kind="known", ended_at=_NOW))
    s.add(_row(req))
    s.flush()  # one resolved plus one unresolved: allowed
    s.add(_row(req))
    with pytest.raises(IntegrityError):
        s.flush()


def test_the_ledger_rejects_an_unknown_operation_or_end_kind(s):
    req = s.get(ScoutRequest, seed_mod.sid("scout", "dallas"))
    s.add(_row(req, operation="chunk"))
    with pytest.raises(IntegrityError):
        s.flush()


def _job(s, req, *, worker="w-1", attempt=1, status=JobStatus.RUNNING.value, lease=None) -> Job:
    job = enqueue_job(
        s,
        job_type=JobType.SCOUT_REQUEST_EXECUTE,
        context=ExecutionContext.for_scout_request(
            organization_id=req.organization_id, workspace_id=req.workspace_id
        ),
        payload={"scout_request_id": req.id, "run_fence": req.run_fence},
        scout_request_id=req.id,
        idempotency_key=uuid.uuid4().hex,
        now=_NOW,
    )
    job.status, job.worker_id, job.attempt_count = status, worker, attempt
    job.lease_token = "tok"
    job.lease_expires_at = lease
    s.flush()
    return job


def test_reconciliation_refuses_over_a_valid_in_flight_admission(s):
    from datetime import UTC, datetime

    req = s.get(ScoutRequest, seed_mod.sid("scout", "lagos"))
    job = _job(s, req, lease=datetime.now(UTC) + timedelta(minutes=5))
    row = _row(req, job_id=job.id)
    s.add(row)
    s.flush()
    listing = list_unresolved(s, req.id)
    assert [a["attempt_holds_authority"] for a in listing["outstanding"]] == [True]
    with pytest.raises(ReconciliationRefused):
        resolve(s, admission_id=row.id, operator="ops", reason="worker_crashed")


def test_reconciliation_resolves_an_orphaned_or_unknown_admission_idempotently(s):
    req = s.get(ScoutRequest, seed_mod.sid("scout", "london"))
    job = _job(s, req, status=JobStatus.DEAD_LETTERED.value)
    orphan = _row(req, job_id=job.id)
    s.add(orphan)
    s.flush()
    with pytest.raises(ReconciliationRefused):
        resolve(s, admission_id=orphan.id, operator="ops", reason="not-a-code")
    with pytest.raises(ReconciliationRefused):
        resolve(s, admission_id=orphan.id, operator="ops", reason="worker_crashed", note="x" * 201)
    assert (
        resolve(
            s, admission_id=orphan.id, operator="ops", reason="worker_crashed", note="pod restarted"
        )
        is True
    )
    assert resolve(s, admission_id=orphan.id, operator="ops", reason="worker_crashed") is False
    s.refresh(orphan)
    assert orphan.resolved_by == "ops" and orphan.resolution_note == "worker_crashed: pod restarted"


def test_a_tick_fans_out_no_run_while_an_admission_is_unresolved(s):
    req, schedule, first = _scheduled_request(s, "dallas")
    s.add(_row(req, end_kind="unknown", ended_at=_NOW))
    s.flush()
    out = _tick(s, schedule, first)
    assert out["outcome"] == sched.ADMISSION_UNRESOLVED_OUTCOME
    assert out["run_enqueued"] is False and out["chained"] is True
    assert (
        s.scalar(
            select(Job.id).where(
                Job.scout_request_id == req.id, Job.job_type == JobType.SCOUT_REQUEST_EXECUTE.value
            )
        )
        is None
    )


def test_a_claim_is_refused_over_a_valid_owner_and_takes_over_a_lapsed_one(factory):
    """O2 on the committed claim (separate connections, file-backed SQLite)."""
    with factory() as setup:
        req = setup.get(ScoutRequest, seed_mod.sid("scout", "nairobi"))
        owner = _job(setup, req, worker="w-owner", lease=_NOW + timedelta(minutes=5))
        me = _job(setup, req, worker="w-me", lease=_NOW + timedelta(minutes=5))
        req.status, req.owner_job_id, req.owner_attempt, req.owner_lease_token = (
            "queued",
            owner.id,
            1,
            "tok",
        )
        setup.commit()
        ids = (req.id, req.organization_id, req.workspace_id, req.run_fence, owner.id, me.id)
    rid, org, ws, fence, owner_id, me_id = ids
    admission = RunAdmission(
        scout_request_id=rid,
        organization_id=org,
        workspace_id=ws,
        fence=fence,
        session_factory=factory,
        job_id=me_id,
        worker_id="w-me",
        attempt=1,
        lease_token="tok",
        process_now=_NOW,
    )
    assert admission.claim(now=_NOW) == SKIPPED_OWNER_ACTIVE
    with factory() as lapse:
        lapse.get(Job, owner_id).lease_expires_at = _NOW - timedelta(minutes=1)
        lapse.commit()
    assert admission.claim(now=_NOW) is None
    with factory() as check:
        req = check.get(ScoutRequest, rid)
        assert (req.owner_job_id, req.status) == (me_id, "running")
        req.status, req.owner_job_id, req.owner_attempt, req.owner_lease_token = (
            "draft",
            None,
            None,
            None,
        )
        check.query(Job).filter(Job.id.in_([owner_id, me_id])).delete(synchronize_session=False)
        check.commit()


def _owned_run(s, city: str):
    """A run holding its claim (what the committed claim leaves): the job running with a live
    lease and token ``tok``, the request ``running`` with this attempt as its O2 owner."""
    req = s.get(ScoutRequest, seed_mod.sid("scout", city))
    job = _job(s, req, lease=_NOW + timedelta(minutes=5))
    req.status = "running"
    req.owner_job_id, req.owner_attempt, req.owner_lease_token = job.id, 1, "tok"
    s.flush()
    admission = _admission(req, job_id=job.id)
    admission.claimed, admission.pre_claim_status = True, "queued"
    return req, job, admission


def _request(s, req) -> tuple:
    return s.execute(
        select(ScoutRequest.status, ScoutRequest.owner_job_id).where(ScoutRequest.id == req.id)
    ).one()


@pytest.mark.parametrize(
    ("change", "cause"),
    [("token_rotated", "lease_lost"), ("owner_superseded", "owner_superseded")],
)
def test_each_authority_component_alone_stops_admission_terminal_write_and_restore(
    s, change, cause
):
    """G2: a rotated lease token alone (same worker, same attempt, the owner columns still
    naming this attempt), or a changed owner alone (the job's authority intact), is enough.
    The admission, the guarded terminal write and the status restore all refuse, and nothing is
    written over the request."""
    req, job, admission = _owned_run(s, "dallas")
    admission.check(s, "model")  # control: the claimed identity is admitted
    if change == "token_rotated":
        job.lease_token = "tok-rotated"
    else:
        rival = _job(s, req, worker="w-2", lease=_NOW + timedelta(minutes=5))
        req.owner_job_id = rival.id
    s.flush()
    with pytest.raises(RunStopped) as at_admission:
        admission.check(s, "model")
    assert at_admission.value.cause == cause
    with pytest.raises(RunStopped) as at_commit:
        admission.finish(s, stats={"scanned": 1}, now=_NOW)
    assert at_commit.value.cause == cause
    admission.restore_writer(now=_NOW)(s)
    s.flush()
    assert _request(s, req)[0] == "running"


@pytest.mark.parametrize(("write", "status"), [("terminal", "completed"), ("restore", "queued")])
def test_the_owning_attempt_writes_when_nothing_changed(s, write, status):
    """Control for the test above: the same attempt, nothing changed, writes (and clears the
    owner)."""
    req, _job_row, admission = _owned_run(s, "dallas")
    if write == "terminal":
        admission.finish(s, stats={"scanned": 1}, now=_NOW)
    else:
        admission.restore_writer(now=_NOW)(s)
    s.flush()
    assert _request(s, req) == (status, None)
