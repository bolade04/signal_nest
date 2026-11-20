"""AD-DECISION-SQLITE-01: what a brief or draft decision serializes with on SQLite.

The operator ruled (2026-11-19) that D-N4 items 34 and 38 apply wherever the capability can be
enabled under a supported configuration, SQLite included: the decider's role is checked under
the required protection (item 34), and a draft approval satisfies the parent-brief approval
requirement at the protected decision boundary (item 38). On PostgreSQL the decision's reads
are row-locked ("Lock order" in ``app.approvals.service``). On SQLite every one of those locks
compiles to a plain read, and with the application's engine (rollback journal, the driver's
default transaction control) a transaction begins only at its first write, so before this repair
a decision's reads held nothing until its first insert.

**The serialization point** is the one PostgreSQL's order gives: before the decision's first
protected read (step 1 of "Lock order", the decider's ``users`` row), and after the gates that
decide nothing about other writers (the route's role and capability, the tenant scope, the
author, the reason). A competing writer that committed before that point is read by the
decision; one that comes later serializes after the decision's commit. On SQLite the decision
takes the database's writer lock there (``app.db.sqlite_writer.hold_sqlite_writer_lock``,
``BEGIN IMMEDIATE``) and its transaction holds it to its end.

**The engine is the application's own** (``app.db.session.build_engine`` on a SQLite file, the
fold's world: ``test_i5e_export_serialization_sqlite.application_sqlite_world``). Every request
goes through its real route, in a thread and on a connection of its own; the answers are the
server's (a server error is the application's 500). This module reuses the fold's harness by
import (its world, statement trace, gate, runner and request helpers); nothing is copied.

**Forced interleavings, both orders, each read back raw on a separate connection:**

* a draft approval and a withdrawal (``revoked``, ``rejected``) of its brief's approval, with
  notifications off and on (item 38);
* a decision (brief, draft) and the decider's demotion or removal (item 34);
* a decision (brief, draft) and the deletion of its organization;
* a decision (brief, draft) and the erasure of the decider's account;
* a draft approval and the erasure of the draft's author's account (notifications on).

*The competitor first*: it is held after its writes are flushed and before it commits; the
decision is started and must be seen waiting at ``BEGIN IMMEDIATE`` having made none of its
protected reads; the competitor is released; the decision must then answer what the committed
competitor means (409, 403) and write nothing. *The decision first*: it is held after its reads
and before its first write (the point at which it held nothing before this repair); the
competitor is started and must be seen waiting, with nothing of it committed; the decision is
released, both commit, the decision first, and a deletion or erasure then anonymises what the
decision wrote. History is not rewritten: an approval that serialized before a withdrawal stays
as it is.

**The mechanism's own facts:** on the real routes the lock is reached with no transaction open
(so ``BEGIN IMMEDIATE`` is really sent) and before every protected read and write; a dark
request, a request without the role and a request refused before the lock take nothing and
wait for nobody; PostgreSQL never sees the statement. **Its consequences, measured:** when the
busy timeout expires, the waiting side answers the application's 500 and writes nothing.
"""

from __future__ import annotations

import argparse
import time
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import update

from app.approvals import service as approvals_service
from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.briefs.models import RecommendationBrief
from app.core.config import get_settings
from app.creative.models import CreativeDraft
from app.exports import service as export_service
from app.exports.service import NOT_APPROVED_MESSAGES
from app.notifications.models import Notification
from app.organizations import members
from app.organizations.models import Organization, OrganizationMember, User
from app.privacy import cli as privacy_cli
from app.privacy import erasure
from app.tests._creative_support import CreativeWorld, postgres_creative_world
from app.tests.test_exports_refusal_preconditions import draft, set_cap
from app.tests.test_i5e_export_serialization import (
    BRIEF_NO_LONGER_APPROVED,
    _decide_brief,
    _decide_draft,
    _delete_account,
    _delete_organization,
    _exists,
    _export,
    _exportable,
    _fresh_organization,
    _Gate,
    _Runner,
)
from app.tests.test_i5e_export_serialization_sqlite import (
    BUSY_TIMEOUT_SECONDS,
    INTERNAL_ERROR,
    SERIALIZE,
    _answer,
    _identity,
    _named_in,
    _report,
    _Trace,
    _traced,
    _writer_lock_held_elsewhere,
    application_sqlite_world,
)

NOTIFICATIONS = pytest.mark.parametrize(
    "notifications", [False, True], ids=["notifications-off", "notifications-on"]
)
WITHDRAWALS = pytest.mark.parametrize("withdrawal", ["revoked", "rejected"])
SUBJECTS = pytest.mark.parametrize("subject", ["brief", "draft"])
CHANGES = pytest.mark.parametrize("change", ["demote", "remove"])
DECIDER = "the decider"
ERASED = "[erased]"
BRIEF_NOT_APPROVED = (
    409, "approval_review_blocked", approvals_service.REVIEW_REFUSALS["brief_not_approved"]
)
NOT_A_MEMBER = (403, "permission_denied", approvals_service.NOT_A_MEMBER)
DEMOTED = (403, "permission_denied", "Role 'marketer' is not permitted for this action.")
#: The decision's protected reads (its "Lock order" statements, the subject's decisions, the
#: draft's brief and its review), as the trace normalizes them. On SQLite the ``FOR`` clauses
#: are not emitted.
PROTECTED_READS = (
    "select users.id from users where users.id =",
    "select organizations.id from organizations where organizations.id =",
    "select organization_members.role from organization_members",
    "select recommendation_briefs.id, recommendation_briefs.brief_version from",
    "select creative_drafts.id, creative_drafts.draft_version from",
    "select recommendation_briefs.id from recommendation_briefs where",
)


