"""Audited manual export (Project Phase 5E, lane 5E-W1) — the only write path for
:class:`~app.exports.models.Export`.

An export records that a human exported the text of **one exact approved creative draft
version** and returns that text, rendered (:mod:`app.exports.render`), in the answer. Nothing
is published, stored as a file, signed into a URL or sent anywhere (5E plan §2).

**The six preconditions** (5E plan §2; interface sheet P2, P4–P7, P12), each fail-closed and
each checked here whatever the route did. Before them, the tenant context answers 401 for no
session and 403 for a non-member. Then, in this order (a refusal writes nothing):

1. *Precondition 4, role* — 403 ``permission_denied``: exactly
   :data:`~app.exports.policy.EXPORTER_ROLES` (the route's ``require_exporter``, re-checked).
2. *Precondition 1, capability* — 503 ``capability_unavailable``: ``content_export`` resolves
   enabled for the workspace through the deny-biased resolver (never a raw flag read). Before
   any id lookup, so a dark deployment discloses nothing.
3. *Precondition 6, tenant scope* — 404 ``not_found``: the draft is this workspace's and this
   organization's, and neither it nor its brief is soft-deleted (P2). One answer for unknown,
   foreign and deleted.
4. *Precondition 5, jurisdiction* — 409 ``export_jurisdiction_missing``: the draft's and the
   brief's ``jurisdiction_code`` each pass the grammar
   (:func:`~app.jurisdiction.codes.is_jurisdiction_code`). NULL, ``UNRESOLVED`` and any
   non-code refuse; nothing is inferred or defaulted (S1-5D 31-A, Q-4).
5. *P12, agreement* — 409 ``jurisdiction_mismatch``: the two codes are equal.
6. *Precondition 3, the latest decision* — 409 ``export_not_approved``: the controlling
   decision (5B: the highest ``decision_seq``) on the draft's exact ``(subject_id,
   subject_version)`` is ``approved`` and is this tenant's. Never "an approved row exists":
   approve-then-reject, a revoked approval, no decision and a decision on another version
   all refuse.
7. *Precondition 2, the review* — 409 ``export_review_blocked``: the draft is still approvable
   now, :func:`app.creative.service.draft_approval_refusal` (its review exists, passed and is
   not blocked, and the claims library as it stands now neither blocks its text nor requires
   a disclaimer it lacks; P6). A soft-deleted review counts as none (P2). Any reason a later
   change adds to that function (item 38's) refuses here too.

The permanent refusals come first: a draft without a jurisdiction can never be exported,
whatever its decision. A refusal writes nothing.

**On success, one transaction** (the route commits before it answers):

* one :class:`Export` row: the draft and its exact version; ``approval_id`` = the controlling
  decision; ``jurisdiction_code`` = the draft's captured code, copied, never re-resolved
  (Q-1); ``exported_by`` and ``exported_by_snapshot`` (:func:`app.core.actor_snapshot.
  actor_snapshot`); ``is_simulated`` = the draft's;
* its audit row, ``creative_draft.exported`` (ids, version, format, jurisdiction and flags;
  no text and no identity beyond the actor column);
* ``export.completed`` through the injected
  :class:`~app.notifications.interface.NotificationEmitter`, in the same transaction, to the
  recipients of the one ``export.completed`` resolver
  (:func:`app.notifications.recipients.export_completed_recipients`, interface sheet P9) via
  :func:`app.notifications.recipients.notify`: the emitter writes and flushes at most, never
  commits; if it raises, nothing is written.

**No lock is taken.** The checks read the decision, the review and the library, then insert;
a decision recorded concurrently (which locks the draft row) can commit between the read and
the insert. The export then serializes before that decision: it rested on the decision that
controlled when it was read, and records that decision's id. Nothing the export writes is
read by a decision, so the interleaving is equivalent to export-then-decision.

The export history (:func:`list_exports`) is gated the same way (capability, the history's
role set, the same scoped 404) and never returns rendered text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.approvals.models import Approval
from app.approvals.service import DRAFT_SUBJECT, controlling_decision
from app.audit.actions import AuditAction
from app.audit.service import record_audit
from app.auth.dependencies import TenantContext
from app.briefs.models import RecommendationBrief
from app.capabilities.registry import Capability
from app.capabilities.resolver import resolve_capability
from app.core.actor_snapshot import actor_snapshot
from app.core.config import Settings, get_settings
from app.core.enums import ApprovalDecision, ExportFormat, NotificationEventCode, Role
from app.core.errors import (
    CapabilityUnavailableError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from app.core.logging import get_logger, log_event
from app.creative.models import ContentReview, CreativeDraft
from app.creative.service import DRAFT_NOT_FOUND, draft_approval_refusal, get_scoped_draft
from app.exports.models import Export
from app.exports.policy import EXPORT_HISTORY_READER_ROLES, EXPORTER_ROLES
from app.exports.render import ExportSource, render_export
from app.jurisdiction.codes import is_jurisdiction_code
from app.notifications.interface import NotificationEmitter
from app.notifications.recipients import notify

logger = get_logger("signalnest.exports")

#: Export history page bounds (the approval history's).
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

#: The audit ``entity_type`` of an export: the draft that was exported.
AUDIT_ENTITY_TYPE = "creative_draft"

# --------------------------------------------------------------------------- #
# Refusal codes and their fixed messages (no draft text, no claim text, no other tenant)
# --------------------------------------------------------------------------- #
EXPORT_UNAVAILABLE = "Export is not available yet."
EXPORT_JURISDICTION_MISSING = "export_jurisdiction_missing"
JURISDICTION_MISSING_MESSAGE = (
    "No jurisdiction was recorded for this draft or its brief, so it can't be exported."
)
JURISDICTION_MISMATCH = "jurisdiction_mismatch"
JURISDICTION_MISMATCH_MESSAGE = (
    "This draft's jurisdiction does not match its brief's, so it can't be exported."
)
EXPORT_NOT_APPROVED = "export_not_approved"
#: Why the controlling decision does not allow an export, per its state.
NOT_APPROVED_MESSAGES: dict[str, str] = {
    "no_decision": "This draft version has not been approved, so it can't be exported.",
    ApprovalDecision.REJECTED.value: (
        "This draft version's latest decision is a rejection, so it can't be exported."
    ),
    ApprovalDecision.REVOKED.value: (
        "This draft version's approval was revoked, so it can't be exported."
    ),
}
EXPORT_REVIEW_BLOCKED = "export_review_blocked"
#: Why an approved draft is no longer exportable, per ``draft_approval_refusal`` reason.
REVIEW_MESSAGES: dict[str, str] = {
    "review_missing": "This draft has no content review, so it can't be exported.",
    "review_blocked": "This draft's content review blocked it, so it can't be exported.",
    "review_not_passed": (
        "A required disclaimer is missing from this draft, so it can't be exported."
    ),
    "blocked_claim": (
        "This draft contains a claim the claims library now blocks, so it can't be exported."
    ),
    "disclaimer_missing": (
        "This draft lacks a disclaimer the claims library now requires, so it can't be exported."
    ),
}
#: Any other reason ``draft_approval_refusal`` gives (a later precondition, such as item 38's,
#: is inherited without a code change here): refused, with this message.
REVIEW_MESSAGE_OTHER = "This draft can no longer be approved, so it can't be exported."


@dataclass(frozen=True)
class ExportResult:
    """The export just recorded (flushed, not committed) and its rendered text."""

    export: Export
    content: str


@dataclass(frozen=True)
class ExportPage:
    """A bounded page of one draft version's exports, newest first."""

    items: tuple[Export, ...]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- #
