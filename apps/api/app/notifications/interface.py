"""The notification emission interface (Project Phase 5E; frozen at W21 CF-3).

Phase 5E plan §2/§5: the emission hooks in the brief, approval and creative services
(5E-W3) and the ``export.completed`` emission in the export service (5E-W1) code against
this interface, which 5E-W3 implements early in its window. It is a contract only: no
implementation exists at CF-3 and nothing calls it.

Binding semantics for any implementation:

* **Same transaction.** It writes through the caller's ``Session`` and flushes at most; it
  never commits. The notification commits with the triggering event, or not at all.
* **In-app only.** It inserts :class:`~app.notifications.models.Notification` rows and does
  nothing else: no mail, SMS, push, webhook or other delivery channel, now or behind a flag.
* **Tenant-bound recipients.** Every recipient must be a member of ``organization_id``; a
  recipient who is not is skipped, never notified across tenants. ``workspace_id`` must
  belong to ``organization_id``.
* **Preferences.** A recipient with a ``notification_preferences`` row for
  ``(workspace_id, user_id, event_code)`` whose ``enabled`` is ``False`` is skipped.
* **Closed vocabulary.** ``event_code`` is a :class:`~app.core.enums.NotificationEventCode`
  and ``subject_id`` names a row of the table
  :data:`~app.core.enums.NOTIFICATION_SUBJECT_TABLES` gives for it, in ``workspace_id``.
* Returns the number of notification rows written.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from sqlalchemy.orm import Session

from app.core.enums import NotificationEventCode


class NotificationEmitter(Protocol):
    """Write in-app notifications for one event, in the caller's transaction."""

    def __call__(
        self,
        db: Session,
        *,
        organization_id: str,
        workspace_id: str,
        recipient_user_ids: Iterable[str],
        event_code: NotificationEventCode,
        subject_id: str,
    ) -> int: ...


__all__ = ["NotificationEmitter"]
