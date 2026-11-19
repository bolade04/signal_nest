"""Project Phase 5E (lane 5E-W4): export tenant isolation, precondition 6 (5E plan §2, exit 3).

``POST`` and ``GET /workspaces/{ws}/drafts/{draft_id}/exports`` resolve the draft inside the
path workspace and the caller's organization. Every draft that is not this workspace's live
draft answers the **same** 404 as an id that never existed: another workspace's draft of the
same organization, another organization's draft, a soft-deleted draft, and a draft whose brief
is soft-deleted (interface sheet P2). Nothing is written for any of them: no ``Export`` row,
no ``creative_draft.exported`` audit row, no notification, and the foreign draft's own export
history is unchanged. Naming another organization's workspace in the path is the membership
403 (never turned into a 404, 5e:59-64). The history lists only the exports of this draft's
exact version in this workspace, even when an export row elsewhere names the same draft id.

Every probe has a positive control (the caller's own approved draft exports, 201), so a
refusal is never a broken route. Export is opened per workspace by a ``content_export``
override (the global flag stays ``False``). The world is the 5C harness (real HTTP, every
role, two organizations), on SQLite and, with ``TEST_POSTGRES_URL``, on a PostgreSQL database
migrated to head.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, update

from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.briefs.models import RecommendationBrief
from app.capabilities.models import WorkspaceCapabilityOverride
from app.core.actor_snapshot import actor_snapshot
from app.creative.models import ContentReview, CreativeDraft
from app.exports.models import Export
from app.notifications.models import Notification
from app.organizations.models import User
from app.privacy.writer import mark_privacy_writer
from app.tests._creative_support import CreativeWorld, creative_world, postgres_creative_world

NOT_FOUND = {"code": "not_found", "message": "Draft not found in this workspace."}
MEMBERSHIP = {"code": "permission_denied", "message": "You are not a member of this organization."}
BODY = {"format": "text/plain"}


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def world(request, tmp_path_factory) -> Iterator[CreativeWorld]:
    if request.param == "sqlite":
        with creative_world(tmp_path_factory.mktemp("w4xi") / "x.db", "w4xi") as w:
            yield w
        return
    with postgres_creative_world("w4xi") as w:  # skips unless TEST_POSTGRES_URL is set
        yield w


# --------------------------------------------------------------------------- #
# Seeding (direct rows, as the 5C/5D batteries seed decisions and reviews)
# --------------------------------------------------------------------------- #
def set_cap(world: CreativeWorld, ws: str, capability: str, enabled: bool | None) -> None:
    """A workspace override for ``capability``; ``None`` removes it."""
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


def exportable(world: CreativeWorld, ws: str, *, code: str = "US-TX") -> str:
    """One approved draft version with a passed review and agreeing jurisdictions."""
    brief_id = world.seed_brief(ws, jurisdiction_code=code)
    tag = uuid.uuid4().hex[:12]
    with world.session() as s:
        brief = s.get(RecommendationBrief, brief_id)
        author = s.get(User, world.owner_id(ws) if ws == world.ws_b else world.users["marketer"])
        snap = actor_snapshot(user_id=author.id, full_name=author.full_name, email=author.email)
        scoped = {"organization_id": brief.organization_id, "workspace_id": ws}
        s.add(CreativeDraft(
            id=f"dr{tag}", brief_id=brief_id, opportunity_id=brief.opportunity_id,
            draft_version=1, channel="social_post", body="Weekend parking is easy.",
            prompt_name="generate_creative", prompt_version="1.0.0", model_identifier="mock-1",
            input_hash="0" * 64, is_simulated=True, input_tokens=1, output_tokens=1,
            estimated_cost_usd=0, jurisdiction_code=code, created_by=author.id,
            created_by_snapshot=snap, **scoped,
        ))
        s.flush()
        s.add(ContentReview(
            draft_id=f"dr{tag}", draft_version=1, risk_level="low", is_blocked=False,
            findings=[], warnings=[], blocked_claim_ids=[], required_disclaimers=[],
            disclaimers_resolved=True, passed=True, jurisdiction_code=code, **scoped,
        ))
        reviewer = s.get(User, world.owner_id(ws) if ws == world.ws_b else world.users["reviewer"])
        s.add(Approval(
            subject_type="creative_draft", subject_id=f"dr{tag}", subject_version=1,
            draft_id=f"dr{tag}", decision="approved", decision_seq=1, decided_by=reviewer.id,
            decided_by_snapshot=actor_snapshot(
                user_id=reviewer.id, full_name=reviewer.full_name, email=reviewer.email
            ),
            decided_role="reviewer", is_simulated=False, **scoped,
        ))
    return f"dr{tag}"


def soft_delete(world: CreativeWorld, model, row_id: str) -> None:
    """``deleted_at`` set through Core in a transaction marked as the sanctioned privacy writer
    (the only writer of these append-only rows; on PostgreSQL the item-37 backstop refuses the
    update otherwise)."""
    with world.session() as s:
        mark_privacy_writer(s)
        s.execute(
            update(model.__table__)
            .where(model.__table__.c.id == row_id)
            .values(deleted_at=datetime.now(UTC))
        )


def written(world: CreativeWorld) -> tuple[int, int, int]:
    """Every export row, export audit row and notification in the database."""
    return (
        world.count(Export),
        world.count(AuditLog, action="creative_draft.exported"),
        world.count(Notification),
    )


def path(ws: str, draft_id: str) -> str:
    return f"/workspaces/{ws}/drafts/{draft_id}/exports"


def error(r) -> dict:
    """The error body without its per-request correlation id."""
    body = r.json()["error"]
    return {k: v for k, v in body.items() if k != "request_id"}


@pytest.fixture
def ws(world: CreativeWorld, request) -> str:
    """A fresh workspace of organization A with export on by override (the flag stays off)."""
    ws = world.workspace(f"x {request.node.name[:40]}")
    set_cap(world, ws, "content_export", True)
    return ws


# --------------------------------------------------------------------------- #
# The uniform 404
# --------------------------------------------------------------------------- #
def test_every_draft_outside_the_workspace_is_the_unknown_id_404_on_post_and_get(world, ws):
    """Unknown, sibling-workspace, other-organization, soft-deleted draft and soft-deleted
    brief: one answer, byte for byte the unknown id's, on both methods; nothing written."""
    set_cap(world, world.ws2, "content_export", True)
    set_cap(world, world.ws_b, "content_export", True)
    sibling = exportable(world, world.ws2)
    foreign = exportable(world, world.ws_b)
    deleted_draft = exportable(world, ws)
    soft_delete(world, CreativeDraft, deleted_draft)
    deleted_brief = exportable(world, ws)
    soft_delete(world, RecommendationBrief, world.row(CreativeDraft, deleted_brief)["brief_id"])
    unknown = uuid.uuid4().hex

    before = written(world)
    for role in ("owner", "admin", "marketer"):  # every exporter
        for draft_id in (unknown, sibling, foreign, deleted_draft, deleted_brief):
            posted = world.send("POST", path(ws, draft_id), role, BODY)
            assert posted.status_code == 404, (role, draft_id, posted.text)
            assert error(posted) == NOT_FOUND
            listed = world.send("GET", path(ws, draft_id), role)
            assert listed.status_code == 404, (role, draft_id, listed.text)
            assert error(listed) == NOT_FOUND
    for role in ("reviewer", "compliance_reviewer"):  # the history's extra readers (item 32)
        for draft_id in (unknown, sibling, foreign, deleted_draft, deleted_brief):
            listed = world.send("GET", path(ws, draft_id), role)
            assert listed.status_code == 404 and error(listed) == NOT_FOUND
    assert written(world) == before

    # Positive control: the caller's own approved draft exports in the same workspace.
    own = exportable(world, ws)
    made = world.send("POST", path(ws, own), "marketer", BODY)
    assert made.status_code == 201, made.text
    assert (made.json()["draft_id"], made.json()["draft_version"]) == (own, 1)