# --------------------------------------------------------------------------- #
# The world: the application's own SQLite engine on a file (the fold's)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def lite(tmp_path_factory) -> Iterator[CreativeWorld]:
    with application_sqlite_world(tmp_path_factory.mktemp("sqdec") / "app.db", "sqdec") as w:
        yield w


@pytest.fixture(autouse=True)
def flags_off(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "notifications_analytics_enabled", False)
    monkeypatch.setattr(settings, "content_export_enabled", False)


def _protected_reads(sent: list[str]) -> list[str]:
    return [
        q for q in sent
        if q.startswith(PROTECTED_READS) or " from approvals" in q or " from content_reviews" in q
    ]


def _deciding_world(w: CreativeWorld, ws: str, *, notifications: bool = False) -> str:
    """Both decision routes and export on for ``ws``; a draft (marketer's, passed review) of an
    approved brief (approved by the reviewer), no decision on the draft. Returns the draft id."""
    for capability in ("creative_generation", "recommendation_briefs", "content_export"):
        set_cap(w, ws, capability, True)
    if notifications:
        set_cap(w, ws, "notifications_analytics", True)
    return draft(w, ws)


def _fresh(w: CreativeWorld, key: str, *, notifications: bool = False) -> tuple[CreativeWorld, str]:
    """A fresh organization (owner, admin, marketer, reviewer) and its deciding world."""
    org = _fresh_organization(w, f"{key}{uuid.uuid4().hex[:4]}")
    return org, _deciding_world(org, org.ws, notifications=notifications)


def _decide(w: CreativeWorld, subject: str, made: str, decision: str = "approved"):
    if subject == "brief":
        return _decide_brief(w, w.ws, made, decision)
    return _decide_draft(w, w.ws, made, decision)


def _subject_id(w: CreativeWorld, subject: str, made: str) -> str:
    return w.row(CreativeDraft, made)["brief_id"] if subject == "brief" else made


def _decision_rows(w: CreativeWorld, subject_id: str, decider: str) -> list[tuple]:
    """``(decision, decided_by, snapshot, decided_role)`` of the subject's committed decisions in
    ``decision_seq`` order; the decider's id reads as :data:`DECIDER`, a kept snapshot as
    ``"kept"``."""
    rows = sorted(w.rows(Approval, subject_id=subject_id), key=lambda a: a["decision_seq"])
    return [
        (
            a["decision"],
            DECIDER if a["decided_by"] == decider else a["decided_by"],
            ERASED if a["decided_by_snapshot"] == ERASED else "kept",
            a["decided_role"],
        )
        for a in rows
    ]


def _approval_audits(w: CreativeWorld, decider: str, **where) -> list[tuple]:
    """``(action, actor)`` of the committed ``approval.*`` audit rows matching ``where``."""
    return sorted(
        (a["action"], DECIDER if a["actor_user_id"] == decider else a["actor_user_id"])
        for a in w.rows(AuditLog, **where)
        if a["action"].startswith("approval.")
    )


def _decided_notifications(w: CreativeWorld, **where) -> int:
    return w.count(Notification, event_code="approval.decided", **where)


def _naming(w: CreativeWorld, identity: tuple[str, ...], organization_id: str) -> list[tuple]:
    """Every column of the organization's committed ``approvals``, ``audit_logs`` and
    ``notifications`` rows that contains one of the identity's values."""
    found = []
    for model in (Approval, AuditLog, Notification):
        for row in w.rows(model, organization_id=organization_id):
            found += _named_in(model.__tablename__, row, identity)
    return found


def _role(w: CreativeWorld, user_id: str) -> str | None:
    rows = w.rows(OrganizationMember, organization_id=w.org, user_id=user_id)
    return rows[0]["role"] if rows else None


def _change_membership(w: CreativeWorld, change: str):
    """The owner demotes the reviewer to marketer, or removes them, through the real routes."""
    path = f"/organizations/{w.org}/members/{w.users['reviewer']}"
    if change == "demote":
        return w.send("PUT", f"{path}/role", "owner", {"role": "marketer"})
    return w.send("DELETE", path, "owner")


def _created_at(w: CreativeWorld, model, **where):
    [row] = w.rows(model, **where)
    return row["created_at"]


