"""G-R3-1 F-1 (AD-EXPORT-ROLE-01), F-2 and F-3: the protected final-write boundaries.

The operator chose these boundaries (2026-11-20, register §51):

* **F-1 / AD-EXPORT-ROLE-01.** "Export creation must use current, protected membership and role
  authorization. A removal or disqualifying demotion that commits before that boundary must
  prevent the export."
* **F-2.** "Before storing a newly generated draft, revalidate the required approval of the exact
  parent-brief version under appropriate protection. A withdrawal that commits first must prevent
  that draft from being stored."
* **F-3.** "Before storing a newly generated brief, revalidate the author's required workspace
  membership and generation authorization under appropriate protection. A removal that commits
  first must prevent that brief from being stored."
* For all three: "When the legitimate write serializes first, preserve its historical record. Do
  not retroactively rewrite it after a later withdrawal or removal." and "An already-admitted
  provider operation may finish. Preserve existing accounting and charge it exactly once; do not
  invent refunds or erase usage because its output is refused."

**The boundary of each write** is a point, after the model call (F-2, F-3) and before the insert,
where the value the write rests on is read again while every writer of that value is excluded
until the record's transaction ends:

* F-1: the export's organization ``KEY SHARE`` on PostgreSQL (every writer of an existing
  membership locks that organization ``FOR UPDATE`` first), and its ``BEGIN IMMEDIATE`` on SQLite;
  the exporter's membership role is read after it;
* F-2: the brief row ``FOR UPDATE`` at version allocation on PostgreSQL (a brief decision locks the
  same row ``FOR UPDATE`` before it reads), and the writer lock the attempt holds from its first
  write on SQLite; the controlling decision on the exact ``(subject_id, subject_version)`` is read
  after it;
* F-3: the author's ``users`` row and the organization, ``KEY SHARE``, on PostgreSQL (a decision's
  steps 1 and 2), and the writer lock from the capture's first write on SQLite; the author's
  membership role is read after it.

**Through the real paths, on both engines.** The export route; the brief capture route; the
generate route and the ``creative.generate`` job run by the real worker (``JobRunner``); the
membership routes and the brief decision route for the competing side. The mock provider. SQLite is
a file opened by the application's own ``app.db.session.build_engine`` (the fold's
``application_sqlite_world``); PostgreSQL is a database of its own migrated to head
(``TEST_POSTGRES_URL``; unset, it fails in CI and skips elsewhere). Each side is its own request,
on its own thread, session and connection. A side is held at a named point by wrapping a product
function in this process (``_Gate``); nothing in the product is changed by a test.

**Orderings, forced, for each boundary:**

* *(a) committed first:* the competing change commits before the write reaches its boundary (held
  before the export's locks, or during the model call). The write must be refused;
* *(a') in flight first:* the competing change is held after its writes are flushed and before it
  commits, the write is released and must be seen waiting, then the change commits. The write must
  read it and be refused (this is what a read placed before the protection would miss);
* *(b) the record first:* the write is held after its boundary; the change is started and must be
  seen waiting; the write commits, then the change. The record and its audit and notification rows
  stay as written.

**Scope and the production path** (fix pass FX-1, after reviews FX-A FXA-2 and G-R3-1
GR31-F123-2): the re-read is this organization's membership, so a member removed or demoted here
who is an exporter or editor of another organization is still refused; and F-3 is also forced on
the priced, shared-ledger path (PostgreSQL), where the capture's transaction holds no lock from its
usage rows and its own organization lock is what makes it wait.

**What is read back,** raw, on the world's witness engine after both sides finish: the committed
rows (exports, drafts, briefs, approvals, audit), the answer (status, code, message) or the job's
final state and error, the notifications written, and the accounting: one usage row and one ledger
reservation for the one admitted model call, whether its output is stored or refused, never none
and never two. Each test compares everything it observed in one comparison.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from app.approvals import service as approvals_service
from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.briefs import service as brief_service
from app.briefs.models import RecommendationBrief
from app.core.config import get_settings
from app.creative import service as creative_service
from app.creative.models import ContentReview, CreativeDraft
from app.exports import service as export_service
from app.exports.models import Export
from app.llm import service as llm_module
from app.llm.models import LLMBudgetReservation, LLMUsageEvent
from app.main import app
from app.notifications.models import Notification
from app.organizations import members
from app.organizations.models import OrganizationMember
from app.tests._creative_support import CEILINGS, CreativeWorld, postgres_creative_world
from app.tests.test_exports_refusal_preconditions import decide, draft, set_cap
from app.tests.test_i5e_export_serialization import (
    PAST_DEADLOCK_TIMEOUT,
    POLL_SECONDS,
    _export,
    _fresh_organization,
    _Gate,
    _lock_waits,
    _Runner,
)
from app.tests.test_i5e_export_serialization_sqlite import (
    _answer,
    _report,
    _Trace,
    application_sqlite_world,
)

#: The competing membership change, made by the organization's owner through the real routes.
CHANGES = ("removed", "demoted")
CHANGE = pytest.mark.parametrize("change", CHANGES)
#: (a) also with a change the write's role set still admits: it must not refuse the write.
CHANGES_AND_A_PROMOTION = pytest.mark.parametrize("change", (*CHANGES, "promoted"))
#: What each change answers, and the role it leaves (marketer → viewer, marketer → admin).
CHANGED = {"removed": (204,), "demoted": (200,), "promoted": (200,)}
ROLE_AFTER = {"removed": None, "demoted": "viewer", "promoted": "admin"}
#: The answer of a write refused at its boundary: the tenant context's and the role gate's own
#: 403 (``app.auth.dependencies``).
REFUSED = {
    "removed": (403, "permission_denied", "You are not a member of this organization."),
    "demoted": (403, "permission_denied", "Role 'viewer' is not permitted for this action."),
}
#: A withdrawal of the parent brief's approval, through the brief decision route.
WITHDRAWAL = pytest.mark.parametrize("withdrawal", ("revoked", "rejected"))
#: The refusal the generation job already uses for a brief that is not approved
#: (``app.creative.service.execute_generation``): non-retryable, so the job ends ``failed``.
GENERATION_REFUSED = ("failed", "validation", "brief_not_approved")
GENERATION_STORED = ("succeeded", None, None)
#: Where a side is seen waiting (statement texts as the trace and PostgreSQL report them).
WAIT_POINTS = (
    ("BEGIN IMMEDIATE", ("begin immediate",)),
    ("organizations FOR KEY SHARE", ("from organizations", "for key share")),
    ("organizations FOR UPDATE", ("from organizations", "for update")),
    ("recommendation_briefs FOR UPDATE", ("from recommendation_briefs", "for update")),
    ("insert llm_budget_reservations", ("insert into llm_budget_reservations",)),
    ("insert recommendation_briefs", ("insert into recommendation_briefs",)),
    ("insert creative_drafts", ("insert into creative_drafts",)),
    ("update organization_members", ("update organization_members",)),
    ("delete organization_members", ("delete from organization_members",)),
)


# --------------------------------------------------------------------------- #
# Worlds
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def base(request, tmp_path_factory) -> Iterator[CreativeWorld]:
    if request.param == "sqlite":
        with application_sqlite_world(tmp_path_factory.mktemp("f123") / "app.db", "f123") as w:
            yield w
        return
    with postgres_creative_world("f123") as w:  # skips unless TEST_POSTGRES_URL is set
        # Bounded waits on every connection opened from here on: a lock that is never released
        # fails its statement instead of hanging the module.
        with w.witness.connect() as conn:
            name = conn.execute(text("SELECT current_database()")).scalar_one()
            conn.execute(text(f'ALTER DATABASE "{name}" SET lock_timeout = \'45s\''))
            conn.commit()
        w.engine.dispose()
        w.witness.dispose()
        # The server's own answer for an unhandled error, as on SQLite.
        w.client = TestClient(app, raise_server_exceptions=False)
        yield w


@pytest.fixture(autouse=True)
def settings_for_the_races(monkeypatch) -> None:
    """Every generation ceiling set (CF-2 leaves them unset); the global capability flags off (each
    workspace is opened by override rows); the worker's lease renewal (every 10 s by default) kept
    out of every race window, so the only statements that wait are the two sides'."""
    settings = get_settings()
    for name, value in {**CEILINGS, "llm_max_calls_per_workspace_per_day": 1000}.items():
        monkeypatch.setattr(settings, name, value)
    for flag in ("recommendation_briefs_enabled", "creative_generation_enabled",
                 "content_export_enabled", "notifications_analytics_enabled"):
        monkeypatch.setattr(settings, flag, False)
    monkeypatch.setattr(settings, "worker_heartbeat_seconds", 25.0)
    monkeypatch.setattr(settings, "worker_lease_seconds", 60.0)


