"""Feature-gated opportunity-feedback API (Phase 3C, 3C-C).

Two endpoints, both nested under an opportunity and both editor-gated *and*
feature-gated:

    POST /api/v1/workspaces/{workspace_id}/opportunities/{opportunity_id}/feedback
    GET  /api/v1/workspaces/{workspace_id}/opportunities/{opportunity_id}/feedback

Design, mirroring the scouting-schedule route boundary:

* **Feature gate** — while ``opportunity_feedback_enabled`` is off (the dark default)
  *every* operation, read and write alike, answers 503 ``capability_unavailable``.
  Unlike the schedule reads, feedback history is also gated: nothing about the loop is
  exposed until the feature is deliberately enabled.
* **Authorization** — both submit and read require an editor role
  (owner / admin / marketer). A non-member is 403; an unauthenticated caller is 401.
* **Scope / IDOR** — the opportunity is resolved within the path workspace (unknown or
  cross-workspace → 404, a hidden IDOR). The target intelligence record must live in
  the same workspace (else 404, uniform with a missing id so existence can't be
  probed); the service then enforces that it belongs to *this* opportunity's exact
  tenant scope, rejecting a sibling-opportunity record as 422.
* **Capture-only** — writes delegate to :func:`app.feedback.service.create_feedback`,
  which is append-only and never scores, rescoring, ranks, trains or emits any
  cross-workspace signal. Reads are a bounded, reverse-chronological projection that
  never mutates anything.

Transaction ownership follows the house rule: the route/service ``flush`` and the
request-scoped ``get_db`` override owns the commit.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import TenantContext, require_role
from app.capabilities.registry import Capability
from app.capabilities.resolver import resolve_capability
from app.core.config import get_settings
from app.core.enums import Role
from app.core.errors import CapabilityUnavailableError, NotFoundError
from app.core.logging import get_logger, log_event
from app.db.session import get_db
from app.feedback.models import OpportunityFeedback
from app.feedback.schemas import FeedbackCreate, FeedbackHistoryOut, FeedbackOut
from app.feedback.service import create_feedback
from app.intelligence.records import SignalIntelligenceRecord
from app.opportunities.models import Opportunity

logger = get_logger(__name__)

router = APIRouter(tags=["opportunity-feedback"])

EDITORS = require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def _require_feedback_feature(db: Session, ctx: TenantContext) -> None:
    """Gate every feedback operation through the deny-biased capability resolver.

    Phase 4B-A: the effective state of ``OPPORTUNITY_FEEDBACK`` for this workspace is
    decided by the central resolver (safety ceiling → honored per-workspace override →
    global configuration → secure default), using the authenticated tenant context's
    **server-resolved** organization/workspace identity — never a client-supplied scope.
    This makes the feedback route a sanctioned resolver consumer; it remains the single
    source of the capability decision (no raw-flag shortcut).

    Fail-closed (D2): only an explicit ``effective_enabled`` resolution opens the gate.
    Every other outcome rejects with the standard 503 ``capability_unavailable`` before
    any feedback row is written. A resolver / database / override-storage failure is
    never swallowed into an enable — it is logged distinctly from an intentional denial
    and re-raised (a 5xx) before any write, so the feature can never fail open.

    Under the shipped dark configuration (global flag ``False``, no override) the
    resolver returns disabled via ``GLOBAL_CONFIGURATION`` for every workspace, so the
    behavior is identical to the previous raw-flag gate. Wiring the gate flips no flag
    and creates no override.
    """
    try:
        resolution = resolve_capability(
            session=db,
            settings=get_settings(),
            capability=Capability.OPPORTUNITY_FEEDBACK,
            organization_id=ctx.organization.id,
            workspace_id=ctx.workspace.id,
        )
    except Exception:
        # Fail-closed: record the dependency failure distinctly from a denial, then let
        # it propagate before any write. Never continue as allowed.
        log_event(
            logger,
            "opportunity_feedback_gate_failed",
            level=logging.ERROR,
            outcome="error",
            capability=Capability.OPPORTUNITY_FEEDBACK.value,
            organization_id=ctx.organization.id,
            workspace_id=ctx.workspace.id,
        )
        raise

    # Structured, secret-free decision log: the capability, scoped ids, the boolean
    # result, and the deciding precedence rule — enough to detect an unexpected enable
    # for any workspace. Carries no override reason note or feedback content.
    log_event(
        logger,
        "opportunity_feedback_gate_decided",
        outcome="allowed" if resolution.effective_enabled else "denied",
        capability=Capability.OPPORTUNITY_FEEDBACK.value,
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        effective_enabled=resolution.effective_enabled,
        decided_by=resolution.decided_by.value,
        global_flag=resolution.global_flag,
        has_override=resolution.has_override,
    )
    if not resolution.effective_enabled:
        raise CapabilityUnavailableError("Opportunity feedback is not available yet.")


def _get_scoped_opportunity(
    db: Session, workspace_id: str, opportunity_id: str
) -> Opportunity:
    """Load the opportunity within this workspace, or 404 (hidden IDOR)."""
    opp = db.get(Opportunity, opportunity_id)
    if opp is None or opp.workspace_id != workspace_id:
        raise NotFoundError("Opportunity not found in this workspace.")
    return opp


def _get_scoped_record(
    db: Session, workspace_id: str, record_id: str
) -> SignalIntelligenceRecord:
    """Load the intelligence record within this workspace, or 404.

    A missing id and a cross-workspace id both return 404 so a caller can never probe
    which record ids exist in other tenants. Whether the record belongs to *this*
    opportunity is enforced downstream by the service (422 on mismatch).
    """
    record = db.get(SignalIntelligenceRecord, record_id)
    if record is None or record.workspace_id != workspace_id:
        raise NotFoundError("Intelligence record not found in this workspace.")
    return record


@router.post(
    "/workspaces/{workspace_id}/opportunities/{opportunity_id}/feedback",
    response_model=FeedbackOut,
    status_code=201,
)
def submit_opportunity_feedback(
    workspace_id: str,
    opportunity_id: str,
    body: FeedbackCreate,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(EDITORS),
) -> FeedbackOut:
    """Append one immutable feedback judgement for an opportunity (editor + gated).

    The record must live in this workspace and belong to this opportunity; the service
    copies version provenance from that record, polarity-checks the optional reason and
    inserts a new append-only row. Returns the stored judgement's customer-safe view.
    """
    _require_feedback_feature(db, ctx)
    opportunity = _get_scoped_opportunity(db, workspace_id, opportunity_id)
    record = _get_scoped_record(db, workspace_id, body.intelligence_record_id)
    feedback = create_feedback(
        db,
        opportunity=opportunity,
        intelligence_record=record,
        is_useful=body.is_useful,
        reason=body.reason_code,
        submitted_by_user_id=ctx.user.id,
    )
    return FeedbackOut.model_validate(feedback)


@router.get(
    "/workspaces/{workspace_id}/opportunities/{opportunity_id}/feedback",
    response_model=FeedbackHistoryOut,
)
def list_opportunity_feedback(
    workspace_id: str,
    opportunity_id: str,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(EDITORS),
) -> FeedbackHistoryOut:
    """Read the opportunity's append-only feedback history (editor + gated).

    Reverse-chronological, bounded page. Read-only: it never enqueues, scores or
    mutates anything. Scoped to this opportunity within the path workspace (404 for an
    unknown/cross-workspace opportunity).
    """
    _require_feedback_feature(db, ctx)
    opportunity = _get_scoped_opportunity(db, workspace_id, opportunity_id)

    base = select(OpportunityFeedback).where(
        OpportunityFeedback.workspace_id == workspace_id,
        OpportunityFeedback.opportunity_id == opportunity.id,
    )
    total = int(
        db.scalar(select(func.count()).select_from(base.subquery())) or 0
    )
    rows = list(
        db.execute(
            base.order_by(
                OpportunityFeedback.created_at.desc(),
                OpportunityFeedback.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    return FeedbackHistoryOut(
        items=[FeedbackOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
