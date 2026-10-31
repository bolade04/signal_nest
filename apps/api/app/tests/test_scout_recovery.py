"""D-25 blocked-Scout recovery (R-D, R-V, R-C) on SQLite: derivation, read model, Run-now
refusal, reconciliation's persisted gate and legacy acknowledgement, Cancel's idle
compare-and-set and the Cancel route's guarded transition.

PROPOSED (contract D-25-RECOVERY-CONTRACT-01 §3-§5, refined in §9); nothing here is a ruling.
The PostgreSQL behaviour (the persisted INSERT, the database clock, the worker paths, the
lock order, the Cancel shapes) is in ``test_scout_recovery_postgres.py``. On SQLite the
admission ledger is never written by a run, so rows here are inserted directly.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, event, select

from app.audit.models import AuditLog
from app.core.enums import Role
from app.db import seed as seed_mod
from app.jobs.admission import (
    RunAdmission,
    idle_status,
    settle_cancelled_request,
)
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.reconcile import (
    NOT_RECORDED,
    ReconciliationRefused,
    blocked,
    list_unresolved,
    resolve,
)
from app.jobs.service import enqueue_job
from app.jobs.status import JobType
from app.jobs.store import DurableJobStore
from app.organizations.models import OrganizationMember, User, Workspace
from app.scouting_requests.execution import derive_execution, needs_attention
from app.scouting_requests.models import ScoutAdmission, ScoutRequest
from app.scouting_requests.schemas import ScoutExecutionOut
from app.tests import test_scout_pause_promise as promise
from app.tests._auth2_support import live_bearer
from app.tests.test_scout_pause_promise import API

factory = promise.factory
s = promise.s
flag_on = promise.flag_on
committed = promise.committed
client = promise.client

APP_DIR = Path(__file__).resolve().parents[1]


def _req(s, market: str = "dallas") -> ScoutRequest:
    return s.get(ScoutRequest, seed_mod.sid("scout", market))


def _job(
    s,
    req,
    *,
    status="running",
    worker="w-1",
    attempt=1,
    token="tok",
    lease=None,
    fence=None,
    payload=None,
) -> Job:
    body = {"scout_request_id": req.id, "run_fence": req.run_fence if fence is None else fence}
    body.update(payload or {})
    job = enqueue_job(
        s,
        job_type=JobType.SCOUT_REQUEST_EXECUTE,
        context=ExecutionContext.for_scout_request(
            organization_id=req.organization_id, workspace_id=req.workspace_id
        ),
        payload=body,
        scout_request_id=req.id,
        idempotency_key=uuid.uuid4().hex,
    )
    job.status, job.worker_id, job.attempt_count, job.lease_token = status, worker, attempt, token
    job.lease_expires_at = lease
    s.flush()
    return job


def _adm(
    s,
    req,
    *,
    job=None,
    attempt=1,
    worker="w-1",
    end_kind=None,
    admitted=None,
    deadline_in: float | None = 60.0,
    legacy=False,
) -> ScoutAdmission:
    """An unresolved admission. ``deadline_in``: seconds from now to its persisted deadline
    (negative: passed). ``legacy``: no recorded deadline (``unrecorded``)."""
    now = datetime.now(UTC)
    admitted = admitted or now - timedelta(seconds=30)
    values = dict(
        id=uuid.uuid4().hex,
        scout_request_id=req.id,
        organization_id=req.organization_id,
        workspace_id=req.workspace_id,
        job_id=job.id if job is not None else None,
        attempt=attempt,
        worker_id=worker,
        operation="model",
        admitted_at=admitted,
    )
    if end_kind is not None:
        values.update(ended_at=now, end_kind=end_kind)
    if not legacy:
        deadline = now + timedelta(seconds=deadline_in)
        basis = (deadline - admitted).total_seconds()
        values.update(
            deadline_rule="client_plus_lease",
            deadline_at=deadline,
            deadline_client_seconds=basis - 30.0,
            deadline_lease_seconds=30.0,
        )
    row = ScoutAdmission(**values)
    s.add(row)
    s.flush()
    return row


def _state(s, req) -> tuple:
    out = derive_execution(s, req)
    return out.state.value, out.reason.value, out.next_action.value


# --------------------------------------------------------------------------- #
# R-V: the derivation (contract §4.1, §9.4)
# --------------------------------------------------------------------------- #
def test_idle_running_paused_and_stopped_from_the_request_and_its_jobs(s):
    req = _req(s)
    req.status = "completed"
    s.flush()
    assert _state(s, req) == ("idle", "none", "run_now")
    req.status = "paused"
    s.flush()
    assert _state(s, req) == ("paused", "paused_by_user", "resume")
    assert derive_execution(s, req).since is None  # §9.4 A13: no stable pause time
    req.status = "failed"
    s.flush()
    assert _state(s, req) == ("stopped", "run_failed", "run_now")
    req.status = "draft"
    job = _job(s, req, status="cancelled")
    job.cancelled_at = datetime.now(UTC)
    s.flush()
    assert _state(s, req) == ("stopped", "run_cancelled", "run_now")


@pytest.mark.parametrize("status", ["queued", "running"])
def test_a_request_left_in_flight_with_no_live_job_is_stopped_interrupted(s, status):
    """§9.4 A5: rows left by 430f7f2f's Cancel (or any path R-C does not cover)."""
    req = _req(s)
    req.status = status
    _job(s, req, status="cancelled")
    s.flush()
    assert _state(s, req) == ("stopped", "run_interrupted", "contact_support")


