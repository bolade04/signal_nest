"""Project Phase 5E (lane 5E-W4): export refuses a draft without a jurisdiction (precondition 5).

Rulings kept (interface sheet D): S1-5D 31-A, "NULL or non-code jurisdiction: no generation and
no export"; its implementation reading, "NULL" means "NULL or not a valid jurisdiction code", so
a stored ``ＵＳ`` or ``U1`` (which passes the column's shape CHECK) refuses exactly as NULL
does; and Q-4, a workspace-wide opportunity's brief is written with jurisdiction NULL, and
"the 5E-W1 export refusal applies to them too". Nothing is inferred or defaulted.

Proven here, through the real route, for an otherwise exportable draft (approved, review
passed, export on): a NULL, non-code or ``UNRESOLVED``-shaped value on the draft or on its
brief answers 409 ``export_jurisdiction_missing`` with its fixed message and writes nothing;
a Q-4 brief (no location, NULL code) refuses whatever the draft carries; draft and brief codes
that disagree answer 409 ``jurisdiction_mismatch`` (P12); the jurisdiction refusal comes
before the decision and review refusals (a permanent refusal first); and the success path
copies the draft's own code onto the export row. At the database: an ``Export`` row with a
NULL code, the resolver's ``UNRESOLVED`` sentinel or a lower-case code cannot be inserted
(``exports.jurisdiction_code`` is NOT NULL with a CHECK that has no ``IS NULL`` limb).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, insert
from sqlalchemy.exc import IntegrityError

from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.briefs.models import RecommendationBrief
from app.capabilities.models import WorkspaceCapabilityOverride
from app.core.actor_snapshot import actor_snapshot
from app.creative.models import ContentReview, CreativeDraft
from app.exports.models import Export
from app.notifications.models import Notification
from app.organizations.models import User
from app.tests._creative_support import CreativeWorld, creative_world, postgres_creative_world

MISSING = {
    "code": "export_jurisdiction_missing",
    "message": "No jurisdiction was recorded for this draft or its brief, so it can't be exported.",
}
MISMATCH = {
    "code": "jurisdiction_mismatch",
    "message": "This draft's jurisdiction does not match its brief's, so it can't be exported.",
}
#: Values a jurisdiction column can hold (they pass its portable shape CHECK) that are not
#: jurisdiction codes by the grammar: full-width letters, a digit in the country part, and a
#: subdivision part with a character outside [A-Z0-9].
NON_CODES = ("ＵＳ", "U1", "1U", "US-T_X")
BODY = {"format": "text/plain"}


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def world(request, tmp_path_factory) -> Iterator[CreativeWorld]:
    if request.param == "sqlite":
        with creative_world(tmp_path_factory.mktemp("w4xj") / "j.db", "w4xj") as w:
            yield w
        return
    with postgres_creative_world("w4xj") as w:  # skips unless TEST_POSTGRES_URL is set
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
    ws = world.workspace(f"j {request.node.name[:40]}")
    set_cap(world, ws, "content_export", True)
    return ws


def draft(
    world: CreativeWorld,
    ws: str,
    *,
    draft_code: str | None,
    brief_code: str | None,
    approved: bool = True,
    review_passed: bool = True,
) -> str:
    """A draft version of a brief (no location: the harness's briefs carry none) with the
    given codes, a review and, unless told not, the reviewer's approval as its latest decision."""
    brief_id = world.seed_brief(ws, jurisdiction_code=brief_code)
    tag = uuid.uuid4().hex[:12]
    with world.session() as s:
        brief = s.get(RecommendationBrief, brief_id)
        author = s.get(User, world.users["marketer"])
        reviewer = s.get(User, world.users["reviewer"])
        scoped = {"organization_id": brief.organization_id, "workspace_id": ws}
        s.add(CreativeDraft(
            id=f"dr{tag}", brief_id=brief_id, opportunity_id=brief.opportunity_id,
            draft_version=1, channel="social_post", body="Weekend parking is easy.",
            prompt_name="generate_creative", prompt_version="1.0.0", model_identifier="mock-1",
            input_hash="0" * 64, is_simulated=True, input_tokens=1, output_tokens=1,
            estimated_cost_usd=0, jurisdiction_code=draft_code, created_by=author.id,
            created_by_snapshot=actor_snapshot(
                user_id=author.id, full_name=author.full_name, email=author.email
            ),
            **scoped,
        ))
        s.flush()
        s.add(ContentReview(
            draft_id=f"dr{tag}", draft_version=1,
            risk_level="low" if review_passed else "blocked", is_blocked=not review_passed,
            findings=[], warnings=[], blocked_claim_ids=[], required_disclaimers=[],
            disclaimers_resolved=review_passed, passed=review_passed,
            jurisdiction_code=draft_code, **scoped,
        ))
        if approved:
            s.add(Approval(
                subject_type="creative_draft", subject_id=f"dr{tag}", subject_version=1,
                draft_id=f"dr{tag}", decision="approved", decision_seq=1,
                decided_by=reviewer.id,
                decided_by_snapshot=actor_snapshot(
                    user_id=reviewer.id, full_name=reviewer.full_name, email=reviewer.email
                ),
                decided_role="reviewer", is_simulated=False, **scoped,
            ))
    return f"dr{tag}"


def written(world: CreativeWorld) -> tuple[int, int, int]:
    return (
        world.count(Export),
        world.count(AuditLog, action="creative_draft.exported"),
        world.count(Notification),
    )


def export(world: CreativeWorld, ws: str, draft_id: str, role: str = "marketer"):
    return world.send("POST", f"/workspaces/{ws}/drafts/{draft_id}/exports", role, BODY)


def error(r) -> dict:
    return {k: v for k, v in r.json()["error"].items() if k != "request_id"}


def assert_refused(world: CreativeWorld, ws: str, draft_id: str, expected: dict) -> None:
    before = written(world)
    for role in ("owner", "admin", "marketer"):
        r = export(world, ws, draft_id, role)
        assert r.status_code == 409, (role, r.text)
        assert error(r) == expected
        assert "Weekend parking" not in r.text  # the draft's text never leaves in a refusal
    assert written(world) == before


# --------------------------------------------------------------------------- #
# NULL on the draft, NULL on the brief, and Q-4's absent-location brief
# --------------------------------------------------------------------------- #
def test_a_null_draft_code_refuses(world, ws):
    assert_refused(world, ws, draft(world, ws, draft_code=None, brief_code="US-TX"), MISSING)


def test_a_null_brief_code_refuses_although_the_draft_carries_one(world, ws):
    """Precondition 5 reads both codes: a code on the draft does not stand in for the brief's."""
    assert_refused(world, ws, draft(world, ws, draft_code="US-TX", brief_code=None), MISSING)


def test_a_q4_absent_location_brief_refuses_its_drafts(world, ws):
    """Q-4: a workspace-wide opportunity's brief has no location and jurisdiction NULL. Its
    drafts are refused whatever they carry (NULL by item 31, or a code a legacy row holds)."""
    for draft_code in (None, "US-TX", "GB"):
        made = draft(world, ws, draft_code=draft_code, brief_code=None)
        brief = world.row(RecommendationBrief, world.row(CreativeDraft, made)["brief_id"])
        assert brief["location_id"] is None and brief["jurisdiction_code"] is None
        assert_refused(world, ws, made, MISSING)


def test_both_codes_null_refuses(world, ws):
    assert_refused(world, ws, draft(world, ws, draft_code=None, brief_code=None), MISSING)


# --------------------------------------------------------------------------- #
# Non-codes (the grammar, not IS NOT NULL)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", NON_CODES)
def test_a_non_code_on_the_draft_refuses(world, ws, value):
    made = draft(world, ws, draft_code=value, brief_code="US-TX")
    assert world.row(CreativeDraft, made)["jurisdiction_code"] == value  # stored as written
    assert_refused(world, ws, made, MISSING)


@pytest.mark.parametrize("value", NON_CODES)
def test_a_non_code_on_the_brief_refuses(world, ws, value):
    assert_refused(world, ws, draft(world, ws, draft_code="US-TX", brief_code=value), MISSING)


@pytest.mark.parametrize("value", NON_CODES)
def test_the_same_non_code_on_both_refuses_rather_than_agreeing(world, ws, value):
    """Two equal non-codes are not an agreement: the grammar refuses before the comparison."""
    assert_refused(world, ws, draft(world, ws, draft_code=value, brief_code=value), MISSING)


# --------------------------------------------------------------------------- #
# P12 and the order of the refusals
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("draft_code", "brief_code"), [("US-TX", "US-CA"), ("GB", "GB-ENG"),
                                                        ("US", "US-TX")])  # fmt: skip
