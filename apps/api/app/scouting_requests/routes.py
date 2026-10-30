"""Scout request endpoints.

Create / configure / pause / resume / run / review scouting requests. Every request is
isolated by workspace + brand + location + campaign, so results never leak across markets.
Running a request enqueues a durable ``scout_request.execute`` job in the database job
store; a worker claims and runs it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.audit.service import record_audit
from app.auth.dependencies import TenantContext, get_tenant_context, require_role
from app.brands.service import get_primary_brand
from app.capabilities.registry import Capability
from app.capabilities.resolver import CapabilityResolution
from app.core.config import get_settings
from app.core.enums import Role, ScoutRequestStatus
from app.core.errors import CapabilityUnavailableError, NotFoundError, ValidationDomainError
from app.core.logging import get_logger, log_event, trace_id_ctx
from app.core.pagination import MAX_PAGE_SIZE, PAGINATION_RESPONSES, paginate
from app.db.session import get_db
from app.jobs.service import enqueue_scout_request
from app.llm.metering import ensure_run_allowed
from app.locations.models import BusinessLocation
from app.locations.service import ensure_location_active
from app.scouting_requests import schedules as schedule_service
from app.scouting_requests.models import ScoutRequest, ScoutSchedule
from app.scouting_requests.run_history import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    get_run_history,
)
from app.scouting_requests.schemas import (
    RunHistoryOut,
    SchedulingCapabilityOut,
    ScoutRequestCreate,
    ScoutRequestOut,
    ScoutRequestUpdate,
    ScoutRunResult,
    ScoutScheduleCreate,
    ScoutScheduleOut,
)

logger = get_logger(__name__)

router = APIRouter(tags=["scout-requests"])

EDITORS = require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)


def _get_scoped(db: Session, workspace_id: str, request_id: str) -> ScoutRequest:
    request = db.get(ScoutRequest, request_id)
    if not request or request.workspace_id != workspace_id:
        raise NotFoundError("Scout request not found in this workspace.")
    return request


def _get_schedule(db: Session, request: ScoutRequest) -> ScoutSchedule:
    """Load the request's single schedule, or 404 when it has none."""
    schedule = db.scalar(
        select(ScoutSchedule).where(
            ScoutSchedule.workspace_id == request.workspace_id,
            ScoutSchedule.scout_request_id == request.id,
        )
    )
    if schedule is None:
        raise NotFoundError("This request has no schedule.")
    return schedule


def _resolve_scheduling(db: Session, ctx: TenantContext) -> CapabilityResolution:
    """Scheduling availability for the caller's workspace, from the server-resolved
    tenant identity — never a client-supplied scope.

    Both the mutation gate and the customer reflection route through here into
    :func:`app.scouting_requests.schedules.resolve_scheduling_capability`, the single
    call site the worker's tick gate shares, so the UI, the HTTP gate and the worker
    cannot disagree about one workspace.
    """
    return schedule_service.resolve_scheduling_capability(
        db, organization_id=ctx.organization.id, workspace_id=ctx.workspace.id
    )


def _require_scheduling_feature(db: Session, ctx: TenantContext) -> None:
    """Gate every schedule *mutation* through the deny-biased capability resolver.

    P6-CAP-1: availability is the resolver's per-workspace decision (safety ceiling →
    honored workspace override → global ``scout_scheduling_enabled`` → secure
    default), not the raw global flag. Under the shipped configuration (flag ``False``,
    no override) it resolves disabled for every workspace, so the dark default is
    unchanged. While disabled, reads still expose any existing records but mutating
    endpoints answer with an explicit, safe feature-disabled response (503
    ``capability_unavailable``) rather than silently creating inert state or implying
    the feature works. A resolver failure is re-raised (a 5xx), never an enable.
    """
    resolution = _resolve_scheduling(db, ctx)
    # Secret-free decision log, enough to detect an unexpected enable for any
    # workspace; carries no override reason note.
    log_event(
        logger,
        "scout_scheduling_gate_decided",
        outcome="allowed" if resolution.effective_enabled else "denied",
        capability=Capability.SCOUT_SCHEDULING.value,
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        effective_enabled=resolution.effective_enabled,
        decided_by=resolution.decided_by.value,
        global_flag=resolution.global_flag,
        has_override=resolution.has_override,
    )
    if not resolution.effective_enabled:
        raise CapabilityUnavailableError("Scout scheduling is not available yet.")


