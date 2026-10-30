"""D-25 execution controls (PROPOSED): the corrections after review r1 (contract §9), on PostgreSQL.

Several tests here are the independent reviewer's (W14-REV-EXECCTL,
``scripts/test_rev_execctl_postgres.py``), adopted with small adaptations: the two refusal-writer
tests (r1 F1) and the Y11 / Y12 / Y13 killers (r1 F4). The harness is
``test_scout_pause_postgres``'s (separate connections, the real worker, the Alembic schema);
offline fakes only.

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

import pytest
from sqlalchemy import select, text, update

from app.core.config import get_settings
from app.infra.vector import SignalVolumeAboveBoundError
from app.jobs import pipeline
from app.jobs.admission import RunAdmission
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
from app.jobs.store import DurableJobStore
from app.scouting_requests.models import ScoutAdmission, ScoutRequest
from app.tests import test_scout_pause_postgres as harness
from app.tests._auth2_support import require_postgres
from app.tests.test_scout_pause_postgres import Sources, _end_state, _start_worker

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
    """The passage of time: the admission's deadline (minutes) lies an hour in the past."""
    with w.factory() as s:
        s.execute(
            update(ScoutAdmission)
            .where(ScoutAdmission.id == admission_id)
            .values(admitted_at=ScoutAdmission.admitted_at - timedelta(hours=hours))
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
    real_fail = DurableJobStore.fail

    def reclaimed_then_fail(self, db, job, **kwargs):
        with w.factory() as other:  # a re-claim of the job row commits at this instant
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
        return real_fail(self, db, job, **kwargs)

    monkeypatch.setattr(DurableJobStore, "fail", reclaimed_then_fail)
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
    with w.factory() as s:
        [item] = list_unresolved(s, w.rid)[UNKNOWN_END]
        assert item["deadline_passed"] is False and item["end_kind"] == "unknown"
        assert resolve(s, admission_id=row, operator="ops", reason="provider_confirmed") is True
        s.commit()
