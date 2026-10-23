from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.models import AuditLog
from app.auth.dependencies import TenantContext, require_exact_roles
from app.core.enums import Role
from app.core.pagination import MAX_PAGE_SIZE, PAGINATION_RESPONSES, paginate
from app.db.session import get_db

router = APIRouter(tags=["audit"])


@router.get("/workspaces/{workspace_id}/audit-logs", responses=PAGINATION_RESPONSES)
def list_audit_logs(
    workspace_id: str,
    response: Response,
    # ``ge=1`` (P6-PLAT-12): a zero or negative limit used to reach the database, where
    # SQLite reads a negative LIMIT as "no limit".
    limit: int = Query(default=100, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None, max_length=1024),
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(
        require_exact_roles(Role.OWNER, Role.ADMIN, Role.COMPLIANCE_REVIEWER)
    ),
) -> list[dict]:
    page = paginate(
        db,
        select(AuditLog).where(
            AuditLog.organization_id == ctx.organization.id,
            (AuditLog.workspace_id == workspace_id) | (AuditLog.workspace_id.is_(None)),
        ),
        order=(AuditLog.created_at, AuditLog.id),
        descending=True,
        scope="audit-logs",
        workspace_id=workspace_id,
        filters={},
        cursor=cursor,
        limit=limit,
    )
    page.apply_headers(response)
    rows = page.rows
    return [
        {
            "id": r.id,
            "action": r.action,
            "actor_user_id": r.actor_user_id,
            "entity_type": r.entity_type,
            "entity_id": r.entity_id,
            "reason": r.reason,
            "created_at": r.created_at,
            "context": r.context,
        }
        for r in rows
    ]
