"""Audit logging service for sensitive actions."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.audit.models import AuditLog


def record_audit(
    db: Session,
    *,
    organization_id: str,
    action: str,
    actor_user_id: str | None = None,
    workspace_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    reason: str | None = None,
    previous_state: dict | None = None,
    new_state: dict | None = None,
    context: dict | None = None,
) -> AuditLog:
    log = AuditLog(
        organization_id=organization_id,
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        reason=reason,
        previous_state=previous_state,
        new_state=new_state,
        context=context or {},
    )
    db.add(log)
    db.flush()
    return log