def _org(base: CreativeWorld, key: str) -> CreativeWorld:
    """A fresh organization (owner, admin, marketer, reviewer) with one workspace in which briefs,
    generation, export and in-app notifications are opened by override rows."""
    w = _fresh_organization(base, f"f123{key}{uuid.uuid4().hex[:4]}")
    for capability in ("creative_generation", "recommendation_briefs", "content_export",
                       "notifications_analytics"):
        set_cap(w, w.ws, capability, True)
    return w


def _is_postgres(w: CreativeWorld) -> bool:
    return w.engine.dialect.name == "postgresql"


# --------------------------------------------------------------------------- #
# The competing side: a membership change, a withdrawal
# --------------------------------------------------------------------------- #
def _change_membership(w: CreativeWorld, change: str):
    """The owner removes the marketer, demotes them to viewer, or promotes them to admin."""
    path = f"/organizations/{w.org}/members/{w.users['marketer']}"
    if change == "removed":
        return w.send("DELETE", path, "owner")
    role = "viewer" if change == "demoted" else "admin"
    return w.send("PUT", f"{path}/role", "owner", {"role": role})


def _hold_change_before_commit(monkeypatch, change: str, gate: _Gate) -> None:
    """The membership change is held after its writes are flushed, before its route commits."""
    name = "remove_member" if change == "removed" else "change_member_role"
    monkeypatch.setattr(members, name, gate.after(getattr(members, name)))


def _role(w: CreativeWorld, user_id: str) -> str | None:
    rows = w.rows(OrganizationMember, organization_id=w.org, user_id=user_id)
    return rows[0]["role"] if rows else None


def _membership_audits(w: CreativeWorld) -> int:
    return len([
        a for a in w.rows(AuditLog, organization_id=w.org)
        if a["action"] in ("organization_member.removed", "organization_member.role_changed")
        and a["entity_id"] == w.users["marketer"]
    ])


def _decide_brief(w: CreativeWorld, brief_id: str, decision: str):
    """The reviewer decides on the brief's exact version through the brief decision route."""
    brief = w.row(RecommendationBrief, brief_id)
    return w.send(
        "POST",
        f"/workspaces/{w.ws}/opportunities/{brief['opportunity_id']}/briefs/{brief_id}/approvals",
        "reviewer", {"decision": decision, "subject_version": brief["brief_version"]},
    )


def _decisions(w: CreativeWorld, subject_id: str) -> list[str]:
    rows = sorted(w.rows(Approval, subject_id=subject_id), key=lambda a: a["decision_seq"])
    return [a["decision"] for a in rows]


# --------------------------------------------------------------------------- #
# Holding a side at the model call, and seeing a side wait
# --------------------------------------------------------------------------- #
def _wrap_the_model_call(monkeypatch, wrap: Callable[[Callable], Callable]) -> None:
    """Replace the model call of the process's LLM service, the one instance every call site
    reaches (``app.llm.service.get_llm_service``: the brief route's dependency and the generation
    handler), by ``wrap(the current call)``. Patched on the instance, not on ``LLMService``: a
    monkeypatch of the instance's ``run`` in an earlier module leaves the bound method in the
    instance's ``__dict__`` when it is undone, which a class-level patch would not reach."""
    service = llm_module.get_llm_service()
    monkeypatch.setattr(service, "run", wrap(service.run))


def _hold_the_model_call(monkeypatch, gate: _Gate, *, after: bool) -> None:
    """The first model call made from here on is held just before the provider is asked
    (``after=False``: the call is in flight) or right after it answered (``after=True``: the
    admitted call is over, nothing of its output is written). Every later call passes."""
    _wrap_the_model_call(monkeypatch, gate.after if after else gate.before)


def _where(statement: str | None) -> str | None:
    """A short name for the statement a side waits in (None: it did not wait)."""
    if statement is None:
        return None
    lowered = " ".join(statement.split()).lower()
    for name, needles in WAIT_POINTS:
        if all(needle in lowered for needle in needles):
            return name
    return lowered[:120]


def _waits(w: CreativeWorld, trace: _Trace, runner: _Runner) -> str | None:
    """Where ``runner`` is seen waiting, or None if it finished without waiting.

    PostgreSQL: the one backend of the database in a heavyweight-lock wait, still the same past
    ``deadlock_timeout`` (a wait, not a cycle). SQLite: the one statement of the application engine
    pending for longer than the fold's threshold (the other side is held, so only the writer lock
    can keep it)."""
    if not _is_postgres(w):
        statement, _sent = trace.waiting(runner)
        return _where(statement)
    deadline = time.monotonic() + POLL_SECONDS
    while time.monotonic() < deadline:
        waiting = _lock_waits(w)
        if waiting:
            assert len(waiting) == 1, waiting
            time.sleep(PAST_DEADLOCK_TIMEOUT)
            assert _lock_waits(w) == waiting, "the wait ended or changed: a cycle?"
            return _where(waiting[0])
        if not runner.is_alive():
            return None
        time.sleep(0.05)
    raise AssertionError(f"timed out after {POLL_SECONDS} s: neither a wait nor an answer")


