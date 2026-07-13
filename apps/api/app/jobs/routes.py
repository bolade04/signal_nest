"""Customer-facing durable-job endpoints.

Read-only lifecycle views plus a cancel action, all workspace-scoped so one
tenant can never observe or cancel another's work. Responses use the
customer-safe :class:`~app.jobs.schemas.JobOut` shape — no worker ids, lease
values, raw payloads or diagnostics.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.dependencies import TenantContext, get_tenant_context, require_role
from app.core.enums import Role
from app.core.errors import NotFoundError
from app.db.session import get_db
from app.jobs.schemas import JobEventOut, JobListOut, JobOut
from app.jobs.store import job_store

router = APIRouter(tags=["jobs"])

EDITORS = require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)


@router.get("/workspaces/{workspace_id}/jobs", response_model=JobListOut)
def list_jobs(
    workspace_id: str,
    location_id: str | None = None,
    scout_request_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> JobListOut:
    rows, total = job_store.list_jobs(
        db,
        organization_id=ctx.organization.id,
        workspace_id=workspace_id,
        location_id=location_id,
        scout_request_id=scout_request_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return JobListOut(
        items=[JobOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/workspaces/{workspace_id}/jobs/{job_id}", response_model=JobOut)
def get_job(
    workspace_id: str,
    job_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> JobOut:
    job = job_store.get_job(db, workspace_id=workspace_id, job_id=job_id)
    if job is None:
        raise NotFoundError("Job not found in this workspace.")
    return JobOut.model_validate(job)


@router.get(
    "/workspaces/{workspace_id}/jobs/{job_id}/events",
    response_model=list[JobEventOut],
)
def list_job_events(
    workspace_id: str,
    job_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> list[JobEventOut]:
    job = job_store.get_job(db, workspace_id=workspace_id, job_id=job_id)
    if job is None:
        raise NotFoundError("Job not found in this workspace.")
    events = job_store.list_events(db, job_id=job_id)
    return [JobEventOut.model_validate(e) for e in events]


@router.post("/workspaces/{workspace_id}/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(
    workspace_id: str,
    job_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(EDITORS),
) -> JobOut:
    """Request cancellation.

    A not-yet-running job is cancelled immediately; a running job is marked for
    cooperative cancellation and stopped by the worker at its next safe point.
    """
    job = job_store.get_job(db, workspace_id=workspace_id, job_id=job_id)
    if job is None:
        raise NotFoundError("Job not found in this workspace.")
    job_store.request_cancel(db, job)
    db.commit()
    return JobOut.model_validate(job)
