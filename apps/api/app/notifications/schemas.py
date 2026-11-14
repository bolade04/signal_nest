"""Request and response schemas of the in-app notification API (Project Phase 5E, 5E-W3).

A notification carries no text, no actor and no other personal data (the model's rule): the
client renders a fixed message per ``event_code`` and links to the subject by ``subject_id``.
No schema carries a user id. Every route acts for the session user only, so the recipient is
never named in a request or an answer.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import NotificationEventCode


class NotificationOut(BaseModel):
    """One of the session user's notifications."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: str
    event_code: NotificationEventCode
    #: The event's subject row (``NOTIFICATION_SUBJECT_TABLES[event_code]``). It may have been
    #: deleted since; its own route then answers 404.
    subject_id: str
    created_at: datetime
    #: ``None`` until the user marks it read; then the first time it was marked.
    read_at: datetime | None


class NotificationFeedOut(BaseModel):
    """One page of the session user's feed in this workspace, newest first."""

    items: list[NotificationOut]
    #: The user's notifications in this workspace (deleted ones excluded), whatever the page.
    total: int
    #: How many of them are unread, whatever the page.
    unread: int
    limit: int
    offset: int


class NotificationPreferenceItem(BaseModel):
    """Whether one event code is delivered to the session user in this workspace."""

    model_config = ConfigDict(extra="forbid")

    event_code: NotificationEventCode
    enabled: bool


class NotificationPreferencesOut(BaseModel):
    """Every event code, in the vocabulary's order (no stored choice: delivered)."""

    items: list[NotificationPreferenceItem]


class NotificationPreferencesUpdate(BaseModel):
    """The event codes to set for the session user in this workspace; others are unchanged."""

    model_config = ConfigDict(extra="forbid")

    items: list[NotificationPreferenceItem] = Field(
        min_length=1, max_length=len(NotificationEventCode)
    )

    @field_validator("items")
    @classmethod
    def _one_entry_per_code(
        cls, items: list[NotificationPreferenceItem]
    ) -> list[NotificationPreferenceItem]:
        codes = [item.event_code for item in items]
        if len(set(codes)) != len(codes):
            raise ValueError("each event code may appear at most once")
        return items


class NotificationsAnalyticsCapabilityOut(BaseModel):
    """Customer-visible availability of notifications and analytics for one workspace.

    Exactly one field (the ``FeedbackCapabilityOut`` precedent): no governance metadata
    (deciding rule, global flag, override) reaches a customer. Returned by
    ``GET /workspaces/{workspace_id}/notifications-analytics-capability`` (O-12(B)).
    """

    enabled: bool


__all__ = [
    "NotificationFeedOut",
    "NotificationOut",
    "NotificationPreferenceItem",
    "NotificationPreferencesOut",
    "NotificationPreferencesUpdate",
    "NotificationsAnalyticsCapabilityOut",
]
