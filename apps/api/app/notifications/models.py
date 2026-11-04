"""Persistence models for in-app notifications (Project Phase 5E; frozen at W21 CF-3).

**:class:`Notification`** — one in-app notification for one user (Phase 5E plan §2):

* **Fully tenant-scoped, per user.** ``organization_id``, ``workspace_id`` and ``user_id`` are
  real foreign keys, all ``CASCADE``: a notification goes with its organization, its
  workspace and its recipient's account. A user reads only their own notifications — the
  cross-*user* IDOR class the 5E routes must refuse with the non-enumerating 404.
* ``event_code`` is the closed :class:`~app.core.enums.NotificationEventCode` vocabulary
  (``ck_notification_event_code``); ``subject_id`` names the event's subject row in the table
  :data:`~app.core.enums.NOTIFICATION_SUBJECT_TABLES` gives for that code. It is not a foreign
  key: a notification outlives neither its recipient nor its workspace, but it may outlive
  its subject, whose link then answers 404. No text, no actor and no other personal data
  is stored.
* ``read_at`` is set by mark-read, which must be idempotent (5E exit criterion 5).
  ``deleted_at`` is the soft-delete marker of the 5E retention mechanism (durations:
  O-5). Not append-only: both are updated after insert.
* ``ix_notifications_feed`` serves the feed read: one user's notifications in one workspace,
  newest first.

**:class:`NotificationPreference`** — one user's opt-out choice for one event code in one
workspace (unique on ``(workspace_id, user_id, event_code)``). No row means the event is
delivered (in-app); a row with ``enabled = False`` suppresses it. Same three cascading scope
keys.

Nothing writes or reads these tables yet. The emission seam the 5A/5B/5C/5E services will call
is :mod:`app.notifications.interface`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import NotificationEventCode
from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin


def event_code_sql() -> str:
    rendered = ", ".join(f"'{v}'" for v in sorted(e.value for e in NotificationEventCode))
    return f"event_code IN ({rendered})"


class Notification(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One in-app notification addressed to one user in one workspace."""

    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(event_code_sql(), name="ck_notification_event_code"),
        Index("ix_notifications_feed", "workspace_id", "user_id", "created_at"),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: The recipient.
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event_code: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The event's subject row (see :data:`app.core.enums.NOTIFICATION_SUBJECT_TABLES`).
    subject_id: Mapped[str] = mapped_column(String(32), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Soft-delete marker of the 5E retention mechanism.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationPreference(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One user's per-event opt-out in one workspace (no row: delivered)."""

    __tablename__ = "notification_preferences"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "user_id", "event_code", name="uq_notification_preference"
        ),
        CheckConstraint(
            event_code_sql(), name="ck_notification_preference_event_code"
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event_code: Mapped[str] = mapped_column(String(32), nullable=False)
    #: ``False`` suppresses the event for this user in this workspace.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)


__all__ = ["Notification", "NotificationPreference", "event_code_sql"]
