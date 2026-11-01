"""Approval decisions API (Project Phase 5B, W21 lane P5B).

Three customer endpoints, all dark behind ``recommendation_briefs_enabled`` /
``Capability.RECOMMENDATION_BRIEFS`` (the same switch as the 5A brief surfaces, Phase 5B
plan §2: approvals act only on briefs, so 5A+5B share one dark switch):

    POST /api/v1/workspaces/{ws}/opportunities/{opp}/briefs/{brief_id}/approvals
    GET  /api/v1/workspaces/{ws}/opportunities/{opp}/briefs/{brief_id}/approvals
    GET  /api/v1/workspaces/{ws}/approval-queue

Boundary, in the order a request meets it:

* **Authentication and membership** (the dependency): 401 without a live session; **403**
  for a caller with no role in the workspace's organization (the universal
  ``PermissionDeniedError`` convention, Phase 5B exit criterion 7(b)).
* **Role** (exact sets via ``require_exact_roles``; rank plays no part, O-1):
  recording a decision and reading the queue are for :data:`~app.approvals.policy.
  APPROVER_ROLES` only (``{REVIEWER}``, O-2); a brief's decision history is readable by
  :data:`APPROVAL_HISTORY_READERS`. Others get 403.
* **Capability** (fail-closed resolver, the ``feedback/routes.py`` pattern): 503
  ``capability_unavailable`` unless the capability resolves enabled for this workspace;
  a resolver failure is re-raised, never turned into an enable.
* **Entity scope**: a brief that does not exist, or exists in another workspace, another
  organization or under another opportunity, answers one uniform **404** (exit criterion
  7(a): "exists but not yours" is indistinguishable from "does not exist").

Approver authority is organization-wide: a reviewer grant made through
``PUT /organizations/{org}/members/{user}/role`` confers approver authority in every
workspace of that organization (there are no workspace-scoped roles; Phase 5B plan §2).

Transaction ownership follows the house rule: the service flushes; the request-scoped
``get_db`` commits the decision and its audit row together, or rolls both back.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.approvals.policy import require_approver
from app.approvals.schemas import ApprovalDecisionCreate, ApprovalHistoryOut, ApprovalOut
from app.approvals.service import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    get_scoped_brief,
    list_awaiting_decision,
    list_decisions,
    record_decision,
)
from app.auth.dependencies import TenantContext, require_exact_roles
from app.briefs.schemas import RecommendationBriefListOut, RecommendationBriefOut
from app.capabilities.registry import Capability
from app.capabilities.resolver import resolve_capability
from app.core.config import get_settings
from app.core.enums import Role
from app.core.errors import CapabilityUnavailableError
from app.core.logging import get_logger, log_event
from app.db.session import get_db

logger = get_logger(__name__)

router = APIRouter(tags=["approvals"])

#: Who may read a brief's decision history: every role except VIEWER. The authors
#: (editors) need to see the outcome, the approver role decides, and the compliance
#: reviewer audits; a viewer holds no duty in the approval workflow. Exact membership.
APPROVAL_HISTORY_READERS = (
    Role.OWNER,
    Role.ADMIN,
    Role.MARKETER,
    Role.REVIEWER,
    Role.COMPLIANCE_REVIEWER,
)
require_history_reader = require_exact_roles(*APPROVAL_HISTORY_READERS)

#: Message of the dark answer (503 ``capability_unavailable``).
APPROVALS_DARK = "Recommendation briefs and approvals are not available yet."

_BRIEF_ROUTE = "/workspaces/{workspace_id}/opportunities/{opportunity_id}/briefs/{brief_id}"


def _require_briefs_capability(db: Session, ctx: TenantContext) -> None:
    """Open only on an explicit ``effective_enabled`` resolution for this workspace.

    The deny-biased resolver decides (safety ceiling → honoured workspace override →
    global flag → secure default) on the server-resolved tenant identity. Any other
    outcome is 503 before anything is read or written. A resolver or storage failure is
    logged and re-raised (5xx): an outage must never look like a policy, and never opens
    the gate.
    """
    try:
        resolution = resolve_capability(
            session=db,
            settings=get_settings(),
            capability=Capability.RECOMMENDATION_BRIEFS,
            organization_id=ctx.organization.id,
            workspace_id=ctx.workspace.id,
        )
    except Exception:
        log_event(
            logger,
            "approval_gate_failed",
            level=logging.ERROR,
            outcome="error",
            capability=Capability.RECOMMENDATION_BRIEFS.value,
            workspace_id=ctx.workspace.id,
        )
        raise
    log_event(
        logger,
        "approval_gate_decided",
        outcome="allowed" if resolution.effective_enabled else "denied",
        capability=Capability.RECOMMENDATION_BRIEFS.value,
        workspace_id=ctx.workspace.id,
        effective_enabled=resolution.effective_enabled,
        decided_by=resolution.decided_by.value,
    )
    if resolution.effective_enabled is not True:
        raise CapabilityUnavailableError(APPROVALS_DARK)


@router.post(f"{_BRIEF_ROUTE}/approvals", response_model=ApprovalOut, status_code=201)
def decide_on_brief(
    workspace_id: str,
    opportunity_id: str,
    brief_id: str,
    body: ApprovalDecisionCreate,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(require_approver),
) -> ApprovalOut:
    """Record one append-only decision (approve / reject / revoke) on the brief's exact
    version. Approver role only; never the brief's author; ``subject_version`` must be the
    brief's version (409 otherwise); ``revoked`` only withdraws a controlling approval."""
    _require_briefs_capability(db, ctx)
    brief = get_scoped_brief(
        db, workspace_id=workspace_id, opportunity_id=opportunity_id, brief_id=brief_id
    )
    approval = record_decision(
        db,
        ctx=ctx,
        brief=brief,
        decision=body.decision,
        subject_version=body.subject_version,
        reason_code=body.reason_code,
        note=body.note,
    )
    return ApprovalOut.model_validate(approval)


@router.get(f"{_BRIEF_ROUTE}/approvals", response_model=ApprovalHistoryOut)
def list_brief_decisions(
    workspace_id: str,
    opportunity_id: str,
    brief_id: str,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(require_history_reader),
) -> ApprovalHistoryOut:
    """The brief version's append-only decisions, newest ``decision_seq`` first: the first
    item, when present, is the controlling decision. Read-only."""
    _require_briefs_capability(db, ctx)
    brief = get_scoped_brief(
        db, workspace_id=workspace_id, opportunity_id=opportunity_id, brief_id=brief_id
    )
    page = list_decisions(db, brief=brief, limit=limit, offset=offset)
    return ApprovalHistoryOut(
        items=[ApprovalOut.model_validate(a) for a in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/workspaces/{workspace_id}/approval-queue", response_model=RecommendationBriefListOut)
def list_approval_queue(
    workspace_id: str,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(require_approver),
) -> RecommendationBriefListOut:
    """The workspace's briefs awaiting a decision, oldest first: the latest version of each
    opportunity's brief whose controlling decision is absent or ``revoked``. Approver role
    only (the approver's work list). Read-only."""
    _require_briefs_capability(db, ctx)
    page = list_awaiting_decision(db, workspace_id=ctx.workspace.id, limit=limit, offset=offset)
    return RecommendationBriefListOut(
        items=[RecommendationBriefOut.model_validate(b) for b in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


__all__ = ["APPROVAL_HISTORY_READERS", "APPROVALS_DARK", "router"]
