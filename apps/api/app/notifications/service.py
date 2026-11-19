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
6. takes ``KEY SHARE`` on the remaining recipients' ``users`` rows with ``SKIP LOCKED``, and
   skips any recipient whose row another transaction holds ``FOR UPDATE`` at that instant (see
   "Locks" below);
7. inserts one :class:`~app.notifications.models.Notification` per remaining recipient and
   flushes. It **never commits**: the rows commit with the triggering event, or not at all.

**Locks (PostgreSQL; 5E-W3 phase B).** While the capability is off the emitter only reads
(plain ``SELECT``s, no row lock). While it is on, a notification row's foreign keys make
PostgreSQL take ``KEY SHARE`` on the organization, workspace and recipient ``users`` rows it
references. The emitter takes no other lock and never waits on a ``users`` row:

* The **recipients' ``users`` rows** are locked first, ``FOR KEY SHARE SKIP LOCKED``, so the
  insert's foreign-key check finds them already held. A recipient whose row is held ``FOR
  UPDATE`` is skipped, not waited for. The holder is an account erasure, a password change or
  reset, or an email verification. Without the skip, a hook's transaction that already holds
  the organization in ``KEY SHARE`` would wait on that user, while an erasure of that user
  (user ``FOR UPDATE``, then its organizations ``FOR UPDATE``) waits on the organization: a
  cycle. The cost is that such a recipient misses that one in-app notification.
* The **organization** row is always already held by the triggering transaction: a decision's
  step 2; a brief's or draft's own foreign key; for ``export.completed``, the ``KEY SHARE`` the
  export takes on it before its checks (:func:`app.exports.service._lock_for_export`; an
  export's own rows are FK-less on the organization, so without that lock the insert here
  would be the export's first touch of the row, and it could wait on an organization deletion
  that waits on the export's draft).
* The **workspace** row is already held by a brief's or a draft's own foreign key, or by
  generation's ``NO KEY UPDATE``. For a decision and for an export (whose rows are FK-less on
  the workspace) it is new: it can wait only for a capability-override or schedule-cap
  mutation (``FOR UPDATE`` on the workspace). Neither waits on any row a decision or an export
  holds, so no cycle of two transactions forms. Organization deletion locks the organization
  first, which the decision or the export already holds.
* **Known limit, three transactions (AD-NOTIF-LOCK-01).** The no-cycle argument above is for
  two transactions. Three can wait in a ring through the workspace row: a schedule-cap or
  override write (the workspace ``FOR UPDATE``, then its audit row's foreign key on the
  acting user's ``users`` row); that same user's organization deletion or account deletion
  (that ``users`` row ``FOR UPDATE``, then the organization); and a decision or an export
  emitting a notification (the organization ``KEY SHARE``, then the workspace ``KEY SHARE``
  here). PostgreSQL then aborts at least one of the three with a deadlock error. This is open:
  nothing here prevents it.
On SQLite every lock here compiles to nothing, the skip included. SQLite's single writer lock
serializes two transactions only from the first write of each: a decision emits after its own
row is inserted, and an export after it has taken the writer lock before its checks
(:func:`app.db.sqlite_writer.hold_sqlite_writer_lock`), so both already hold it here.

Steps 1-3 are the caller's contract. A breach raises :class:`NotificationContractError`
whatever the capability says, so a hook passing the wrong ids fails in the dark state too,
where a test can see it. A breach is a programming error, never a customer answer. In-app
only: nothing here sends mail, SMS, push or any other message.

**The reads and writes behind the routes** (:mod:`app.notifications.routes`). Each takes the
session user's id from the route (the tenant context), never from the client:

* :func:`list_feed` returns one user's notifications in one workspace, newest first, without
  soft-deleted rows (P2), with the total and unread counts of that same set.
* :func:`mark_read` matches on the id, the workspace **and** the session user, so a miss,
  including another user's notification, is the uniform 404 (the cross-user IDOR class, 5E
  exit criterion 5). It is idempotent: a conditional ``UPDATE … WHERE read_at IS NULL`` keeps
  the first ``read_at``, even when two requests race.
* :func:`get_preferences` answers every event code (no row: delivered);
  :func:`update_preferences` upserts the given codes and leaves the others as they are.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.approvals.models import Approval
from app.briefs.models import RecommendationBrief
from app.capabilities.registry import Capability
from app.capabilities.resolver import CapabilityResolution, resolve_capability
from app.core.config import Settings, get_settings
from app.core.enums import NOTIFICATION_SUBJECT_TABLES, NotificationEventCode
from app.core.errors import NotFoundError
from app.core.logging import get_logger, log_event
from app.creative.models import ContentReview, CreativeDraft
from app.db.base import utcnow
from app.exports.models import Export
from app.notifications.interface import NotificationEmitter
from app.notifications.models import Notification, NotificationPreference
from app.organizations.models import OrganizationMember, User, Workspace

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


