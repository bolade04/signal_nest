"""Project Phase 5 CF-1 (W21): the frozen API schemas, the approver policy, and "nothing activates".

* Every closed vocabulary on the 5A/5B response schemas is enum-typed (Project Phase 5 plan
  §6.2.1), so it will reach ``schema.d.ts`` as a closed union when the routes land.
* Requests are strict (``extra="forbid"``): scope, provenance, author and decider can never be
  supplied by a client; the approval request refuses a reason of the wrong polarity (422 at
  the boundary, the same rule the database CHECK enforces); content is bounded.
* Responses project stored rows without the internal columns (scope ids, ``input_hash``,
  ``trace_id``, the usage-ledger link; ``updated_at`` for audit records).
* The approver gate is the Phase 6 exact-role primitive admitting exactly ``REVIEWER`` (O-1,
  O-2).
* Nothing activates: the flag defaults ``False``, and CF-1 mounts no brief or approval route.
"""

from __future__ import annotations

import typing
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.approvals.models import APPROVAL_NOTE_MAX_LENGTH, Approval
from app.approvals.policy import APPROVER_ROLES, require_approver
from app.approvals.reasons import ApprovalReason
from app.approvals.schemas import ApprovalDecisionCreate, ApprovalOut
from app.audit.actions import AuditAction
from app.audit.models import AuditLog
from app.audit.schemas import AuditLogOut
from app.briefs.models import RecommendationBrief
from app.briefs.schemas import (
    BRIEF_BOUNDS,
    BriefContent,
    RecommendationBriefCreate,
    RecommendationBriefOut,
)
from app.core.config import Settings
from app.core.enums import ApprovalDecision, ApprovalSubjectType, Role
from app.core.errors import PermissionDeniedError
from app.main import app

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def _annotation(model, field: str):
    return model.model_fields[field].annotation


@pytest.mark.parametrize(
    ("model", "field", "enum"),
    [
        (ApprovalOut, "decision", ApprovalDecision),
        (ApprovalOut, "subject_type", ApprovalSubjectType),
        (ApprovalOut, "reason_code", ApprovalReason),
        (ApprovalOut, "decided_role", Role),
        (ApprovalDecisionCreate, "decision", ApprovalDecision),
        (ApprovalDecisionCreate, "reason_code", ApprovalReason),
        (AuditLogOut, "action", AuditAction),
    ],
)
def test_closed_vocabularies_are_enum_typed(model, field, enum) -> None:
    annotation = _annotation(model, field)
    members = typing.get_args(annotation) or (annotation,)
    assert enum in members, (model.__name__, field, annotation)


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "smuggled",
    ["workspace_id", "organization_id", "created_by", "brief_version", "parent_brief_id",
     "analysis_version", "input_hash", "location_id"],
)
def test_the_brief_request_accepts_only_caller_owned_facts(smuggled: str) -> None:
    RecommendationBriefCreate(intelligence_record_id="intel-1", campaign_id=None)
    with pytest.raises(ValidationError):
        RecommendationBriefCreate(intelligence_record_id="intel-1", **{smuggled: "x"})


@pytest.mark.parametrize(
    "smuggled", ["decided_by", "decided_role", "decision_seq", "is_simulated", "workspace_id"]
)
def test_the_decision_request_accepts_only_caller_owned_facts(smuggled: str) -> None:
    with pytest.raises(ValidationError):
        ApprovalDecisionCreate(decision="approved", subject_version=1, **{smuggled: "x"})


def test_the_decision_request_enforces_polarity_and_bounds() -> None:
    ok = ApprovalDecisionCreate(
        decision="rejected", subject_version=2, reason_code="inaccurate", note="n"
    )
    assert ok.reason_code is ApprovalReason.INACCURATE
    ApprovalDecisionCreate(decision="revoked", subject_version=1)  # a reason is optional
    for bad in (
        {"decision": "approved", "subject_version": 1, "reason_code": "inaccurate"},
        {"decision": "revoked", "subject_version": 1, "reason_code": "evidence_sufficient"},
        {"decision": "approved", "subject_version": 0},
        {"decision": "approve", "subject_version": 1},
        {"decision": "approved", "subject_version": 1,
         "note": "x" * (APPROVAL_NOTE_MAX_LENGTH + 1)},
    ):
        with pytest.raises(ValidationError):
            ApprovalDecisionCreate(**bad)


