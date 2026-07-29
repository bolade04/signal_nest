"""API startup schema-failure containment tests (Phase 4 Gate 4F).

The schema-compatibility probe (``check_schema_compatibility`` /
``require_startup_schema``) does live database I/O; a raw driver exception
escaping it would reach Uvicorn's startup traceback carrying the DSN. These
tests pin the containment boundary: connection failures surface as a fixed,
secret-free :class:`SchemaVerificationError` with the cause suppressed, while
the positive (compatible) and fail-closed (pending/uninitialized) paths keep
their existing behavior — and the probe never mutates.
"""

from __future__ import annotations

import sqlite3
import traceback

import pytest
from sqlalchemy import create_engine

from alembic import command
from app.core.config import Settings, get_settings
from app.db.schema import (
    SchemaNotReadyError,
    SchemaState,
    SchemaVerificationError,
    alembic_config,
    check_schema_compatibility,
    require_startup_schema,
)

_SENTINEL_HOST = "db-sentinel-host.internal"
_SENTINEL_PASS = "sn-sentinel-p4ssw0rd"


def _boom_engine():
    """An engine whose every connection attempt raises a DSN-bearing error."""

    def _boom():
        raise sqlite3.OperationalError(
            f"connection to server at {_SENTINEL_HOST}, port 5432 failed: "
            f"password authentication failed (postgresql://svc:{_SENTINEL_PASS}@"
            f"{_SENTINEL_HOST}:5432/appdb)"
        )

    return create_engine("sqlite://", creator=_boom)


@pytest.fixture()
def migrated_engine(tmp_path, monkeypatch):
    """A real, fully migrated SQLite engine (real alembic upgrade, no stubs)."""
    db_path = tmp_path / "startup_schema.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    command.upgrade(alembic_config(), "head")
    engine = create_engine(url)
    yield engine
    engine.dispose()
    get_settings.cache_clear()


def test_connection_failure_raises_fixed_safe_error() -> None:
    with pytest.raises(SchemaVerificationError) as ei:
        require_startup_schema(_boom_engine(), settings=Settings())
    exc = ei.value

    # Cause suppressed: nothing for Uvicorn or a default hook to chain-print.
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True

    # Positive: the safe classification is present in the message and in the
    # rendered traceback exactly as Uvicorn would print it.
    rendered = "".join(traceback.format_exception(exc))
    assert "error_class=OperationalError" in str(exc)
    assert "SchemaVerificationError" in rendered

    # Negative: no fragment of the DSN survives anywhere in the rendering.
    assert _SENTINEL_HOST not in rendered
    assert _SENTINEL_PASS not in rendered
    assert "postgresql://" not in rendered


def test_check_schema_compatibility_contains_at_the_choke_point() -> None:
    # The same containment holds for direct callers (migrate.check, tests).
    with pytest.raises(SchemaVerificationError) as ei:
        check_schema_compatibility(_boom_engine())
    assert ei.value.__cause__ is None
    assert ei.value.__suppress_context__ is True


def test_compatible_schema_still_starts(migrated_engine) -> None:
    compat = require_startup_schema(migrated_engine, settings=Settings())
    assert compat.state is SchemaState.COMPATIBLE
    assert compat.db_revision == compat.code_head


def test_pending_schema_fails_closed_without_mutation(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "pending.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    # Real upgrade to an ancestor revision: the database is genuinely behind.
    command.upgrade(alembic_config(), "9a7c614699d8")
    get_settings.cache_clear()

    engine = create_engine(url)
    try:
        with pytest.raises(SchemaNotReadyError, match="behind"):
            require_startup_schema(engine, settings=Settings())
    finally:
        engine.dispose()

    # Verify-never-mutate: the ancestor revision is untouched — no upgrade, no
    # downgrade, no metadata.create_all happened as a side effect.
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT version_num FROM alembic_version").fetchall()
        tables = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        con.close()
    assert rows == [("9a7c614699d8",)]
    assert "workspace_capability_overrides" not in tables  # head table absent


def test_uninitialized_schema_fails_closed(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        with pytest.raises(SchemaNotReadyError, match="not initialized"):
            require_startup_schema(engine, settings=Settings())
    finally:
        engine.dispose()