# --------------------------------------------------------------------------- #
# Item 38: a draft approval and a withdrawal of its brief's approval
# --------------------------------------------------------------------------- #
@NOTIFICATIONS
@WITHDRAWALS
def test_sqlite_a_brief_withdrawal_first_the_draft_approval_waits_and_is_refused(
    lite: CreativeWorld, monkeypatch, withdrawal: str, notifications: bool
):
    ws = lite.workspace("sqdec withdrawal first")
    made = _deciding_world(lite, ws, notifications=notifications)
    brief_id = lite.row(CreativeDraft, made)["brief_id"]
    reviewer = lite.users["reviewer"]
    gate = _Gate()
    # The withdrawal is held after its decision and audit rows are flushed, before it commits.
    monkeypatch.setattr(approvals_service, "notify", gate.before(approvals_service.notify))
    withdrawing = _Runner(lambda: _decide_brief(lite, ws, made, withdrawal))
    withdrawing.start()
    try:
        gate.wait("the withdrawal")
        with _Trace(lite.engine) as trace:
            approving = _Runner(lambda: _decide_draft(lite, ws, made, "approved"))
            approving.start()
            waiting, before = trace.waiting(approving)
    finally:
        gate.release.set()
    observed = {
        "withdrawal": _answer(withdrawing.outcome()),
        "draft approval": _answer(approving.outcome()),
        "the approval waited at, and its protected reads before": (
            waiting, _protected_reads(before)
        ),
        "brief decisions": _decision_rows(lite, brief_id, reviewer),
        "draft decisions": _decision_rows(lite, made, reviewer),
        "approval audit rows": _approval_audits(lite, reviewer, workspace_id=ws),
        "approval.decided notifications": _decided_notifications(lite, workspace_id=ws),
        "the next export": _answer(_export(lite, ws, made)),
    }
    _report("brief withdrawal first, draft approval", observed, {
        "withdrawal": (201,),
        "draft approval": BRIEF_NOT_APPROVED,
        "the approval waited at, and its protected reads before": (SERIALIZE, []),
        "brief decisions": [
            ("approved", DECIDER, "kept", "reviewer"), (withdrawal, DECIDER, "kept", "reviewer"),
        ],
        "draft decisions": [],
        "approval audit rows": [(f"approval.{withdrawal}", DECIDER)],
        "approval.decided notifications": 1 if notifications else 0,
        "the next export": (409, "export_not_approved", NOT_APPROVED_MESSAGES["no_decision"]),
    })


@NOTIFICATIONS
@WITHDRAWALS
def test_sqlite_a_draft_approval_first_the_brief_withdrawal_waits_until_it_has_committed(
    lite: CreativeWorld, monkeypatch, withdrawal: str, notifications: bool
):
    ws = lite.workspace("sqdec approval first")
    made = _deciding_world(lite, ws, notifications=notifications)
    brief_id = lite.row(CreativeDraft, made)["brief_id"]
    reviewer = lite.users["reviewer"]
    gate = _Gate()
    # The approval is held after its reads (role, draft, the brief's approval, the review) and
    # before its first write. Before the repair it held nothing there.
    monkeypatch.setattr(
        approvals_service, "_insert_decision", gate.before(approvals_service._insert_decision)
    )
    approving = _Runner(lambda: _decide_draft(lite, ws, made, "approved"))
    approving.start()
    try:
        gate.wait("the draft approval")
        with _Trace(lite.engine) as trace:
            withdrawing = _Runner(lambda: _decide_brief(lite, ws, made, withdrawal))
            withdrawing.start()
            waiting, _before = trace.waiting(withdrawing)
        committed_while_the_approval_was_held = _decision_rows(lite, brief_id, reviewer)
    finally:
        gate.release.set()
    approved, withdrawn = approving.outcome(), withdrawing.outcome()
    approval_at = _created_at(lite, Approval, subject_id=made)
    withdrawal_at = _created_at(lite, Approval, subject_id=brief_id, decision=withdrawal)
    observed = {
        "brief decisions committed while the approval was held":
            committed_while_the_approval_was_held,
        "the withdrawal waited at": waiting,
        "draft approval": _answer(approved),
        "withdrawal": _answer(withdrawn),
        "brief decisions": _decision_rows(lite, brief_id, reviewer),
        # History is not rewritten: the approval serialized before the withdrawal and stands.
        "draft decisions": _decision_rows(lite, made, reviewer),
        "approval audit rows": _approval_audits(lite, reviewer, workspace_id=ws),
        "created_at in commit order (approval, then withdrawal)": approval_at <= withdrawal_at,
        "approval.decided notifications": _decided_notifications(lite, workspace_id=ws),
        "the next export": _answer(_export(lite, ws, made)),
    }
    _report("draft approval first, brief withdrawal", observed, {
        "brief decisions committed while the approval was held": [
            ("approved", DECIDER, "kept", "reviewer"),
        ],
        "the withdrawal waited at": SERIALIZE,
        "draft approval": (201,),
        "withdrawal": (201,),
        "brief decisions": [
            ("approved", DECIDER, "kept", "reviewer"), (withdrawal, DECIDER, "kept", "reviewer"),
        ],
        "draft decisions": [("approved", DECIDER, "kept", "reviewer")],
        "approval audit rows": sorted(
            [("approval.approved", DECIDER), (f"approval.{withdrawal}", DECIDER)]
        ),
        "created_at in commit order (approval, then withdrawal)": True,
        "approval.decided notifications": 2 if notifications else 0,
        "the next export": (409, "export_review_blocked", BRIEF_NO_LONGER_APPROVED),
    })


