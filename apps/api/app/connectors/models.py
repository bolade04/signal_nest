"""Operator takedown record for a live source (P6-PRIV-4)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
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
