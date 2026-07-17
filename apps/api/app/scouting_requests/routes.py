"""Scout request endpoints.

Create / configure / pause / resume / run / review scouting requests. Every request is
isolated by workspace + brand + location + campaign, so results never leak across markets.
Running a request enqueues ``run_scout_request`` on the queue adapter (in-process by
default; Redis worker in full mode).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.audit.service import record_audit
from app.auth.dependencies import TenantContext, get_tenant_context, require_role
from app.brands.service import get_primary_brand
from app.core.enums import Role, ScoutRequestStatus
from app.core.errors import NotFoundError, ValidationDomainError
from app.core.logging import trace_id_ctx
from app.db.session import get_db
from app.jobs.service import enqueue_scout_request
from app.locations.models import BusinessLocation
from app.scouting_requests.models import ScoutRequest
from app.scouting_requests.run_history import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    get_run_history,
)
from app.scouting_requests.schemas import (
    RunHistoryOut,
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
    """Enqueue a durable scout-execute job and return immediately (non-blocking).

    The pipeline no longer runs inside the request: a worker claims and executes
    the queued job. The caller polls the request/job status for progress. A run
    is accepted only from a settled state (draft / completed / failed), and the
    ``draft/... -> queued`` flip is an atomic compare-and-set so two concurrent
    submissions cannot enqueue duplicate work for the same request.
    """
    request = _get_scoped(db, workspace_id, request_id)
    observed = request.status
    if observed in (ScoutRequestStatus.RUNNING.value, ScoutRequestStatus.QUEUED.value):
        raise ValidationDomainError("Request is already queued or running.")
    if observed == ScoutRequestStatus.PAUSED.value:
        raise ValidationDomainError("Resume the request before running it.")

    # Atomic claim of the run: only the caller that flips the observed status to
    # QUEUED proceeds to enqueue; a lost race is reported rather than duplicated.
    claimed = db.execute(
        update(ScoutRequest)
        .where(ScoutRequest.id == request.id, ScoutRequest.status == observed)
        .values(status=ScoutRequestStatus.QUEUED.value)
    )
    if claimed.rowcount != 1:
        raise ValidationDomainError("Request state changed; refresh and try again.")

    job = enqueue_scout_request(
        db,
        organization_id=request.organization_id,
        workspace_id=request.workspace_id,
        scout_request_id=request.id,
        location_id=request.location_id,
        campaign_id=request.campaign_id,
        request_id=request.id,
        trace_id=trace_id_ctx.get(),
    )
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=workspace_id,
        actor_user_id=ctx.user.id,
        action="scout_request.run",
        entity_type="scout_request",
        entity_id=request.id,
    )
    db.commit()

    return ScoutRunResult(
        scout_request_id=request.id,
        status=ScoutRequestStatus.QUEUED.value,
        stats={"job_id": job.id, "job_status": job.status},
    )


@router.get(
    "/workspaces/{workspace_id}/scout-requests/{request_id}/runs",
    response_model=RunHistoryOut,
)
def list_scout_request_runs(
    workspace_id: str,
    request_id: str,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> RunHistoryOut:
    """Read-only, reverse-chronological history of this request's scouting runs.

    Any workspace member may read it (``get_tenant_context``). The request is first
    authorized within the workspace (``_get_scoped`` → 404 for an unknown/cross-
    workspace id), then the durable jobs for that request are projected into a
    bounded, customer-safe page. This endpoint only reads: it never enqueues,
    cancels, retries or otherwise touches job-execution behaviour.
    """
    _get_scoped(db, workspace_id, request_id)
    return get_run_history(
        db,
        organization_id=ctx.organization.id,
        workspace_id=workspace_id,
        scout_request_id=request_id,
        limit=limit,
        offset=offset,
    )