# --------------------------------------------------------------------------- #
# Item 34: a decision and the decider's demotion or removal
# --------------------------------------------------------------------------- #
@SUBJECTS
@CHANGES
def test_sqlite_a_membership_change_first_the_decision_waits_and_is_refused(
    lite: CreativeWorld, monkeypatch, subject: str, change: str
):
    org, made = _fresh(lite, "sqdmc")
    subject_id = _subject_id(org, subject, made)
    reviewer = org.users["reviewer"]
    before_rows = _decision_rows(org, subject_id, reviewer)
    gate = _Gate()
    # The change is held after its membership write is flushed, before its audit row and commit.
    monkeypatch.setattr(members, "record_audit", gate.before(members.record_audit))
    changing = _Runner(lambda: _change_membership(org, change))
    changing.start()
    try:
        gate.wait("the membership change")
        with _Trace(lite.engine) as trace:
            deciding = _Runner(lambda: _decide(org, subject, made))
            deciding.start()
            waiting, before = trace.waiting(deciding)
    finally:
        gate.release.set()
    observed = {
        "change": _answer(changing.outcome()),
        "decision": _answer(deciding.outcome()),
        "the decision waited at, and its protected reads before": (
            waiting, _protected_reads(before)
        ),
        "the decider's role": _role(org, reviewer),
        "the subject's decisions": _decision_rows(org, subject_id, reviewer),
        "approval audit rows": _approval_audits(org, reviewer, organization_id=org.org),
    }
    _report(f"{change} first, {subject} decision", observed, {
        "change": (200,) if change == "demote" else (204,),
        "decision": DEMOTED if change == "demote" else NOT_A_MEMBER,
        "the decision waited at, and its protected reads before": (SERIALIZE, []),
        "the decider's role": "marketer" if change == "demote" else None,
        "the subject's decisions": before_rows,  # nothing written
        "approval audit rows": [],
    })


@SUBJECTS
@CHANGES
def test_sqlite_a_decision_first_the_membership_change_waits_until_it_has_committed(
    lite: CreativeWorld, monkeypatch, subject: str, change: str
):
    org, made = _fresh(lite, "sqddf")
    subject_id = _subject_id(org, subject, made)
    reviewer = org.users["reviewer"]
    before_rows = _decision_rows(org, subject_id, reviewer)
    gate = _Gate()
    monkeypatch.setattr(
        approvals_service, "_insert_decision", gate.before(approvals_service._insert_decision)
    )
    deciding = _Runner(lambda: _decide(org, subject, made))
    deciding.start()
    try:
        gate.wait("the decision")
        with _Trace(lite.engine) as trace:
            changing = _Runner(lambda: _change_membership(org, change))
            changing.start()
            waiting, _before = trace.waiting(changing)
        role_while_the_decision_was_held = _role(org, reviewer)
    finally:
        gate.release.set()
    decided, changed = deciding.outcome(), changing.outcome()
    action = (
        "organization_member.role_changed" if change == "demote"
        else "organization_member.removed"
    )
    decision_at = _created_at(org, AuditLog, organization_id=org.org, action="approval.approved")
    change_at = _created_at(org, AuditLog, organization_id=org.org, action=action)
    observed = {
        "the decider's role while the decision was held": role_while_the_decision_was_held,
        "the change waited in": " ".join((waiting or "").split()[:3]),
        "decision": _answer(decided),
        "change": _answer(changed),
        "the decider's role": _role(org, reviewer),
        "the subject's decisions": _decision_rows(org, subject_id, reviewer),
        "approval audit rows": _approval_audits(org, reviewer, organization_id=org.org),
        "created_at in commit order (decision, then change)": decision_at <= change_at,
    }
    _report(f"{subject} decision first, {change}", observed, {
        "the decider's role while the decision was held": "reviewer",
        "the change waited in": (
            "update organization_members set" if change == "demote"
            else "delete from organization_members"
        ),
        "decision": (201,),
        "change": (200,) if change == "demote" else (204,),
        "the decider's role": "marketer" if change == "demote" else None,
        # The decision serialized before the change: it was a reviewer's, and it stands.
        "the subject's decisions": [*before_rows, ("approved", DECIDER, "kept", "reviewer")],
        "approval audit rows": [("approval.approved", DECIDER)],
        "created_at in commit order (decision, then change)": True,
    })


# --------------------------------------------------------------------------- #
# A decision and the deletion of its organization
# --------------------------------------------------------------------------- #
@SUBJECTS
def test_sqlite_an_organization_deletion_first_the_decision_waits_and_is_refused(
    lite: CreativeWorld, monkeypatch, subject: str
):
    org, made = _fresh(lite, "sqdod")
    subject_id = _subject_id(org, subject, made)
    reviewer = org.users["reviewer"]
    decider = _identity(lite, reviewer)
    gate = _Gate()
    monkeypatch.setattr(erasure, "erase_organization", gate.after(erasure.erase_organization))
    deleting = _Runner(lambda: _delete_organization(org))
    deleting.start()
    try:
        gate.wait("the deletion")  # everything of the organization is deleted, not committed
        with _Trace(lite.engine) as trace:
            deciding = _Runner(lambda: _decide(org, subject, made))
            deciding.start()
            waiting, before = trace.waiting(deciding)
    finally:
        gate.release.set()
    observed = {
        "deletion": _answer(deleting.outcome()),
        "decision": _answer(deciding.outcome()),
        "the decision waited at, and its protected reads before": (
            waiting, _protected_reads(before)
        ),
        "the organization exists": _exists(lite, Organization, org.org),
        "the subject's decisions": _decision_rows(org, subject_id, reviewer),
        "approval audit rows": _approval_audits(org, reviewer, organization_id=org.org),
        "columns naming the decider": _naming(lite, decider, org.org),
    }
    _report(f"organization deletion first, {subject} decision", observed, {
        "deletion": (200,),
        "decision": NOT_A_MEMBER,
        "the decision waited at, and its protected reads before": (SERIALIZE, []),
        "the organization exists": False,
        # Only the seeded approval of the brief, anonymised by the deletion; nothing new.
        "the subject's decisions": (
            [("approved", None, ERASED, "reviewer")] if subject == "brief" else []
        ),
        "approval audit rows": [],
        "columns naming the decider": [],
    })