def _schedule_out(db: Session, schedule: ScoutSchedule) -> ScoutScheduleOut:
    """Project a schedule into its customer-safe view, deriving live state."""
    failure = schedule_service.last_tick_failure(db, schedule)
    return ScoutScheduleOut(
        id=schedule.id,
        scout_request_id=schedule.scout_request_id,
        location_id=schedule.location_id,
        interval=schedule.interval_enum(),
        state=schedule_service.derive_schedule_state(db, schedule),
        enabled=schedule.enabled,
        next_run_at=schedule.next_run_at,
        last_tick_at=schedule.last_tick_at,
        last_tick_failed_at=failure.failed_at if failure is not None else None,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )


@router.get(
    "/workspaces/{workspace_id}/scout-requests",
    response_model=list[ScoutRequestOut],
    responses=PAGINATION_RESPONSES,
)
def list_scout_requests(
    workspace_id: str,
    response: Response,
    location_id: str | None = None,
    # No limit by default, as before P6-PLAT-12; a client that pages sends one.
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None, max_length=1024),
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> list[ScoutRequest]:
    stmt = select(ScoutRequest).where(ScoutRequest.workspace_id == workspace_id)
    if location_id:
        stmt = stmt.where(ScoutRequest.location_id == location_id)
    page = paginate(
        db,
        stmt,
        order=(ScoutRequest.created_at, ScoutRequest.id),
        descending=True,
        scope="scout-requests",
        workspace_id=workspace_id,
        filters={"location_id": location_id},
        cursor=cursor,
        limit=limit,
    )
    page.apply_headers(response)
    return page.rows


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
        ensure_location_active(db, location.id)
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
    # D-25 cooperative pause (PROPOSED, ``app.jobs.admission``): ``paused`` and a new run
    # fence in ONE statement. Every run claimed under the old fence is refused at its next
    # admission, even after a resume. The response is the recorded pause (the request,
    # ``paused``); whether its active work has stopped is the job's state, not this
    # response. Idempotent: every call advances the fence and returns 200. No job row is
    # touched (Cancel stays separate). This waits only for short transactions that make
    # no external call (an admission, a claim, a run's terminal write).
    db.execute(
        update(ScoutRequest)
        .where(ScoutRequest.id == request.id, ScoutRequest.workspace_id == workspace_id)
        .values(
            status=ScoutRequestStatus.PAUSED.value,
            run_fence=ScoutRequest.run_fence + 1,
        )
        .execution_options(synchronize_session=False)
    )
    db.refresh(request)
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=workspace_id,
        actor_user_id=ctx.user.id,
        action="scout_request.paused",
        entity_type="scout_request",
        entity_id=request.id,
    )
    # Commit before answering (as the run route does): ``get_db`` commits only in its
    # teardown, which FastAPI runs after the response has been sent, so without this the
    # 200 could reach the client before the pause is durable -- and the pause's commit is
    # the cut-off after which nothing is admitted (D-25 candidate, PROPOSED).
    db.commit()
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
    ensure_location_active(db, request.location_id)
    # P6-LLM-4: a workspace whose monthly AI budget is spent is refused here (HTTP 429,
    # ``llm_budget_exhausted``) before any state changes or work is enqueued.
    ensure_run_allowed(db, get_settings(), request.workspace_id)

    # Atomic claim of the run: only the caller that flips the observed status to
    # QUEUED proceeds to enqueue; a lost race is reported rather than duplicated. The
    # run fence (D-25, PROPOSED) is read by the same statement, so the job carries the
    # fence of exactly the state it was queued from (no ABA across Pause -> Resume).
    run_fence = db.execute(
        update(ScoutRequest)
        .where(ScoutRequest.id == request.id, ScoutRequest.status == observed)
        .values(status=ScoutRequestStatus.QUEUED.value)
        .returning(ScoutRequest.run_fence)
    ).scalar_one_or_none()
    if run_fence is None:
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
        run_fence=run_fence,
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