# Precondition 1: the capability (the module's single resolver call site)
# --------------------------------------------------------------------------- #
def export_capability(db: Session, ctx: TenantContext, settings: Settings | None = None):
    """The deny-biased resolution of ``content_export`` for the context's workspace.

    A resolver failure is logged and re-raised (a 5xx), never turned into a decision: a
    coerced ``False`` would make an outage look like a governance denial, and a coerced
    ``True`` would open the gate."""
    try:
        return resolve_capability(
            session=db,
            settings=settings or get_settings(),
            capability=Capability.CONTENT_EXPORT,
            organization_id=ctx.organization.id,
            workspace_id=ctx.workspace.id,
        )
    except Exception:
        log_event(
            logger,
            "content_export_gate_failed",
            level=logging.ERROR,
            outcome="error",
            capability=Capability.CONTENT_EXPORT.value,
            organization_id=ctx.organization.id,
            workspace_id=ctx.workspace.id,
        )
        raise


def require_export_feature(
    db: Session, ctx: TenantContext, settings: Settings | None = None
) -> None:
    """Fail-closed gate: only an explicit ``effective_enabled is True`` opens it (503)."""
    resolution = export_capability(db, ctx, settings)
    log_event(
        logger,
        "content_export_gate_decided",
        outcome="allowed" if resolution.effective_enabled is True else "denied",
        capability=Capability.CONTENT_EXPORT.value,
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        effective_enabled=resolution.effective_enabled,
        decided_by=resolution.decided_by.value,
        global_flag=resolution.global_flag,
        has_override=resolution.has_override,
    )
    if resolution.effective_enabled is not True:
        raise CapabilityUnavailableError(EXPORT_UNAVAILABLE)


