"""Project Phase 5C exit criteria 3 and 6 through the 5B approval service:
``POST/GET /workspaces/{ws}/drafts/{draft_id}/approvals`` (W21 lane P5C; coordinator ruling on
hand-back r1, item 1).

* **Exit criterion 6** — a draft whose review is blocked, names a blocked claim, misses a
  required disclaimer, or contains a claim the library blocks *now* cannot be approved (409
  ``approval_review_blocked``, nothing written); rejecting it is allowed; a retired claim
  blocks nothing.
* **Exit criterion 3** — brief → draft → content review → 5B approval, end to end on the mock.
* **5B semantics for drafts** — reviewer only (exact), never the draft's author (by
  ``created_by`` or the snapshot's id), the exact ``draft_version`` (stale 409), revoke only
  while approved, ``decision_seq`` per draft version, the audit row in the same transaction,
  ``subject_type = creative_draft`` with ``draft_id`` set, committed before the 201, dark
  before any lookup, history for every role but viewer, non-enumerating 404s.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, update

from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.campaign_context.models import ClaimsLibraryEntry
from app.core.config import get_settings
from app.creative.models import ContentReview, CreativeDraft
from app.main import app
from app.tests._creative_support import ALL_ROLES, CEILINGS, CreativeWorld, creative_world

HISTORY_READERS = ("owner", "admin", "marketer", "reviewer", "compliance_reviewer")


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Iterator[CreativeWorld]:
    with creative_world(tmp_path_factory.mktemp("p5c-appr") / "appr.db", "p5cappr") as w:
        yield w


@pytest.fixture(autouse=True)
def ceilings(monkeypatch) -> None:
    for name, value in {**CEILINGS, "llm_max_calls_per_workspace_per_day": 1000}.items():
        monkeypatch.setattr(get_settings(), name, value)


def _path(ws: str, draft_id: str) -> str:
    return f"/workspaces/{ws}/drafts/{draft_id}/approvals"


def _decide(world, ws, draft_id, decision="approved", role="reviewer", version=1, **extra):
    return world.send("POST", _path(ws, draft_id), role,
                      {"decision": decision, "subject_version": version, **extra})


def _error(r) -> tuple[int, str]:
    return r.status_code, r.json()["error"]["code"]


def _draft(world: CreativeWorld, ws: str, **generate) -> dict:
    brief_id = world.seed_brief(ws)
    assert world.generated(brief_id, ws=ws, **generate)["status"] == "succeeded"
    [draft] = world.rows(CreativeDraft, brief_id=brief_id)
    return draft


# --------------------------------------------------------------------------- #
# Exit criterion 3: brief → draft → review → approval
# --------------------------------------------------------------------------- #
def test_a_generated_draft_is_approved_by_a_reviewer_end_to_end(world):
    ws = world.workspace("e2e approve")
    draft = _draft(world, ws, channel="email")
    r = _decide(world, ws, draft["id"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["subject_type"], body["subject_id"], body["subject_version"]) == (
        "creative_draft", draft["id"], 1
    )
    assert (body["decision"], body["decision_seq"], body["is_simulated"]) == ("approved", 1, True)
    row = world.row(Approval, body["id"])
    assert (row["draft_id"], row["brief_id"], row["decided_role"]) == (
        draft["id"], None, "reviewer"
    )
    [audit] = world.rows(AuditLog, entity_id=draft["id"], action="approval.approved")
    assert audit["entity_type"] == "creative_draft"
    assert audit["new_state"]["approval_id"] == body["id"]
    history = world.send("GET", _path(ws, draft["id"]), "marketer").json()
    assert [i["id"] for i in history["items"]] == [body["id"]]


# --------------------------------------------------------------------------- #
# Exit criterion 6: the review blocks approval
# --------------------------------------------------------------------------- #
def test_a_blocked_draft_cannot_be_approved_but_can_be_rejected(world):
    ws = world.workspace("blocked")
    world.claim("weekend parking", ws=ws)
    draft = _draft(world, ws)
    assert world.rows(ContentReview, draft_id=draft["id"])[0]["is_blocked"] is True
    r = _decide(world, ws, draft["id"])
    assert _error(r) == (409, "approval_review_blocked")
    assert "blocked it" in r.json()["error"]["message"]
    assert world.count(Approval, draft_id=draft["id"]) == 0
    assert world.count(AuditLog, entity_id=draft["id"], action="approval.approved") == 0
    rejected = _decide(world, ws, draft["id"], "rejected", reason_code="brand_or_compliance_risk")
    assert rejected.status_code == 201, rejected.text


def test_a_draft_missing_a_required_disclaimer_cannot_be_approved(world, monkeypatch):
    from app.llm import service as service_module
    from app.tests.test_creative_generation import _Spy

    ws = world.workspace("disclaimer")
    world.claim("Licensed in Texas.", kind="required_disclaimer", ws=ws)
    spy = _Spy(service_module.llm_service._provider,
               {"headline": "", "body": "Come by.", "call_to_action": ""})
    monkeypatch.setattr(service_module.llm_service, "_provider", spy)
    draft = _draft(world, ws)
    r = _decide(world, ws, draft["id"])
    assert _error(r) == (409, "approval_review_blocked")
    assert "disclaimer" in r.json()["error"]["message"]


def test_a_claim_blocked_after_the_review_blocks_and_retiring_it_unblocks(world):
    ws = world.workspace("late claim")
    draft = _draft(world, ws)
    claim = world.claim("weekend parking", ws=ws)
    assert _error(_decide(world, ws, draft["id"])) == (409, "approval_review_blocked")
    with world.witness.begin() as conn:
        conn.execute(update(ClaimsLibraryEntry.__table__)
                     .where(ClaimsLibraryEntry.__table__.c.id == claim)
                     .values(retired_at=datetime.now(UTC)))
    assert _decide(world, ws, draft["id"]).status_code == 201


def test_a_required_disclaimer_added_after_the_review_blocks_approval(world):
    """O-4: the library's required disclaimers are re-read at approval, like its blocked
    claims; rejecting stays allowed."""
    ws = world.workspace("late disclaimer")
    draft = _draft(world, ws)
    world.claim("Licensed in Texas.", kind="required_disclaimer", ws=ws)
    r = _decide(world, ws, draft["id"])
    assert _error(r) == (409, "approval_review_blocked")
    assert "disclaimer the claims library now requires" in r.json()["error"]["message"]
    assert "Texas" not in r.text
    assert world.count(Approval, draft_id=draft["id"]) == 0
    rejected = _decide(world, ws, draft["id"], "rejected", reason_code="brand_or_compliance_risk")
    assert rejected.status_code == 201, rejected.text


def _ui_claim(world, ws: str, text: str, kind: str) -> str:
    """A claim created the way the campaign-context UI creates one: a steward's POST to
    ``/claims`` with the form's fields (``kind`` from its select: approved / restricted /
    blocked / required_disclaimer; D-N4 item 36 replaced the select's "Prohibited" with
    "Blocked", value ``blocked``, and the API refuses ``prohibited`` with a 422)."""
    r = world.send("POST", f"/workspaces/{ws}/claims", "compliance_reviewer",
                   {"text": text, "kind": kind, "risk_level": "high", "category": "parking"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_a_claim_the_ui_marks_blocked_blocks_the_draft_end_to_end(world):
    """DEFECT-5CW4-2 through the API: the UI's blocking kind ("Blocked", formerly
    "Prohibited") blocks the review of a draft generated after it, and the approval of a
    draft generated before it; the UI's "Restricted" blocks nothing. A stored
    ``prohibited`` is migrated to ``blocked`` (D-N4 item 36) and is still read as blocking
    (``test_creative_generation``, ``test_claim_kinds``)."""
    ws = world.workspace("ui blocked")
    claim = _ui_claim(world, ws, "weekend parking", "blocked")
    draft = _draft(world, ws, channel="email")
    [review] = world.rows(ContentReview, draft_id=draft["id"])
    assert (review["is_blocked"], review["blocked_claim_ids"]) == (True, [claim])
    assert _error(_decide(world, ws, draft["id"])) == (409, "approval_review_blocked")
    rejected = _decide(world, ws, draft["id"], "rejected", reason_code="brand_or_compliance_risk")
    assert rejected.status_code == 201, rejected.text

    later = world.workspace("ui blocked later")
    _ui_claim(world, later, "weekend parking", "restricted")  # restricted: inert
    before = _draft(world, later)
    _ui_claim(world, later, "weekend parking", "blocked")  # added after the review
    r = _decide(world, later, before["id"])
    assert _error(r) == (409, "approval_review_blocked")
    assert "claims library now blocks" in r.json()["error"]["message"]
    assert world.count(Approval, draft_id=before["id"]) == 0


# --------------------------------------------------------------------------- #
# 5B semantics on drafts
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", [r for r in ALL_ROLES if r != "reviewer"])
def test_only_a_reviewer_decides(world, role):
    ws = world.workspace(f"role {role}")
    draft = _draft(world, ws)
    assert _error(_decide(world, ws, draft["id"], role=role)) == (403, "permission_denied")
    assert world.count(Approval, draft_id=draft["id"]) == 0


@pytest.mark.parametrize("role", ALL_ROLES)
def test_the_history_is_for_every_role_but_viewer(world, role):
    ws = world.workspace(f"history {role}")
    draft = _draft(world, ws)
    r = world.send("GET", _path(ws, draft["id"]), role)
    assert r.status_code == (200 if role in HISTORY_READERS else 403)


@pytest.mark.parametrize("by", ["created_by", "snapshot"])
def test_the_author_cannot_decide(world, by):
    """A draft whose author is the reviewer (by the live id, or by the snapshot's id once the
    FK is nulled). Written through Core: the generation path only lets editors author."""
    ws = world.workspace(f"author {by}")
    draft = _draft(world, ws)
    reviewer = world.users["reviewer"]
    authored = {**{k: v for k, v in draft.items() if k not in ("id", "created_at")},
                "id": f"au{draft['id'][:30]}", "draft_version": 2,
                "created_by": reviewer if by == "created_by" else None,
                "created_by_snapshot": f"{reviewer} reviewer member <x@example.com>"}
    with world.witness.begin() as conn:
        conn.execute(insert(CreativeDraft.__table__).values(**authored))
        review = world.rows(ContentReview, draft_id=draft["id"])[0]
        conn.execute(insert(ContentReview.__table__).values(
            **{k: v for k, v in review.items()
               if k not in ("id", "created_at", "draft_id", "draft_version")},
            id=f"rv{authored['id'][:30]}", draft_id=authored["id"], draft_version=2,
        ))
    r = _decide(world, ws, authored["id"], version=2)
    assert _error(r) == (403, "approval_author_conflict")


def test_a_stale_version_and_a_revoke_without_approval_are_conflicts(world):
    ws = world.workspace("rules")
    draft = _draft(world, ws)
    assert _error(_decide(world, ws, draft["id"], version=2)) == (409, "approval_stale_version")
    revoke = _decide(world, ws, draft["id"], "revoked", reason_code="circumstances_changed")
    assert _error(revoke) == (409, "approval_nothing_to_revoke")
    assert _decide(world, ws, draft["id"]).json()["decision_seq"] == 1
    again = _decide(world, ws, draft["id"], "revoked", reason_code="circumstances_changed")
    assert again.status_code == 201 and again.json()["decision_seq"] == 2
    history = world.send("GET", _path(ws, draft["id"]), "reviewer").json()["items"]
    assert [(i["decision"], i["decision_seq"]) for i in history] == [
        ("revoked", 2), ("approved", 1)
    ]


def test_dark_before_any_lookup_and_scoped_404s(world):
    ws, sibling = world.workspace("dark appr", enabled=False), world.workspace("sibling appr")
    their = _draft(world, sibling)
    for draft_id in (their["id"], "0" * 32):
        assert _error(_decide(world, ws, draft_id)) == (503, "capability_unavailable")
        dark = world.send("GET", _path(ws, draft_id), "reviewer")
        assert _error(dark) == (503, "capability_unavailable")
    world.enable(ws)
    for draft_id in (their["id"], "0" * 32):
        r = _decide(world, ws, draft_id)
        assert (r.status_code, r.json()["error"]["message"]) == (
            404, "Draft not found in this workspace."
        )
        assert world.send("GET", _path(ws, draft_id), "reviewer").status_code == 404
    assert world.count(Approval, draft_id=their["id"]) == 0


def test_the_role_is_refused_before_the_dark_gate(world):
    """Role (exact sets) → capability: a non-reviewer deciding, or a viewer reading the
    history, on a dark workspace is refused 403, not told the capability's state."""
    ws = world.workspace("dark roles", enabled=False)
    assert _error(_decide(world, ws, "0" * 32, role="marketer")) == (403, "permission_denied")
    viewer = world.send("GET", _path(ws, "0" * 32), "viewer")
    assert _error(viewer) == (403, "permission_denied")


