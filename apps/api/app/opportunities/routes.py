"""Opportunity feed + detail endpoints.

The feed supports filtering (classification, decision, status, risk, location, market,
minimum score), full-text search over the title/why-it-matters, and sorting. Crucially,
every query is scoped to the workspace and can be narrowed by ``location_id`` so results
from one location never appear under another (per-location separation). The feed pages
with an opaque cursor (P6-PLAT-12, :mod:`app.core.pagination`): the body stays a list, and
``X-Total-Count`` / ``X-Next-Cursor`` report the total and the next page.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.audit.actions import opportunity_status_action
from app.audit.service import record_audit
from app.auth.dependencies import TenantContext, get_tenant_context, require_role
from app.core.enums import OpportunityStatus, Role
from app.core.errors import NotFoundError, ValidationDomainError
from app.core.pagination import MAX_PAGE_SIZE, PAGINATION_RESPONSES, paginate
from app.db.session import get_db
from app.intelligence.read_service import get_opportunity_intelligence
from app.intelligence.schemas import OpportunityIntelligenceResponse
from app.opportunities.identity import with_sightings
from app.opportunities.models import Opportunity, OpportunityScore, ValidationEvidence
from app.opportunities.schemas import (
    OpportunityCard,
    OpportunityDetail,
    OpportunityStatusUpdate,
    ScoreBreakdown,
    ValidationEvidenceOut,
)

router = APIRouter(tags=["opportunities"])

EDITORS = require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)

_SORTABLE = {
    "priority": Opportunity.priority_score,
    "opportunity": Opportunity.opportunity_score,
    "confidence": Opportunity.confidence_score,
    "relevance": Opportunity.relevance_score,
    "created": Opportunity.created_at,
}
#: The accepted ``sort`` values. An unknown value used to fall back to ``priority``
#: silently, which hid a client sending column names instead of these keys.
SortKey = Literal["priority", "opportunity", "confidence", "relevance", "created"]


@router.get(
    "/workspaces/{workspace_id}/opportunities",
    response_model=list[OpportunityCard],
    responses=PAGINATION_RESPONSES,
)
def list_opportunities(
    workspace_id: str,
    response: Response,
    location_id: str | None = None,
    campaign_id: str | None = None,
    scout_request_id: str | None = None,
    classification: str | None = None,
    decision: str | None = None,
    status: str | None = None,
    risk_level: str | None = None,
    market: str | None = None,
    min_score: int = Query(default=0, ge=0, le=100),
    search: str | None = None,
    sort: SortKey = Query(default="priority"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=100, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None, max_length=1024),
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> list[OpportunityCard]:
    stmt = select(Opportunity).where(Opportunity.workspace_id == workspace_id)

    if location_id:
        stmt = stmt.where(Opportunity.location_id == location_id)
    if campaign_id:
        stmt = stmt.where(Opportunity.campaign_id == campaign_id)
    if scout_request_id:
        stmt = stmt.where(Opportunity.scout_request_id == scout_request_id)
    if classification:
        stmt = stmt.where(Opportunity.classification == classification)
    if decision:
        stmt = stmt.where(Opportunity.decision == decision)
    if status:
        stmt = stmt.where(Opportunity.status == status)
    if risk_level:
        stmt = stmt.where(Opportunity.risk_level == risk_level)
    if market:
        stmt = stmt.where(Opportunity.resolved_market == market)
    if min_score:
        stmt = stmt.where(Opportunity.opportunity_score >= min_score)
    if search:
        like = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                Opportunity.title.ilike(like),
                Opportunity.why_it_matters.ilike(like),
            )
        )

    page = paginate(
        db,
        stmt,
        order=(_SORTABLE[sort], Opportunity.id),
        descending=order == "desc",
        scope="opportunities",
        workspace_id=workspace_id,
        filters={
            "location_id": location_id,
            "campaign_id": campaign_id,
            "scout_request_id": scout_request_id,
            "classification": classification,
            "decision": decision,
            "status": status,
            "risk_level": risk_level,
            "market": market,
            "min_score": min_score,
            "search": search,
            "sort": sort,
            "order": order,
        },
        cursor=cursor,
        limit=limit,
    )
    page.apply_headers(response)
    # W16 (PROPOSED): each card's sightings, one statement for the page.
    return with_sightings(db, workspace_id, list(page.rows), OpportunityCard)


def _get_scoped(db: Session, workspace_id: str, opportunity_id: str) -> Opportunity:
    opp = db.get(Opportunity, opportunity_id)
    if not opp or opp.workspace_id != workspace_id:
        raise NotFoundError("Opportunity not found in this workspace.")
    return opp


@router.get(
    "/workspaces/{workspace_id}/opportunities/{opportunity_id}",
    response_model=OpportunityDetail,
)
def get_opportunity(
    workspace_id: str,
    opportunity_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> OpportunityDetail:
    opp = _get_scoped(db, workspace_id, opportunity_id)

    scores = db.execute(
        select(OpportunityScore).where(OpportunityScore.opportunity_id == opp.id)
    ).scalars()
    evidence = db.execute(
        select(ValidationEvidence).where(ValidationEvidence.opportunity_id == opp.id)
    ).scalars()

    detail = with_sightings(db, workspace_id, [opp], OpportunityDetail)[0]
    detail.scores = [
        ScoreBreakdown(kind=s.kind, total=s.total, breakdown=s.breakdown) for s in scores
    ]
    detail.validation_evidence = [
        ValidationEvidenceOut.model_validate(e) for e in evidence
    ]
    return detail


@router.get(
    "/workspaces/{workspace_id}/opportunities/{opportunity_id}/intelligence",
    response_model=OpportunityIntelligenceResponse,
)
def get_opportunity_intelligence_route(
    workspace_id: str,
    opportunity_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> OpportunityIntelligenceResponse:
    """Return the deterministic latest eligible persisted intelligence for one opportunity.

    Read-only (Batch 4B). ``_get_scoped`` authorizes the opportunity within the
    workspace first (a foreign/missing opportunity yields the same ``404``), then the
    intelligence read-service returns the single latest eligible record — or ``None``
    (``intelligence: null``) when no first-class record exists. Legacy
    ``ingest_metadata["intelligence"]`` is never fabricated into a response.
    """
    opp = _get_scoped(db, workspace_id, opportunity_id)
    payload = get_opportunity_intelligence(
        db, workspace_id=workspace_id, opportunity_id=opp.id
    )
    return OpportunityIntelligenceResponse(opportunity_id=opp.id, intelligence=payload)


@router.put(
    "/workspaces/{workspace_id}/opportunities/{opportunity_id}/status",
    response_model=OpportunityCard,
)
def update_opportunity_status(
    workspace_id: str,
    opportunity_id: str,
    body: OpportunityStatusUpdate,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(EDITORS),
) -> OpportunityCard:
    opp = _get_scoped(db, workspace_id, opportunity_id)
    valid = {s.value for s in OpportunityStatus}
    if body.status not in valid:
        raise ValidationDomainError(f"Status must be one of: {', '.join(sorted(valid))}.")
    opp.status = body.status
    db.add(opp)
    db.flush()
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=workspace_id,
        actor_user_id=ctx.user.id,
        action=opportunity_status_action(body.status),
        entity_type="opportunity",
        entity_id=opp.id,
    )
    return with_sightings(db, workspace_id, [opp], OpportunityCard)[0]