# --------------------------------------------------------------------------- #
# Preconditions 4 and 6: role, tenant scope
# --------------------------------------------------------------------------- #
def _require_role(ctx: TenantContext, allowed: frozenset[Role]) -> None:
    """Exact membership (O-1), re-checked here whatever the route's dependency did."""
    role = Role(ctx.role)
    if role not in allowed:
        raise PermissionDeniedError(f"Role '{role.value}' is not permitted for this action.")


def scoped_subject(
    db: Session, ctx: TenantContext, draft_id: str
) -> tuple[CreativeDraft, RecommendationBrief]:
    """The draft and its brief, only when both are this tenant's and neither is soft-deleted.

    One answer (404, the creative router's message) for an unknown id, another workspace's or
    organization's draft, a soft-deleted draft, and a draft whose brief is missing, foreign or
    soft-deleted (P2): the caller never learns which."""
    draft = get_scoped_draft(db, ctx.workspace.id, draft_id)
    if draft.organization_id != ctx.organization.id or draft.deleted_at is not None:
        raise NotFoundError(DRAFT_NOT_FOUND)
    brief = db.get(RecommendationBrief, draft.brief_id)
    if (
        brief is None
        or brief.workspace_id != draft.workspace_id
        or brief.organization_id != draft.organization_id
        or brief.deleted_at is not None
    ):
        raise NotFoundError(DRAFT_NOT_FOUND)
    return draft, brief


# --------------------------------------------------------------------------- #
# Preconditions 5, P12, 3 and 2
# --------------------------------------------------------------------------- #
def _refuse(code: str, message: str, *, ctx: TenantContext, reason: str) -> ConflictError:
    log_event(
        logger,
        "content_export_refused",
        outcome="refused",
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        code=code,
        reason=reason,
    )
    return ConflictError(message, code=code)


def _require_jurisdiction(
    draft: CreativeDraft, brief: RecommendationBrief, ctx: TenantContext
) -> str:
    """The draft's captured code, when it and the brief's are both codes and agree."""
    if not is_jurisdiction_code(draft.jurisdiction_code):
        raise _refuse(
            EXPORT_JURISDICTION_MISSING, JURISDICTION_MISSING_MESSAGE, ctx=ctx, reason="draft"
        )
    if not is_jurisdiction_code(brief.jurisdiction_code):
        raise _refuse(
            EXPORT_JURISDICTION_MISSING, JURISDICTION_MISSING_MESSAGE, ctx=ctx, reason="brief"
        )
    if draft.jurisdiction_code != brief.jurisdiction_code:
        raise _refuse(
            JURISDICTION_MISMATCH, JURISDICTION_MISMATCH_MESSAGE, ctx=ctx, reason="mismatch"
        )
    return draft.jurisdiction_code


def _require_approved(db: Session, draft: CreativeDraft, ctx: TenantContext) -> Approval:
    """The controlling decision on the draft's exact version, when it is ``approved``."""
    latest = controlling_decision(
        db,
        subject_type=DRAFT_SUBJECT,
        subject_id=draft.id,
        subject_version=draft.draft_version,
    )
    if (
        latest is not None
        and latest.workspace_id == draft.workspace_id
        and latest.organization_id == draft.organization_id
        and latest.decision == ApprovalDecision.APPROVED.value
    ):
        return latest
    state = "no_decision" if latest is None else latest.decision
    message = NOT_APPROVED_MESSAGES.get(state, NOT_APPROVED_MESSAGES["no_decision"])
    raise _refuse(EXPORT_NOT_APPROVED, message, ctx=ctx, reason=state)


def _require_review(db: Session, draft: CreativeDraft, ctx: TenantContext) -> None:
    """The draft is still approvable now (precondition 2; ``draft_approval_refusal``)."""
    review_deleted = db.scalar(
        select(ContentReview.id).where(
            ContentReview.workspace_id == draft.workspace_id,
            ContentReview.draft_id == draft.id,
            ContentReview.deleted_at.is_not(None),
        )
    )
    refusal = "review_missing" if review_deleted is not None else draft_approval_refusal(db, draft)
    if refusal is not None:
        message = REVIEW_MESSAGES.get(refusal, REVIEW_MESSAGE_OTHER)
        raise _refuse(EXPORT_REVIEW_BLOCKED, message, ctx=ctx, reason=refusal)