# --------------------------------------------------------------------------- #
# What was committed
# --------------------------------------------------------------------------- #
def _export_state(w: CreativeWorld, draft_id: str) -> dict:
    exported = w.rows(Export, draft_id=draft_id)
    return {
        "exports (by the marketer)": [e["exported_by"] == w.users["marketer"] for e in exported],
        "export audit rows": w.count(
            AuditLog, action="creative_draft.exported", entity_id=draft_id
        ),
        "export.completed notifications": w.count(
            Notification, workspace_id=w.ws, event_code="export.completed"
        ),
        "the draft's decisions": _decisions(w, draft_id),
    }


def _generation_state(w: CreativeWorld, brief_id: str, job_id: str) -> dict:
    job = w.job(job_id)
    drafts = w.rows(CreativeDraft, workspace_id=w.ws)
    usage = w.rows(LLMUsageEvent, job_id=job_id)
    return {
        "job": (job["status"], job["last_error_code"], job["last_error_summary"]),
        "drafts (brief, version)": sorted(
            ("the parent" if d["brief_id"] == brief_id else d["brief_id"], d["draft_version"])
            for d in drafts
        ),
        "content reviews": w.count(ContentReview, workspace_id=w.ws),
        "draft audit rows": w.count(AuditLog, action="creative_draft.created", workspace_id=w.ws),
        "draft.generated notifications": w.count(
            Notification, workspace_id=w.ws, event_code="draft.generated"
        ),
        "usage rows of the job": len(usage),
        "ledger reservations of the job": w.count(LLMBudgetReservation, job_id=job_id),
        "each draft names the job's usage row": [
            bool(usage) and d["llm_usage_event_id"] == usage[0]["id"] for d in drafts
        ],
        "the parent brief's decisions": _decisions(w, brief_id),
    }


def _capture_state(w: CreativeWorld, opportunity_id: str) -> dict:
    briefs = w.rows(RecommendationBrief, opportunity_id=opportunity_id)
    usage = w.rows(LLMUsageEvent, workspace_id=w.ws, task=brief_service.BRIEF_TASK)
    new = [b for b in briefs if b["brief_version"] > 1]
    return {
        "captured briefs (version, by the marketer)": sorted(
            (b["brief_version"], b["created_by"] == w.users["marketer"]) for b in new
        ),
        "brief audit rows": w.count(
            AuditLog, action="recommendation_brief.created", workspace_id=w.ws
        ),
        "brief.created notifications": w.count(
            Notification, workspace_id=w.ws, event_code="brief.created"
        ),
        "usage rows of the capture": len(usage),
        "ledger reservations of the workspace": w.count(LLMBudgetReservation, workspace_id=w.ws),
        "each brief names the capture's usage row": [
            bool(usage) and b["llm_usage_event_id"] == usage[0]["id"] for b in new
        ],
    }


# --------------------------------------------------------------------------- #
# Seeding and the writes
# --------------------------------------------------------------------------- #
def _exportable(w: CreativeWorld) -> str:
    """An approved draft (by the marketer, passed review) of an approved brief."""
    made = draft(w, w.ws)
    decide(w, made, "approved")
    return made


def _generate(w: CreativeWorld, brief_id: str) -> str:
    """The marketer's generate request (202) through the route; returns the job id."""
    queued = w.send("POST", f"/workspaces/{w.ws}/briefs/{brief_id}/drafts", "marketer",
                    {"channel": "social_post"})
    assert queued.status_code == 202, queued.text
    return queued.json()["job_id"]


def _capturable(w: CreativeWorld) -> tuple[str, str]:
    """``(opportunity id, intelligence record id)`` of an opportunity with one earlier brief
    version (seeded, not approved): a capture makes version 2."""
    seeded = w.row(RecommendationBrief, w.seed_brief(w.ws, approved=False))
    return seeded["opportunity_id"], seeded["intelligence_record_id"]


def _capture(w: CreativeWorld, opportunity_id: str, record_id: str):
    """The marketer's brief capture through the real route (the model call included)."""
    return w.send("POST", f"/workspaces/{w.ws}/opportunities/{opportunity_id}/briefs", "marketer",
                  {"intelligence_record_id": record_id})


def _in_flight(start: Callable, gate: _Gate, what: str) -> _Runner:
    runner = _Runner(start)
    runner.start()
    gate.wait(what)
    return runner


# =========================================================================== #
# F-1 / AD-EXPORT-ROLE-01: export creation
# =========================================================================== #
@CHANGES_AND_A_PROMOTION
def test_f1_a_change_committed_before_the_export_boundary_decides_the_export(
    base: CreativeWorld, monkeypatch, change: str
):
    """(a) The export has resolved the marketer's role and passed its role and capability gates;
    it is held before its locks. The owner's change commits. The export must then read it: a
    removal or a demotion to viewer refuses it (403, nothing written); a promotion to admin, still
    an exporter role, does not."""
    w = _org(base, "f1a")
    made = _exportable(w)
    gate = _Gate()
    monkeypatch.setattr(export_service, "hold_sqlite_writer_lock",
                        gate.before(export_service.hold_sqlite_writer_lock))
    exporting = _in_flight(lambda: _export(w, w.ws, made, "marketer"), gate, "the export")
    try:
        changed = _change_membership(w, change)
    finally:
        gate.release.set()
    exported = exporting.outcome()
    admitted = change == "promoted"
    _report("F-1 (a)", {
        "change": _answer(changed),
        "export": _answer(exported),
        **_export_state(w, made),
        "membership": _role(w, w.users["marketer"]),
    }, {
        "change": CHANGED[change],
        "export": (201,) if admitted else REFUSED[change],
        "exports (by the marketer)": [True] if admitted else [],
        "export audit rows": 1 if admitted else 0,
        "export.completed notifications": 1 if admitted else 0,  # the approving reviewer
        "the draft's decisions": ["approved"],
        "membership": ROLE_AFTER[change],
    })


