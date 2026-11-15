"""Project Phase 5E (lane 5E-W4): export refusals, preconditions 2, 3 and 4 (5E plan §2; exit 3).

For a draft that passes every other precondition (export on, its own workspace, agreeing
jurisdiction codes), each case below is refused and writes nothing (no export, no audit row,
no notification):

* **Precondition 3, the latest decision** (409 ``export_not_approved``): no decision;
  approve-then-reject; approved then revoked; a decision on another version of the subject;
  a controlling decision row that is another workspace's. "An approved row exists" never
  suffices: the highest ``decision_seq`` on the exact ``(subject_id, subject_version)``
  decides, and reject-then-approve exports (the control).
* **Precondition 2, the review** (409 ``export_review_blocked``, P6: ``draft_approval_refusal``
  is reused): no review; a blocked review; a review that did not pass (a required disclaimer
  unresolved); a soft-deleted review (P2: counts as none); a claim the library blocks *now*;
  a disclaimer the library requires *now*. Any reason a later change adds to
  ``draft_approval_refusal`` (item 38's ``brief_not_approved`` among them) refuses too.
* **Precondition 4, the role** (403 ``permission_denied``; operator ruling D-N4 item 32):
  creating an export is exactly OWNER, ADMIN, MARKETER; reading the history is those plus
  REVIEWER and COMPLIANCE_REVIEWER; VIEWER is admitted to neither. The route's dependency is
  the exact-role one, and the service re-checks the role whatever the route did.
* The order is the service's: the decision refusal before the review refusal.

Each refusal's message is the static one of ``app.exports.service`` (no draft text). The
world is the 5C harness on SQLite and, with ``TEST_POSTGRES_URL``, PostgreSQL at head.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.routing import iter_route_contexts
from sqlalchemy import delete, func, select, update

from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.briefs.models import RecommendationBrief
from app.capabilities.models import WorkspaceCapabilityOverride
from app.core.actor_snapshot import actor_snapshot
from app.core.enums import Role
from app.core.errors import PermissionDeniedError
from app.creative.models import ContentReview, CreativeDraft
from app.exports import policy
from app.exports.models import Export
from app.main import app
from app.notifications.models import Notification
from app.organizations.models import Organization, User, Workspace
from app.privacy.writer import mark_privacy_writer
from app.tests._creative_support import API, CreativeWorld, creative_world, postgres_creative_world

BODY = {"format": "text/plain"}
NOT_APPROVED = "export_not_approved"
REVIEW_BLOCKED = "export_review_blocked"
MESSAGES = {
    "no_decision": "This draft version has not been approved, so it can't be exported.",
    "rejected": "This draft version's latest decision is a rejection, so it can't be exported.",
    "revoked": "This draft version's approval was revoked, so it can't be exported.",
    "review_missing": "This draft has no content review, so it can't be exported.",
    "review_blocked": "This draft's content review blocked it, so it can't be exported.",
    "review_not_passed": (
        "A required disclaimer is missing from this draft, so it can't be exported."
    ),
    "blocked_claim": (
        "This draft contains a claim the claims library now blocks, so it can't be exported."
    ),
    "disclaimer_missing": (
        "This draft lacks a disclaimer the claims library now requires, so it can't be exported."
    ),
    "other": "This draft can no longer be approved, so it can't be exported.",
}
TEXT = "Weekend parking is easy near the market."


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def world(request, tmp_path_factory) -> Iterator[CreativeWorld]:
    if request.param == "sqlite":
        with creative_world(tmp_path_factory.mktemp("w4xp") / "p.db", "w4xp") as w:
            yield w
        return
    with postgres_creative_world("w4xp") as w:  # skips unless TEST_POSTGRES_URL is set
        yield w


def set_cap(world: CreativeWorld, ws: str, capability: str, enabled: bool | None) -> None:
    with world.session() as s:
        s.execute(
            delete(WorkspaceCapabilityOverride).where(
                WorkspaceCapabilityOverride.workspace_id == ws,
                WorkspaceCapabilityOverride.capability == capability,
            )
        )
        if enabled is not None:
            s.add(WorkspaceCapabilityOverride(
                organization_id=world.org_of(ws), workspace_id=ws, capability=capability,
                enabled=enabled, set_by_user_id=world.owner_id(ws), reason="5E-W4 test",
            ))


@pytest.fixture
def ws(world: CreativeWorld, request) -> str:
    ws = world.workspace(f"p {request.node.name[:40]}")
    set_cap(world, ws, "content_export", True)
    return ws


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #
def _snapshot(user: User) -> str:
    return actor_snapshot(user_id=user.id, full_name=user.full_name, email=user.email)


def draft(world: CreativeWorld, ws: str, *, version: int = 1, brief_id: str | None = None,
          review: str | None = "passed", text: str = TEXT) -> str:  # fmt: skip
    """A draft version (US-TX on it and its brief) with a review in the given state
    (``passed``, ``blocked``, ``not_passed`` or ``None`` for no review) and no decision."""
    brief_id = brief_id or world.seed_brief(ws, jurisdiction_code="US-TX")
    tag = uuid.uuid4().hex[:12]
    with world.session() as s:
        brief = s.get(RecommendationBrief, brief_id)
        author = s.get(User, world.users["marketer"])
        scoped = {"organization_id": brief.organization_id, "workspace_id": ws}
        s.add(CreativeDraft(
            id=f"dr{tag}", brief_id=brief_id, opportunity_id=brief.opportunity_id,
            draft_version=version, channel="social_post", body=text,
            prompt_name="generate_creative", prompt_version="1.0.0", model_identifier="mock-1",
            input_hash="0" * 64, is_simulated=True, input_tokens=1, output_tokens=1,
            estimated_cost_usd=0, jurisdiction_code="US-TX", created_by=author.id,
            created_by_snapshot=_snapshot(author), **scoped,
        ))
        s.flush()
        if review is not None:
            blocked, passed = review == "blocked", review == "passed"
            s.add(ContentReview(
                draft_id=f"dr{tag}", draft_version=version,
                risk_level="blocked" if blocked else ("low" if passed else "medium"),
                is_blocked=blocked, findings=[], warnings=[], blocked_claim_ids=[],
                required_disclaimers=[] if passed or blocked else ["Terms apply."],
                disclaimers_resolved=passed or blocked, passed=passed,
                jurisdiction_code="US-TX", **scoped,
            ))
    return f"dr{tag}"


def decide(world: CreativeWorld, draft_id: str, decision: str, *,
           subject_version: int | None = None, workspace_id: str | None = None,
           organization_id: str | None = None) -> None:  # fmt: skip
    """The next 5B decision row on the draft (``decision_seq`` = max + 1), by the reviewer.
    ``workspace_id``/``organization_id`` forge the row's scope (approvals are FK-less, O-6)."""
    with world.session() as s:
        d = s.get(CreativeDraft, draft_id)
        version = subject_version or d.draft_version
        seq = (s.scalar(
            select(func.max(Approval.decision_seq)).where(
                Approval.subject_id == draft_id, Approval.subject_version == version
            )
        ) or 0) + 1  # fmt: skip
        reviewer = s.get(User, world.users["reviewer"])
        s.add(Approval(
            organization_id=organization_id or d.organization_id,
            workspace_id=workspace_id or d.workspace_id, subject_type="creative_draft",
            subject_id=draft_id, subject_version=version, draft_id=draft_id,
            decision=decision, decision_seq=seq, decided_by=reviewer.id,
            decided_by_snapshot=_snapshot(reviewer), decided_role="reviewer",
            is_simulated=False,
        ))


