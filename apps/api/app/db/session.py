"""Engine + session factory. SQLite (local) or PostgreSQL (full) via settings."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()

_connect_args = {"check_same_thread": False} if _settings.is_sqlite else {}
engine: Engine = create_engine(
    _settings.database_url,
    connect_args=_connect_args,
    pool_pre_ping=not _settings.is_sqlite,
    future=True,
)


if _settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_conn, _):  # pragma: no cover - trivial
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        # Concurrent job workers contend on the same file. A short busy timeout
        # lets a blocked writer wait for the lock instead of failing immediately
        # with "database is locked", so atomic compare-and-set claims are
        # reliable under concurrency (the claim itself stays correct because it
        # is guarded by ``WHERE status = <observed>``).
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a scoped session."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