@SUBJECTS
def test_sqlite_a_decision_first_the_organization_deletion_waits_then_erases(
    lite: CreativeWorld, monkeypatch, subject: str
):
    org, made = _fresh(lite, "sqddo")
    subject_id = _subject_id(org, subject, made)
    reviewer = org.users["reviewer"]
    decider = _identity(lite, reviewer)
    gate = _Gate()
    monkeypatch.setattr(
        approvals_service, "_insert_decision", gate.before(approvals_service._insert_decision)
    )
    deciding = _Runner(lambda: _decide(org, subject, made))
    deciding.start()
    try:
        gate.wait("the decision")
        with _Trace(lite.engine) as trace:
            deleting = _Runner(lambda: _delete_organization(org))
            deleting.start()
            waiting, _before = trace.waiting(deleting)
        existed_while_the_decision_was_held = _exists(lite, Organization, org.org)
    finally:
        gate.release.set()
    decided, deleted = deciding.outcome(), deleting.outcome()
    observed = {
        "the organization existed while the decision was held":
            existed_while_the_decision_was_held,
        "the deletion waited in": (waiting or "")[:21],
        "decision": _answer(decided),
        "deletion": _answer(deleted),
        "the organization exists": _exists(lite, Organization, org.org),
        "the decider's account exists": _exists(lite, User, reviewer),
        "the subject's decisions": _decision_rows(org, subject_id, reviewer),
        "approval audit rows": _approval_audits(org, reviewer, organization_id=org.org),
        "columns naming the decider": _naming(lite, decider, org.org),
    }
    kept = [("approved", None, ERASED, "reviewer")]  # the row stays (O-6), its identity does not
    _report(f"{subject} decision first, organization deletion", observed, {
        "the organization existed while the decision was held": True,
        "the deletion waited in": "update audit_logs set",
        "decision": (201,),
        "deletion": (200,),
        "the organization exists": False,
        "the decider's account exists": True,
        "the subject's decisions": kept * 2 if subject == "brief" else kept,
        "approval audit rows": [("approval.approved", None)],
        "columns naming the decider": [],
    })


# --------------------------------------------------------------------------- #
# A decision and the erasure of the decider's account
# --------------------------------------------------------------------------- #
@SUBJECTS
def test_sqlite_the_deciders_account_erasure_first_the_decision_waits_and_is_refused(
    lite: CreativeWorld, monkeypatch, subject: str
):
    org, made = _fresh(lite, "sqdde")
    subject_id = _subject_id(org, subject, made)
    reviewer = org.users["reviewer"]
    decider = _identity(lite, reviewer)
    gate = _Gate()
    monkeypatch.setattr(erasure, "delete_account", gate.after(erasure.delete_account))
    erasing = _Runner(lambda: _delete_account(org, "reviewer"))
    erasing.start()
    try:
        gate.wait("the erasure")  # the account and its memberships are deleted, not committed
        with _Trace(lite.engine) as trace:
            deciding = _Runner(lambda: _decide(org, subject, made))
            deciding.start()
            waiting, before = trace.waiting(deciding)
    finally:
        gate.release.set()
    observed = {
        "erasure": _answer(erasing.outcome()),
        "decision": _answer(deciding.outcome()),
        "the decision waited at, and its protected reads before": (
            waiting, _protected_reads(before)
        ),
        "the decider's account exists": _exists(lite, User, reviewer),
        "the subject's decisions": _decision_rows(org, subject_id, reviewer),
        "approval audit rows": _approval_audits(org, reviewer, organization_id=org.org),
        "columns naming the decider": _naming(lite, decider, org.org),
    }
    _report(f"decider's erasure first, {subject} decision", observed, {
        "erasure": (200,),
        "decision": NOT_A_MEMBER,
        "the decision waited at, and its protected reads before": (SERIALIZE, []),
        "the decider's account exists": False,
        "the subject's decisions": (
            [("approved", None, ERASED, "reviewer")] if subject == "brief" else []
        ),
        "approval audit rows": [],
        "columns naming the decider": [],
    })


