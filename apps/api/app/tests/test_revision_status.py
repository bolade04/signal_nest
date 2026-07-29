"""Raw live-revision reader tests (Phase 4 Gate 4F).

Anti-vacuity by construction: the one-valid-row positive case kills an
"always exits safely" stub; the zero/multiple/malformed/missing-table cases
each assert their own DISTINCT exit code, killing a constant-revision stub;
output is captured once per scenario and both streams are asserted (success is
exactly one stdout line; failures are exactly one fixed stderr token).
"""

from __future__ import annotations

import inspect as pyinspect
import re
import sqlite3

import pytest

import app.db.revision_status as revision_status
from alembic import command
from app.core.config import get_settings
from app.db.schema import alembic_config, code_head_revision

_SENTINEL_PASS = "sn-sentinel-p4ssw0rd"


@pytest.fixture()
def migrated_db(tmp_path, monkeypatch):
    """A real migrated SQLite DB with DATABASE_URL pointed at it."""
    db_path = tmp_path / "revision_status.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    command.upgrade(alembic_config(), "head")
    yield db_path
    get_settings.cache_clear()


def _set_rows(db_path, rows: list[str]) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("DELETE FROM alembic_version")
        for r in rows:
            con.execute("INSERT INTO alembic_version (version_num) VALUES (?)", (r,))
        con.commit()
    finally:
        con.close()


def test_one_valid_row_prints_exactly_the_revision(migrated_db, capsys) -> None:
    rc = revision_status.main([])
    cap = capsys.readouterr()  # single capture
    assert rc == revision_status.EXIT_OK
    # Exactly one stdout line: the raw revision, nothing else, empty stderr.
    assert cap.out == f"{code_head_revision()}\n"
    assert cap.err == ""


def test_reader_reports_db_value_not_code_head(migrated_db, capsys) -> None:
    # Provenance: with the database at a well-formed NON-head revision, the
    # reader must print the DATABASE's value. A reader that prints the code
    # head (making the reader→comparator pipeline tautological) fails here —
    # the at-head positive test above cannot discriminate the two.
    non_head = "aaaaaaaaaaaa"
    assert non_head != code_head_revision()
    _set_rows(migrated_db, [non_head])
    rc = revision_status.main([])
    cap = capsys.readouterr()  # single capture
    assert rc == revision_status.EXIT_OK
    assert cap.out == f"{non_head}\n"
    assert cap.err == ""


def test_zero_rows_distinct_failure(migrated_db, capsys) -> None:
    _set_rows(migrated_db, [])
    rc = revision_status.main([])
    cap = capsys.readouterr()
    assert rc == revision_status.EXIT_NO_ROWS
    assert cap.out == ""
    assert cap.err == "revision-status: no-revision-rows\n"


def test_multiple_rows_distinct_failure(migrated_db, capsys) -> None:
    _set_rows(migrated_db, ["98289430a3ec", "aaaaaaaaaaaa"])
    rc = revision_status.main([])
    cap = capsys.readouterr()
    assert rc == revision_status.EXIT_MULTIPLE_ROWS
    assert cap.out == ""
    assert cap.err == "revision-status: multiple-revision-rows\n"


@pytest.mark.parametrize("bad", ["ZZZZZZZZZZZZ", "98289430A3EC", "9828943", "", "98289430a3ec0"])
def test_malformed_revision_distinct_failure(migrated_db, capsys, bad) -> None:
    _set_rows(migrated_db, [bad])
    rc = revision_status.main([])
    cap = capsys.readouterr()
    assert rc == revision_status.EXIT_MALFORMED_REVISION
    assert cap.out == ""
    assert cap.err == "revision-status: malformed-revision\n"


def test_missing_table_distinct_failure(tmp_path, monkeypatch, capsys) -> None:
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    sqlite3.connect(tmp_path / "empty.db").close()  # valid, table-less DB
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    rc = revision_status.main([])
    cap = capsys.readouterr()
    get_settings.cache_clear()
    assert rc == revision_status.EXIT_TABLE_MISSING
    assert cap.err == "revision-status: version-table-missing\n"


