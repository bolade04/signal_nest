"""Feature-gated recommendation-brief API (Project Phase 5A).

Three endpoints, nested under an opportunity, all capability-gated:

    GET  /api/v1/workspaces/{workspace_id}/opportunities/{opportunity_id}/briefs
    POST /api/v1/workspaces/{workspace_id}/opportunities/{opportunity_id}/briefs
    GET  /api/v1/workspaces/{workspace_id}/opportunities/{opportunity_id}/briefs/{brief_id}

and one workspace-scoped reflection, deliberately *not* capability-gated (it reports the
capability decision; the feedback-capability precedent):

    GET  /api/v1/workspaces/{workspace_id}/brief-capability

The boundary mirrors the opportunity-feedback router (:mod:`app.feedback.routes`):

* **Capability gate.** Every endpoint answers 503 ``capability_unavailable`` unless
  ``recommendation_briefs`` resolves *enabled for this workspace* through the deny-biased
  resolver (an honored workspace override outranks the global flag in both directions; the
  shipped state is the flag ``False`` with no override, so every endpoint is dark). A
  resolver failure is logged and re-raised (a 5xx), never turned into a decision. The gate
  runs before any scope lookup, so a dark deployment discloses nothing about ids.
* **Authorization.** Capture (``POST``) is editor-only: owner / admin / marketer, the
  feedback precedent. Reads are open to every workspace member, like the opportunity they
  belong to, so the reviewer who approves a brief (Phase 5B, ``APPROVER_ROLES``) can read
  it. A non-member is 403; an unauthenticated caller 401.
* **Scope / IDOR.** The opportunity is resolved within the path workspace, and a brief
  within that opportunity and workspace: unknown or foreign ids answer the same 404 (hidden
  IDOR, non-enumerating). The body's intelligence record must live in the workspace (404,
  uniform with a missing id); the service then requires it to be this opportunity's (422)
  and the optional campaign to be the workspace's (404).
* **Immutable.** There is no update or delete endpoint; a revision is a new version
  (:func:`app.briefs.service.create_brief`).
* **Reflection.** ``brief-capability`` answers ``{"enabled": bool}`` from the same single
  resolver call site the gate uses, so the UI shows what the backend enforces, per
  workspace (an honored override included). Member-gated like the brief reads. Advisory
  only: every brief endpoint re-decides and answers 503 while disabled.

Transaction ownership follows the house rule: the request-scoped ``get_db`` commits. The one
exception is deliberate: when no brief is produced after a model call, the route commits the
meter's usage rows before answering, so spend is never rolled back with the error.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import TenantContext, get_tenant_context, require_role
from app.briefs.models import RecommendationBrief
from app.briefs.schemas import (
    RecommendationBriefCapabilityOut,
    RecommendationBriefCreate,
    RecommendationBriefListOut,
    RecommendationBriefOut,
)
from app.briefs.service import BriefNotProducedError, create_brief
from app.capabilities.registry import Capability
from app.capabilities.resolver import resolve_capability
from app.core.config import get_settings
from app.core.enums import Role
from app.core.errors import CapabilityUnavailableError, NotFoundError
from app.core.logging import get_logger, log_event
from app.db.session import get_db
from app.intelligence.records import SignalIntelligenceRecord
from app.llm.service import LLMService, get_llm_service
from app.opportunities.models import Opportunity

logger = get_logger(__name__)

router = APIRouter(tags=["recommendation-briefs"])

#: Capture: the workspace editors (the feedback router's set).
EDITORS = require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def _resolve_briefs_capability(db: Session, ctx: TenantContext):
    """The one place this package asks whether briefs are available for a workspace.

    A dependency failure is re-raised, never converted into a decision: a coerced ``False``
    would make an outage look like a governance denial.
    """
    try:
        return resolve_capability(
            session=db,
            settings=get_settings(),
            capability=Capability.RECOMMENDATION_BRIEFS,
            organization_id=ctx.organization.id,
            workspace_id=ctx.workspace.id,
        )
    except Exception:
        log_event(
            logger,
            "recommendation_briefs_gate_failed",
            level=logging.ERROR,
            outcome="error",
            capability=Capability.RECOMMENDATION_BRIEFS.value,
            organization_id=ctx.organization.id,
            workspace_id=ctx.workspace.id,
        )
        raise


def _require_briefs_feature(db: Session, ctx: TenantContext) -> None:
    """Fail-closed gate: only an explicit ``effective_enabled`` resolution opens it."""
    resolution = _resolve_briefs_capability(db, ctx)
    log_event(
        logger,
        "recommendation_briefs_gate_decided",
        outcome="allowed" if resolution.effective_enabled else "denied",
        capability=Capability.RECOMMENDATION_BRIEFS.value,
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        effective_enabled=resolution.effective_enabled,
        decided_by=resolution.decided_by.value,
        global_flag=resolution.global_flag,
        has_override=resolution.has_override,
    )
    if not resolution.effective_enabled:
        raise CapabilityUnavailableError("Recommendation briefs are not available yet.")


@router.get(
    "/workspaces/{workspace_id}/brief-capability",
    response_model=RecommendationBriefCapabilityOut,
    summary="Whether recommendation briefs are available for this workspace",
)
def read_brief_capability(
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> RecommendationBriefCapabilityOut:
    """Customer-facing, workspace-effective brief availability (Phase 5A).

    Workspace-scoped, not opportunity-scoped: the resolver decides per
    ``(capability, workspace)``. Any member may read it, matching the brief reads. Advisory
    only; the brief endpoints remain the enforcement boundary.
    """
    resolution = _resolve_briefs_capability(db, ctx)
    # Distinct from `recommendation_briefs_gate_decided`, which exists to surface an
    # unexpected enable; reflection traffic must not dilute that signal.
    log_event(
        logger,
        "recommendation_briefs_capability_reflected",
        outcome="allowed" if resolution.effective_enabled else "denied",
        capability=Capability.RECOMMENDATION_BRIEFS.value,
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        effective_enabled=resolution.effective_enabled,
    )
    return RecommendationBriefCapabilityOut(enabled=resolution.effective_enabled)


def _get_scoped_opportunity(db: Session, workspace_id: str, opportunity_id: str) -> Opportunity:
    """The opportunity within this workspace, or 404 (hidden IDOR)."""
    opp = db.get(Opportunity, opportunity_id)
    if opp is None or opp.workspace_id != workspace_id:
        raise NotFoundError("Opportunity not found in this workspace.")
    return opp


def _get_scoped_record(db: Session, workspace_id: str, record_id: str) -> SignalIntelligenceRecord:
    """The intelligence record within this workspace, or 404 (uniform with a missing id)."""
    record = db.get(SignalIntelligenceRecord, record_id)
    if record is None or record.workspace_id != workspace_id:
        raise NotFoundError("Intelligence record not found in this workspace.")
    return record


@router.get(
    "/workspaces/{workspace_id}/opportunities/{opportunity_id}/briefs",
    response_model=RecommendationBriefListOut,
    summary="List an opportunity's recommendation briefs, newest version first",
)
def list_opportunity_briefs(
    workspace_id: str,
    opportunity_id: str,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> RecommendationBriefListOut:
    """A bounded page of the opportunity's immutable brief versions (any member; gated)."""
    _require_briefs_feature(db, ctx)
    opportunity = _get_scoped_opportunity(db, workspace_id, opportunity_id)
    base = select(RecommendationBrief).where(
        RecommendationBrief.workspace_id == workspace_id,
        RecommendationBrief.opportunity_id == opportunity.id,
    )
    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = db.scalars(
        base.order_by(RecommendationBrief.brief_version.desc()).limit(limit).offset(offset)
    ).all()
    return RecommendationBriefListOut(
        items=[RecommendationBriefOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/workspaces/{workspace_id}/opportunities/{opportunity_id}/briefs",
    response_model=RecommendationBriefOut,
    status_code=201,
    summary="Generate and capture a new recommendation-brief version",
)
def create_opportunity_brief(
    workspace_id: str,
    opportunity_id: str,
    body: RecommendationBriefCreate,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(EDITORS),
    llm: LLMService = Depends(get_llm_service),
) -> RecommendationBriefOut:
    """Generate a brief from one of the opportunity's intelligence records (editor; gated).

    The record must live in this workspace (404) and belong to this opportunity (422); an
    optional campaign must belong to the workspace (404). Answers 429
    ``llm_budget_exhausted`` when the workspace's AI budget is spent, 503
    ``brief_generation_unavailable`` when the model call fails, 422 ``brief_withheld`` when
    the output fails the safety screen, and 409 when a concurrent capture took the version;
    none of these stores a brief.
    """
    _require_briefs_feature(db, ctx)
    opportunity = _get_scoped_opportunity(db, workspace_id, opportunity_id)
    record = _get_scoped_record(db, workspace_id, body.intelligence_record_id)
    try:
        brief = create_brief(
            db,
            opportunity=opportunity,
            intelligence_record=record,
            campaign_id=body.campaign_id,
            author=ctx.user,
            llm=llm,
            settings=get_settings(),
        )
    except BriefNotProducedError:
        # No brief, but any model attempt made is accounted for: keep its usage rows.
        db.commit()
        raise
    return RecommendationBriefOut.model_validate(brief)


@router.get(
    "/workspaces/{workspace_id}/opportunities/{opportunity_id}/briefs/{brief_id}",
    response_model=RecommendationBriefOut,
    summary="Read one recommendation-brief version",
)
def get_opportunity_brief(
    workspace_id: str,
    opportunity_id: str,
    brief_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> RecommendationBriefOut:
    """One immutable brief version of this opportunity (any member; gated)."""
    _require_briefs_feature(db, ctx)
    opportunity = _get_scoped_opportunity(db, workspace_id, opportunity_id)
    brief = db.get(RecommendationBrief, brief_id)
    if (
        brief is None
        or brief.workspace_id != workspace_id
        or brief.opportunity_id != opportunity.id
    ):
        raise NotFoundError("Brief not found for this opportunity.")
    return RecommendationBriefOut.model_validate(brief)
