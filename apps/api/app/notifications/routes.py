"""In-app notification API (Project Phase 5E, lane 5E-W3; interface sheet P4 and §A).

Four endpoints, all gated by the O-12(B) capability and all acting for the session user
only:

    GET  /api/v1/workspaces/{workspace_id}/notifications                          the feed
    POST /api/v1/workspaces/{workspace_id}/notifications/{notification_id}/read  mark one read
    GET  /api/v1/workspaces/{workspace_id}/notification-preferences              per event code
    PUT  /api/v1/workspaces/{workspace_id}/notification-preferences              set some codes

and one workspace-scoped reflection, deliberately *not* gated (it reports the gate's
decision; the brief-, creative- and feedback-capability precedent):

    GET  /api/v1/workspaces/{workspace_id}/notifications-analytics-capability

* **Order of checks.** Authentication (401), then the workspace and the caller's membership of
  its organization (``get_tenant_context``: unknown workspace 404, non-member **403**, which
  stays 403 and is never turned into a 404, 5e:59-64), then the capability. The capability gate
  is one router-level dependency on the four endpoints
  (:func:`_require_notifications_analytics_feature`). It answers 503
  ``capability_unavailable`` unless ``notifications_analytics`` resolves enabled for the
  workspace through the deny-biased resolver (operator ruling O-12(B); the shipped state is the
  flag ``False`` with no override, so all four are dark). It runs before any notification is
  looked up and before the request body is validated, so a dark workspace discloses nothing.
  A resolver failure is logged and re-raised (a 5xx), never turned into a decision.
* **The user is always the session's.** No path, query or body field names a user. The feed,
  mark-read and the preferences read and write only rows whose ``user_id`` is the caller's.
* **Mark-read** matches on the notification id, the workspace and the session user. Unknown,
  another workspace's, another user's or a deleted notification are the same 404 with the
  same message. It is idempotent: the first ``read_at`` is kept.
* **Every member may use them** (any role, viewer included): each user's notifications are
  their own. The reflection is member-gated the same way.

Transaction ownership: the service flushes; the two writing routes declare ``WriteDb``
(:mod:`app.db.request_tx`), which commits before the response is sent.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.dependencies import TenantContext, get_tenant_context
from app.capabilities.registry import Capability
from app.capabilities.resolver import CapabilityResolution
from app.core.config import get_settings
from app.core.enums import NotificationEventCode
from app.core.errors import CapabilityUnavailableError
from app.core.logging import get_logger, log_event
from app.db.request_tx import WriteDb
from app.db.session import get_db
from app.notifications.schemas import (
    NotificationFeedOut,
    NotificationOut,
    NotificationPreferenceItem,
    NotificationPreferencesOut,
    NotificationPreferencesUpdate,
    NotificationsAnalyticsCapabilityOut,
)
from app.notifications.service import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    get_preferences,
    list_feed,
    mark_read,
    notifications_analytics_capability,
    update_preferences,
)

logger = get_logger(__name__)


def _resolve_notifications_analytics_capability(
    db: Session, ctx: TenantContext
) -> CapabilityResolution:
    """The routes' one call into the resolver, for the gate and the reflection alike.

    A dependency failure is re-raised, never converted into a decision: a coerced ``False``
    would make an outage look like a governance denial.
    """
    try:
        return notifications_analytics_capability(
            db, get_settings(), ctx.organization.id, ctx.workspace.id
        )
    except Exception:
        log_event(
            logger,
            "notifications_analytics_gate_failed",
            level=logging.ERROR,
            outcome="error",
            capability=Capability.NOTIFICATIONS_ANALYTICS.value,
            organization_id=ctx.organization.id,
            workspace_id=ctx.workspace.id,
        )
        raise


def _require_notifications_analytics_feature(
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> None:
    """The O-12(B) gate: only an explicit ``effective_enabled`` resolution opens it (503 else).

    A router dependency, so every notification endpoint carries it. The tenant context is
    resolved first (404 unknown workspace, 403 non-member), as on the existing dark routes.
    """
    resolution = _resolve_notifications_analytics_capability(db, ctx)
    log_event(
        logger,
        "notifications_analytics_gate_decided",
        outcome="allowed" if resolution.effective_enabled else "denied",
        capability=Capability.NOTIFICATIONS_ANALYTICS.value,
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        effective_enabled=resolution.effective_enabled,
        decided_by=resolution.decided_by.value,
        global_flag=resolution.global_flag,
        has_override=resolution.has_override,
    )
    if not resolution.effective_enabled:
        raise CapabilityUnavailableError("Notifications are not available yet.")


#: The router :mod:`app.api.router` mounts: the reflection, plus the gated routes below.
router = APIRouter(tags=["notifications"])

#: Every gated endpoint lives on this router, whose one dependency is the O-12(B) gate.
_gated = APIRouter(dependencies=[Depends(_require_notifications_analytics_feature)])


@router.get(
    "/workspaces/{workspace_id}/notifications-analytics-capability",
    response_model=NotificationsAnalyticsCapabilityOut,
    summary="Whether notifications and analytics are available for this workspace",
)
def read_notifications_analytics_capability(
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> NotificationsAnalyticsCapabilityOut:
    """Customer-facing, workspace-effective availability of notifications and analytics.

    Any member may read it. Advisory only: every notification and analytics endpoint
    re-decides and answers 503 while it is off. It answers 200 while dark, by design.
    """
    resolution = _resolve_notifications_analytics_capability(db, ctx)
    # Distinct from `notifications_analytics_gate_decided`, which exists to surface an
    # unexpected enable; reflection traffic must not dilute that signal.
    log_event(
        logger,
        "notifications_analytics_capability_reflected",
        outcome="allowed" if resolution.effective_enabled else "denied",
        capability=Capability.NOTIFICATIONS_ANALYTICS.value,
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        effective_enabled=resolution.effective_enabled,
    )
    return NotificationsAnalyticsCapabilityOut(enabled=resolution.effective_enabled is True)


@_gated.get(
    "/workspaces/{workspace_id}/notifications",
    response_model=NotificationFeedOut,
    summary="The session user's notifications in this workspace, newest first",
)
def list_notifications(
    workspace_id: str,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> NotificationFeedOut:
    """A bounded page of the caller's own feed, with its total and unread counts."""
    rows, total, unread = list_feed(
        db, workspace_id=ctx.workspace.id, user_id=ctx.user.id, limit=limit, offset=offset
    )
    return NotificationFeedOut(
        items=[NotificationOut.model_validate(row) for row in rows],
        total=total,
        unread=unread,
        limit=limit,
        offset=offset,
    )