@pytest.mark.parametrize(
    "job_status", ["pending", "retry_wait", "claimed", "running", "cancel_requested"]
)
def test_a_live_job_not_fenced_out_is_in_progress_whatever_the_request_status(s, job_status):
    """§9.4 A6: a pending scheduled run (the tick never sets ``queued``) is not idle."""
    req = _req(s)
    req.status = "completed"
    _job(s, req, status=job_status)
    s.flush()
    assert _state(s, req) == ("in_progress", "run_active", "wait")


def test_a_job_fenced_out_by_a_pause_is_not_live(s):
    req = _req(s)
    req.status, req.run_fence = "draft", 3
    _job(s, req, status="running", fence=2)  # enqueued before the pause
    s.flush()
    assert _state(s, req) == ("idle", "none", "run_now")


def test_needs_attention_for_each_eligible_class_and_never_for_a_healthy_in_flight_one(s):
    req = _req(s)
    req.status = "failed"
    s.flush()
    unknown = _adm(s, req, end_kind="unknown")
    out = derive_execution(s, req)
    assert (out.state.value, out.reason.value, out.next_action.value) == (
        "needs_attention",
        "operation_outcome_unknown",
        "contact_support",
    )
    assert out.since is not None
    s.delete(unknown)
    s.flush()
    overdue = _adm(s, req, deadline_in=-5)
    assert _state(s, req)[:2] == ("needs_attention", "operation_overdue")
    s.delete(overdue)
    s.flush()
    before = _adm(s, req, deadline_in=60)
    assert _state(s, req) == ("in_progress", "operation_finishing", "wait")
    s.delete(before)
    s.flush()
    _adm(s, req, legacy=True)
    assert _state(s, req)[:2] == ("needs_attention", "operation_outcome_unknown")


def test_a_healthy_in_flight_admission_past_its_deadline_is_in_progress(s):
    """Mutant V1's killer: an attempt holding valid authority is never needs_attention, even
    once its deadline has passed or with an unknown end."""
    req = _req(s)
    req.status = "running"
    job = _job(s, req, lease=datetime.now(UTC) + timedelta(minutes=5))
    row = _adm(s, req, job=job, deadline_in=-60)
    assert _state(s, req) == ("in_progress", "run_active", "wait")
    row.ended_at, row.end_kind = datetime.now(UTC), "unknown"
    s.flush()
    assert _state(s, req)[0] == "in_progress"
    job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)  # authority gone
    s.flush()
    assert _state(s, req)[0] == "needs_attention"


def test_needs_attention_wins_over_paused(s):
    req = _req(s)
    req.status = "paused"
    s.flush()
    _adm(s, req, end_kind="unknown")
    assert _state(s, req)[0] == "needs_attention"