@SUBJECTS
def test_sqlite_a_decision_first_the_deciders_account_erasure_waits_then_anonymises_it(
    lite: CreativeWorld, monkeypatch, subject: str
):
    org, made = _fresh(lite, "sqdda")
    subject_id = _subject_id(org, subject, made)
    reviewer = org.users["reviewer"]
    decider = _identity(lite, reviewer)
    gate = _Gate()
    monkeypatch.setattr(
        approvals_service, "_insert_decision", gate.before(approvals_service._insert_decision)
    )
    deciding = _Runner(lambda: _decide(org, subject, made))
    deciding.start()
    try:
        gate.wait("the decision")
        with _Trace(lite.engine) as trace:
            erasing = _Runner(lambda: _delete_account(org, "reviewer"))
            erasing.start()
            waiting, _before = trace.waiting(erasing)
        existed_while_the_decision_was_held = _exists(lite, User, reviewer)
    finally:
        gate.release.set()
    decided, erased = deciding.outcome(), erasing.outcome()
    observed = {
        "the decider's account existed while the decision was held":
            existed_while_the_decision_was_held,
        "the erasure waited at": waiting,
        "decision": _answer(decided),
        "erasure": _answer(erased),
        "the decider's account exists": _exists(lite, User, reviewer),
        "the subject's decisions": _decision_rows(org, subject_id, reviewer),
        "approval audit rows": _approval_audits(org, reviewer, organization_id=org.org),
        "columns naming the decider": _naming(lite, decider, org.org),
    }
    kept = [("approved", None, ERASED, "reviewer")]
    _report(f"{subject} decision first, decider's erasure", observed, {
        "the decider's account existed while the decision was held": True,
        "the erasure waited at": SERIALIZE,
        "decision": (201,),
        "erasure": (200,),
        "the decider's account exists": False,
        "the subject's decisions": kept * 2 if subject == "brief" else kept,
        "approval audit rows": [("approval.approved", None)],
        "columns naming the decider": [],
    })


# --------------------------------------------------------------------------- #
# A draft approval and the erasure of the draft's (and the brief's) author
# --------------------------------------------------------------------------- #
def test_sqlite_the_authors_account_erasure_first_the_draft_approval_waits_then_decides(
    lite: CreativeWorld, monkeypatch
):
    org, made = _fresh(lite, "sqdae", notifications=True)
    reviewer, author = org.users["reviewer"], org.users["marketer"]
    identity = _identity(lite, author)
    gate = _Gate()
    monkeypatch.setattr(erasure, "delete_account", gate.after(erasure.delete_account))
    erasing = _Runner(lambda: _delete_account(org, "marketer"))
    erasing.start()
    try:
        gate.wait("the author's erasure")
        with _Trace(lite.engine) as trace:
            approving = _Runner(lambda: _decide_draft(org, org.ws, made, "approved"))
            approving.start()
            waiting, before = trace.waiting(approving)
    finally:
        gate.release.set()
    observed = {
        "erasure": _answer(erasing.outcome()),
        "draft approval": _answer(approving.outcome()),
        "the approval waited at, and its protected reads before": (
            waiting, _protected_reads(before)
        ),
        "draft decisions": _decision_rows(org, made, reviewer),
        "the draft's author": org.row(CreativeDraft, made)["created_by"],
        "approval.decided notifications": _decided_notifications(org, organization_id=org.org),
        "columns naming the author": _naming(lite, identity, org.org),
    }
    _report("author's erasure first, draft approval", observed, {
        "erasure": (200,),
        "draft approval": (201,),  # the author's erasure changes nothing items 34 and 38 read
        "the approval waited at, and its protected reads before": (SERIALIZE, []),
        "draft decisions": [("approved", DECIDER, "kept", "reviewer")],
        "the draft's author": None,
        "approval.decided notifications": 0,  # nobody left to notify
        "columns naming the author": [],
    })


def test_sqlite_a_draft_approval_first_the_authors_account_erasure_waits_then_anonymises(
    lite: CreativeWorld, monkeypatch
):
    org, made = _fresh(lite, "sqdaf", notifications=True)
    reviewer, author = org.users["reviewer"], org.users["marketer"]
    identity = _identity(lite, author)
    gate = _Gate()
    monkeypatch.setattr(
        approvals_service, "_insert_decision", gate.before(approvals_service._insert_decision)
    )
    approving = _Runner(lambda: _decide_draft(org, org.ws, made, "approved"))
    approving.start()
    try:
        gate.wait("the draft approval")
        with _Trace(lite.engine) as trace:
            erasing = _Runner(lambda: _delete_account(org, "marketer"))
            erasing.start()
            waiting, _before = trace.waiting(erasing)
        existed_while_the_approval_was_held = _exists(lite, User, author)
    finally:
        gate.release.set()
    approved, erased = approving.outcome(), erasing.outcome()
    observed = {
        "the author's account existed while the approval was held":
            existed_while_the_approval_was_held,
        "the erasure waited at": waiting,
        "draft approval": _answer(approved),
        "erasure": _answer(erased),
        "draft decisions": _decision_rows(org, made, reviewer),
        "the draft's author": org.row(CreativeDraft, made)["created_by"],
        # The approval's notification to the author went with the author's account.
        "approval.decided notifications": _decided_notifications(org, organization_id=org.org),
        "columns naming the author": _naming(lite, identity, org.org),
    }
    _report("draft approval first, author's erasure", observed, {
        "the author's account existed while the approval was held": True,
        "the erasure waited at": SERIALIZE,
        "draft approval": (201,),
        "erasure": (200,),
        "draft decisions": [("approved", DECIDER, "kept", "reviewer")],
        "the draft's author": None,
        "approval.decided notifications": 0,
        "columns naming the author": [],
    })


# --------------------------------------------------------------------------- #
# The mechanism: where the lock is taken, and that it is really taken
# --------------------------------------------------------------------------- #
def _recording_hold(module, records: list):
    real = module.hold_sqlite_writer_lock

    def recorded(db):
        raw = db.connection().connection.dbapi_connection
        records.append((db.get_bind().dialect.name, raw.in_transaction))
        return real(db)

    return recorded