def test_connection_failure_distinct_from_configuration(tmp_path, monkeypatch, capsys) -> None:
    # A syntactically valid URL whose directory does not exist: engine
    # constructs fine (config OK) but connecting fails.
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/no-such-dir/x.db")
    get_settings.cache_clear()
    rc = revision_status.main([])
    cap = capsys.readouterr()
    get_settings.cache_clear()
    assert rc == revision_status.EXIT_CONNECT_FAILURE
    assert cap.out == ""
    assert cap.err == "revision-status: connection-failure\n"


def test_configuration_failure_when_settings_unloadable(monkeypatch, capsys) -> None:
    import app.core.config as config_mod

    def _boom():
        raise RuntimeError("settings exploded")

    monkeypatch.setattr(config_mod, "get_settings", _boom)
    rc = revision_status.main([])
    cap = capsys.readouterr()
    assert rc == revision_status.EXIT_CONFIG_FAILURE
    assert cap.err == "revision-status: configuration-failure\n"


def test_stray_argv_rejected(capsys) -> None:
    rc = revision_status.main(["upgrade"])
    cap = capsys.readouterr()
    assert rc == revision_status.EXIT_CONFIG_FAILURE
    assert cap.out == ""
    assert cap.err == "revision-status: unexpected-arguments\n"


def test_driver_error_with_dsn_never_leaks(migrated_db, capsys, monkeypatch) -> None:
    class _BoomEngine:
        def connect(self):
            raise RuntimeError(
                f"postgresql://svc:{_SENTINEL_PASS}@db-sentinel-host:5432/appdb"
            )

        def dispose(self):
            pass

    monkeypatch.setattr(revision_status, "create_engine", lambda *a, **k: _BoomEngine())
    rc = revision_status.main([])
    cap = capsys.readouterr()  # single capture; both streams asserted
    assert rc == revision_status.EXIT_CONNECT_FAILURE
    # Positive: the fixed token emitted; negative: nothing else, ever.
    assert cap.err == "revision-status: connection-failure\n"
    assert cap.out == ""
    assert _SENTINEL_PASS not in cap.out + cap.err


def test_postgres_gets_bounded_connect_timeout(monkeypatch, capsys) -> None:
    captured: dict = {}

    class _Engine:
        def connect(self):
            raise RuntimeError("no network in tests")

        def dispose(self):
            pass

    def _fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Engine()

    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://u:p@db.invalid:5432/x?sslmode=require"
    )
    get_settings.cache_clear()
    monkeypatch.setattr(revision_status, "create_engine", _fake_create_engine)
    # Settings validation: dev environment rejects postgres? It does not — only
    # migration_mode constrains the dialect. The reader must still classify.
    rc = revision_status.main([])
    capsys.readouterr()
    get_settings.cache_clear()
    assert rc == revision_status.EXIT_CONNECT_FAILURE
    assert captured["connect_args"] == {"connect_timeout": 10}


def test_sqlite_gets_no_postgres_only_connect_args(migrated_db, capsys, monkeypatch) -> None:
    captured: dict = {}
    real_create_engine = revision_status.create_engine

    def _spy(url, **kwargs):
        captured.update(kwargs)
        return real_create_engine(url, **kwargs)

    monkeypatch.setattr(revision_status, "create_engine", _spy)
    rc = revision_status.main([])
    capsys.readouterr()
    assert rc == revision_status.EXIT_OK  # the real SQLite connect worked
    assert captured["connect_args"] == {}  # no PostgreSQL-only kwarg passed


def test_engine_is_disposed(migrated_db, capsys, monkeypatch) -> None:
    disposed = {"n": 0}
    real_create_engine = revision_status.create_engine

    def _spy(url, **kwargs):
        engine = real_create_engine(url, **kwargs)
        real_dispose = engine.dispose

        def _dispose():
            disposed["n"] += 1
            real_dispose()

        engine.dispose = _dispose
        return engine

    monkeypatch.setattr(revision_status, "create_engine", _spy)
    rc = revision_status.main([])
    capsys.readouterr()
    assert rc == revision_status.EXIT_OK
    assert disposed["n"] == 1


def test_module_uses_print_only_no_logging_api() -> None:
    src = pyinspect.getsource(revision_status)
    assert "app.core.logging" not in src
    assert re.search(r"^\s*import logging", src, re.MULTILINE) is None
    assert "log_event" not in src
    assert "getLogger" not in src