# --------------------------------------------------------------------------- #
# The export
# --------------------------------------------------------------------------- #
def _source(draft: CreativeDraft, jurisdiction_code: str) -> ExportSource:
    return ExportSource(
        draft_id=draft.id,
        draft_version=draft.draft_version,
        channel=draft.channel,
        jurisdiction_code=jurisdiction_code,
        is_simulated=bool(draft.is_simulated),
        headline=draft.headline,
        body=draft.body,
        call_to_action=draft.call_to_action,
    )


def create_export(
    db: Session,
    *,
    ctx: TenantContext,
    draft_id: str,
    export_format: ExportFormat,
    emitter: NotificationEmitter,
    settings: Settings | None = None,
) -> ExportResult:
    """Check the six preconditions, then record and render one export. Flushes only.

    Raises (writing nothing) 403, 503, 404 or 409 in the order of the module docstring.
    """
    export_format = ExportFormat(export_format)
    _require_role(ctx, EXPORTER_ROLES)
    require_export_feature(db, ctx, settings)
    draft, brief = scoped_subject(db, ctx, draft_id)
    jurisdiction_code = _require_jurisdiction(draft, brief, ctx)
    approval = _require_approved(db, draft, ctx)
    _require_review(db, draft, ctx)

    content = render_export(_source(draft, jurisdiction_code), export_format)
    export = Export(
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        draft_id=draft.id,
        draft_version=draft.draft_version,
        approval_id=approval.id,
        format=export_format.value,
        jurisdiction_code=jurisdiction_code,
        exported_by=ctx.user.id,
        exported_by_snapshot=actor_snapshot(
            user_id=ctx.user.id, full_name=ctx.user.full_name, email=ctx.user.email
        ),
        is_simulated=bool(draft.is_simulated),
    )
    db.add(export)
    db.flush()
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        actor_user_id=ctx.user.id,
        action=AuditAction.CREATIVE_DRAFT_EXPORTED,
        entity_type=AUDIT_ENTITY_TYPE,
        entity_id=draft.id,
        new_state={
            "export_id": export.id,
            "draft_version": export.draft_version,
            "approval_id": export.approval_id,
            "format": export.format,
            "jurisdiction_code": export.jurisdiction_code,
            "is_simulated": export.is_simulated,
        },
    )
    # export.completed: the recipients are the one resolver's for the event code
    # (app.notifications.recipients, interface sheet P9), written through the injected
    # emitter after the Export row is flushed (I-5E: one emitter, one resolver).
    notified = notify(
        db,
        NotificationEventCode.EXPORT_COMPLETED,
        export,
        actor_user_id=ctx.user.id,
        emitter=emitter,
    )
    log_event(
        logger,
        "content_export_recorded",
        outcome="success",
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        export_id=export.id,
        draft_id=draft.id,
        draft_version=export.draft_version,
        format=export.format,
        is_simulated=export.is_simulated,
        notified=notified,
    )
    return ExportResult(export=export, content=content)


# --------------------------------------------------------------------------- #
# The export history
# --------------------------------------------------------------------------- #
def _clamp(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(MAX_LIMIT, int(limit))), max(0, int(offset))


def list_exports(
    db: Session,
    *,
    ctx: TenantContext,
    draft_id: str,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    settings: Settings | None = None,
) -> ExportPage:
    """A bounded page of the draft version's exports, newest first. Read-only.

    The role is exactly :data:`~app.exports.policy.EXPORT_HISTORY_READER_ROLES` (403), then the
    capability (503) and the scoped draft (404) as for an export."""
    _require_role(ctx, EXPORT_HISTORY_READER_ROLES)
    require_export_feature(db, ctx, settings)
    draft, _brief = scoped_subject(db, ctx, draft_id)
    limit, offset = _clamp(limit, offset)
    base = select(Export).where(
        Export.organization_id == ctx.organization.id,
        Export.workspace_id == ctx.workspace.id,
        Export.draft_id == draft.id,
        Export.draft_version == draft.draft_version,
    )
    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = tuple(
        db.scalars(
            base.order_by(Export.created_at.desc(), Export.id.desc()).limit(limit).offset(offset)
        )
    )
    return ExportPage(items=rows, total=total, limit=limit, offset=offset)


__all__ = [
    "AUDIT_ENTITY_TYPE",
    "DEFAULT_LIMIT",
    "EXPORT_JURISDICTION_MISSING",
    "EXPORT_NOT_APPROVED",
    "EXPORT_REVIEW_BLOCKED",
    "JURISDICTION_MISMATCH",
    "MAX_LIMIT",
    "NOT_APPROVED_MESSAGES",
    "REVIEW_MESSAGES",
    "REVIEW_MESSAGE_OTHER",
    "ExportPage",
    "ExportResult",
    "create_export",
    "export_capability",
    "list_exports",
    "require_export_feature",
    "scoped_subject",
]