def test_the_sibling_and_foreign_drafts_stay_exportable_only_in_their_own_workspace(world, ws):
    """The refused ids are real, exportable drafts: their own workspace exports them, and the
    refused attempts left their history untouched."""
    set_cap(world, world.ws2, "content_export", True)
    sibling = exportable(world, world.ws2)
    for _ in range(2):
        r = world.send("POST", path(ws, sibling), "owner", BODY)
        assert r.status_code == 404 and error(r) == NOT_FOUND
    assert world.count(Export, draft_id=sibling) == 0
    home = world.send("POST", path(world.ws2, sibling), "owner", BODY)
    assert home.status_code == 201, home.text
    row = world.row(Export, home.json()["id"])
    assert (row["organization_id"], row["workspace_id"]) == (world.org, world.ws2)
    history = world.send("GET", path(world.ws2, sibling), "owner").json()
    assert [i["id"] for i in history["items"]] == [home.json()["id"]]


def test_soft_deleted_draft_or_brief_refuses_even_with_earlier_exports(world, ws):
    """P2: once deleted, the draft has no history and no export, even though it had both."""
    draft = exportable(world, ws)
    first = world.send("POST", path(ws, draft), "admin", BODY)
    assert first.status_code == 201
    assert world.send("GET", path(ws, draft), "admin").json()["total"] == 1
    soft_delete(world, CreativeDraft, draft)
    before = written(world)
    for method, body in (("POST", BODY), ("GET", None)):
        r = world.send(method, path(ws, draft), "admin", body)
        assert r.status_code == 404 and error(r) == NOT_FOUND

    other = exportable(world, ws)
    assert world.send("POST", path(ws, other), "admin", BODY).status_code == 201
    soft_delete(world, RecommendationBrief, world.row(CreativeDraft, other)["brief_id"])
    for method, body in (("POST", BODY), ("GET", None)):
        r = world.send(method, path(ws, other), "admin", body)
        assert r.status_code == 404 and error(r) == NOT_FOUND
    assert written(world)[0] == before[0] + 1  # only the second draft's export, before deletion