def written(world: CreativeWorld) -> tuple[int, int, int]:
    return (
        world.count(Export),
        world.count(AuditLog, action="creative_draft.exported"),
        world.count(Notification),
    )


def path(ws: str, draft_id: str) -> str:
    return f"/workspaces/{ws}/drafts/{draft_id}/exports"


def export(world: CreativeWorld, ws: str, draft_id: str, role: str = "marketer"):
    return world.send("POST", path(ws, draft_id), role, BODY)


def assert_refused(world: CreativeWorld, ws: str, draft_id: str, code: str, reason: str) -> None:
    before = written(world)
    for role in ("owner", "admin", "marketer"):
        r = export(world, ws, draft_id, role)
        assert r.status_code == 409, (role, r.text)
        body = r.json()["error"]
        assert (body["code"], body["message"]) == (code, MESSAGES[reason])
        assert TEXT not in r.text
    assert written(world) == before


# --------------------------------------------------------------------------- #
# Precondition 3: the latest decision on the exact version
# --------------------------------------------------------------------------- #
def test_an_unapproved_draft_is_refused(world, ws):
    assert_refused(world, ws, draft(world, ws), NOT_APPROVED, "no_decision")


def test_approve_then_reject_is_refused(world, ws):
    made = draft(world, ws)
    decide(world, made, "approved")
    assert export(world, ws, made).status_code == 201  # it was exportable while approved
    decide(world, made, "rejected")
    assert_refused(world, ws, made, NOT_APPROVED, "rejected")


def test_a_revoked_approval_is_refused(world, ws):
    made = draft(world, ws)
    decide(world, made, "approved")
    decide(world, made, "revoked")
    assert_refused(world, ws, made, NOT_APPROVED, "revoked")


