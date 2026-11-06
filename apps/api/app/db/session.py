"""Engine + session factory. SQLite (local) or PostgreSQL (full) via settings.

A single process-wide :class:`~sqlalchemy.engine.Engine` is built once from
:class:`~app.core.config.Settings`. The dialect is resolved through SQLAlchemy's
own URL parsing (``Settings.db_backend_name``), never by string matching, and the
SQLite and PostgreSQL branches configure disjoint, dialect-appropriate pools:

* **SQLite** keeps ``check_same_thread=False`` (so worker threads may share the
  connection) plus a ``connect`` listener that enables foreign keys and a short
  busy timeout.
* **PostgreSQL** uses a bounded ``QueuePool`` (``pool_size``/``max_overflow``/
  ``pool_timeout``/``pool_recycle``), ``pool_pre_ping`` to defeat stale
  connections, and a bounded ``connect_timeout`` + non-secret ``application_name``.
  Every session is pinned to UTC at connect and a session that is not in UTC is
  refused (:mod:`app.db.utc_session`), so a ``timestamptz`` always comes back in UTC.

The database URL is treated as secret-bearing: it is never logged, and any
failure surfaced elsewhere reports the exception *class name* only.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.db.utc_session import create_utc_engine


def build_engine(settings: Settings) -> Engine:
    """Create the process engine with dialect-isolated pool configuration.

    SQLite and PostgreSQL receive entirely separate keyword sets so a pool option
    that is meaningful for one dialect is never passed to the other.
    """
    if settings.is_postgres:
        # The session time zone is pinned to UTC ('-c timezone=UTC', after the connect
        # args below) and each new connection is verified: psycopg returns timestamptz in
        # the session's zone, and same-zone datetimes compare by wall time (W21 TZPIN).
        engine = create_utc_engine(
            settings.database_url,
            future=True,
            # Bound parameters never appear in SQLAlchemy's own log lines or error text:
            # a row may hold an operator's private free text (D-9e, review r8 C-r8-3).
            hide_parameters=True,
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            pool_recycle=int(settings.db_pool_recycle_seconds),
            connect_args={
                "connect_timeout": int(settings.db_connect_timeout_seconds),
                "application_name": settings.db_application_name,
            },
        )
        return engine

    # SQLite (and any other non-PostgreSQL dialect) path. check_same_thread is a
    # SQLite-specific connect arg; it is only applied for the sqlite dialect.
    connect_args = {"check_same_thread": False} if settings.is_sqlite else {}
    engine = create_engine(
        settings.database_url,
        future=True,
        hide_parameters=True,
        connect_args=connect_args,
    )
    if settings.is_sqlite:

        @event.listens_for(engine, "connect")
        def _enable_sqlite_fk(dbapi_conn, _):  # pragma: no cover - trivial
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            # Concurrent job workers contend on the same file. A short busy
            # timeout lets a blocked writer wait for the lock instead of failing
            # immediately with "database is locked", so atomic compare-and-set
            # claims are reliable under concurrency (the claim itself stays
            # correct because it is guarded by ``WHERE status = <observed>``).
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


_settings = get_settings()
engine: Engine = build_engine(_settings)

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