@pytest.mark.parametrize(
    ("subject", "decision", "status"),
    [("brief", "approved", 201), ("draft", "approved", 201), ("draft", "rejected", 201),
     ("brief", "revoked", 201)],
)
def test_sqlite_on_the_real_route_the_decision_takes_the_writer_lock_with_no_transaction_open(
    lite: CreativeWorld, monkeypatch, subject: str, decision: str, status: int
):
    """The precondition the repair rests on (coordinator ruling SD-2): on the real entry point
    the helper is reached with no transaction open, so ``BEGIN IMMEDIATE`` is really sent; once,
    before every protected read and every write of the decision. A change that opened a
    transaction earlier (a SAVEPOINT, a deferred ``BEGIN``) would make the helper send nothing
    and fails here."""
    ws = lite.workspace("sqdec precondition")
    made = _deciding_world(lite, ws)
    records: list = []
    monkeypatch.setattr(approvals_service, "hold_sqlite_writer_lock",
                        _recording_hold(approvals_service, records))
    w = CreativeWorld(lite.client, lite.engine, lite.witness, lite.factory, org=lite.org, ws=ws,
                      tokens=lite.tokens, users=lite.users)
    response, sent, _seconds = _traced(lite, lambda: _decide(w, subject, made, decision))
    assert response.status_code == status, response.text
    assert records == [("sqlite", False)]
    assert [q for q in sent if q.startswith(("begin", "savepoint"))] == [SERIALIZE]
    taken = sent.index(SERIALIZE)
    reads = [i for i, q in enumerate(sent) if q in _protected_reads(sent)]
    writes = [i for i, q in enumerate(sent) if q.startswith(("insert", "update", "delete"))]
    assert reads and writes
    assert taken < min(reads) and taken < min(writes), sent


def test_sqlite_a_refused_decision_has_taken_the_lock_and_released_it(
    lite: CreativeWorld, monkeypatch
):
    """A draft approval refused under the lock (its brief is not approved: 409) took the lock
    before its reads, wrote nothing, and released it when it answered."""
    ws = lite.workspace("sqdec refused")
    made = _deciding_world(lite, ws)
    assert _answer(_decide_brief(lite, ws, made, "revoked")) == (201,)
    response, sent, _seconds = _traced(lite, lambda: _decide_draft(lite, ws, made, "approved"))
    assert _answer(response) == BRIEF_NOT_APPROVED
    assert [q for q in sent if q.startswith("begin")] == [SERIALIZE]
    assert sent.index(SERIALIZE) < min(
        i for i, q in enumerate(sent) if q in _protected_reads(sent)
    )
    assert not [q for q in sent if q.startswith(("insert", "update", "delete"))]
    assert _decision_rows(lite, made, lite.users["reviewer"]) == []
    with lite.witness.connect() as conn:  # released: another connection takes it at once
        conn.exec_driver_sql("PRAGMA busy_timeout=0")
        try:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        finally:
            conn.rollback()
            conn.exec_driver_sql("PRAGMA busy_timeout=5000")


@pytest.mark.parametrize("request_kind", ["dark", "no role", "reason mismatch", "author"])
@SUBJECTS
def test_sqlite_a_request_refused_before_the_lock_takes_none_and_waits_for_nobody(
    lite: CreativeWorld, subject: str, request_kind: str
):
    """The lock comes after the gates: a dark workspace (503), a caller without the approver
    role (403), a reason of the wrong polarity (422) and the subject's own author deciding (403,
    the service's separation-of-duties check) are answered at once while another connection
    holds the writer lock, and send no ``BEGIN``."""
    ws = lite.workspace("sqdec gates", enabled=False)
    made = draft(lite, ws)
    if request_kind != "dark":
        for capability in ("creative_generation", "recommendation_briefs"):
            set_cap(lite, ws, capability, True)
    w = CreativeWorld(lite.client, lite.engine, lite.witness, lite.factory, org=lite.org, ws=ws,
                      tokens=lite.tokens, users=lite.users)
    record = w.row(CreativeDraft, made)
    if request_kind == "author":  # the reviewer is the subject's author
        table = (RecommendationBrief if subject == "brief" else CreativeDraft).__table__
        subject_id = record["brief_id"] if subject == "brief" else made
        with lite.witness.begin() as conn:
            conn.execute(update(table).where(table.c.id == subject_id)
                         .values(created_by=lite.users["reviewer"]))
    path = (
        f"/workspaces/{ws}/opportunities/{record['opportunity_id']}/briefs/{record['brief_id']}"
        "/approvals" if subject == "brief" else f"/workspaces/{ws}/drafts/{made}/approvals"
    )
    role = "marketer" if request_kind == "no role" else "reviewer"
    body = {"decision": "approved", "subject_version": 1}
    if request_kind == "reason mismatch":
        body["reason_code"] = "insufficient_evidence"  # a rejection reason on an approval
    with _writer_lock_held_elsewhere(lite):
        response, sent, seconds = _traced(lite, lambda: w.send("POST", path, role, body))
    expected = {"dark": 503, "no role": 403, "reason mismatch": 422, "author": 403}[request_kind]
    assert response.status_code == expected, response.text
    assert not [q for q in sent if q.startswith("begin")]
    assert _protected_reads(sent) == []
    assert seconds < BUSY_TIMEOUT_SECONDS / 2, seconds