def test_reject_then_approve_exports(world, ws):
    """The control: the latest decision governs, not any earlier rejection."""
    made = draft(world, ws)
    decide(world, made, "rejected")
    decide(world, made, "approved")
    r = export(world, ws, made)
    assert r.status_code == 201, r.text
    latest = max(world.rows(Approval, subject_id=made), key=lambda a: a["decision_seq"])
    assert r.json()["approval_id"] == latest["id"]  # bound to the controlling decision


def test_a_decision_on_another_version_does_not_approve_this_one(world, ws):
    """Version 2 of a brief's draft has only decisions on version 1 of its subject: refused.
    Version 1 (approved) exports; version 2 then does once its own version is approved."""
    v1 = draft(world, ws)
    brief_id = world.row(CreativeDraft, v1)["brief_id"]
    v2 = draft(world, ws, version=2, brief_id=brief_id)
    decide(world, v1, "approved")
    decide(world, v2, "approved", subject_version=1)  # a decision on another version
    assert_refused(world, ws, v2, NOT_APPROVED, "no_decision")
    assert export(world, ws, v1).status_code == 201
    decide(world, v2, "approved")
    assert export(world, ws, v2).status_code == 201


def test_a_newer_rejection_on_another_version_does_not_refuse_this_one(world, ws):
    v1 = draft(world, ws)
    decide(world, v1, "approved")
    decide(world, v1, "rejected", subject_version=2)
    assert export(world, ws, v1).status_code == 201


@pytest.mark.parametrize("forged", ["workspace", "organization"])
def test_a_controlling_decision_of_another_tenant_never_approves(world, ws, forged):
    """An approved row naming this draft but scoped to another workspace (or organization)
    is the highest ``decision_seq``: it controls, is not this tenant's, and so refuses."""
    made = draft(world, ws)
    decide(world, made, "rejected")
    scope = ({"workspace_id": world.ws2} if forged == "workspace"
             else {"organization_id": world.org_b, "workspace_id": world.ws_b})  # fmt: skip
    decide(world, made, "approved", **scope)
    before = written(world)
    r = export(world, ws, made)
    assert r.status_code == 409 and r.json()["error"]["code"] == NOT_APPROVED
    assert written(world) == before


# --------------------------------------------------------------------------- #
# Precondition 2: the review, and the claims library as it stands now
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("review", "reason"), [(None, "review_missing"),
                                                ("blocked", "review_blocked"),
                                                ("not_passed", "review_not_passed")])  # fmt: skip
def test_an_approved_draft_whose_review_does_not_allow_it_is_refused(world, ws, review, reason):
    made = draft(world, ws, review=review)
    decide(world, made, "approved")
    assert_refused(world, ws, made, REVIEW_BLOCKED, reason)


def test_a_soft_deleted_review_counts_as_none(world, ws):
    made = draft(world, ws)
    decide(world, made, "approved")
    assert export(world, ws, made).status_code == 201
    review = world.rows(ContentReview, draft_id=made)[0]["id"]
    with world.session() as s:
        mark_privacy_writer(s)
        s.execute(
            update(ContentReview.__table__)
            .where(ContentReview.__table__.c.id == review)
            .values(deleted_at=datetime.now(UTC))
        )
    assert_refused(world, ws, made, REVIEW_BLOCKED, "review_missing")


def test_a_claim_the_library_blocks_now_refuses_a_passed_review(world, ws):
    made = draft(world, ws)
    decide(world, made, "approved")
    assert export(world, ws, made).status_code == 201
    claim = world.claim("parking is easy", "blocked", ws=ws)
    assert_refused(world, ws, made, REVIEW_BLOCKED, "blocked_claim")
    with world.session() as s:  # a retired entry blocks nothing (the control)
        from app.campaign_context.models import ClaimsLibraryEntry

        s.get(ClaimsLibraryEntry, claim).retired_at = datetime.now(UTC)
    assert export(world, ws, made).status_code == 201


def test_a_disclaimer_the_library_requires_now_refuses_a_passed_review(world, ws):
    made = draft(world, ws)
    decide(world, made, "approved")
    world.claim("Terms and conditions apply.", "required_disclaimer", ws=ws)
    assert_refused(world, ws, made, REVIEW_BLOCKED, "disclaimer_missing")
    covered = draft(world, ws, text=f"{TEXT} Terms and conditions apply.")
    decide(world, covered, "approved")
    assert export(world, ws, covered).status_code == 201


def test_another_workspaces_library_does_not_refuse_this_draft(world, ws):
    made = draft(world, ws)
    decide(world, made, "approved")
    world.claim("parking is easy", "blocked", ws=world.ws2)
    assert export(world, ws, made).status_code == 201


def test_a_reason_added_later_to_draft_approval_refusal_is_inherited(world, ws, monkeypatch):
    """P6: export reuses ``draft_approval_refusal``, so a precondition added there later (item
    38's ``brief_not_approved``, item 39's withheld reasons) refuses export with no change in
    the export service: an unknown reason gets the generic line, never an export."""
    import app.exports.service as service

    made = draft(world, ws)
    decide(world, made, "approved")
    monkeypatch.setattr(service, "draft_approval_refusal", lambda db, d: "brief_not_approved")
    assert_refused(world, ws, made, REVIEW_BLOCKED, "other")