def test_the_service_itself_refuses_a_foreign_draft_and_a_non_reviewer(world):
    """Defence in depth behind the route: ``record_draft_decision`` re-checks the role and the
    draft's tenant scope itself, before any lock or write."""
    from types import SimpleNamespace

    from app.approvals.service import record_draft_decision
    from app.core.enums import Role
    from app.core.errors import NotFoundError, PermissionDeniedError
    from app.organizations.models import User

    ws, other = world.workspace("svc scope"), world.workspace("svc other")
    draft = _draft(world, ws)

    def ctx(s, role: Role, workspace: str):
        user = s.get(User, world.users["reviewer"])
        return SimpleNamespace(role=role, user=user, workspace=SimpleNamespace(id=workspace),
                               organization=SimpleNamespace(id=world.org))

    with world.session() as s:
        row = s.get(CreativeDraft, draft["id"])
        with pytest.raises(NotFoundError):
            record_draft_decision(s, ctx=ctx(s, Role.REVIEWER, other), draft=row,
                                  decision="approved", subject_version=1)
        with pytest.raises(PermissionDeniedError):
            record_draft_decision(s, ctx=ctx(s, Role.MARKETER, ws), draft=row,
                                  decision="approved", subject_version=1)
    assert world.count(Approval, draft_id=draft["id"]) == 0