def test_codes_that_disagree_answer_jurisdiction_mismatch(world, ws, draft_code, brief_code):
    assert_refused(world, ws, draft(world, ws, draft_code=draft_code, brief_code=brief_code),
                   MISMATCH)  # fmt: skip


def test_the_jurisdiction_refusal_precedes_the_decision_and_the_review(world, ws):
    """A permanent refusal first: an unapproved, review-blocked draft without a code is refused
    for its code, so the answer does not change when it is later approved."""
    for codes in ({"draft_code": None, "brief_code": "US-TX"},
                  {"draft_code": "U1", "brief_code": "US-TX"},
                  {"draft_code": "US-TX", "brief_code": None}):  # fmt: skip
        made = draft(world, ws, approved=False, review_passed=False, **codes)
        assert_refused(world, ws, made, MISSING)
    made = draft(world, ws, draft_code="US-TX", brief_code="US-CA", approved=False,
                 review_passed=False)  # fmt: skip
    assert_refused(world, ws, made, MISMATCH)


# --------------------------------------------------------------------------- #
# Success copies the draft's code; the database refuses a code-less export row
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", ["US-TX", "GB", "NG-LA", "GB-ENG"])
def test_success_copies_the_drafts_code_onto_the_export(world, ws, code):
    made = draft(world, ws, draft_code=code, brief_code=code)
    r = export(world, ws, made)
    assert r.status_code == 201, r.text
    assert r.json()["jurisdiction_code"] == code
    row = world.row(Export, r.json()["id"])
    assert row["jurisdiction_code"] == code
    assert r.json()["is_simulated"] is True and row["is_simulated"] is True  # the draft's


