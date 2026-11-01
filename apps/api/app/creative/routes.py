"""Feature-gated creative-draft API (Project Phase 5C, W21 lane P5C).

Three draft endpoints, all capability-gated (Phase 5C plan §2):

    POST /api/v1/workspaces/{workspace_id}/briefs/{brief_id}/drafts   generate / revise
    GET  /api/v1/workspaces/{workspace_id}/briefs/{brief_id}/drafts   list a brief's drafts
    GET  /api/v1/workspaces/{workspace_id}/drafts/{draft_id}          fetch one draft

and one workspace-scoped reflection, deliberately *not* capability-gated (it reports the
capability decision; the feedback- and brief-capability precedent):

    GET  /api/v1/workspaces/{workspace_id}/creative-capability

* **Capability gate.** Every draft endpoint answers 503 ``capability_unavailable`` unless
  ``creative_generation`` resolves *enabled for this workspace* through the deny-biased
  resolver (an honored workspace override outranks the global flag; the shipped state is the
  flag ``False`` with no override, so every endpoint is dark and no generation path is
  reachable). A resolver failure is logged and re-raised (a 5xx), never turned into a
  decision. The gate runs before any id lookup, so a dark deployment discloses nothing.
* **Authorization.** Generating (``POST``) is editor-only — owner, admin, marketer — the brief
  capture's set. Reads are open to every workspace member, like the brief a draft comes from,
  so the reviewer who approves a draft (5B, ``APPROVER_ROLES``) can read it. A non-member is
  403; an unauthenticated caller 401.
* **Scope / IDOR.** The brief and the draft are resolved within the path workspace, and a
  revision's parent within the brief: an unknown or foreign id answers the same 404
  (non-enumerating).
* **Generation is a durable job.** ``POST`` validates (brief approved 422 ``brief_not_approved``;
  ceilings configured 503 ``creative_generation_unconfigured``; daily ceilings not reached 429
  ``llm_quota_exceeded``) and answers 202 with the ``creative.generate`` job to poll
  (``GET /workspaces/{workspace_id}/jobs/{job_id}``). A repeated identical request while that
  job is queued or running answers the same job. The draft — never updated; a revision is a new
  version — carries its content review.

Transaction ownership follows the house rule: the request-scoped ``get_db`` commits.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.dependencies import TenantContext, get_tenant_context, require_role
from app.capabilities.registry import Capability
from app.core.config import get_settings
from app.core.enums import Role
from app.core.errors import CapabilityUnavailableError
from app.core.logging import get_logger, log_event
from app.creative.schemas import (
    CreativeDraftCreate,
    CreativeDraftListOut,
    CreativeDraftOut,
    CreativeGenerationCapabilityOut,
    CreativeGenerationQueuedOut,
)
from app.creative.service import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    creative_capability,
    draft_out,
    get_scoped_brief,
    get_scoped_draft,
    list_brief_drafts,
    request_generation,
    reviews_by_draft,
)
from app.db.session import get_db

logger = get_logger(__name__)

router = APIRouter(tags=["creative-drafts"])

#: Generating: the workspace editors (the brief-capture set).
EDITORS = require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)


def _resolve_creative_capability(db: Session, ctx: TenantContext):
    """The one place this package asks whether creative generation is available.

    A dependency failure is re-raised, never converted into a decision: a coerced ``False``
    would make an outage look like a governance denial."""
    try:
        return creative_capability(db, get_settings(), ctx.organization.id, ctx.workspace.id)
    except Exception:
        log_event(
            logger,
            "creative_generation_gate_failed",
            level=logging.ERROR,
            outcome="error",
            capability=Capability.CREATIVE_GENERATION.value,
            organization_id=ctx.organization.id,
            workspace_id=ctx.workspace.id,
        )
        raise


def _require_creative_feature(db: Session, ctx: TenantContext) -> None:
    """Fail-closed gate: only an explicit ``effective_enabled`` resolution opens it."""
    resolution = _resolve_creative_capability(db, ctx)
    log_event(
        logger,
        "creative_generation_gate_decided",
        outcome="allowed" if resolution.effective_enabled else "denied",
        capability=Capability.CREATIVE_GENERATION.value,
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        effective_enabled=resolution.effective_enabled,
        decided_by=resolution.decided_by.value,
        global_flag=resolution.global_flag,
        has_override=resolution.has_override,
    )
    if not resolution.effective_enabled:
        raise CapabilityUnavailableError("Creative generation is not available yet.")


@router.get(
    "/workspaces/{workspace_id}/creative-capability",
    response_model=CreativeGenerationCapabilityOut,
    summary="Whether creative generation is available for this workspace",
)
def read_creative_capability(
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> CreativeGenerationCapabilityOut:
    """Customer-facing, workspace-effective availability (any member). Advisory only: every
    draft endpoint re-decides and answers 503 while disabled."""
    resolution = _resolve_creative_capability(db, ctx)
    log_event(
        logger,
        "creative_generation_capability_reflected",
        outcome="allowed" if resolution.effective_enabled else "denied",
        capability=Capability.CREATIVE_GENERATION.value,
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        effective_enabled=resolution.effective_enabled,
    )
    return CreativeGenerationCapabilityOut(enabled=resolution.effective_enabled)


@router.post(
    "/workspaces/{workspace_id}/briefs/{brief_id}/drafts",
    response_model=CreativeGenerationQueuedOut,
    status_code=202,
    summary="Generate a new draft version from an approved brief (a durable job)",
)
def generate_brief_draft(
    workspace_id: str,
    brief_id: str,
    body: CreativeDraftCreate,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(EDITORS),
) -> CreativeGenerationQueuedOut:
    """Enqueue (or join) the generation of one draft version; poll the returned job."""
    _require_creative_feature(db, ctx)
    brief = get_scoped_brief(db, workspace_id, brief_id)
    job = request_generation(
        db, brief=brief, body=body, requested_by=ctx.user, settings=get_settings()
    )
    return CreativeGenerationQueuedOut(job_id=job.id)


@router.get(
    "/workspaces/{workspace_id}/briefs/{brief_id}/drafts",
    response_model=CreativeDraftListOut,
    summary="List a brief's drafts, newest version first",
)
def list_drafts(
    workspace_id: str,
    brief_id: str,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> CreativeDraftListOut:
    """A bounded page of the brief's immutable draft versions, reviews embedded."""
    _require_creative_feature(db, ctx)
    brief = get_scoped_brief(db, workspace_id, brief_id)
    rows, total = list_brief_drafts(db, brief, limit=limit, offset=offset)
    reviews = reviews_by_draft(db, rows)
    return CreativeDraftListOut(
        items=[draft_out(row, reviews.get(row.id)) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/workspaces/{workspace_id}/drafts/{draft_id}",
    response_model=CreativeDraftOut,
    summary="Fetch one draft version with its content review",
)
def get_draft(
    workspace_id: str,
    draft_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> CreativeDraftOut:
    """One draft version (any member; gated); unknown or foreign ids answer 404."""
    _require_creative_feature(db, ctx)
    draft = get_scoped_draft(db, workspace_id, draft_id)
    return draft_out(draft, reviews_by_draft(db, [draft]).get(draft.id))


__all__ = ["router"]