def test_brief_content_is_bounded() -> None:
    BriefContent(summary="s", rationale="r", recommended_actions=["a"] * BRIEF_BOUNDS["list_items"])
    for bad in (
        {"summary": "x" * (BRIEF_BOUNDS["summary"] + 1), "rationale": "r"},
        {"summary": "s", "rationale": "r", "risk_notes": ["n"] * (BRIEF_BOUNDS["list_items"] + 1)},
        {"summary": "s", "rationale": "r",
         "suggested_angles": ["x" * (BRIEF_BOUNDS["list_item_chars"] + 1)]},
        {"summary": "", "rationale": "r"},
        {"summary": "s", "rationale": "r", "extra_field": 1},
    ):
        with pytest.raises(ValidationError):
            BriefContent(**bad)


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #
def test_the_brief_response_projects_a_row_without_internal_columns() -> None:
    row = RecommendationBrief(
        id="b1", organization_id="o", workspace_id="w", opportunity_id="opp",
        intelligence_record_id="intel", brief_version=1, analysis_version="1",
        scoring_version="1", prompt_name="p", prompt_version="1.0.0", model_identifier="m",
        input_hash="0" * 64, trace_id="t", llm_usage_event_id="u", is_simulated=True,
        created_by="u1", created_by_snapshot="u1 N <e@x.test>", summary="s", rationale="r",
        recommended_actions=["a"], suggested_angles=[], risk_notes=[], evidence_refs=["intel"],
        created_at=NOW,
    )
    out = RecommendationBriefOut.model_validate(row).model_dump()
    assert out["brief_version"] == 1 and out["evidence_refs"] == ["intel"]
    hidden = {"organization_id", "workspace_id", "input_hash", "trace_id", "llm_usage_event_id"}
    assert not hidden & set(out)
    assert "status" not in out  # derived at read time, never frozen (Phase 5A plan §2)


def test_the_approval_and_audit_responses_project_rows() -> None:
    approval = Approval(
        id="a1", organization_id="o", workspace_id="w", subject_type="recommendation_brief",
        subject_id="b1", subject_version=1, brief_id="b1", decision="approved", decision_seq=1,
        reason_code="evidence_sufficient", note=None, decided_by="u2",
        decided_by_snapshot="u2 R <r@x.test>", decided_role="reviewer", is_simulated=False,
        created_at=NOW,
    )
    out = ApprovalOut.model_validate(approval)
    assert out.decision is ApprovalDecision.APPROVED and out.decided_role is Role.REVIEWER
    assert "organization_id" not in out.model_dump()

    log = AuditLog(
        id="l1", organization_id="o", workspace_id=None, action="opportunity.saved",
        context={}, created_at=NOW, updated_at=NOW,
    )
    audit = AuditLogOut.model_validate(log)
    assert audit.action is AuditAction.OPPORTUNITY_SAVED
    assert {"organization_id", "updated_at"}.isdisjoint(audit.model_dump())


# --------------------------------------------------------------------------- #
# Approver policy (O-1, O-2) — the Phase 6 exact-role primitive, reused
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", list(Role))
def test_only_a_reviewer_passes_the_approver_gate(role: Role) -> None:
    assert APPROVER_ROLES == frozenset({Role.REVIEWER})
    ctx = SimpleNamespace(role=role)
    if role is Role.REVIEWER:
        assert require_approver(ctx=ctx) is ctx
    else:  # no rank inheritance: OWNER and ADMIN are refused too
        with pytest.raises(PermissionDeniedError):
            require_approver(ctx=ctx)


# --------------------------------------------------------------------------- #
# Nothing activates
# --------------------------------------------------------------------------- #
def test_the_flag_ships_dark_and_no_route_is_mounted() -> None:
    assert Settings(_env_file=None).recommendation_briefs_enabled is False
    paths = set(app.openapi()["paths"])
    assert not [p for p in paths if "/briefs" in p or "approval" in p], sorted(paths)
    schemas = set(app.openapi()["components"]["schemas"])
    assert not {"RecommendationBriefOut", "ApprovalOut", "AuditLogOut"} & schemas
