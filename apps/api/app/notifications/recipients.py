"""Who is notified of each event (Project Phase 5E, lane 5E-W3; interface sheet P9).

**These recipient rules are the coordinator's default, recorded in the 5E interface sheet
(P9) as "least disclosure". They await the operator's confirmation at activation.** No plan,
sub-plan or ruling names recipients (5E package §6). Each event code has exactly one
resolver below, so a later ruling changes one function:

* ``brief.created`` → the members of the workspace's organization whose role is REVIEWER, the
  approvers under O-2 (:data:`app.approvals.policy.APPROVER_ROLES`, exactly REVIEWER);
* ``approval.decided`` → the author of the decided subject: the brief's or the draft's
  ``created_by``;
* ``draft.generated`` and ``review.blocked`` → the user who requested the generation, which is
  the draft's ``created_by`` (``app.creative.service.execute_generation`` writes the requester
  there);
* ``export.completed`` → the draft's author and the reviewer whose approval authorized the
  export (the export's ``approval_id`` row's ``decided_by``).

**The actor is always excluded.** Every resolver drops ``actor_user_id``, the user whose
action raised the event. Generation runs as a durable job and has no acting user, so its
hooks pass ``None`` and the requester is notified.

A resolver only proposes candidates. :func:`app.notifications.service.emit_notifications`
still skips non-members and opted-out users, and writes nothing while the capability is off.
Unknown users (an erased author, ``created_by`` set to ``NULL``) are dropped here.
Soft-deleted parents are not followed (interface sheet P2).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from types import MappingProxyType
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.approvals.models import Approval
from app.approvals.policy import APPROVER_ROLES
from app.briefs.models import RecommendationBrief
from app.core.enums import ApprovalSubjectType, NotificationEventCode
from app.creative.models import ContentReview, CreativeDraft
from app.exports.models import Export
from app.notifications.service import emit_notifications
from app.organizations.models import OrganizationMember

#: A resolver: the candidate recipients of one event about ``subject``, the actor excluded.
Resolver = Callable[..., list[str]]


def _without_actor(user_ids: Iterable[str | None], actor_user_id: str | None) -> list[str]:
    """Known user ids, de-duplicated in order, the actor removed."""
    return [
        user_id
        for user_id in dict.fromkeys(user_ids)
        if user_id is not None and user_id != actor_user_id
    ]


def _live_brief(db: Session, workspace_id: str, brief_id: str | None) -> RecommendationBrief | None:
    if brief_id is None:
        return None
    return db.scalar(
        select(RecommendationBrief).where(
            RecommendationBrief.id == brief_id,
            RecommendationBrief.workspace_id == workspace_id,
            RecommendationBrief.deleted_at.is_(None),
        )
    )


def _live_draft(db: Session, workspace_id: str, draft_id: str | None) -> CreativeDraft | None:
    if draft_id is None:
        return None
    return db.scalar(
        select(CreativeDraft).where(
            CreativeDraft.id == draft_id,
            CreativeDraft.workspace_id == workspace_id,
            CreativeDraft.deleted_at.is_(None),
        )
    )


def brief_created_recipients(
    db: Session, brief: RecommendationBrief, *, actor_user_id: str | None
) -> list[str]:
    """``brief.created``: the organization's REVIEWER members (O-2 approvers), not the actor."""
    reviewers = db.scalars(
        select(OrganizationMember.user_id)
        .where(
            OrganizationMember.organization_id == brief.organization_id,
            OrganizationMember.role.in_(sorted(role.value for role in APPROVER_ROLES)),
        )
        .order_by(OrganizationMember.user_id)
    )
    return _without_actor(reviewers, actor_user_id)


