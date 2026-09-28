"""Database-clock primitives shared by every module that decides time on the database.

A leaf module: it imports only ``datetime`` and SQLAlchemy, so the request-authentication
dependency, the auth service and the organization services can all use it without an
import cycle (``app.organizations`` imports ``app.auth.dependencies`` at module level).
Both helpers moved here verbatim from ``app.organizations.members`` (``as_utc``) and
``app.organizations.invitations`` (``database_now``), which import them back so every
existing caller and ``__all__`` keep working.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session


def as_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime.

    SQLite hands back naive datetimes; every value this package writes is UTC, so a
    naive value is UTC by construction.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def database_now(db: Session) -> datetime:
    """The database's current time, as an aware UTC datetime.

    PostgreSQL's ``now()`` is the transaction's start time, so every reading in one
    transaction agrees. SQLite's ``CURRENT_TIMESTAMP`` has whole-second resolution,
    so the millisecond form of ``'now'`` is read instead; SQLite returns it as naive
    UTC text.
    """
    if db.get_bind().dialect.name == "sqlite":
        text_value = db.scalar(select(func.strftime("%Y-%m-%d %H:%M:%f", "now")))
        return as_utc(datetime.fromisoformat(text_value))
    return as_utc(db.scalar(select(func.now())))


__all__ = ["as_utc", "database_now"]
