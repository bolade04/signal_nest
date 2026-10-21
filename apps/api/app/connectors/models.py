"""Operator takedown record for a live source (P6-PRIV-4)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow


class SourceTakedown(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A source key an operator has taken down. Permanent for that key.

    While a row exists the source is never fetched, whatever its registry record
    says, and its signals have been purged (``purge_summary`` holds the counts of the
    latest purge). Re-onboarding the publisher means a new source record with a new
    key and a fresh approval.
    """

    __tablename__ = "source_takedowns"

    source_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    #: A public ticket / request id for the takedown (never a person's address).
    reference: Mapped[str | None] = mapped_column(String(200))
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    taken_down_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    purge_summary: Mapped[dict] = mapped_column(JSON, default=dict)