def test_brief_and_draft_histories_never_mix_even_when_their_ids_collide(world):
    """A brief's decisions exclude draft decisions and a draft's exclude brief decisions
    (W21-REV-INT5AB r2 §9, item 4), shown on the one case where only ``subject_type`` tells
    them apart: a draft whose id and version equal an approved brief's. The sequence numbers,
    both histories, and whether the brief counts as approved for generation stay apart."""
    from types import SimpleNamespace

    from app.approvals.service import list_decisions, record_decision
    from app.briefs.models import RecommendationBrief
    from app.core.enums import Role
    from app.organizations.models import User

    ws = world.workspace("ids collide")
    brief_id = world.seed_brief(ws)  # the brief's decision 1: approved
    source = _draft(world, ws)
    host = world.seed_brief(ws, approved=False)
    twin = {**{k: v for k, v in source.items() if k not in ("id", "created_at")},
            "id": brief_id, "brief_id": host, "draft_version": 1}
    [review] = world.rows(ContentReview, draft_id=source["id"])
    with world.witness.begin() as conn:
        conn.execute(insert(CreativeDraft.__table__).values(**twin))
        conn.execute(insert(ContentReview.__table__).values(
            **{k: v for k, v in review.items() if k not in ("id", "created_at", "draft_id")},
            id=f"rv{brief_id[:30]}", draft_id=brief_id,
        ))

    def draft_history() -> list[tuple[str, str, int]]:
        items = world.send("GET", _path(ws, brief_id), "reviewer").json()["items"]
        return [(i["subject_type"], i["decision"], i["decision_seq"]) for i in items]

    assert draft_history() == []  # the brief's approval is not the draft's
    assert _decide(world, ws, brief_id).json()["decision_seq"] == 1
    revoke = _decide(world, ws, brief_id, "revoked", reason_code="circumstances_changed")
    assert revoke.json()["decision_seq"] == 2
    assert _decide(world, ws, brief_id).json()["decision_seq"] == 3

    with world.session() as s:  # the brief's own next decision: its sequence, not the draft's
        brief = s.get(RecommendationBrief, brief_id)
        ctx = SimpleNamespace(role=Role.REVIEWER, user=s.get(User, world.users["reviewer"]),
                              workspace=SimpleNamespace(id=ws),
                              organization=SimpleNamespace(id=world.org))
        rejected = record_decision(s, ctx=ctx, brief=brief, decision="rejected",
                                   subject_version=1, reason_code="inaccurate")
        assert rejected.decision_seq == 2
    with world.session() as s:
        page = list_decisions(s, brief=s.get(RecommendationBrief, brief_id))
        assert [(a.subject_type, a.decision, a.decision_seq) for a in page.items] == [
            ("recommendation_brief", "rejected", 2), ("recommendation_brief", "approved", 1)
        ]
    assert draft_history() == [
        ("creative_draft", "approved", 3), ("creative_draft", "revoked", 2),
        ("creative_draft", "approved", 1),
    ]
    # The brief's controlling decision is its rejection; the draft's later approval under
    # the same id and version does not make the brief generate.
    refused = world.generate(brief_id, ws=ws)
    assert _error(refused) == (422, "brief_not_approved")