def _lock_recipient_rows(db: Session, recipients: list[str]) -> list[str]:
    """The recipients whose ``users`` row this transaction now holds ``KEY SHARE``, in order.

    ``FOR KEY SHARE SKIP LOCKED``: the lock the notification row's foreign key takes anyway,
    taken first and without waiting. A row held ``FOR UPDATE`` elsewhere is skipped, so is
    its recipient (module docstring, "Locks"). A row this transaction already holds is never
    skipped.
    """
    if not recipients:
        return []
    held = set(
        db.scalars(
            select(User.id)
            .where(User.id.in_(recipients))
            .with_for_update(read=True, key_share=True, skip_locked=True)
        )
    )
    return [user_id for user_id in recipients if user_id in held]


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
    deliverable = _deliverable(
        db,
        organization_id=organization_id,
        workspace_id=workspace_id,
        event_code=code,
        candidates=candidates,
    )
    recipients = _lock_recipient_rows(db, deliverable)
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
        skipped_busy=len(deliverable) - len(recipients),
        written=len(recipients),
    )
    return len(recipients)


#: The implementation, typed as the frozen Protocol so a signature drift is a type error.
emitter: NotificationEmitter = emit_notifications


# --------------------------------------------------------------------------- #
# Feed, mark-read and preferences (the session user's own rows only)
# --------------------------------------------------------------------------- #
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

#: The one message for every notification miss: unknown, foreign, another user's or deleted.
NOTIFICATION_NOT_FOUND = "Notification not found."


def _own_live_rows(workspace_id: str, user_id: str) -> tuple[Any, ...]:
    """The predicate of every feed read: this user's live rows in this workspace."""
    return (
        Notification.workspace_id == workspace_id,
        Notification.user_id == user_id,
        Notification.deleted_at.is_(None),
    )


def list_feed(
    db: Session, *, workspace_id: str, user_id: str, limit: int, offset: int
) -> tuple[list[Notification], int, int]:
    """One page of the user's feed, newest first, with the total and unread counts."""
    own = _own_live_rows(workspace_id, user_id)
    rows = list(
        db.scalars(
            select(Notification)
            .where(*own)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    total = db.scalar(select(func.count()).select_from(Notification).where(*own)) or 0
    unread = (
        db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(*own, Notification.read_at.is_(None))
        )
        or 0
    )
    return rows, total, unread


def mark_read(
    db: Session, *, workspace_id: str, user_id: str, notification_id: str
) -> Notification:
    """Mark one of the user's notifications read; idempotent; the first ``read_at`` is kept.

    Raises :class:`~app.core.errors.NotFoundError` (404, one message) unless ``notification_id``
    is a live row of this user in this workspace.
    """
    mine = (Notification.id == notification_id, *_own_live_rows(workspace_id, user_id))
    row = db.scalar(select(Notification).where(*mine))
    if row is None:
        raise NotFoundError(NOTIFICATION_NOT_FOUND)
    if row.read_at is None:
        db.execute(
            update(Notification)
            .where(*mine, Notification.read_at.is_(None))
            .values(read_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        # Whichever request set it first, this reads the stored value back.
        db.refresh(row)
    return row


def get_preferences(
    db: Session, *, workspace_id: str, user_id: str
) -> list[tuple[NotificationEventCode, bool]]:
    """Every event code with whether it is delivered to this user in this workspace."""
    stored = {
        row.event_code: row.enabled
        for row in db.scalars(
            select(NotificationPreference).where(
                NotificationPreference.workspace_id == workspace_id,
                NotificationPreference.user_id == user_id,
            )
        )
    }
    return [(code, stored.get(code.value, True)) for code in NotificationEventCode]


def _set_preference(
    db: Session,
    *,
    organization_id: str,
    workspace_id: str,
    user_id: str,
    event_code: NotificationEventCode,
    enabled: bool,
) -> None:
    stmt = select(NotificationPreference).where(
        NotificationPreference.workspace_id == workspace_id,
        NotificationPreference.user_id == user_id,
        NotificationPreference.event_code == event_code.value,
    )
    row = db.scalar(stmt)
    if row is None:
        try:
            with db.begin_nested():
                db.add(
                    NotificationPreference(
                        organization_id=organization_id,
                        workspace_id=workspace_id,
                        user_id=user_id,
                        event_code=event_code.value,
                        enabled=enabled,
                    )
                )
                db.flush()
            return
        except IntegrityError:
            # A concurrent request inserted the same (workspace, user, event) first. The
            # savepoint is rolled back; update the row it wrote (the capabilities upsert's
            # backstop).
            row = db.scalar(stmt)
            if row is None:  # pragma: no cover - the constraint fired but the row is absent
                raise
    row.enabled = enabled


def update_preferences(
    db: Session,
    *,
    organization_id: str,
    workspace_id: str,
    user_id: str,
    changes: Mapping[NotificationEventCode, bool],
) -> list[tuple[NotificationEventCode, bool]]:
    """Set the given event codes for this user in this workspace; answer every code."""
    for event_code, enabled in changes.items():
        _set_preference(
            db,
            organization_id=organization_id,
            workspace_id=workspace_id,
            user_id=user_id,
            event_code=NotificationEventCode(event_code),
            enabled=enabled,
        )
    db.flush()
    return get_preferences(db, workspace_id=workspace_id, user_id=user_id)


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "NOTIFICATION_NOT_FOUND",
    "NotificationContractError",
    "emit_notifications",
    "emitter",
    "get_preferences",
    "list_feed",
    "mark_read",
    "notifications_analytics_capability",
    "update_preferences",
]
