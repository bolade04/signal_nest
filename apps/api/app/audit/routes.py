"""The typed audit read API (Project Phase 5B audit spine; W21 lane P5B).

    GET /api/v1/workspaces/{workspace_id}/audit-logs

* **Gate** (operator decision O-1, P6-AUTH-3): exactly OWNER, ADMIN or COMPLIANCE_REVIEWER
  (``require_exact_roles``); a caller with no role in the organization gets 403.
* **Scope**: the caller's own organization only, and within it the path workspace's rows
  plus the organization-level rows (``workspace_id IS NULL``: login, membership and
  invitation records).
* **Typed**: each row is an :class:`~app.audit.schemas.AuditLogOut` (frozen at CF-1): the
  ``action`` is the closed :class:`~app.audit.actions.AuditAction` vocabulary and
  ``previous_state`` / ``new_state`` are surfaced. ``organization_id`` and ``updated_at``
  are never returned.
* **Filters** (all optional, combined with AND): ``action`` (a registry member; anything
  else is 422), ``entity_type``, ``entity_id``, and the time range ``created_from``
  (inclusive) / ``created_before`` (exclusive). A timestamp without an offset is read as
  UTC. A filter naming another tenant's entity matches nothing, exactly as an id that does
  not exist matches nothing (an empty page with ``X-Total-Count: 0``), so a filter can
  never confirm that an id exists elsewhere.
* **Cursor pagination** (P6-PLAT-12): newest first; ``X-Total-Count`` counts the filtered
  rows; ``X-Next-Cursor`` resumes the same filtered listing. A cursor is bound to the
  route, the workspace and the filters, so replaying it with other filters is 422.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.actions import AuditAction
from app.audit.models import AuditLog
from app.audit.schemas import AuditLogOut
from app.auth.dependencies import TenantContext, require_exact_roles
from app.core.enums import Role
from app.core.errors import ValidationDomainError
from app.core.pagination import MAX_PAGE_SIZE, PAGINATION_RESPONSES, paginate
from app.db.session import get_db

router = APIRouter(tags=["audit"])

#: Bounds of the string filters: the widths of the columns they compare with.
ENTITY_TYPE_MAX = AuditLog.__table__.c.entity_type.type.length
ENTITY_ID_MAX = AuditLog.__table__.c.entity_id.type.length

EMPTY_RANGE = "created_from must be earlier than created_before."


def _utc(value: datetime | None) -> datetime | None:
    """An aware UTC instant; a naive value is read as UTC (the stored clock)."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@router.get(
    "/workspaces/{workspace_id}/audit-logs",
    response_model=list[AuditLogOut],
    responses=PAGINATION_RESPONSES,
)
def list_audit_logs(
    workspace_id: str,
    response: Response,
    # ``ge=1`` (P6-PLAT-12): a zero or negative limit used to reach the database, where
    # SQLite reads a negative LIMIT as "no limit".
    limit: int = Query(default=100, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None, max_length=1024),
    action: AuditAction | None = Query(default=None),
    entity_type: str | None = Query(default=None, min_length=1, max_length=ENTITY_TYPE_MAX),
    entity_id: str | None = Query(default=None, min_length=1, max_length=ENTITY_ID_MAX),
    created_from: datetime | None = Query(
        default=None, description="Inclusive lower bound on created_at (UTC if no offset)."
    ),
    created_before: datetime | None = Query(
        default=None, description="Exclusive upper bound on created_at (UTC if no offset)."
    ),
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(
        require_exact_roles(Role.OWNER, Role.ADMIN, Role.COMPLIANCE_REVIEWER)
    ),
) -> list[AuditLogOut]:
    since, until = _utc(created_from), _utc(created_before)
    if since is not None and until is not None and since >= until:
        raise ValidationDomainError(EMPTY_RANGE)

    stmt = select(AuditLog).where(
        AuditLog.organization_id == ctx.organization.id,
        (AuditLog.workspace_id == workspace_id) | (AuditLog.workspace_id.is_(None)),
    )
    if action is not None:
        stmt = stmt.where(AuditLog.action == action.value)
    if entity_type is not None:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if since is not None:
        stmt = stmt.where(AuditLog.created_at >= since)
    if until is not None:
        stmt = stmt.where(AuditLog.created_at < until)

    page = paginate(
        db,
        stmt,
        order=(AuditLog.created_at, AuditLog.id),
        descending=True,
        scope="audit-logs",
        workspace_id=workspace_id,
        # The cursor is bound to exactly these filters (P6-PLAT-12): a cursor issued for
        # one filtered listing is refused on another.
        filters={
            "action": None if action is None else action.value,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "created_from": None if since is None else since.isoformat(),
            "created_before": None if until is None else until.isoformat(),
        },
        cursor=cursor,
        limit=limit,
    )
    page.apply_headers(response)
    return [AuditLogOut.model_validate(row) for row in page.rows]