def test_the_derivation_reads_only_this_scouts_tenancy(s):
    """Mutant V4's killer: another Scout's (another tenant's) unresolved admission never
    changes this Scout's state."""
    mine, other = _req(s, "dallas"), _req(s, "london")
    mine.status = other.status = "completed"
    s.flush()
    row = _adm(s, other, end_kind="unknown")
    assert _state(s, other)[0] == "needs_attention"
    assert _state(s, mine) == ("idle", "none", "run_now")
    row.scout_request_id = mine.id
    row.organization_id = uuid.uuid4().hex  # a row naming this Scout but another tenant
    s.flush()
    assert _state(s, mine) == ("idle", "none", "run_now")


def test_the_execution_model_exposes_only_closed_codes():
    assert set(ScoutExecutionOut.model_fields) == {"state", "reason", "next_action", "since"}
    with pytest.raises(ValueError):
        ScoutExecutionOut(state="idle", reason="none", next_action="run_now", job_id="x")


def test_the_derivation_writes_nothing(s):
    req = _req(s)
    _adm(s, req, end_kind="unknown")
    statements: list[str] = []

    def capture(_conn, _cursor, statement, *_a):
        statements.append(statement.split()[0].upper())

    bind = s.get_bind()
    event.listen(bind, "before_cursor_execute", capture)
    try:
        derive_execution(s, req)
        needs_attention(s, req)
    finally:
        event.remove(bind, "before_cursor_execute", capture)
    assert statements and set(statements) <= {"SELECT"}, statements


# --------------------------------------------------------------------------- #
# R-V over HTTP: the read model, roles, the Run-now refusal (§4.2, §9.4, §9.5.6)
# --------------------------------------------------------------------------- #
def _base(market="dallas") -> str:
    return f"{API}/workspaces/{seed_mod.sid('ws')}/scout-requests/{seed_mod.sid('scout', market)}"


@pytest.fixture
def blocked_scout(factory, committed):
    """The dallas Scout with an unresolved unknown-end admission (no job: no authority)."""
    rid = seed_mod.sid("scout", "dallas")
    with factory() as session:
        _adm(session, session.get(ScoutRequest, rid), end_kind="unknown")
        session.commit()
    yield rid
    with factory() as session:
        session.execute(delete(ScoutAdmission))
        session.commit()


@pytest.fixture
def members(factory, committed):
    """A user of each role in the seeded organization (and one outsider)."""
    org = seed_mod.sid("org")
    made: dict[str, str] = {}
    with factory() as session:
        for role in Role:
            if role is Role.OWNER:
                made[role.value] = seed_mod.sid("user")
                continue
            user = User(
                id=uuid.uuid4().hex,
                email=f"{role.value}-{uuid.uuid4().hex[:6]}@example.test",
                full_name=role.value,
                hashed_password="x",
                is_active=True,
            )
            session.add(user)
            session.flush()
            session.add(
                OrganizationMember(
                    id=uuid.uuid4().hex, organization_id=org, user_id=user.id, role=role.value
                )
            )
            made[role.value] = user.id
        outsider = User(
            id=uuid.uuid4().hex,
            email=f"out-{uuid.uuid4().hex[:6]}@example.test",
            full_name="out",
            hashed_password="x",
            is_active=True,
        )
        session.add(outsider)
        made["outsider"] = outsider.id
        session.commit()
    yield made
    with factory() as session:
        extra = [v for k, v in made.items() if k not in ("owner",)]
        session.execute(delete(OrganizationMember).where(OrganizationMember.user_id.in_(extra)))
        session.commit()


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {live_bearer(user_id)}"}


def test_single_scout_responses_carry_the_execution_and_the_list_does_not(client, blocked_scout):
    owner = _auth(seed_mod.sid("user"))
    got = client.get(_base(), headers=owner)
    assert got.status_code == 200
    assert got.json()["execution"] == {
        "state": "needs_attention",
        "reason": "operation_outcome_unknown",
        "next_action": "contact_support",
        "since": got.json()["execution"]["since"],
    }
    listed = client.get(f"{API}/workspaces/{seed_mod.sid('ws')}/scout-requests", headers=owner)
    assert listed.status_code == 200 and {r["execution"] for r in listed.json()} == {None}
    upd = client.put(_base(), json={"notes": "n"}, headers=owner)
    paused = client.post(_base() + "/pause", headers=owner)
    resumed = client.post(_base() + "/resume", headers=owner)
    for r in (upd, paused, resumed):
        assert r.status_code == 200 and r.json()["execution"]["state"] == "needs_attention"
    # Resume returns the Scout to draft, but the state stays needs_attention (§6).
    assert resumed.json()["status"] == "draft"