# --------------------------------------------------------------------------- #
# The membership 403
# --------------------------------------------------------------------------- #
def test_another_organizations_workspace_in_the_path_is_the_membership_403(world, ws):
    """Not a 404: a non-member naming a workspace is refused by the tenant dependency, for
    POST, GET and the reflection, whatever the draft id; nothing is written."""
    set_cap(world, world.ws_b, "content_export", True)
    theirs = exportable(world, world.ws_b)
    mine = exportable(world, ws)
    before = written(world)
    for role in ("owner", "admin", "marketer", "reviewer", "compliance_reviewer", "viewer"):
        for draft_id in (theirs, mine, uuid.uuid4().hex):
            for method, body in (("POST", BODY), ("GET", None)):
                r = world.send(method, path(world.ws_b, draft_id), role, body)
                assert r.status_code == 403, (role, method, r.text)
                assert error(r) == MEMBERSHIP
        r = world.send("GET", f"/workspaces/{world.ws_b}/export-capability", role)
        assert r.status_code == 403 and error(r) == MEMBERSHIP
    # And from organization B's side: its owner naming organization A's workspace.
    for method, body in (("POST", BODY), ("GET", None)):
        r = world.send(method, path(ws, mine), body=body, token=world.owner_b)
        assert r.status_code == 403 and error(r) == MEMBERSHIP
    assert written(world) == before
    assert world.send("POST", path(world.ws_b, theirs), body=BODY,
                      token=world.owner_b).status_code == 201  # fmt: skip


def test_the_membership_403_comes_before_the_capability_gate(world, ws):
    """A dark foreign workspace still answers 403 to a non-member, never 503."""
    set_cap(world, world.ws_b, "content_export", None)
    theirs = uuid.uuid4().hex
    for method, body in (("POST", BODY), ("GET", None)):
        r = world.send(method, path(world.ws_b, theirs), "owner", body)
        assert r.status_code == 403 and error(r) == MEMBERSHIP


# --------------------------------------------------------------------------- #
# The history's own scope
# --------------------------------------------------------------------------- #
def test_history_lists_only_this_workspaces_exports_of_this_exact_version(world, ws):
    """Export rows elsewhere that name the same draft id (another workspace's or another
    organization's row, another version) never appear, and are never counted."""
    draft = exportable(world, ws)
    made = [world.send("POST", path(ws, draft), "marketer", BODY).json()["id"] for _ in range(2)]
    with world.session() as s:
        who = s.get(User, world.users["marketer"])
        snap = actor_snapshot(user_id=who.id, full_name=who.full_name, email=who.email)
        approval = world.rows(Export, id=made[0])[0]["approval_id"]
        # Rows a defect elsewhere could have written: Export is FK-less on its scope (O-6).
        for org, workspace, version in (
            (world.org, world.ws2, 1),
            (world.org_b, world.ws_b, 1),
            (world.org, ws, 2),
        ):
            s.add(Export(
                organization_id=org, workspace_id=workspace, draft_id=draft,
                draft_version=version, approval_id=approval, format="text/plain",
                jurisdiction_code="US-TX", exported_by=who.id, exported_by_snapshot=snap,
                is_simulated=True,
            ))
    for role in ("owner", "admin", "marketer", "reviewer", "compliance_reviewer"):
        history = world.send("GET", path(ws, draft), role)
        assert history.status_code == 200, history.text
        body = history.json()
        assert sorted(i["id"] for i in body["items"]) == sorted(made)
        assert body["total"] == 2
        assert all(i["draft_version"] == 1 for i in body["items"])
        assert all("content" not in i for i in body["items"])  # the text is never listed


def test_history_pages_never_leave_the_scope(world, ws):
    draft = exportable(world, ws)
    made = [world.send("POST", path(ws, draft), "owner", BODY).json()["id"] for _ in range(3)]
    other = exportable(world, ws)
    world.send("POST", path(ws, other), "owner", BODY)
    seen: list[str] = []
    for offset in range(4):
        page = world.send("GET", f"{path(ws, draft)}?limit=1&offset={offset}", "owner").json()
        assert page["total"] == 3
        seen.extend(i["id"] for i in page["items"])
    assert sorted(seen) == sorted(made)
