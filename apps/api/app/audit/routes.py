from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.models import AuditLog
from app.auth.dependencies import TenantContext, require_role
from app.core.enums import Role
from app.db.session import get_db

router = APIRouter(tags=["audit"])


@router.get("/workspaces/{workspace_id}/audit-logs")
def list_audit_logs(
    workspace_id: str,
    limit: int = Query(default=100, le=500),
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(require_role(Role.OWNER, Role.ADMIN, Role.COMPLIANCE_REVIEWER)),
) -> list[dict]:
    rows = db.execute(
        select(AuditLog)
        .where(
            AuditLog.organization_id == ctx.organization.id,
            (AuditLog.workspace_id == workspace_id) | (AuditLog.workspace_id.is_(None)),
        )
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    ).scalars()
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
