"""Scout request endpoints.

Create / configure / pause / resume / run / review scouting requests. Every request is
isolated by workspace + brand + location + campaign, so results never leak across markets.
Running a request enqueues ``run_scout_request`` on the queue adapter (in-process by
default; Redis worker in full mode).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_audit
from app.auth.dependencies import TenantContext, get_tenant_context, require_role
from app.brands.service import get_primary_brand
from app.core.enums import Role, ScoutRequestStatus
from app.core.errors import NotFoundError, ValidationDomainError
from app.core.logging import trace_id_ctx
from app.db.session import get_db
from app.infra.queue import queue
from app.jobs.context import ExecutionContext
from app.jobs.contracts import wrap
from app.locations.models import BusinessLocation
from app.scouting_requests.models import ScoutRequest
from app.scouting_requests.schemas import (
    ScoutRequestCreate,
    ScoutRequestOut,
    ScoutRequestUpdate,
    ScoutRunResult,
)

router = APIRouter(tags=["scout-requests"])

EDITORS = require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)


def _get_scoped(db: Session, workspace_id: str, request_id: str) -> ScoutRequest:
    request = db.get(ScoutRequest, request_id)
    if not request or request.workspace_id != workspace_id:
        raise NotFoundError("Scout request not found in this workspace.")
    return request


@router.get("/workspaces/{workspace_id}/scout-requests", response_model=list[ScoutRequestOut])
def list_scout_requests(
    workspace_id: str,
    location_id: str | None = None,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> list[ScoutRequest]:
    stmt = select(ScoutRequest).where(ScoutRequest.workspace_id == workspace_id)
    if location_id:
        stmt = stmt.where(ScoutRequest.location_id == location_id)
    stmt = stmt.order_by(ScoutRequest.created_at.desc())
    return list(db.execute(stmt).scalars())


@router.post(
    "/workspaces/{workspace_id}/scout-requests",
    response_model=ScoutRequestOut,
    status_code=201,
)
def create_scout_request(
    workspace_id: str,
    body: ScoutRequestCreate,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(EDITORS),
) -> ScoutRequest:
    brand = get_primary_brand(db, workspace_id)
    if not brand:
        raise ValidationDomainError("Complete onboarding before creating scout requests.")

    resolved_market = body.resolved_market
    if body.location_id:
        location = db.get(BusinessLocation, body.location_id)
        if not location or location.workspace_id != workspace_id:
            raise NotFoundError("Location not found in this workspace.")
        if not resolved_market:
            resolved_market = f"{location.city}, {location.state_province or location.country}"

    request = ScoutRequest(
        organization_id=ctx.organization.id,
        workspace_id=workspace_id,
        brand_id=brand.id,
        location_id=body.location_id,
        campaign_id=body.campaign_id,
        name=body.name,
        status=ScoutRequestStatus.DRAFT.value,
        source_types=body.source_types,
        keywords=body.keywords,
        product_profile_id=body.product_profile_id,
        resolved_market=resolved_market,
        notes=body.notes,
        stats={},
    )
    db.add(request)
    db.flush()
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=workspace_id,
        actor_user_id=ctx.user.id,
        action="scout_request.created",
        entity_type="scout_request",
        entity_id=request.id,
    )
    return request


@router.get(
    "/workspaces/{workspace_id}/scout-requests/{request_id}",
    response_model=ScoutRequestOut,
)
def get_scout_request(
    workspace_id: str,
    request_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> ScoutRequest:
    return _get_scoped(db, workspace_id, request_id)


@router.put(
    "/workspaces/{workspace_id}/scout-requests/{request_id}",
    response_model=ScoutRequestOut,
)
def update_scout_request(
    workspace_id: str,
    request_id: str,
    body: ScoutRequestUpdate,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(EDITORS),
) -> ScoutRequest:
    request = _get_scoped(db, workspace_id, request_id)
    if request.status == ScoutRequestStatus.RUNNING.value:
        raise ValidationDomainError("Cannot edit a request while it is running.")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(request, key, value)
    db.add(request)
    db.flush()
    return request


@router.post(
    "/workspaces/{workspace_id}/scout-requests/{request_id}/pause",
    response_model=ScoutRequestOut,
)
def pause_scout_request(
    workspace_id: str,
    request_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(EDITORS),
) -> ScoutRequest:
    request = _get_scoped(db, workspace_id, request_id)
    request.status = ScoutRequestStatus.PAUSED.value
    db.add(request)
    db.flush()
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=workspace_id,
        actor_user_id=ctx.user.id,
        action="scout_request.paused",
        entity_type="scout_request",
        entity_id=request.id,
    )
    return request


@router.post(
    "/workspaces/{workspace_id}/scout-requests/{request_id}/resume",
    response_model=ScoutRequestOut,
)
def resume_scout_request(
    workspace_id: str,
    request_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(EDITORS),
) -> ScoutRequest:
    request = _get_scoped(db, workspace_id, request_id)
    if request.status != ScoutRequestStatus.PAUSED.value:
        raise ValidationDomainError("Only paused requests can be resumed.")
    request.status = ScoutRequestStatus.DRAFT.value
    db.add(request)
    db.flush()
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=workspace_id,
        actor_user_id=ctx.user.id,
        action="scout_request.resumed",
        entity_type="scout_request",
        entity_id=request.id,
    )
    return request


@router.post(
    "/workspaces/{workspace_id}/scout-requests/{request_id}/run",
    response_model=ScoutRunResult,
)
def run_scout_request_endpoint(
    workspace_id: str,
    request_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(EDITORS),
) -> ScoutRunResult:
    request = _get_scoped(db, workspace_id, request_id)
    if request.status == ScoutRequestStatus.RUNNING.value:
        raise ValidationDomainError("Request is already running.")
    if request.status == ScoutRequestStatus.PAUSED.value:
        raise ValidationDomainError("Resume the request before running it.")

    request.status = ScoutRequestStatus.QUEUED.value
    db.add(request)
    db.flush()
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=workspace_id,
        actor_user_id=ctx.user.id,
        action="scout_request.run",
        entity_type="scout_request",
        entity_id=request.id,
    )
    # Commit so the in-process job (opening its own session) sees the queued request.
    db.commit()

    # Carry the tenant/location scope with the job as a versioned envelope so isolation
    # travels with the work (and a durable queue can validate the contract version).
    envelope = wrap(
        "run_scout_request",
        ExecutionContext.for_scout_request(
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
            location_id=request.location_id,
            campaign_id=request.campaign_id,
            request_id=request.id,
            trace_id=trace_id_ctx.get(),
        ),
        {"scout_request_id": request.id},
    )
    queue.enqueue("run_scout_request", envelope.to_message())

    # In-process backend runs synchronously and commits; refresh to read final state.
    db.refresh(request)
    return ScoutRunResult(
        scout_request_id=request.id, status=request.status, stats=request.stats or {}
    )