@pytest.mark.parametrize("caller", ["export 201", "export 409", "account deletion",
                                    "invitee-email erasure"])
def test_sqlite_the_existing_callers_reach_the_writer_lock_with_no_transaction_open(
    lite: CreativeWorld, monkeypatch, caller: str
):
    """The same precondition for the helper's existing callers (an added case, SD-2): on their
    real entry points no transaction is open when they call it, so ``BEGIN IMMEDIATE`` is sent."""
    records: list = []
    monkeypatch.setattr(export_service, "hold_sqlite_writer_lock",
                        _recording_hold(export_service, records))
    monkeypatch.setattr(erasure, "hold_sqlite_writer_lock", _recording_hold(erasure, records))
    if caller.startswith("export"):
        ws = lite.workspace("sqdec export precondition", enabled=False)
        if caller == "export 201":
            made, _approval = _exportable(lite, ws, notifications=False)
        else:
            set_cap(lite, ws, "content_export", True)
            made = draft(lite, ws)
        response, sent, _seconds = _traced(lite, lambda: _export(lite, ws, made))
        assert response.status_code == int(caller[-3:]), response.text
    elif caller == "account deletion":
        org = _fresh_organization(lite, f"sqdpre{uuid.uuid4().hex[:4]}")
        response, sent, _seconds = _traced(lite, lambda: _delete_account(org, "admin"))
        assert response.status_code == 200, response.text
    else:
        monkeypatch.setattr(privacy_cli, "SessionLocal", lite.factory)
        args = argparse.Namespace(email=f"sqdec-{uuid.uuid4().hex[:6]}@example.com", confirm=True)
        rc, sent, _seconds = _traced(lite, lambda: privacy_cli._erase_email(args))
        assert rc == 0
    assert records == [("sqlite", False)]
    assert [q for q in sent if q.startswith(("begin", "savepoint"))][:1] == [SERIALIZE]


def test_postgresql_a_decision_sends_no_sqlite_statement():
    """Engine-conditional: on PostgreSQL a brief or draft decision issues no ``BEGIN
    IMMEDIATE`` (PostgreSQL would refuse it); its row locks are what serialize it."""
    with postgres_creative_world("sqdecpg") as w:  # skips unless TEST_POSTGRES_URL is set
        ws = w.workspace("sqdec pg", enabled=False)
        made = _deciding_world(w, ws)
        w.ws = ws
        for subject, decision in (("draft", "approved"), ("brief", "revoked")):
            response, sent, _seconds = _traced(
                w, lambda s=subject, d=decision: _decide(w, s, made, d)
            )
            assert response.status_code == 201, response.text
            assert not [q for q in sent if "immediate" in q]
            assert [q for q in sent if q.endswith(("for share", "for key share", "for update",
                                                   "for no key update"))]


# --------------------------------------------------------------------------- #
# Consequences: what each side answers when the busy timeout expires
# --------------------------------------------------------------------------- #
def test_sqlite_a_decision_that_cannot_get_the_writer_lock_in_time_answers_500_and_writes_nothing(
    lite: CreativeWorld,
):
    """A writer that stays uncommitted past the busy timeout (5 s): the decision's ``BEGIN
    IMMEDIATE`` fails before any of its reads, the application answers its generic 500,
    nothing is written, and the next decision succeeds."""
    ws = lite.workspace("sqdec busy decision")
    made = _deciding_world(lite, ws)
    with _writer_lock_held_elsewhere(lite):
        response, sent, seconds = _traced(lite, lambda: _decide_draft(lite, ws, made, "approved"))
    assert _answer(response) == INTERNAL_ERROR
    assert sent[-1] == SERIALIZE and _protected_reads(sent) == []
    assert seconds >= BUSY_TIMEOUT_SECONDS - 0.5, seconds
    assert _decision_rows(lite, made, lite.users["reviewer"]) == []
    assert _answer(_decide_draft(lite, ws, made, "approved")) == (201,)


def test_sqlite_a_demotion_that_waits_past_the_busy_timeout_for_a_decision_answers_500(
    lite: CreativeWorld, monkeypatch
):
    """The other side: a decision held past the busy timeout holds the writer lock from before
    its reads, so a demotion of its decider fails at its own first write after five seconds,
    answers the generic 500 and writes nothing; the decision then commits as a reviewer's."""
    org, made = _fresh(lite, "sqdbt")
    reviewer = org.users["reviewer"]
    gate = _Gate()
    monkeypatch.setattr(
        approvals_service, "_insert_decision", gate.before(approvals_service._insert_decision)
    )
    deciding = _Runner(lambda: _decide_draft(org, org.ws, made, "approved"))
    deciding.start()
    try:
        gate.wait("the decision")
        started = time.monotonic()
        refused = _change_membership(org, "demote")
        seconds = time.monotonic() - started
    finally:
        gate.release.set()
    assert _answer(refused) == INTERNAL_ERROR
    assert seconds >= BUSY_TIMEOUT_SECONDS - 0.5, seconds
    assert _answer(deciding.outcome()) == (201,)
    assert _role(org, reviewer) == "reviewer"
    assert _decision_rows(org, made, reviewer) == [("approved", DECIDER, "kept", "reviewer")]
    assert not [a for a in org.rows(AuditLog, organization_id=org.org)
                if a["action"] == "organization_member.role_changed"]