def test_the_field_is_visible_to_exactly_the_roles_that_read_the_scout(
    client, blocked_scout, members
):
    for role, user_id in members.items():
        r = client.get(_base(), headers=_auth(user_id))
        if role == "outsider":
            assert r.status_code == 403, (role, r.text)
            assert "execution" not in r.text
        else:
            assert r.status_code == 200, (role, r.text)
            assert r.json()["execution"]["state"] == "needs_attention", role


def test_run_now_on_a_needs_attention_scout_is_refused_and_creates_no_job(
    client, blocked_scout, members, factory
):
    with factory() as session:
        jobs_before = session.scalar(select(Job.id).limit(1)) is not None
    refused = client.post(_base() + "/run", headers=_auth(seed_mod.sid("user")))
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "scout_needs_attention"
    assert "Resume and Run now will not restore service" in refused.json()["error"]["message"]
    with factory() as session:
        assert (session.scalar(select(Job.id).limit(1)) is not None) == jobs_before
        assert session.get(ScoutRequest, blocked_scout).status == "draft"
    # A Viewer meets the existing role refusal before the needs-attention check.
    viewer = client.post(_base() + "/run", headers=_auth(members["viewer"]))
    assert viewer.status_code == 403


def test_run_now_is_accepted_while_an_operation_is_only_finishing(client, factory, committed):
    rid = seed_mod.sid("scout", "dallas")
    with factory() as session:
        _adm(session, session.get(ScoutRequest, rid), deadline_in=600)
        session.commit()
    try:
        ok = client.post(_base() + "/run", headers=_auth(seed_mod.sid("user")))
        assert ok.status_code == 200, ok.text
    finally:
        with factory() as session:
            session.execute(delete(ScoutAdmission))
            session.commit()


def test_a_scout_of_another_workspace_of_the_same_organization_is_not_found(
    client, blocked_scout, factory
):

    ws2 = uuid.uuid4().hex
    with factory() as session:
        session.add(
            Workspace(id=ws2, organization_id=seed_mod.sid("org"), name="Second", slug="second")
        )
        session.commit()
    try:
        owner = _auth(seed_mod.sid("user"))
        path = f"{API}/workspaces/{ws2}/scout-requests/{blocked_scout}"
        got = client.get(path, headers=owner)
        assert got.status_code == 404 and "execution" not in got.text
        run = client.post(path + "/run", headers=owner)
        assert run.status_code == 404  # not the needs-attention refusal: non-enumerable
    finally:
        with factory() as session:
            session.execute(delete(Workspace).where(Workspace.id == ws2))
            session.commit()


# --------------------------------------------------------------------------- #
# R-D / A2: reconciliation reads the persisted deadline; legacy rows; authority
# --------------------------------------------------------------------------- #
def test_the_listing_shows_the_persisted_deadline_and_basis_or_not_recorded(s):
    req = _req(s)
    job = _job(s, req, status="failed")
    recorded = _adm(s, req, job=job, deadline_in=-5)
    [item] = list_unresolved(s, req.id)["earlier_owners_outstanding"]
    assert item["deadline_basis"]["rule"] == "client_plus_lease"
    assert item["deadline_passed"] is True and item["deadline"] != NOT_RECORDED
    s.delete(recorded)
    s.flush()
    _adm(s, req, job=job, legacy=True)
    [item] = list_unresolved(s, req.id)["earlier_owners_outstanding"]
    assert item["deadline"] == NOT_RECORDED
    assert item["deadline_basis"] == {"rule": "unrecorded"} and item["deadline_passed"] is None


def test_a_legacy_no_end_admission_needs_the_explicit_acknowledgement(s):
    req = _req(s)
    job = _job(s, req, status="failed")
    row = _adm(s, req, job=job, legacy=True, admitted=datetime.now(UTC) - timedelta(days=2))
    with pytest.raises(ReconciliationRefused, match="acknowledge-unrecorded-deadline"):
        resolve(s, admission_id=row.id, operator="ops", reason="worker_crashed")
    assert (
        resolve(
            s,
            admission_id=row.id,
            operator="ops",
            reason="worker_crashed",
            acknowledge_unrecorded_deadline=True,
        )
        is True
    )
    audit = s.scalars(select(AuditLog).where(AuditLog.entity_id == row.id)).one()
    assert audit.context == {
        "deadline_rule": "unrecorded",
        "acknowledged_unrecorded_deadline": True,
    }


