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
    says, and its signals have been purged. The row is a **suppression record only**
    (D-9d): the source key, when it was taken down, and aggregate counts —
    ``purge_summary`` holds the counts of the first purge (what the takedown removed)
    and is never overwritten; each repeated takedown appends ``{at, purged}`` to
    ``repeat_purges``. It carries no user id, no free text and no reference: who took
    the source down, and the public request id, are recorded in each affected
    workspace's audit log (``data_source.taken_down``) under the audit log's own
    retention, so anonymising or expiring that metadata never touches this row and
    cannot re-enable the source. Re-onboarding the publisher means a new source record
    with a new key and a fresh approval.
    """

    __tablename__ = "source_takedowns"

    source_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    taken_down_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    #: Counts only (``raw_signals``, ``normalized_signals``, ...): the first purge.
    purge_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    #: ``[{"at": <ISO timestamp>, "purged": {<count name>: <int>, ...}}, ...]``.
    repeat_purges: Mapped[list] = mapped_column(JSON, default=list)


class SourceTakedownCase(Base, UUIDPrimaryKeyMixin):
    """The restricted operator case record of one takedown request (D-9e).

    One row per takedown request (a repeat creates a new row). ``id`` is the opaque case
    reference: it is the only thing a customer-visible audit entry carries
    (``context.case_id``). ``reason`` (the operator's free text) and
    ``external_reference`` (the operator-typed ticket id) are **private**: they are read
    only through the operator-only ``GET /internal/system/data-sources/{key}/cases``
    route, are never copied into an audit entry, the registry listing, an export, a log
    line or a metric, and the table is not exported (``exported=False``).

    The access and retention policy of this record is separately approved and still
    pending: no retention duration is set here. ``actor_user_id`` is ``SET NULL`` when
    the actor's account is erased. Deleting the parent suppression record cascades, but
    nothing in the product deletes a suppression record.
    """

    __tablename__ = "source_takedown_cases"

    takedown_id: Mapped[str] = mapped_column(
        ForeignKey("source_takedowns.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_key: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    external_reference: Mapped[str | None] = mapped_column(String(200))
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