def approval_decided_recipients(
    db: Session, approval: Approval, *, actor_user_id: str | None
) -> list[str]:
    """``approval.decided``: the author of the decided brief or draft, not the actor."""
    subject_type = ApprovalSubjectType(approval.subject_type)
    author: str | None = None
    if subject_type is ApprovalSubjectType.RECOMMENDATION_BRIEF:
        brief = _live_brief(db, approval.workspace_id, approval.subject_id)
        author = brief.created_by if brief is not None else None
    elif subject_type is ApprovalSubjectType.CREATIVE_DRAFT:
        draft = _live_draft(db, approval.workspace_id, approval.subject_id)
        author = draft.created_by if draft is not None else None
    return _without_actor([author], actor_user_id)


def draft_generated_recipients(
    db: Session, draft: CreativeDraft, *, actor_user_id: str | None
) -> list[str]:
    """``draft.generated``: the user who requested the generation (the draft's author)."""
    return _without_actor([draft.created_by], actor_user_id)


def review_blocked_recipients(
    db: Session, review: ContentReview, *, actor_user_id: str | None
) -> list[str]:
    """``review.blocked``: the user who requested the generation of the reviewed draft."""
    draft = _live_draft(db, review.workspace_id, review.draft_id)
    return _without_actor([draft.created_by if draft is not None else None], actor_user_id)


def export_completed_recipients(
    db: Session, export: Export, *, actor_user_id: str | None
) -> list[str]:
    """``export.completed``: the draft's author and the approving reviewer, not the exporter."""
    draft = _live_draft(db, export.workspace_id, export.draft_id)
    approver: str | None = None
    if export.approval_id is not None:
        approver = db.scalar(
            select(Approval.decided_by).where(
                Approval.id == export.approval_id,
                Approval.workspace_id == export.workspace_id,
            )
        )
    return _without_actor(
        [draft.created_by if draft is not None else None, approver], actor_user_id
    )


#: Exactly one resolver per event code.
RECIPIENT_RESOLVERS: Mapping[NotificationEventCode, Resolver] = MappingProxyType(
    {
        NotificationEventCode.BRIEF_CREATED: brief_created_recipients,
        NotificationEventCode.APPROVAL_DECIDED: approval_decided_recipients,
        NotificationEventCode.DRAFT_GENERATED: draft_generated_recipients,
        NotificationEventCode.REVIEW_BLOCKED: review_blocked_recipients,
        NotificationEventCode.EXPORT_COMPLETED: export_completed_recipients,
    }
)
if set(RECIPIENT_RESOLVERS) != set(NotificationEventCode):  # pragma: no cover
    raise RuntimeError("every notification event code needs exactly one recipient resolver")


def resolve_recipients(
    db: Session,
    event_code: NotificationEventCode,
    subject: Any,
    *,
    actor_user_id: str | None,
) -> list[str]:
    """The candidate recipients of ``event_code`` about ``subject`` (its resolver's answer)."""
    return RECIPIENT_RESOLVERS[NotificationEventCode(event_code)](
        db, subject, actor_user_id=actor_user_id
    )


def notify(
    db: Session,
    event_code: NotificationEventCode,
    subject: Any,
    *,
    actor_user_id: str | None,
) -> int:
    """Resolve the recipients of one event and emit it, in the caller's transaction.

    ``subject`` is the flushed row the event is about: a brief, an approval, a draft, a
    content review or an export, as :data:`app.core.enums.NOTIFICATION_SUBJECT_TABLES` gives
    for ``event_code``. Returns the number of notifications written (``0`` while the
    capability is off). Never commits.

    The recipients are resolved lazily: the emitter reads them only after the capability
    check, so a hook in a dark workspace runs no resolver query.
    """

    def _recipients() -> Iterator[str]:
        yield from resolve_recipients(db, event_code, subject, actor_user_id=actor_user_id)

    return emit_notifications(
        db,
        organization_id=subject.organization_id,
        workspace_id=subject.workspace_id,
        recipient_user_ids=_recipients(),
        event_code=event_code,
        subject_id=subject.id,
    )


__all__ = [
    "RECIPIENT_RESOLVERS",
    "approval_decided_recipients",
    "brief_created_recipients",
    "draft_generated_recipients",
    "export_completed_recipients",
    "notify",
    "resolve_recipients",
    "review_blocked_recipients",
]
