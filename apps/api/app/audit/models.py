"""Audit log model for sensitive actions.

Append-only (Project Phase 5B audit spine, W21 CF-1): the ORM refuses to update or delete
a row (:func:`app.db.append_only.append_only`), and the only constructor is
:func:`app.audit.service.record_audit`, which admits only the closed
:class:`app.audit.actions.AuditAction` registry. See :mod:`app.audit.service` for the
enforcement level (application layer; database-level immutability is a separate
infrastructure change, O-10).

**Why ``updated_at`` is retained (Phase 5B plan §2 asks for the drop or this record).**
``audit_logs`` inherits ``updated_at`` with ``onupdate`` from :class:`TimestampMixin`.
It is kept, deliberately, because the table is *not* write-once: Phase 6 privacy
erasure (P6-PRIV-1) anonymises audit records in place with Core ``UPDATE`` statements
when an account is erased or an organization deleted (O-6), and SQLAlchemy applies the
column's ``onupdate`` to those statements. ``updated_at`` therefore records when a record
was last anonymised; on a record never anonymised it keeps its insert-time value.
No other path changes it: the ORM refuses updates. Dropping the column would also rebuild
the largest compliance table on SQLite for no protective gain.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.append_only import append_only
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


@append_only
class AuditLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "audit_logs"

    organization_id: Mapped[str] = mapped_column(index=True, nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    previous_state: Mapped[dict | None] = mapped_column(JSON)
    new_state: Mapped[dict | None] = mapped_column(JSON)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