# --------------------------------------------------------------------------- #
# Scouting schedule (SB-C) — customer-facing recurrence controls
#
# Reads are open to any workspace member; mutations require an editor role AND the
# scheduling capability resolving enabled for the workspace (P6-CAP-1). The service
# owns every product limit (one schedule per request, four active per workspace), the
# transactional cap enforcement and the activation/resume semantics — the routes only
# authorize, gate and project.
# --------------------------------------------------------------------------- #
@router.get(
    "/workspaces/{workspace_id}/scheduling-capability",
    response_model=SchedulingCapabilityOut,
    summary="Whether scout scheduling is available for this workspace",
)
def read_scheduling_capability(
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> SchedulingCapabilityOut:
    """Customer-facing, workspace-effective scheduling availability (P6-UI-006).

    The mirror of ``GET /workspaces/{workspace_id}/feedback-capability`` (P6-UI-005):
    one boolean, decided by the same resolver call the schedule mutation gate uses, so
    the UI offers schedule controls exactly when the server will accept them.

    Member-gated rather than editor-gated, matching the schedule *read*: a view-only
    member still sees a schedule, and the panel needs this answer to describe it
    truthfully (an ``active`` row is reported as unavailable while the capability is
    off). It discloses nothing beyond that boolean for the caller's own workspace.

    Advisory only, and deliberately not capability-gated (that would make it useless):
    the mutation routes re-decide independently and answer 503 when unavailable.
    """
    resolution = _resolve_scheduling(db, ctx)
    # Distinct from `scout_scheduling_gate_decided`, which exists to surface an
    # unexpected *enable*; read traffic must not dilute that signal.
    log_event(
        logger,
        "scout_scheduling_capability_reflected",
        outcome="allowed" if resolution.effective_enabled else "denied",
        capability=Capability.SCOUT_SCHEDULING.value,
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        effective_enabled=resolution.effective_enabled,
    )
    return SchedulingCapabilityOut(enabled=resolution.effective_enabled)


@router.get(
    "/workspaces/{workspace_id}/scout-requests/{request_id}/schedule",
    response_model=ScoutScheduleOut,
)
def get_scout_schedule(
    workspace_id: str,
    request_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> ScoutScheduleOut:
    """Read the request's schedule. Any workspace member may read it.

    Available even while the capability is dark, so the UI can surface an existing
    record, its derived ``state`` (including ``activation_required`` and ``stalled``)
    and ``last_tick_failed_at``. Returns 404 when the request has no schedule.
    """
    request = _get_scoped(db, workspace_id, request_id)
    return _schedule_out(db, _get_schedule(db, request))


@router.post(
    "/workspaces/{workspace_id}/scout-requests/{request_id}/schedule",
    response_model=ScoutScheduleOut,
    status_code=201,
)
def create_scout_schedule(
    workspace_id: str,
    request_id: str,
    body: ScoutScheduleCreate,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(EDITORS),
) -> ScoutScheduleOut:
    """Attach the single recurring schedule to a request (editor + feature-gated).

    The service enforces one schedule per request and the four-active-per-workspace
    cap under a workspace row lock, so a conflict surfaces as 409. An unsupported
    cadence is rejected as 422 at the request boundary.
    """
    _require_scheduling_feature(db, ctx)
    request = _get_scoped(db, workspace_id, request_id)
    ensure_location_active(db, request.location_id)
    schedule = schedule_service.create_schedule(
        db,
        request=request,
        interval=body.interval,
        actor_user_id=ctx.user.id,
    )
    return _schedule_out(db, schedule)


@router.post(
    "/workspaces/{workspace_id}/scout-requests/{request_id}/schedule/pause",
    response_model=ScoutScheduleOut,
)
def pause_scout_schedule(
    workspace_id: str,
    request_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(EDITORS),
) -> ScoutScheduleOut:
    """Pause the schedule (idempotent). No further runs are fanned out until resume."""
    _require_scheduling_feature(db, ctx)
    request = _get_scoped(db, workspace_id, request_id)
    schedule = schedule_service.pause_schedule(
        db, schedule=_get_schedule(db, request), actor_user_id=ctx.user.id
    )
    return _schedule_out(db, schedule)


@router.post(
    "/workspaces/{workspace_id}/scout-requests/{request_id}/schedule/resume",
    response_model=ScoutScheduleOut,
)
def resume_scout_schedule(
    workspace_id: str,
    request_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(EDITORS),
) -> ScoutScheduleOut:
    """Resume or activate the schedule, recomputing the next run from now.

    Also the explicit activation for a schedule left in ``activation_required`` and
    the restart for one left ``stalled`` by a failed tick. Re-enabling is subject to
    the four-active cap (409 when full); an already-running schedule is returned
    unchanged.
    """
    _require_scheduling_feature(db, ctx)
    request = _get_scoped(db, workspace_id, request_id)
    ensure_location_active(db, request.location_id)
    schedule = schedule_service.resume_schedule(
        db, schedule=_get_schedule(db, request), actor_user_id=ctx.user.id
    )
    return _schedule_out(db, schedule)


@router.delete(
    "/workspaces/{workspace_id}/scout-requests/{request_id}/schedule",
    status_code=204,
)
def delete_scout_schedule(
    workspace_id: str,
    request_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(EDITORS),
) -> Response:
    """Hard-delete the schedule. Any in-flight tick self-terminates on next fire."""
    _require_scheduling_feature(db, ctx)
    request = _get_scoped(db, workspace_id, request_id)
    schedule_service.delete_schedule(
        db, schedule=_get_schedule(db, request), actor_user_id=ctx.user.id
    )
    return Response(status_code=204)
