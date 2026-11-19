"""Feature-gated audited manual export API (Project Phase 5E, lane 5E-W1).

Two export endpoints on one draft version, both capability-gated (5E plan §2; interface
sheet P4):

    POST /api/v1/workspaces/{workspace_id}/drafts/{draft_id}/exports   exporter roles only
    GET  /api/v1/workspaces/{workspace_id}/drafts/{draft_id}/exports   the history's roles

and one workspace-scoped reflection, deliberately *not* capability-gated (it reports the
capability decision; the brief- and creative-capability precedent; interface sheet P7):

    GET  /api/v1/workspaces/{workspace_id}/export-capability          any member

* **Capability gate.** Both export endpoints answer 503 ``capability_unavailable`` unless
  ``content_export`` resolves *enabled for this workspace* through the deny-biased resolver
  (:func:`app.exports.service.require_export_feature`). The shipped state is the flag
  ``False`` with no override, so both are dark. The gate runs before any id lookup.
* **Authorization** (exact roles, O-1; item 32): exporting is OWNER, ADMIN or MARKETER
  (``require_exporter``); the history is those plus REVIEWER and COMPLIANCE_REVIEWER
  (``require_export_history_reader``). Any other role is 403, a non-member 403, no session 401.
  The service re-checks the role.
* **Scope / IDOR.** The draft is resolved within the path workspace and the caller's
  organization; an unknown, foreign or soft-deleted draft (or brief) answers the same 404.
* **Refusals** (409, nothing written): ``export_jurisdiction_missing``,
  ``jurisdiction_mismatch``, ``export_not_approved``, ``export_review_blocked``; the order and
  meaning are in :mod:`app.exports.service`.
* **Success** answers 201 with the export record and the rendered text inline
  (``content``). The export row, its audit row and the ``export.completed`` notification are
  written in one transaction, committed before the 201 is sent (``WriteDb``).

**The notification emitter is injected** (:func:`get_notification_emitter`), so the service
codes against the frozen :class:`~app.notifications.interface.NotificationEmitter` Protocol.
At I-5E it is bound to lane 5E-W3's emitter,
:func:`app.notifications.service.emit_notifications`, the one emitter of the codebase.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.dependencies import TenantContext, get_tenant_context
from app.capabilities.registry import Capability
from app.core.config import get_settings
from app.core.logging import get_logger, log_event
from app.db.request_tx import WriteDb
from app.db.session import get_db
from app.exports.policy import require_export_history_reader, require_exporter
from app.exports.schemas import (
    ExportCapabilityOut,
    ExportHistoryItemOut,
    ExportHistoryOut,
    ExportIn,
    ExportOut,
)
from app.exports.service import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    create_export,
    export_capability,
    list_exports,
)
from app.notifications.interface import NotificationEmitter
from app.notifications.service import emit_notifications

logger = get_logger(__name__)

router = APIRouter(tags=["exports"])

_DRAFT_EXPORTS = "/workspaces/{workspace_id}/drafts/{draft_id}/exports"


def get_notification_emitter() -> NotificationEmitter:
    """The emitter an export writes ``export.completed`` through (the injection seam).

    Bound at I-5E to lane 5E-W3's :func:`~app.notifications.service.emit_notifications`. It is
    called only after every precondition has passed, inside the export's transaction: it
    writes nothing while the O-12(B) capability is off for the workspace, flushes and never
    commits, and if it raises the export rolls back (a 500) and nothing is written, so an
    export never commits without its notification. Refusals (403, 503, 404, 409) are
    unaffected. Tests may override this dependency."""
    return emit_notifications


@router.get(
    "/workspaces/{workspace_id}/export-capability",
    response_model=ExportCapabilityOut,
    summary="Whether export is available for this workspace",
)
def read_export_capability(
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> ExportCapabilityOut:
    """Customer-facing, workspace-effective export availability (any member). Advisory
    only: both export endpoints re-decide and answer 503 while it is false. Answers 200 while
    export is dark."""
    resolution = export_capability(db, ctx, get_settings())
    enabled = resolution.effective_enabled is True
    log_event(
        logger,
        "content_export_capability_reflected",
        outcome="allowed" if enabled else "denied",
        capability=Capability.CONTENT_EXPORT.value,
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        effective_enabled=resolution.effective_enabled,
    )
    return ExportCapabilityOut(enabled=enabled)


@router.post(
    _DRAFT_EXPORTS,
    response_model=ExportOut,
    status_code=201,
    summary="Export one approved draft version (owner, admin or marketer)",
)
def export_draft(
    workspace_id: str,
    draft_id: str,
    body: ExportIn,
    db: Session = WriteDb,
    ctx: TenantContext = Depends(require_exporter),
    emitter: NotificationEmitter = Depends(get_notification_emitter),
) -> ExportOut:
    """Record one audited export of the draft's exact version and answer its text, rendered
    in ``format`` (``text/plain``, ``text/markdown`` or ``json``).

    Answers 503 ``capability_unavailable`` while export is not available for the workspace,
    404 for an unknown, foreign or deleted draft, and 409 when the draft cannot be exported:
    ``export_jurisdiction_missing`` (no valid jurisdiction recorded on the draft or its brief),
    ``jurisdiction_mismatch`` (the two differ), ``export_not_approved`` (the latest decision on
    this version is not an approval) or ``export_review_blocked`` (its content review, or the
    claims library as it stands now, does not allow it). A refusal records nothing."""
    result = create_export(
        db,
        ctx=ctx,
        draft_id=draft_id,
        export_format=body.format,
        emitter=emitter,
        settings=get_settings(),
    )
    record = ExportHistoryItemOut.model_validate(result.export)
    return ExportOut(**record.model_dump(), content=result.content)


@router.get(
    _DRAFT_EXPORTS,
    response_model=ExportHistoryOut,
    summary="A draft version's exports, newest first",
)
def list_draft_exports(
    workspace_id: str,
    draft_id: str,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(require_export_history_reader),
) -> ExportHistoryOut:
    """The draft version's export records, newest first, without their text. Read-only."""
    page = list_exports(
        db, ctx=ctx, draft_id=draft_id, limit=limit, offset=offset, settings=get_settings()
    )
    return ExportHistoryOut(
        items=[ExportHistoryItemOut.model_validate(row) for row in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


__all__ = ["get_notification_emitter", "router"]