def test_the_decision_is_committed_before_the_201(world):
    from app.db.session import get_db

    ws = world.workspace("commit appr")
    draft = _draft(world, ws)
    committed = app.dependency_overrides[get_db]

    def rollback_teardown():
        s = world.factory()
        try:
            yield s
            s.rollback()
        finally:
            s.close()

    app.dependency_overrides[get_db] = rollback_teardown
    try:
        r = _decide(world, ws, draft["id"])
    finally:
        app.dependency_overrides[get_db] = committed
    assert r.status_code == 201, r.text
    assert world.row(Approval, r.json()["id"]) is not None
    assert world.count(AuditLog, entity_id=draft["id"], action="approval.approved") == 1


def test_postgres_concurrent_decisions_on_one_draft_take_distinct_sequence_numbers():
    """Four deciders at once on one draft version, each in a session of its own (the service
    path; READ COMMITTED): the draft row lock serializes them, so the sequence is 1..4 and no
    decision is refused as a clash."""
    from types import SimpleNamespace

    from app.approvals.service import record_draft_decision
    from app.core.enums import Role
    from app.organizations.models import User
    from app.tests._creative_support import postgres_creative_world

    with postgres_creative_world("p5cappr") as world:
        world.enable()
        draft = _draft(world, world.ws)
        barrier, errors = threading.Barrier(4), []

        def decide():
            try:
                with world.session() as s:
                    reviewer = s.get(User, world.users["reviewer"])
                    ctx = SimpleNamespace(
                        role=Role.REVIEWER, user=reviewer,
                        workspace=SimpleNamespace(id=world.ws),
                        organization=SimpleNamespace(id=world.org),
                    )
                    row = s.get(CreativeDraft, draft["id"])
                    barrier.wait(30)
                    record_draft_decision(s, ctx=ctx, draft=row, decision="rejected",
                                          subject_version=1, reason_code="inaccurate")
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(repr(exc))

        threads = [threading.Thread(target=decide) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert errors == [] and not any(t.is_alive() for t in threads)
        seqs = sorted(a["decision_seq"] for a in world.rows(Approval, draft_id=draft["id"]))
        assert seqs == [1, 2, 3, 4]