@CHANGE
def test_f1_a_change_in_flight_at_the_export_boundary_is_waited_for_and_refuses_the_export(
    base: CreativeWorld, monkeypatch, change: str
):
    """(a') The change holds the organization (PostgreSQL ``FOR UPDATE``) or the writer lock
    (SQLite) with its write flushed and uncommitted when the export arrives. The export waits at
    its boundary (the organization ``KEY SHARE``; ``BEGIN IMMEDIATE``), the change commits, and
    the export must read the committed membership and be refused."""
    w = _org(base, "f1i")
    made = _exportable(w)
    gate = _Gate()
    _hold_change_before_commit(monkeypatch, change, gate)
    changing = _in_flight(lambda: _change_membership(w, change), gate, "the change")
    try:
        with _Trace(w.engine) as trace:
            exporting = _Runner(lambda: _export(w, w.ws, made, "marketer"))
            exporting.start()
            waited_at = _waits(w, trace, exporting)
    finally:
        gate.release.set()
    _report("F-1 (a')", {
        "the export waited at": waited_at,
        "change": _answer(changing.outcome()),
        "export": _answer(exporting.outcome()),
        **_export_state(w, made),
        "membership": _role(w, w.users["marketer"]),
    }, {
        "the export waited at": (
            "organizations FOR KEY SHARE" if _is_postgres(w) else "BEGIN IMMEDIATE"
        ),
        "change": CHANGED[change],
        "export": REFUSED[change],
        "exports (by the marketer)": [],
        "export audit rows": 0,
        "export.completed notifications": 0,
        "the draft's decisions": ["approved"],
        "membership": ROLE_AFTER[change],
    })


@CHANGE
def test_f1_an_export_past_its_boundary_is_kept_and_the_change_waits_for_it(
    base: CreativeWorld, monkeypatch, change: str
):
    """(b) The export is held after its locks (PostgreSQL: user, organization, draft, brief;
    SQLite: the writer lock). The change starts and must wait (the organization ``FOR UPDATE``; its
    first write). The export commits (201), then the change. The export row, its audit row and its
    notification stay exactly as written."""
    w = _org(base, "f1b")
    made = _exportable(w)
    gate = _Gate()
    monkeypatch.setattr(export_service, "_lock_for_export",
                        gate.after(export_service._lock_for_export))
    exporting = _in_flight(lambda: _export(w, w.ws, made, "marketer"), gate, "the export")
    try:
        with _Trace(w.engine) as trace:
            changing = _Runner(lambda: _change_membership(w, change))
            changing.start()
            waited_at = _waits(w, trace, changing)
    finally:
        gate.release.set()
    exported = exporting.outcome()
    written = {
        "export": w.rows(Export, draft_id=made),
        "audit": w.rows(AuditLog, action="creative_draft.exported", entity_id=made),
        "notifications": w.rows(Notification, workspace_id=w.ws, event_code="export.completed"),
    }
    changed = changing.outcome()
    after = {
        "export": w.rows(Export, draft_id=made),
        "audit": w.rows(AuditLog, action="creative_draft.exported", entity_id=made),
        "notifications": w.rows(Notification, workspace_id=w.ws, event_code="export.completed"),
    }
    _report("F-1 (b)", {
        "the change waited at": waited_at,
        "export": _answer(exported),
        "change": _answer(changed),
        **_export_state(w, made),
        "the export's rows unchanged by the change": after == written,
        "membership": _role(w, w.users["marketer"]),
        "membership audit rows": _membership_audits(w),
    }, {
        "the change waited at": (
            "organizations FOR UPDATE" if _is_postgres(w)
            else ("delete organization_members" if change == "removed"
                  else "update organization_members")
        ),
        "export": (201,),
        "change": CHANGED[change],
        "exports (by the marketer)": [True],
        "export audit rows": 1,
        "export.completed notifications": 1,
        "the draft's decisions": ["approved"],
        "the export's rows unchanged by the change": True,
        "membership": ROLE_AFTER[change],
        "membership audit rows": 1,
    })


# =========================================================================== #
# F-2: storing a generated draft
# =========================================================================== #
@WITHDRAWAL
def test_f2_a_withdrawal_committed_during_the_model_call_prevents_the_draft(
    base: CreativeWorld, monkeypatch, withdrawal: str
):
    """(a) The job has passed every gate (the brief approved) and is held in its model call. The
    reviewer's withdrawal of the brief's approval commits. The admitted call then finishes; the
    draft must not be stored: the job ends ``failed`` with the refusal it already uses
    (``validation``, ``brief_not_approved``), with no draft, review, audit row or notification,
    and the call's usage row and ledger reservation kept, once."""
    w = _org(base, "f2a")
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    gate = _Gate()
    _hold_the_model_call(monkeypatch, gate, after=False)
    generating = _in_flight(w.drain, gate, "the generation")
    try:
        withdrawn = _decide_brief(w, brief_id, withdrawal)
    finally:
        gate.release.set()
    generating.outcome()
    _report("F-2 (a)", {
        "withdrawal": _answer(withdrawn),
        **_generation_state(w, brief_id, job_id),
    }, {
        "withdrawal": (201,),
        "job": GENERATION_REFUSED,
        "drafts (brief, version)": [],
        "content reviews": 0,
        "draft audit rows": 0,
        "draft.generated notifications": 0,
        "usage rows of the job": 1,
        "ledger reservations of the job": 1,
        "each draft names the job's usage row": [],
        "the parent brief's decisions": ["approved", withdrawal],
    })


def test_f2_a_withdrawal_in_flight_at_the_generation_boundary_is_waited_for_and_prevents_the_draft(
    base: CreativeWorld, monkeypatch
):
    """(a') The job's model call has answered; nothing of it is written. The revocation is then
    held with its decision row flushed and uncommitted (PostgreSQL: the brief ``FOR UPDATE``;
    SQLite: the writer lock). The job is released and must wait (PostgreSQL: the brief lock at
    version allocation; SQLite: its ``BEGIN IMMEDIATE`` before version allocation, the call's rows
    being written in transactions of their own (O-F123-2, Design B)), the revocation commits, and
    the job must read it and store nothing."""
    w = _org(base, "f2i")
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    model, held = _Gate(), _Gate()
    _hold_the_model_call(monkeypatch, model, after=True)
    monkeypatch.setattr(approvals_service, "notify", held.before(approvals_service.notify))
    generating = _in_flight(w.drain, model, "the generation")
    try:
        revoking = _in_flight(lambda: _decide_brief(w, brief_id, "revoked"), held,
                              "the revocation")
        with _Trace(w.engine) as trace:
            model.release.set()
            waited_at = _waits(w, trace, generating)
    finally:
        model.release.set()
        held.release.set()
    _report("F-2 (a')", {
        "the job waited at": waited_at,
        "revocation": _answer(revoking.outcome()),
        "drained": generating.outcome(),
        **_generation_state(w, brief_id, job_id),
    }, {
        "the job waited at": (
            "recommendation_briefs FOR UPDATE" if _is_postgres(w)
            else "BEGIN IMMEDIATE"
        ),
        "revocation": (201,),
        "drained": 1,
        "job": GENERATION_REFUSED,
        "drafts (brief, version)": [],
        "content reviews": 0,
        "draft audit rows": 0,
        "draft.generated notifications": 0,
        "usage rows of the job": 1,
        "ledger reservations of the job": 1,
        "each draft names the job's usage row": [],
        "the parent brief's decisions": ["approved", "revoked"],
    })


