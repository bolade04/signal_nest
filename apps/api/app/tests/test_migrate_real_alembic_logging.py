"""Real-Alembic logging preservation tests (Phase 4 Gate 4F).

These are the falsifiability controls for the caller-controlled Alembic logging
fix. Unlike the unit tests in ``test_migrate_upgrade_verify.py`` (which stub the
Config helper for exit-code dispatch), everything here runs the REAL path:
``app.db.schema.alembic_config()`` → ``alembic.command.upgrade`` → the real
``alembic/env.py`` → the production logging stack installed by
``configure_logging()``.

Anti-vacuity properties enforced:

* If ``alembic_config()`` stops clearing ``config_file_name``, env.py runs
  ``fileConfig()``, which replaces the root handler (stderr/plain/WARNING) and
  disables the ``signalnest.db.migrate`` logger — the positive post-upgrade
  event assertions below then FAIL.
* If application logging were silenced entirely, the same positive assertions
  FAIL before any no-leak assertion is reached.
* If the ini were cleared *before* the lazy parse, the ini-sentinel assertions
  FAIL (the broken ordering is reproduced here as a regression proof).
* ``caplog`` is never used: ``configure_logging()`` replaces the root handler
  list, evicting pytest's capture handler — only real captured stream bytes
  through the real formatter are asserted, captured ONCE per test with both
  streams read from the same capture.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest
from alembic.config import Config

import app.db.migrate as migrate
from alembic import command
from app.core.config import get_settings
from app.core.logging import JsonFormatter, configure_logging
from app.db.schema import _api_root, alembic_config


@pytest.fixture(autouse=True)
def _restore_root_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers = handlers
    root.level = level


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """A fresh SQLite target wired through the real Settings path for env.py."""
    db_path = tmp_path / "real_alembic.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    yield db_path, url
    get_settings.cache_clear()


def _json_events(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Lazy Config ordering pins
# --------------------------------------------------------------------------- #
def test_helper_clears_filename_and_preserves_ini_options() -> None:
    cfg = alembic_config()
    # Both halves are required: the cleared filename (env.py skips fileConfig)
    # AND the ini-derived options surviving the clear (lazy parse ran first).
    # Options are compared for EQUALITY against a pristine parse of the real
    # ini (not merely truthiness), so forging a plausible sentinel is not
    # enough — the whole parsed configuration must have survived.
    pristine = Config(str(_api_root() / "alembic.ini"))
    assert cfg.config_file_name is None
    assert cfg.get_main_option("prepend_sys_path") == "."
    assert cfg.get_main_option("path_separator") == "os"
    assert cfg.get_main_option("file_template") == pristine.get_main_option(
        "file_template"
    )
    assert cfg.get_section("post_write_hooks") == pristine.get_section(
        "post_write_hooks"
    )
    assert cfg.get_section("post_write_hooks")  # and genuinely non-empty


def test_broken_ordering_is_distinguishable() -> None:
    """Regression proof: clearing before the lazy parse silently loses the ini.

    This is what makes the sentinel assertion above non-vacuous — a helper that
    cleared ``config_file_name`` first would still upgrade successfully, but
    this test pins that the sentinel it loses is exactly what we assert on.
    """
    ini = str(_api_root() / "alembic.ini")

    pristine = Config(ini)
    assert pristine.get_main_option("prepend_sys_path") == "."

    broken = Config(ini)
    broken.config_file_name = None  # cleared BEFORE any read: parse never runs
    assert broken.get_main_option("prepend_sys_path") is None


# --------------------------------------------------------------------------- #
# Real env.py execution
# --------------------------------------------------------------------------- #
def test_real_env_py_executes_against_helper_config(temp_db) -> None:
    db_path, url = temp_db
    cfg = alembic_config()
    command.upgrade(cfg, "head")

    # env.py mutates the CALLER's Config (injects sqlalchemy.url from Settings)
    # — impossible if command.upgrade or env.py were stubbed.
    assert cfg.get_main_option("sqlalchemy.url") == url
    # And the real upgrade wrote the version table to the real file.
    from app.db.schema import code_head_revision

    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT version_num FROM alembic_version").fetchall()
    finally:
        con.close()
    assert rows == [(code_head_revision(),)]


# --------------------------------------------------------------------------- #
# Structured logging survives the real upgrade (the core fix)
# --------------------------------------------------------------------------- #
def test_real_upgrade_preserves_structured_logging(temp_db, capsys) -> None:
    db_path, url = temp_db
    configure_logging("INFO", log_format="json")

    rc = migrate.upgrade_and_verify()

    cap = capsys.readouterr()  # exactly one capture; both streams asserted below
    assert rc == migrate.EXIT_OK

    events = _json_events(cap.out)
    names = [e.get("event") for e in events]

    # POSITIVE emission first: the pre-upgrade event AND the post-upgrade
    # success event (the one the fileConfig clobbering used to destroy).
    assert "migrate.upgrade.start" in names
    done = [e for e in events if e.get("event") == "migrate.upgrade_verify.done"]
    assert len(done) == 1
    # Provenance fields, independently sourced and matching.
    assert done[0]["code_head"]
    assert done[0]["db_revision"] == done[0]["code_head"]
    # And the version table really carries that revision (real env.py proof).
    con = sqlite3.connect(db_path)
    try:
        db_rows = con.execute("SELECT version_num FROM alembic_version").fetchall()
    finally:
        con.close()
    assert db_rows == [(done[0]["db_revision"],)]

    # Alembic's own records now flow through the application JSON handler.
    assert any(e.get("logger") == "alembic.runtime.migration" for e in events)

    # The application logging stack survived the real env.py execution.
    root = logging.getLogger()
    assert root.level == logging.INFO
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    assert logging.getLogger("signalnest.db.migrate").disabled is False

    # No plain-format Alembic output on stderr; no traceback anywhere.
    assert "[alembic.runtime.migration]" not in cap.err
    assert cap.err.strip() == ""
    assert "Traceback" not in cap.out and "Traceback" not in cap.err


def test_post_upgrade_failure_event_is_emitted(temp_db, capsys) -> None:
    """Failure classifications must be observable (dead zone on the baseline).

    A corrupted version table (second row) makes the real ``command.upgrade``
    itself fail — the fail-closed band is ``EXIT_UPGRADE_FAILED``, and on the
    broken baseline its ``migrate.upgrade.failed`` event was destroyed by
    ``fileConfig()`` (env.py executes before the failure surfaces). This test
    fails if that event is silenced again.
    """
    db_path, url = temp_db
    # Build the schema first (quietly), then corrupt the version table.
    command.upgrade(alembic_config(), "head")
    con = sqlite3.connect(db_path)
    try:
        con.execute("INSERT INTO alembic_version (version_num) VALUES ('aaaaaaaaaaaa')")
        con.commit()
    finally:
        con.close()

    configure_logging("INFO", log_format="json")
    rc = migrate.upgrade_and_verify()

    cap = capsys.readouterr()  # single capture
    assert rc == migrate.EXIT_UPGRADE_FAILED

    events = _json_events(cap.out)
    failed = [e for e in events if e.get("event") == "migrate.upgrade.failed"]
    assert len(failed) == 1  # positive: the classification actually emitted
    assert failed[0]["error_class"]  # class name only
    assert failed[0]["outcome"] == "failure"
    # No secrets, no traceback, on either stream.
    blob = cap.out + cap.err
    assert "Traceback" not in blob
    assert str(db_path) not in blob  # the database location never appears


def test_no_sqlalchemy_engine_records_even_at_debug(temp_db, capsys) -> None:
    """The ini's ``[logger_sqlalchemy] WARNING`` pin is gone with fileConfig;
    SQLAlchemy's own default (WARN when unset) must keep SQL off stdout."""
    configure_logging("DEBUG", log_format="json")

    rc = migrate.upgrade_and_verify()

    cap = capsys.readouterr()  # single capture
    assert rc == migrate.EXIT_OK
    events = _json_events(cap.out)
    # Positive control: the run did emit (DEBUG-level capture is live).
    assert any(e.get("event") == "migrate.upgrade_verify.done" for e in events)
    # No sqlalchemy.engine records, no raw SQL text.
    assert not any(
        str(e.get("logger", "")).startswith("sqlalchemy") for e in events
    )
    blob = cap.out + cap.err
    assert "CREATE TABLE" not in blob
    assert "[parameters:" not in blob


# --------------------------------------------------------------------------- #
# ``check`` connection-failure containment (fixed event, no traceback)
# --------------------------------------------------------------------------- #
def test_check_connection_failure_is_safe(temp_db, capsys, monkeypatch) -> None:
    import sqlite3 as _sqlite3

    from sqlalchemy import create_engine

    import app.db.session as session_mod

    sentinel = "postgresql://svc_user:sn-sentinel-p4ss@db-sentinel-host:5432/appdb"

    def _boom():
        raise _sqlite3.OperationalError(f"connection failed for {sentinel}")

    monkeypatch.setattr(
        session_mod, "engine", create_engine("sqlite://", creator=_boom)
    )
    configure_logging("INFO", log_format="json")

    rc = migrate.check()

    cap = capsys.readouterr()  # single capture
    assert rc == 1  # fail-closed
    events = _json_events(cap.out)
    # Positive: the fixed classification event actually emitted.
    assert any(e.get("event") == "migrate.check.unverifiable" for e in events)
    blob = cap.out + cap.err
    assert "sn-sentinel-p4ss" not in blob
    assert "db-sentinel-host" not in blob
    assert "Traceback" not in blob
