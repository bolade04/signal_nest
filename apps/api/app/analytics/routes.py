"""Status-analytics API (Project Phase 5E, lane 5E-W2).

One read-only endpoint (interface sheet P4):

    GET /api/v1/workspaces/{workspace_id}/analytics            the whole workspace
    GET /api/v1/workspaces/{workspace_id}/analytics?location_id=…   one of its locations

The boundary, in the order the checks run:

1. **Authentication and membership** (``get_tenant_context``): 401 without a session, 404 for an
   unknown workspace, 403 for a non-member.
2. **Exact role** (``require_analytics_reader``, ``app.analytics.policy``; frozen at CF-3, the
   5E plan's gate): OWNER, ADMIN, MARKETER and REVIEWER. VIEWER and COMPLIANCE_REVIEWER are
   refused with 403 whatever the capability says: the aggregates include approval latency and
   generation and review outcomes, the sensitivity class the audit read is narrowed for (O-1).
3. **Capability gate (operator ruling O-12(B))**: "Choose O-12(B): one additional flag for the
   notifications and analytics surfaces, off by default. Explicitly amend O-4’s three-flag
   limit. Gate the backend routes, customer UI and applicable notification emission
   consistently." :func:`_require_notifications_analytics_feature` resolves
   ``notifications_analytics`` for this workspace through the deny-biased resolver; only an
   explicit ``effective_enabled`` opens it, and every other outcome answers the standard 503
   ``capability_unavailable`` before any figure is read. A resolver failure is logged and
   re-raised (a 5xx), never turned into a decision. No raw settings read. The shipped state is
   the flag ``False`` with no override, so the route is dark everywhere.
4. **Location scope**: a ``location_id`` that is not a location of this workspace answers the
   uniform 404 (``app.analytics.service.resolve_location``), checked after the gate, so a dark
   deployment discloses nothing about ids.

The workspace-effective reflection the UI reads
(``GET /workspaces/{workspace_id}/notifications-analytics-capability``) is the notifications
router's (lane 5E-W3): the capability is shared by both surfaces.

Read-only: no write, no audit row, nothing cached. The request-scoped session is never
committed by this route.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.analytics.policy import require_analytics_reader
from app.analytics.schemas import AnalyticsOut
from app.analytics.service import workspace_analytics
from app.auth.dependencies import TenantContext
from app.capabilities.registry import Capability
from app.capabilities.resolver import resolve_capability
from app.core.config import get_settings
from app.core.errors import CapabilityUnavailableError
from app.core.logging import get_logger, log_event
from app.db.session import get_db

logger = get_logger(__name__)

router = APIRouter(tags=["analytics"])

#: The dark answer's message (static; the 503 body's ``message``).
ANALYTICS_DARK = "Status analytics is not available yet."

#: Bound on the ``location_id`` query value (ids are 32 hex characters).
LOCATION_ID_MAX_LENGTH = 64


def _resolve_notifications_analytics_capability(db: Session, ctx: TenantContext):
    """The one place this router asks whether the O-12(B) capability is on for a workspace.

    A dependency failure is re-raised, never converted into a decision: a coerced ``False``
    would make an outage look like a governance denial.
    """
    try:
        return resolve_capability(
            session=db,
            settings=get_settings(),
            capability=Capability.NOTIFICATIONS_ANALYTICS,
            organization_id=ctx.organization.id,
            workspace_id=ctx.workspace.id,
        )
    except Exception:
        log_event(
            logger,
            "status_analytics_gate_failed",
            level=logging.ERROR,
            outcome="error",
            capability=Capability.NOTIFICATIONS_ANALYTICS.value,
            organization_id=ctx.organization.id,
            workspace_id=ctx.workspace.id,
        )
        raise


def _require_notifications_analytics_feature(db: Session, ctx: TenantContext) -> None:
    """The router's one gate (O-12(B)): fail closed unless ``effective_enabled``."""
    resolution = _resolve_notifications_analytics_capability(db, ctx)
    log_event(
        logger,
        "status_analytics_gate_decided",
        outcome="allowed" if resolution.effective_enabled else "denied",
        capability=Capability.NOTIFICATIONS_ANALYTICS.value,
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        effective_enabled=resolution.effective_enabled,
        decided_by=resolution.decided_by.value,
        global_flag=resolution.global_flag,
        has_override=resolution.has_override,
    )
    if resolution.effective_enabled is not True:
        raise CapabilityUnavailableError(ANALYTICS_DARK)


@router.get(
    "/workspaces/{workspace_id}/analytics",
    response_model=AnalyticsOut,
    summary="Status analytics for this workspace, or one of its locations",
)
def read_workspace_analytics(
    workspace_id: str,
    location_id: str | None = Query(default=None, min_length=1, max_length=LOCATION_ID_MAX_LENGTH),
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(require_analytics_reader),
) -> AnalyticsOut:
    """Counts by state for opportunities, briefs, approvals, drafts, reviews and exports;
    approval latency; generation and review outcomes; and a per-location comparison.

    Exact roles OWNER, ADMIN, MARKETER and REVIEWER (403 otherwise); 503 while the
    notifications-and-analytics capability is off for the workspace; 404 for a location that
    is not this workspace's. A figure without a basis is null, never 0. No attribution or
    advertising-performance figure.
    """
    _require_notifications_analytics_feature(db, ctx)
    return workspace_analytics(
        db,
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        location_id=location_id,
    )


__all__ = ["ANALYTICS_DARK", "router"]