def test_the_decision_refusal_precedes_the_review_refusal(world, ws):
    made = draft(world, ws, review="blocked")
    assert_refused(world, ws, made, NOT_APPROVED, "no_decision")
    decide(world, made, "approved")
    assert_refused(world, ws, made, REVIEW_BLOCKED, "review_blocked")


# --------------------------------------------------------------------------- #
# Precondition 4: the role (item 32's sets)
# --------------------------------------------------------------------------- #
def test_only_owner_admin_and_marketer_may_export(world, ws):
    made = draft(world, ws)
    decide(world, made, "approved")
    before = written(world)
    for role in ("reviewer", "compliance_reviewer", "viewer"):
        r = export(world, ws, made, role)
        assert r.status_code == 403, (role, r.text)
        assert r.json()["error"] == {
            "code": "permission_denied",
            "message": f"Role '{role}' is not permitted for this action.",
            "request_id": r.json()["error"]["request_id"],
        }
    assert written(world) == before
    for role in ("owner", "admin", "marketer"):
        r = export(world, ws, made, role)
        assert r.status_code == 201, (role, r.text)
        row = world.row(Export, r.json()["id"])
        assert row["exported_by"] == world.users[role]
        audits = world.rows(AuditLog, action="creative_draft.exported", entity_id=made)
        assert r.json()["id"] in {a["new_state"]["export_id"] for a in audits}
    assert written(world)[:2] == (before[0] + 3, before[1] + 3)  # one audit row per export


def test_history_readers_are_the_exporters_plus_reviewer_and_compliance_reviewer(world, ws):
    made = draft(world, ws)
    decide(world, made, "approved")
    exported = export(world, ws, made, "owner").json()["id"]
    for role in ("owner", "admin", "marketer", "reviewer", "compliance_reviewer"):
        r = world.send("GET", path(ws, made), role)
        assert r.status_code == 200, (role, r.text)
        assert [i["id"] for i in r.json()["items"]] == [exported]
    r = world.send("GET", path(ws, made), "viewer")
    assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied"


def test_the_routes_depend_on_the_exact_role_sets():
    """The route's own dependency is the exact-role one (not only the service's re-check)."""
    assert policy.EXPORTER_ROLES == {Role.OWNER, Role.ADMIN, Role.MARKETER}
    assert policy.EXPORT_HISTORY_READER_ROLES == {
        Role.OWNER, Role.ADMIN, Role.MARKETER, Role.REVIEWER, Role.COMPLIANCE_REVIEWER
    }
    want = {"POST": policy.require_exporter, "GET": policy.require_export_history_reader}
    found = {}
    for rc in iter_route_contexts(app.routes):
        if getattr(rc, "dependant", None) is not None and rc.path == (
            f"{API}/workspaces/{{workspace_id}}/drafts/{{draft_id}}/exports"
        ):
            for method in set(rc.methods) - {"HEAD", "OPTIONS"}:
                found[method] = [d.call for d in rc.dependant.dependencies]
    assert set(found) == {"POST", "GET"}
    for method, checker in want.items():
        assert checker in found[method], method
    for checker, roles in ((policy.require_exporter, policy.EXPORTER_ROLES),
                           (policy.require_export_history_reader,
                            policy.EXPORT_HISTORY_READER_ROLES)):  # fmt: skip
        assert inspect.getclosurevars(checker).nonlocals["permitted"] == roles


def test_the_service_rechecks_the_role_whatever_the_route_did(world, ws):
    from app.auth.dependencies import TenantContext
    from app.exports.service import create_export, list_exports
    from app.notifications.service import emit_notifications

    made = draft(world, ws)
    decide(world, made, "approved")
    before = written(world)
    s = world.factory()
    try:
        org, space = s.get(Organization, world.org), s.get(Workspace, ws)
        for role in ("reviewer", "compliance_reviewer", "viewer"):
            ctx = TenantContext(user=s.get(User, world.users[role]), organization=org,
                                workspace=space, role=Role(role))  # fmt: skip
            with pytest.raises(PermissionDeniedError):
                create_export(s, ctx=ctx, draft_id=made, export_format="text/plain",
                              emitter=emit_notifications)  # fmt: skip
        viewer = TenantContext(user=s.get(User, world.users["viewer"]), organization=org,
                               workspace=space, role=Role.VIEWER)  # fmt: skip
        with pytest.raises(PermissionDeniedError):
            list_exports(s, ctx=viewer, draft_id=made)
        s.rollback()
    finally:
        s.close()
    assert written(world) == before