@pytest.mark.parametrize("value", [None, "UNRESOLVED", "us-tx", "USA", ""])
def test_the_database_refuses_an_export_row_without_a_code(world, ws, value):
    """``exports.jurisdiction_code``: NOT NULL, and its CHECK has no ``IS NULL`` limb and
    refuses the sentinel, lower case and a three-letter country (exports/models.py)."""
    made = draft(world, ws, draft_code="US-TX", brief_code="US-TX")
    approval = world.rows(Approval, subject_id=made)[0]["id"]
    who = world.users["owner"]
    row = {
        "id": uuid.uuid4().hex, "organization_id": world.org, "workspace_id": ws,
        "draft_id": made, "draft_version": 1, "approval_id": approval, "format": "text/plain",
        "jurisdiction_code": value, "exported_by": who,
        "exported_by_snapshot": f"{who} owner <o@example.com>", "is_simulated": True,
    }
    with pytest.raises(IntegrityError):
        with world.engine.begin() as conn:
            conn.execute(insert(Export.__table__).values(**row))
    assert world.count(Export, draft_id=made) == 0
    with world.engine.begin() as conn:  # the same row with a code is accepted (control)
        conn.execute(insert(Export.__table__).values(**{**row, "jurisdiction_code": "US-TX"}))
    assert world.count(Export, draft_id=made) == 1
