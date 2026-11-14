"""In-app notification service (Project Phase 5E, lane 5E-W3).

**The emitter.** :func:`emit_notifications` implements the frozen
:class:`~app.notifications.interface.NotificationEmitter` Protocol. The brief, approval and
creative services (5E-W3's hooks) and the export service (5E-W1's ``export.completed``) call
it inside the transaction of the event they report. In order, it:

1. takes ``event_code`` from the closed :class:`~app.core.enums.NotificationEventCode`
   vocabulary (anything else is a :class:`NotificationContractError`);
2. requires ``workspace_id`` to belong to ``organization_id``. This is the code substitute for
   the composite organization/workspace key the notification tables do not have yet (5E
   package §6; it needs a migration-freeze exception);
3. requires ``subject_id`` to name a row of ``NOTIFICATION_SUBJECT_TABLES[event_code]`` in that
   organization and workspace that is not soft-deleted (interface sheet P2);
4. writes **nothing** while ``Capability.NOTIFICATIONS_ANALYTICS`` is not effectively enabled
   for the workspace (operator ruling O-12(B); interface sheet §A.2). The decision comes from
   the deny-biased resolver, never a raw flag read, and a resolver failure is re-raised,
   never turned into a decision;
5. skips every recipient who is not a member of ``organization_id``, and every recipient whose
   ``notification_preferences`` row for ``(workspace_id, user_id, event_code)`` has
   ``enabled = False``. Recipients are de-duplicated: one row per user per event;
6. inserts one :class:`~app.notifications.models.Notification` per remaining recipient and
   flushes. It **never commits**: the rows commit with the triggering event, or not at all.

Steps 1-3 are the caller's contract. A breach raises :class:`NotificationContractError`
whatever the capability says, so a hook passing the wrong ids fails in the dark state too,
where a test can see it. A breach is a programming error, never a customer answer. In-app
only: nothing here sends mail, SMS, push or any other message.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.approvals.models import Approval
from app.briefs.models import RecommendationBrief
from app.capabilities.registry import Capability
from app.capabilities.resolver import CapabilityResolution, resolve_capability
from app.core.config import Settings, get_settings
from app.core.enums import NOTIFICATION_SUBJECT_TABLES, NotificationEventCode
from app.core.logging import get_logger, log_event
from app.creative.models import ContentReview, CreativeDraft
from app.exports.models import Export
from app.notifications.interface import NotificationEmitter
from app.notifications.models import Notification, NotificationPreference
from app.organizations.models import OrganizationMember, Workspace

logger = get_logger(__name__)


class NotificationContractError(ValueError):
    """A caller broke the emission contract of :mod:`app.notifications.interface`.

    Raised for an unknown event code, a workspace outside the organization, or a subject that
    is not a live row of the event's table in that workspace. A programming error: it is
    never mapped to a customer answer.
    """


#: The ORM model behind each subject table :data:`NOTIFICATION_SUBJECT_TABLES` names.
_SUBJECT_MODELS: Mapping[str, Any] = MappingProxyType(
    {
        model.__tablename__: model
        for model in (RecommendationBrief, Approval, CreativeDraft, ContentReview, Export)
    }
)
_UNMAPPED = set(NOTIFICATION_SUBJECT_TABLES.values()) - set(_SUBJECT_MODELS)
if _UNMAPPED:  # pragma: no cover - a vocabulary change without its model
    raise RuntimeError(f"notification subject tables without a model: {sorted(_UNMAPPED)}")


def notifications_analytics_capability(
    db: Session, settings: Settings, organization_id: str, workspace_id: str
) -> CapabilityResolution:
    """The deny-biased resolution of ``notifications_analytics`` for one workspace (O-12(B)).

    This package's single call into the resolver: the emitter, the route gate and the
    reflection route all decide through it, so the UI shows what the backend enforces.
    """
    return resolve_capability(
        session=db,
        settings=settings,
        capability=Capability.NOTIFICATIONS_ANALYTICS,
        organization_id=organization_id,
        workspace_id=workspace_id,
    )


def _workspace_in_organization(db: Session, organization_id: str, workspace_id: str) -> bool:
    found = db.scalar(
        select(Workspace.id).where(
            Workspace.id == workspace_id, Workspace.organization_id == organization_id
        )
    )
    return found is not None


def _live_subject(
    db: Session,
    event_code: NotificationEventCode,
    *,
    organization_id: str,
    workspace_id: str,
    subject_id: str,
) -> bool:
    """Whether ``subject_id`` is a live row of the event's subject table in this scope.

    "Live" excludes a soft-deleted row (``deleted_at`` set) on the tables that carry the marker
    (briefs, drafts, reviews). The caller must have flushed the subject: the session does not
    autoflush, and an unflushed row has no id yet anyway.
    """
    model = _SUBJECT_MODELS[NOTIFICATION_SUBJECT_TABLES[event_code]]
    stmt = select(model.id).where(
        model.id == subject_id,
        model.organization_id == organization_id,
        model.workspace_id == workspace_id,
    )
    deleted_at = getattr(model, "deleted_at", None)
    if deleted_at is not None:
        stmt = stmt.where(deleted_at.is_(None))
    return db.scalar(stmt) is not None


def _deliverable(
    db: Session,
    *,
    organization_id: str,
    workspace_id: str,
    event_code: NotificationEventCode,
    candidates: list[str],
) -> list[str]:
    """The candidates who are members of the organization and have not opted out, in order."""
    if not candidates:
        return []
    members = set(
        db.scalars(
            select(OrganizationMember.user_id).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id.in_(candidates),
            )
        )
    )
    opted_out = set(
        db.scalars(
            select(NotificationPreference.user_id).where(
                NotificationPreference.workspace_id == workspace_id,
                NotificationPreference.event_code == event_code.value,
                NotificationPreference.user_id.in_(candidates),
                NotificationPreference.enabled.is_(False),
            )
        )
    )
    return [user_id for user_id in candidates if user_id in members and user_id not in opted_out]


def _emission_capability(
    db: Session, organization_id: str, workspace_id: str, event_code: NotificationEventCode
) -> CapabilityResolution:
    try:
        return notifications_analytics_capability(db, get_settings(), organization_id, workspace_id)
    except Exception:
        log_event(
            logger,
            "notifications_emission_gate_failed",
            level=logging.ERROR,
            outcome="error",
            capability=Capability.NOTIFICATIONS_ANALYTICS.value,
            event_code=event_code.value,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        raise


def emit_notifications(
    db: Session,
    *,
    organization_id: str,
    workspace_id: str,
    recipient_user_ids: Iterable[str],
    event_code: NotificationEventCode,
    subject_id: str,
) -> int:
    """Write in-app notifications for one event in the caller's transaction (module docstring).

    Returns the number of rows written: ``0`` while the capability is off for the workspace,
    or when every recipient was skipped. Raises :class:`NotificationContractError` when the
    caller breaks the contract.
    """
    try:
        code = NotificationEventCode(event_code)
    except ValueError as exc:
        raise NotificationContractError("Unknown notification event code.") from exc
    if isinstance(recipient_user_ids, str):
        # A bare string is an Iterable[str] of characters, never a list of user ids.
        raise NotificationContractError("recipient_user_ids must be a collection of user ids.")
    if not _workspace_in_organization(db, organization_id, workspace_id):
        raise NotificationContractError("The workspace does not belong to the organization.")
    if not _live_subject(
        db,
        code,
        organization_id=organization_id,
        workspace_id=workspace_id,
        subject_id=subject_id,
    ):
        raise NotificationContractError("The subject is not a live row of the event's table.")

    resolution = _emission_capability(db, organization_id, workspace_id, code)
    if not resolution.effective_enabled:
        log_event(
            logger,
            "notifications_emission_skipped",
            outcome="denied",
            capability=Capability.NOTIFICATIONS_ANALYTICS.value,
            event_code=code.value,
            organization_id=organization_id,
            workspace_id=workspace_id,
            decided_by=resolution.decided_by.value,
        )
        return 0

    # Materialised only now, so a lazy iterable is not consumed while the capability is off.
    candidates = list(dict.fromkeys(user_id for user_id in recipient_user_ids if user_id))
    recipients = _deliverable(
        db,
        organization_id=organization_id,
        workspace_id=workspace_id,
        event_code=code,
        candidates=candidates,
    )
    if recipients:
        db.add_all(
            Notification(
                organization_id=organization_id,
                workspace_id=workspace_id,
                user_id=user_id,
                event_code=code.value,
                subject_id=subject_id,
            )
            for user_id in recipients
        )
        db.flush()
    log_event(
        logger,
        "notifications_emitted",
        outcome="allowed",
        event_code=code.value,
        organization_id=organization_id,
        workspace_id=workspace_id,
        candidates=len(candidates),
        written=len(recipients),
    )
    return len(recipients)


#: The implementation, typed as the frozen Protocol so a signature drift is a type error.
emitter: NotificationEmitter = emit_notifications


__all__ = [
    "NotificationContractError",
    "emit_notifications",
    "emitter",
    "notifications_analytics_capability",
]
