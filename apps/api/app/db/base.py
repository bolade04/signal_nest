"""Declarative base and shared column/mixin helpers.

Models are written to work on both SQLite (local mode) and PostgreSQL (full mode).
We use portable column types (String UUID, JSON, Text) so a single model set targets
both engines. There is no vector column type: embeddings are JSON, and similarity is
in-process per-run similarity, bound N_max = 100 signals per run (engineering capacity
bound, not a quota); no cross-run or history-wide similarity; pgvector refused until
implemented (P6-LLM-7 deferred); B-5 not applicable while no pgvector path exists
(P6-PLAT-1, operator decision 2026-10-27).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )


class CreatedAtMixin:
    """``created_at`` only, for append-only tables (Project Phase 5, W21 CF-1).

    A deliberate departure from :class:`TimestampMixin`: an append-only row is never
    updated, so it carries no ``updated_at`` and no ``onupdate`` hook (Phase 5A plan §2).
    Pair it with :func:`app.db.append_only.append_only`, which makes an ORM update raise.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )


class UUIDPrimaryKeyMixin:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_uuid)