def test_f2_a_generation_past_its_boundary_stores_its_draft_and_the_withdrawal_waits_for_it(
    base: CreativeWorld, monkeypatch
):
    """(b) The job is held after version allocation (PostgreSQL: the brief ``FOR UPDATE``;
    SQLite: the writer lock). The revocation starts and must wait (the brief's row lock; its
    ``BEGIN IMMEDIATE``). The draft, its review, its audit row and its notification commit, then
    the revocation; the draft stays exactly as written."""
    w = _org(base, "f2b")
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    gate = _Gate()
    monkeypatch.setattr(creative_service, "_next_version",
                        gate.after(creative_service._next_version))
    generating = _in_flight(w.drain, gate, "the generation")
    try:
        with _Trace(w.engine) as trace:
            revoking = _Runner(lambda: _decide_brief(w, brief_id, "revoked"))
            revoking.start()
            waited_at = _waits(w, trace, revoking)
    finally:
        gate.release.set()
    drained = generating.outcome()
    written = w.rows(CreativeDraft, workspace_id=w.ws)
    revoked = revoking.outcome()
    _report("F-2 (b)", {
        "the revocation waited at": waited_at,
        "drained": drained,
        "revocation": _answer(revoked),
        **_generation_state(w, brief_id, job_id),
        "the draft unchanged by the revocation": w.rows(CreativeDraft, workspace_id=w.ws)
        == written,
    }, {
        "the revocation waited at": (
            "recommendation_briefs FOR UPDATE" if _is_postgres(w) else "BEGIN IMMEDIATE"
        ),
        "drained": 1,
        "revocation": (201,),
        "job": GENERATION_STORED,
        "drafts (brief, version)": [("the parent", 1)],
        "content reviews": 1,
        "draft audit rows": 1,
        "draft.generated notifications": 1,
        "usage rows of the job": 1,
        "ledger reservations of the job": 1,
        "each draft names the job's usage row": [True],
        "the parent brief's decisions": ["approved", "revoked"],
        "the draft unchanged by the revocation": True,
    })


def test_f2_a_newer_brief_version_during_the_model_call_does_not_prevent_the_draft(
    base: CreativeWorld, monkeypatch
):
    """Supersession: a new version of the brief is captured through its route while the job's
    model call is in flight. The approval the job re-validates is the exact parent version's, which
    the new version does not change: the draft is stored against that version."""
    w = _org(base, "f2s")
    brief_id = w.seed_brief(w.ws)
    parent = w.row(RecommendationBrief, brief_id)
    job_id = _generate(w, brief_id)
    gate = _Gate()
    _hold_the_model_call(monkeypatch, gate, after=False)
    generating = _in_flight(w.drain, gate, "the generation")
    try:
        newer = _capture(w, parent["opportunity_id"], parent["intelligence_record_id"])
    finally:
        gate.release.set()
    generating.outcome()
    state = _generation_state(w, brief_id, job_id)
    _report("F-2 supersession", {
        "the newer version": (newer.status_code, newer.json().get("brief_version")),
        "job": state["job"],
        "drafts (brief, version)": state["drafts (brief, version)"],
        "usage rows of the job": state["usage rows of the job"],
        "the parent brief's decisions": state["the parent brief's decisions"],
    }, {
        "the newer version": (201, 2),
        "job": GENERATION_STORED,
        "drafts (brief, version)": [("the parent", 1)],
        "usage rows of the job": 1,
        "the parent brief's decisions": ["approved"],
    })


# =========================================================================== #
# F-3: storing a generated brief
# =========================================================================== #
@CHANGES_AND_A_PROMOTION
def test_f3_a_change_committed_during_the_model_call_decides_the_brief(
    base: CreativeWorld, monkeypatch, change: str
):
    """(a) The capture has passed the editors' gate and is held in its model call. The owner's
    change commits. The admitted call finishes; a removal or a demotion to viewer must prevent the
    brief (403, as the gate answers; no brief, audit row or notification) and keep the call's usage
    row and ledger reservation, once; a promotion to admin does not prevent it."""
    w = _org(base, "f3a")
    opportunity_id, record_id = _capturable(w)
    gate = _Gate()
    _hold_the_model_call(monkeypatch, gate, after=False)
    capturing = _in_flight(lambda: _capture(w, opportunity_id, record_id), gate, "the capture")
    try:
        changed = _change_membership(w, change)
    finally:
        gate.release.set()
    captured = capturing.outcome()
    admitted = change == "promoted"
    _report("F-3 (a)", {
        "change": _answer(changed),
        "capture": _answer(captured),
        **_capture_state(w, opportunity_id),
        "membership": _role(w, w.users["marketer"]),
    }, {
        "change": CHANGED[change],
        "capture": (201,) if admitted else REFUSED[change],
        "captured briefs (version, by the marketer)": [(2, True)] if admitted else [],
        "brief audit rows": 1 if admitted else 0,
        "brief.created notifications": 1 if admitted else 0,  # the organization's reviewer
        "usage rows of the capture": 1,
        "ledger reservations of the workspace": 1,
        "each brief names the capture's usage row": [True] if admitted else [],
        "membership": ROLE_AFTER[change],
    })


@CHANGE
def test_f3_a_change_in_flight_at_the_capture_boundary_is_waited_for_and_prevents_the_brief(
    base: CreativeWorld, monkeypatch, change: str
):
    """(a') The capture's model call has answered, nothing of it written. The change is then held
    with its write flushed and uncommitted. The capture is released and must wait, the change
    commits, and the capture must read the committed membership: 403, no brief, usage kept once.

    Where it waits differs by engine and by provider: with the mock the usage rows are written in
    the request's own transaction right after the call, and their foreign keys take the
    organization ``KEY SHARE`` on PostgreSQL, so the capture meets the change there; on SQLite the
    call's rows are written in transactions of their own (O-F123-2, Design B), so the capture meets
    the change at its ``BEGIN IMMEDIATE`` before the author re-read."""
    w = _org(base, "f3i")
    opportunity_id, record_id = _capturable(w)
    model, held = _Gate(), _Gate()
    _hold_the_model_call(monkeypatch, model, after=True)
    _hold_change_before_commit(monkeypatch, change, held)
    capturing = _in_flight(lambda: _capture(w, opportunity_id, record_id), model, "the capture")
    try:
        changing = _in_flight(lambda: _change_membership(w, change), held, "the change")
        with _Trace(w.engine) as trace:
            model.release.set()
            waited_at = _waits(w, trace, capturing)
    finally:
        model.release.set()
        held.release.set()
    _report("F-3 (a')", {
        "the capture waited at": waited_at,
        "change": _answer(changing.outcome()),
        "capture": _answer(capturing.outcome()),
        **_capture_state(w, opportunity_id),
        "membership": _role(w, w.users["marketer"]),
    }, {
        "the capture waited at": ("insert llm_budget_reservations" if _is_postgres(w)
                                  else "BEGIN IMMEDIATE"),
        "change": CHANGED[change],
        "capture": REFUSED[change],
        "captured briefs (version, by the marketer)": [],
        "brief audit rows": 0,
        "brief.created notifications": 0,
        "usage rows of the capture": 1,
        "ledger reservations of the workspace": 1,
        "each brief names the capture's usage row": [],
        "membership": ROLE_AFTER[change],
    })