def test_the_acknowledgement_is_refused_where_it_does_not_apply(s):
    req = _req(s)
    job = _job(s, req, status="failed")
    row = _adm(s, req, job=job, deadline_in=-5)
    with pytest.raises(ReconciliationRefused, match="applies only"):
        resolve(
            s,
            admission_id=row.id,
            operator="ops",
            reason="worker_crashed",
            acknowledge_unrecorded_deadline=True,
        )
    assert resolve(s, admission_id=row.id, operator="ops", reason="worker_crashed") is True


def test_a_legacy_admission_is_still_refused_while_its_attempt_holds_authority(s):
    req = _req(s)
    job = _job(s, req, lease=datetime.now(UTC) + timedelta(minutes=5))
    row = _adm(s, req, job=job, legacy=True)
    with pytest.raises(ReconciliationRefused, match="holds valid authority"):
        resolve(
            s,
            admission_id=row.id,
            operator="ops",
            reason="worker_crashed",
            acknowledge_unrecorded_deadline=True,
        )


def test_resolve_refuses_an_unknown_end_whose_attempt_still_holds_authority(s):
    """§9.3 A2 (mutant A2's killer)."""
    req = _req(s)
    job = _job(s, req, lease=datetime.now(UTC) + timedelta(minutes=5))
    row = _adm(s, req, job=job, end_kind="unknown")
    with pytest.raises(ReconciliationRefused, match="holds valid authority"):
        resolve(s, admission_id=row.id, operator="ops", reason="provider_confirmed")
    job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    s.flush()
    assert resolve(s, admission_id=row.id, operator="ops", reason="provider_confirmed") is True


def test_the_blocked_view_lists_exactly_the_eligible_admissions_with_the_command(s):
    a, b, c = _req(s, "dallas"), _req(s, "london"), _req(s, "lagos")
    unknown = _adm(s, a, end_kind="unknown")
    legacy = _adm(s, b, legacy=True)
    _adm(s, c, deadline_in=600)  # not yet eligible: not listed
    out = blocked(s)
    by_id = {item["admission_id"]: item for item in out["blocked"]}
    assert set(by_id) == {unknown.id, legacy.id}
    assert by_id[unknown.id]["category"] == "unknown_end"
    assert "--acknowledge-unrecorded-deadline" not in by_id[unknown.id]["resolve_command"]
    assert by_id[legacy.id]["deadline"] == NOT_RECORDED
    assert by_id[legacy.id]["end_kind"] == "no recorded end"
    assert by_id[legacy.id]["resolve_command"].endswith("--acknowledge-unrecorded-deadline")
    assert blocked(s, workspace_id=uuid.uuid4().hex)["blocked"] == []
    assert {
        i["admission_id"] for i in blocked(s, organization_id=a.organization_id)["blocked"]
    } >= {unknown.id}


# --------------------------------------------------------------------------- #
# R-C: Cancel's idle compare-and-set (§5.1, §9.5.2-§9.5.5)
# --------------------------------------------------------------------------- #
def _cancel_requested(s, req, **kw) -> Job:
    return _job(s, req, status="cancel_requested", **kw)


def _holder(req, job) -> RunAdmission:
    return RunAdmission(
        scout_request_id=req.id,
        organization_id=req.organization_id,
        workspace_id=req.workspace_id,
        fence=req.run_fence,
        session_factory=lambda: None,
        job_id=job.id,
        worker_id=job.worker_id,
        attempt=job.attempt_count,
        lease_token=job.lease_token,
    )


def _settle(s, req, job, *, holder: bool | RunAdmission = True, fence=None) -> bool:
    if holder is True:
        holder = _holder(req, job)
    return settle_cancelled_request(
        s,
        scout_request_id=req.id,
        organization_id=req.organization_id,
        workspace_id=req.workspace_id,
        job_id=job.id,
        fence=req.run_fence if fence is None else fence,
        payload=job.payload,
        now=datetime.now(UTC),
        holder=holder or None,
    )