@_gated.post(
    "/workspaces/{workspace_id}/notifications/{notification_id}/read",
    response_model=NotificationOut,
    summary="Mark one of the session user's notifications read (idempotent)",
)
def mark_notification_read(
    workspace_id: str,
    notification_id: str,
    db: Session = WriteDb,
    ctx: TenantContext = Depends(get_tenant_context),
) -> NotificationOut:
    """Mark the caller's own notification read; the first ``read_at`` is kept.

    Any miss (unknown id, another workspace's, another user's, deleted) is the same 404.
    """
    row = mark_read(
        db, workspace_id=ctx.workspace.id, user_id=ctx.user.id, notification_id=notification_id
    )
    return NotificationOut.model_validate(row)


def _preferences_out(
    pairs: Iterable[tuple[NotificationEventCode, bool]],
) -> NotificationPreferencesOut:
    return NotificationPreferencesOut(
        items=[NotificationPreferenceItem(event_code=code, enabled=on) for code, on in pairs]
    )


@_gated.get(
    "/workspaces/{workspace_id}/notification-preferences",
    response_model=NotificationPreferencesOut,
    summary="The session user's notification preferences in this workspace",
)
def read_notification_preferences(
    workspace_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> NotificationPreferencesOut:
    """Every event code with whether it is delivered to the caller here (default: delivered)."""
    return _preferences_out(get_preferences(db, workspace_id=ctx.workspace.id, user_id=ctx.user.id))


@_gated.put(
    "/workspaces/{workspace_id}/notification-preferences",
    response_model=NotificationPreferencesOut,
    summary="Set the session user's notification preferences in this workspace",
)
def update_notification_preferences(
    workspace_id: str,
    body: NotificationPreferencesUpdate,
    db: Session = WriteDb,
    ctx: TenantContext = Depends(get_tenant_context),
) -> NotificationPreferencesOut:
    """Set the given event codes for the caller; the others are unchanged. Answers every code."""
    return _preferences_out(
        update_preferences(
            db,
            organization_id=ctx.organization.id,
            workspace_id=ctx.workspace.id,
            user_id=ctx.user.id,
            changes={item.event_code: item.enabled for item in body.items},
        )
    )


# Last, so every gated route above is copied in with the gate.
router.include_router(_gated)


__all__ = ["router"]