@CHANGE
def test_f3_a_capture_past_its_boundary_stores_its_brief_and_the_change_waits_for_it(
    base: CreativeWorld, monkeypatch, change: str
):
    """(b) The capture is held after version allocation (past its re-read). The change starts and
    must wait (PostgreSQL: the organization ``FOR UPDATE``; SQLite: its first write). The brief,
    its audit row and its notification commit, then the change; the brief stays as written, its
    author included."""
    w = _org(base, "f3b")
    opportunity_id, record_id = _capturable(w)
    gate = _Gate()
    monkeypatch.setattr(brief_service, "_next_version", gate.after(brief_service._next_version))
    capturing = _in_flight(lambda: _capture(w, opportunity_id, record_id), gate, "the capture")
    try:
        with _Trace(w.engine) as trace:
            changing = _Runner(lambda: _change_membership(w, change))
            changing.start()
            waited_at = _waits(w, trace, changing)
    finally:
        gate.release.set()
    captured = capturing.outcome()
    written = w.rows(RecommendationBrief, opportunity_id=opportunity_id)
    changed = changing.outcome()
    _report("F-3 (b)", {
        "the change waited at": waited_at,
        "capture": _answer(captured),
        "change": _answer(changed),
        **_capture_state(w, opportunity_id),
        "the brief unchanged by the change": w.rows(
            RecommendationBrief, opportunity_id=opportunity_id
        ) == written,
        "membership": _role(w, w.users["marketer"]),
    }, {
        "the change waited at": (
            "organizations FOR UPDATE" if _is_postgres(w)
            else ("delete organization_members" if change == "removed"
                  else "update organization_members")
        ),
        "capture": (201,),
        "change": CHANGED[change],
        "captured briefs (version, by the marketer)": [(2, True)],
        "brief audit rows": 1,
        "brief.created notifications": 1,
        "usage rows of the capture": 1,
        "ledger reservations of the workspace": 1,
        "each brief names the capture's usage row": [True],
        "the brief unchanged by the change": True,
        "membership": ROLE_AFTER[change],
    })


@pytest.mark.parametrize(
    "role", ["owner", "admin", "marketer", "reviewer", "compliance_reviewer", "viewer"]
)
def test_f3_the_role_read_at_the_boundary_decides_as_the_capture_gate_would(
    base: CreativeWorld, monkeypatch, role: str
):
    """The generation authorization re-read at the boundary is the capture route's own: the
    editors (owner, admin, marketer) are admitted, every other role is refused with the gate's
    answer. The marketer's role is changed by the owner during the model call."""
    w = _org(base, "f3r")
    opportunity_id, record_id = _capturable(w)
    gate = _Gate()
    _hold_the_model_call(monkeypatch, gate, after=False)
    capturing = _in_flight(lambda: _capture(w, opportunity_id, record_id), gate, "the capture")
    try:
        if role != "marketer":
            changed = w.send("PUT", f"/organizations/{w.org}/members/{w.users['marketer']}/role",
                             "owner", {"role": role})
            assert changed.status_code == 200, changed.text
    finally:
        gate.release.set()
    admitted = role in ("owner", "admin", "marketer")
    _report("F-3 roles", {
        "capture": _answer(capturing.outcome()),
        "usage rows of the capture": _capture_state(w, opportunity_id)[
            "usage rows of the capture"
        ],
    }, {
        "capture": (201,) if admitted else (
            403, "permission_denied", f"Role '{role}' is not permitted for this action."
        ),
        "usage rows of the capture": 1,
    })


# =========================================================================== #
# The re-read's organization scope, and F-3's production (priced) path (fix pass FX-1)
# =========================================================================== #
def _member_elsewhere(base: CreativeWorld, w: CreativeWorld) -> str:
    """The marketer also holds a ``marketer`` row in a second, fresh organization (written
    directly, as an accepted invitation leaves it). Returns that organization's id."""
    other = _fresh_organization(base, f"f123else{uuid.uuid4().hex[:4]}")
    with w.session() as s:
        s.add(OrganizationMember(organization_id=other.org, user_id=w.users["marketer"],
                                 role="marketer"))
    return other.org


def _role_in(w: CreativeWorld, organization_id: str, user_id: str) -> str | None:
    rows = w.rows(OrganizationMember, organization_id=organization_id, user_id=user_id)
    return rows[0]["role"] if rows else None


@CHANGE
def test_f1_a_change_here_refuses_the_export_of_a_member_who_is_an_exporter_elsewhere(
    base: CreativeWorld, monkeypatch, change: str
):
    """(a), the re-read's scope (review FX-A, FXA-2). The marketer is also a marketer of another
    organization. The export is in flight past its gates; the owner removes the marketer from
    this organization, or demotes them to viewer, and that commits. The re-read is this
    organization's membership, so the export is refused (403) and writes nothing; the membership
    elsewhere is untouched and does not admit it."""
    w = _org(base, "f1x")
    other = _member_elsewhere(base, w)
    made = _exportable(w)
    gate = _Gate()
    monkeypatch.setattr(export_service, "hold_sqlite_writer_lock",
                        gate.before(export_service.hold_sqlite_writer_lock))
    exporting = _in_flight(lambda: _export(w, w.ws, made, "marketer"), gate, "the export")
    try:
        changed = _change_membership(w, change)
    finally:
        gate.release.set()
    exported = exporting.outcome()
    _report("F-1 scope", {
        "change": _answer(changed),
        "export": _answer(exported),
        **_export_state(w, made),
        "membership": _role(w, w.users["marketer"]),
        "membership elsewhere": _role_in(w, other, w.users["marketer"]),
    }, {
        "change": CHANGED[change],
        "export": REFUSED[change],
        "exports (by the marketer)": [],
        "export audit rows": 0,
        "export.completed notifications": 0,
        "the draft's decisions": ["approved"],
        "membership": ROLE_AFTER[change],
        "membership elsewhere": "marketer",
    })