@pytest.mark.parametrize(
    ("case", "written", "status"),
    [
        ("queued_no_owner", True, "completed"),
        ("running_owned_by_this_attempt", True, "completed"),
        ("running_owned_by_an_earlier_attempt_of_this_job", True, "completed"),
        ("owned_by_another_job", False, "running"),
        ("paused", False, "paused"),
        ("completed", False, "completed"),
        ("fence_moved", False, "running"),
        ("a_live_rival_with_this_fence", False, "running"),
        ("a_rival_fenced_out_by_a_pause", True, "completed"),
        ("the_holder_no_longer_holds_the_job", False, "running"),
    ],
)
def test_the_idle_write_preserves_every_newer_state(s, case, written, status):
    req = _req(s)
    req.status, req.run_fence = "running", 2
    s.flush()
    job = _cancel_requested(s, req, attempt=2, payload={"prior_status": "completed"})
    claim = _holder(req, job)  # the identity captured at this attempt's claim
    req.owner_job_id, req.owner_attempt, req.owner_lease_token = job.id, 2, "tok"
    if case == "queued_no_owner":
        req.status = "queued"
        req.owner_job_id = req.owner_attempt = req.owner_lease_token = None
    elif case == "running_owned_by_an_earlier_attempt_of_this_job":
        req.owner_attempt, req.owner_lease_token = 1, "tok-old"
    elif case == "owned_by_another_job":
        other = _job(s, req, status="succeeded")
        req.owner_job_id = other.id
    elif case in ("paused", "completed"):
        req.status = case
    elif case == "fence_moved":
        req.run_fence = 3
    elif case == "a_live_rival_with_this_fence":
        _job(s, req, status="pending")
    elif case == "a_rival_fenced_out_by_a_pause":
        _job(s, req, status="running", fence=1)
    elif case == "the_holder_no_longer_holds_the_job":
        job.lease_token = "tok-rotated"
    s.flush()
    assert _settle(s, req, job, holder=claim, fence=2) is written
    s.refresh(req)
    assert req.status == status
    if written:
        assert req.owner_job_id is None


def test_the_route_path_clears_an_owner_from_this_job_only(s):
    req = _req(s)
    req.status = "running"
    job = _job(s, req, status="cancelled", payload={"prior_status": "failed"})
    req.owner_job_id, req.owner_attempt, req.owner_lease_token = job.id, 1, "tok-dead"
    s.flush()
    assert _settle(s, req, job, holder=False) is True
    s.refresh(req)
    assert (req.status, req.owner_job_id) == ("failed", None)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"prior_status": "completed"}, "completed"),
        ({"prior_status": "failed"}, "failed"),
        ({"prior_status": "draft"}, "draft"),
        ({"prior_status": "queued"}, "draft"),
        ({"prior_status": "running"}, "draft"),
        ({}, "draft"),
        (None, "draft"),
    ],
)
def test_the_idle_status_is_the_recorded_prior_status_or_draft(payload, expected):
    assert idle_status(payload) == expected


def test_cancel_resolves_no_admission_and_touches_no_ledger(s):
    req = _req(s)
    req.status = "running"
    job = _cancel_requested(s, req)
    row = _adm(s, req, job=job, end_kind="unknown")
    s.flush()
    assert _settle(s, req, job) is True
    s.refresh(row)
    assert row.resolved_at is None and row.end_kind == "unknown"


# --------------------------------------------------------------------------- #
# §9.5.4: the Cancel route's job transition is a compare-and-set
# --------------------------------------------------------------------------- #
def test_cancel_never_overwrites_a_job_that_completed_since_it_was_read(factory, committed):
    store = DurableJobStore()
    with factory() as setup:
        req = _req(setup)
        job_id = _job(setup, req, status="running").id
        setup.commit()
    with factory() as route, factory() as worker:
        read = route.get(Job, job_id)  # the route read it running
        worker.get(Job, job_id).status = "succeeded"
        worker.commit()
        calls = []
        store.request_cancel(route, read, on_cancelled=calls.append)
        route.commit()
        assert calls == []
    with factory() as check:
        job = check.get(Job, job_id)
        assert (job.status, job.cancel_requested_at) == ("succeeded", None)