@CHANGE
def test_f3_a_change_here_during_the_model_call_refuses_the_brief_of_an_editor_elsewhere(
    base: CreativeWorld, monkeypatch, change: str
):
    """(a), the re-read's scope (review FX-A, FXA-2). The marketer is also a marketer of another
    organization. The capture is in its model call; the owner removes the marketer from this
    organization, or demotes them to viewer, and that commits. The re-read is this organization's
    membership: 403, no brief, audit row or notification, and the admitted call's usage row and
    reservation kept, once. The membership elsewhere is untouched and does not admit it."""
    w = _org(base, "f3x")
    other = _member_elsewhere(base, w)
    opportunity_id, record_id = _capturable(w)
    gate = _Gate()
    _hold_the_model_call(monkeypatch, gate, after=False)
    capturing = _in_flight(lambda: _capture(w, opportunity_id, record_id), gate, "the capture")
    try:
        changed = _change_membership(w, change)
    finally:
        gate.release.set()
    captured = capturing.outcome()
    _report("F-3 scope", {
        "change": _answer(changed),
        "capture": _answer(captured),
        **_capture_state(w, opportunity_id),
        "membership": _role(w, w.users["marketer"]),
        "membership elsewhere": _role_in(w, other, w.users["marketer"]),
    }, {
        "change": CHANGED[change],
        "capture": REFUSED[change],
        "captured briefs (version, by the marketer)": [],
        "brief audit rows": 0,
        "brief.created notifications": 0,
        "usage rows of the capture": 1,
        "ledger reservations of the workspace": 1,
        "each brief names the capture's usage row": [],
        "membership": ROLE_AFTER[change],
        "membership elsewhere": "marketer",
    })


@CHANGE
def test_f3_a_change_in_flight_on_the_priced_shared_ledger_path_is_waited_for_and_prevents_it(
    base: CreativeWorld, monkeypatch, change: str
):
    """(a') on F-3's production path (review G-R3-1, GR31-F123-2). A priced provider (the real
    Anthropic adapter over an httpx ``MockTransport``, offline: ``test_briefs_postgres._priced``)
    puts the meter on the shared ledger: the call's reservation and usage row are committed in
    short transactions of their own, so the capture's transaction holds no organization lock when
    the call returns (with the mock it does, through its usage rows' foreign keys). The change is
    then held with its write flushed and uncommitted. The capture is released and must wait at
    its own organization ``KEY SHARE``, then read the committed membership: 403, no brief, audit
    row or notification, and exactly one usage row (priced) and one settled reservation.

    PostgreSQL only: the shared ledger is PostgreSQL's (``app.llm.metering``); on SQLite every
    provider's usage rows join the capture's own transaction, the path the other F-3 tests run."""
    if not _is_postgres(base):
        pytest.skip("the shared ledger (a priced provider's meter) is PostgreSQL's")
    from app.tests.test_briefs_postgres import _priced

    w = _org(base, "f3p")
    opportunity_id, record_id = _capturable(w)
    priced, _settings, _bodies = _priced()
    model, held = _Gate(), _Gate()
    _wrap_the_model_call(monkeypatch, lambda _mock: model.after(priced.run))
    _hold_change_before_commit(monkeypatch, change, held)
    capturing = _in_flight(lambda: _capture(w, opportunity_id, record_id), model, "the capture")
    try:
        changing = _in_flight(lambda: _change_membership(w, change), held, "the change")
        with _Trace(w.engine) as trace:
            model.release.set()
            waited_at = _waits(w, trace, capturing)
    finally:
        model.release.set()
        held.release.set()
    usage = w.rows(LLMUsageEvent, workspace_id=w.ws, task=brief_service.BRIEF_TASK)
    _report("F-3 priced", {
        "the capture waited at": waited_at,
        "change": _answer(changing.outcome()),
        "capture": _answer(capturing.outcome()),
        **_capture_state(w, opportunity_id),
        "the reservations' states": sorted(
            r["state"] for r in w.rows(LLMBudgetReservation, workspace_id=w.ws)
        ),
        "the usage row is priced": [u["cost_usd"] > 0 for u in usage],
        "membership": _role(w, w.users["marketer"]),
    }, {
        "the capture waited at": "organizations FOR KEY SHARE",
        "change": CHANGED[change],
        "capture": REFUSED[change],
        "captured briefs (version, by the marketer)": [],
        "brief audit rows": 0,
        "brief.created notifications": 0,
        "usage rows of the capture": 1,
        "ledger reservations of the workspace": 1,
        "each brief names the capture's usage row": [],
        "the reservations' states": ["settled"],
        "the usage row is priced": [True],
        "membership": ROLE_AFTER[change],
    })


# =========================================================================== #
# The mechanism: where each re-read is, and what holds it
# =========================================================================== #
MODEL_RETURNED = "-- the model call returned"
USER_LOCK = "select users.id from users where users.id ="
ORGANIZATION_LOCK = "select organizations.id from organizations where organizations.id ="
MEMBERSHIP_READ = "select organization_members.role from organization_members"
DRAFT_LOCK = "select creative_drafts.id from creative_drafts where creative_drafts.id ="
BRIEF_ROW_LOCK = "select recommendation_briefs.id from recommendation_briefs where"
OPPORTUNITY_LOCK = "select opportunities.id from opportunities where opportunities.id ="
APPROVAL_READ = "select approvals.decision from approvals"


def _mark_the_model_call(monkeypatch, trace: _Trace) -> None:
    """Put a marker into the trace when a model call returns."""

    def wrap(real: Callable) -> Callable:
        def marked(*args, **kwargs):
            result = real(*args, **kwargs)
            with trace._lock:
                trace.sent.append(MODEL_RETURNED)
            return result

        return marked

    _wrap_the_model_call(monkeypatch, wrap)


def _positions(sent: list[str], prefix: str) -> list[int]:
    return [i for i, q in enumerate(sent) if q.startswith(prefix)]


def _first_insert(sent: list[str], table: str) -> int:
    [position] = _positions(sent, f"insert into {table} ")
    return position


def test_f1_the_membership_is_read_after_the_organization_lock_and_before_the_draft_lock(
    base: CreativeWorld,
):
    """The export's statements: the user and organization locks, then the exporter's membership
    role (fresh, a column read), then the draft and brief locks, then the inserts. On SQLite all of
    it after the one ``BEGIN IMMEDIATE``; on PostgreSQL the membership read takes no lock of its own
    (the organization ``KEY SHARE`` already excludes every membership writer)."""
    w = _org(base, "f1o")
    made = _exportable(w)
    with _Trace(w.engine) as trace:
        assert _answer(_export(w, w.ws, made, "marketer")) == (201,)
    sent = trace.sent
    [user] = _positions(sent, USER_LOCK)
    [organization] = _positions(sent, ORGANIZATION_LOCK)
    membership = _positions(sent, MEMBERSHIP_READ)
    [draft_lock] = _positions(sent, DRAFT_LOCK)
    assert user < organization < draft_lock < _first_insert(sent, "exports"), sent
    assert len(membership) == 1 and organization < membership[0] < draft_lock, sent
    if _is_postgres(w):
        assert " for " not in sent[membership[0]], sent[membership[0]]
    else:
        assert sent.index("begin immediate") < user


def test_f2_the_approval_is_read_after_the_model_call_and_the_brief_lock_and_before_the_insert(
    base: CreativeWorld, monkeypatch
):
    """The job's last read of the parent brief's controlling decision comes after the model call
    returned and after version allocation's brief lock, and before the draft insert."""
    w = _org(base, "f2o")
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    with _Trace(w.engine) as trace:
        _mark_the_model_call(monkeypatch, trace)
        assert w.drain() == 1
    assert w.job(job_id)["status"] == "succeeded"
    sent = trace.sent
    returned = sent.index(MODEL_RETURNED)
    brief_lock = [i for i in _positions(sent, BRIEF_ROW_LOCK) if i > returned]
    approval_reads = _positions(sent, APPROVAL_READ)
    assert len(brief_lock) == 1, sent
    assert approval_reads and returned < brief_lock[0] < approval_reads[-1], sent
    assert approval_reads[-1] < _first_insert(sent, "creative_drafts"), sent
    if _is_postgres(w):
        assert sent[brief_lock[0]].endswith("for update"), sent[brief_lock[0]]


def test_f3_the_membership_is_read_after_the_model_call_under_the_user_and_organization_locks(
    base: CreativeWorld, monkeypatch
):
    """The capture's statements after the model call: the author's user lock, the organization
    lock, the author's membership role, then the opportunity lock and the brief insert. On
    PostgreSQL the two locks are ``KEY SHARE`` and the membership read takes no lock of its own."""
    w = _org(base, "f3o")
    opportunity_id, record_id = _capturable(w)
    with _Trace(w.engine) as trace:
        _mark_the_model_call(monkeypatch, trace)
        assert _answer(_capture(w, opportunity_id, record_id)) == (201,)
    sent = trace.sent
    returned = sent.index(MODEL_RETURNED)
    after = {
        name: [i for i in _positions(sent, prefix) if i > returned]
        for name, prefix in (("user", USER_LOCK), ("organization", ORGANIZATION_LOCK),
                             ("membership", MEMBERSHIP_READ), ("opportunity", OPPORTUNITY_LOCK))
    }
    assert all(len(found) == 1 for found in after.values()), (after, sent)
    order = [after[name][0] for name in ("user", "organization", "membership", "opportunity")]
    assert order == sorted(order) and order[-1] < _first_insert(sent, "recommendation_briefs"), sent
    if _is_postgres(w):
        assert sent[after["user"][0]].endswith("for key share")
        assert sent[after["organization"][0]].endswith("for key share")
        assert " for " not in sent[after["membership"][0]]


def _writer_lock_probe(db_file: str) -> str:
    """``held`` if another connection holds SQLite's writer lock now (a ``BEGIN IMMEDIATE`` with
    no busy timeout fails at once), ``free`` otherwise."""
    raw = sqlite3.connect(db_file, timeout=0, isolation_level=None)
    try:
        raw.execute("BEGIN IMMEDIATE")
        raw.execute("ROLLBACK")
        return "free"
    except sqlite3.OperationalError as exc:
        return "held" if "locked" in str(exc) else str(exc)
    finally:
        raw.close()


class _LockedAt:
    """For each statement of the application engine that starts with one of ``prefixes``: whether
    its own connection was in a transaction, and whether SQLite's writer lock was held, at the
    moment it was sent (after a model call returned, when ``after_model`` is set)."""

    def __init__(self, w: CreativeWorld, prefixes: tuple[str, ...], *, after_model: bool):
        self._engine, self._file = w.engine, w.engine.url.database
        self._prefixes, self._after_model = prefixes, after_model
        self.model_returned = threading.Event()
        self.seen: list[tuple[str, bool, str]] = []

    def _before(self, conn, cursor, statement, parameters, context, executemany) -> None:
        lowered = " ".join(statement.split()).lower()
        if self._after_model and not self.model_returned.is_set():
            return
        for prefix in self._prefixes:
            if lowered.startswith(prefix):
                in_transaction = conn.connection.dbapi_connection.in_transaction
                self.seen.append((prefix, in_transaction, _writer_lock_probe(self._file)))

    def __enter__(self) -> _LockedAt:
        event.listen(self._engine, "before_cursor_execute", self._before)
        return self

    def __exit__(self, *exc) -> None:
        event.remove(self._engine, "before_cursor_execute", self._before)


def _signal_the_model_call(monkeypatch, returned: threading.Event) -> None:
    def wrap(real: Callable) -> Callable:
        def signalled(*args, **kwargs):
            result = real(*args, **kwargs)
            returned.set()
            return result

        return signalled

    _wrap_the_model_call(monkeypatch, wrap)


def test_sqlite_each_protected_reread_runs_while_the_writer_lock_is_held(
    base: CreativeWorld, monkeypatch
):
    """On SQLite the protection is the database's writer lock, held by the writing transaction
    from its ``BEGIN IMMEDIATE`` (export) or its first write (the usage rows of the capture and of
    the job) to its commit. Each re-read is sent while that transaction is open and the lock is
    held: another connection cannot take it at that moment."""
    if _is_postgres(base):
        pytest.skip("SQLite's writer lock: PostgreSQL's row locks are pinned by the order tests")
    w = _org(base, "sqlk")
    made = _exportable(w)
    with _LockedAt(w, (MEMBERSHIP_READ,), after_model=False) as export_reads:
        assert _answer(_export(w, w.ws, made, "marketer")) == (201,)
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    with _LockedAt(w, (APPROVAL_READ,), after_model=True) as job_reads:
        _signal_the_model_call(monkeypatch, job_reads.model_returned)
        assert w.drain() == 1
    assert w.job(job_id)["status"] == "succeeded"
    opportunity_id, record_id = _capturable(w)
    with _LockedAt(w, (MEMBERSHIP_READ,), after_model=True) as capture_reads:
        _signal_the_model_call(monkeypatch, capture_reads.model_returned)
        assert _answer(_capture(w, opportunity_id, record_id)) == (201,)
    _report("SQLite writer lock at each re-read", {
        "F-1": export_reads.seen,
        "F-2": job_reads.seen,
        "F-3": capture_reads.seen,
    }, {
        "F-1": [(MEMBERSHIP_READ, True, "held")],
        "F-2": [(APPROVAL_READ, True, "held")],
        "F-3": [(MEMBERSHIP_READ, True, "held")],
    })