def test_cancel_of_a_job_claimed_since_it_was_read_requests_cancellation(factory, committed):
    store = DurableJobStore()
    with factory() as setup:
        job_id = _job(setup, _req(setup), status="pending").id
        setup.commit()
    with factory() as route, factory() as worker:
        read = route.get(Job, job_id)
        claimed = worker.get(Job, job_id)
        claimed.status, claimed.worker_id, claimed.lease_token = "claimed", "w-9", "tok-9"
        worker.commit()
        calls = []
        store.request_cancel(route, read, on_cancelled=calls.append)
        route.commit()
        assert calls == []  # not cancelled at once: a worker holds it
    with factory() as check:
        job = check.get(Job, job_id)
        assert (job.status, job.worker_id) == ("cancel_requested", "w-9")


def test_cancel_of_an_enqueued_job_runs_the_hook_once_in_its_transaction(factory, committed):
    store = DurableJobStore()
    with factory() as setup:
        job_id = _job(setup, _req(setup), status="pending").id
        setup.commit()
    with factory() as route:
        calls = []
        store.request_cancel(route, route.get(Job, job_id), on_cancelled=calls.append)
        assert [j.status for j in calls] == ["cancelled"]
        route.rollback()  # the hook's transaction is the cancel's: nothing committed
    with factory() as check:
        assert check.get(Job, job_id).status == "pending"


# --------------------------------------------------------------------------- #
# §8.8: no customer route can resolve an admission
# --------------------------------------------------------------------------- #
CUSTOMER_ROUTERS = [
    APP_DIR / "scouting_requests" / "routes.py",
    APP_DIR / "jobs" / "routes.py",
]


@pytest.mark.parametrize("path", CUSTOMER_ROUTERS, ids=lambda p: p.parent.name)
def test_no_customer_router_references_resolution(path):
    tree = ast.parse(path.read_text())
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    imported = {(n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "resolved_at" not in names and "resolve" not in names
    assert not any(m.startswith("app.jobs.reconcile") for m in imported)


def test_every_customer_action_leaves_an_unresolved_admission_unresolved(
    client, blocked_scout, factory
):
    owner = _auth(seed_mod.sid("user"))
    for method, suffix, body in (
        ("put", "", {"notes": "x"}),
        ("post", "/pause", None),
        ("post", "/resume", None),
        ("post", "/run", None),
        ("get", "", None),
        ("get", "/runs", None),
    ):
        getattr(client, method)(_base() + suffix, headers=owner, **({"json": body} if body else {}))
    with factory() as session:
        rows = session.scalars(select(ScoutAdmission)).all()
        assert [r.resolved_at for r in rows] == [None]


@pytest.mark.parametrize("kind", ["overdue_no_end", "legacy_no_end"])
def test_rev_s3_run_now_is_refused_for_every_needs_attention_class(
    client, factory, committed, kind
):
    """Review r1 F2: the independent reviewer's S3 (lane W15-REV-RECOVERY,
    ``scripts/test_rev_recovery_r1b_sqlite.py``, sha256 ee78376e...390e), adopted with credit;
    adapted only to this module's helpers (no ``rec.`` prefix). Run now is refused for EVERY
    needs_attention class, not only a recorded unknown end: an overdue admission with no
    recorded end (its stored deadline passed, its attempt holds no authority) and a legacy
    admission with no recorded end and no recorded deadline. Kills the reviewer's mutant YR5
    (refusal only for unknown ends)."""
    from sqlalchemy import func

    rid = seed_mod.sid("scout", "dallas")
    with factory() as session:
        req = session.get(ScoutRequest, rid)
        job = _job(session, req, status="failed", worker="w-1", attempt=1)
        job_id = job.id
        if kind == "overdue_no_end":
            _adm(session, req, job=job, deadline_in=-5)
        else:
            _adm(session, req, job=job, legacy=True)
        session.commit()
        jobs_before = session.scalar(select(func.count()).select_from(Job))
    try:
        got = client.get(_base(), headers=_auth(seed_mod.sid("user")))
        assert got.json()["execution"]["state"] == "needs_attention", got.json()["execution"]
        r = client.post(_base() + "/run", headers=_auth(seed_mod.sid("user")))
        assert r.status_code == 422 and r.json()["error"]["code"] == "scout_needs_attention", r.text
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(Job)) == jobs_before
    finally:
        with factory() as session:
            session.execute(delete(ScoutAdmission))
            session.execute(delete(Job).where(Job.id == job_id))
            session.commit()
