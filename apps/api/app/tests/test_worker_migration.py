"""Worker-registry migration test (Phase 3A.4a).

Exercises the additive ``worker_registrations`` migration against a throwaway
SQLite database via the real Alembic CLI (matching how CI validates migrations):

* **upgrade** to head creates the table and its indexes,
* **check** reports no drift between the ORM models and the migrations,
* **downgrade** one step surgically drops only the worker table while preserving
  all pre-existing business/job data,
* **re-upgrade** restores the table.

Alembic reads ``DATABASE_URL`` from the environment (via ``get_settings()``), so
each step runs as a subprocess with that variable pointed at the temp DB.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# apps/api (holds alembic.ini) — three levels up from this test file.
API_DIR = Path(__file__).resolve().parents[2]
HEAD = "d4f6a8c0b2e1"
PREV = "c3e5a7b9d1f2"


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
    )


def _tables(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


def _indexes(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


@pytest.fixture()
def db_path(tmp_path) -> Path:
    return tmp_path / "migration.db"


def test_upgrade_creates_worker_table_and_indexes(db_path) -> None:
    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    tables = _tables(db_path)
    assert "worker_registrations" in tables
    indexes = _indexes(db_path)
    assert {
        "ix_worker_registrations_status",
        "ix_worker_registrations_heartbeat",
        "ix_worker_registrations_type",
    }.issubset(indexes)


def test_upgrade_leaves_no_model_drift(db_path) -> None:
    assert _alembic(db_path, "upgrade", "head").returncode == 0
    result = _alembic(db_path, "check")
    # ``alembic check`` exits 0 only when the models match the migrated schema.
    assert result.returncode == 0, result.stdout + result.stderr


def test_downgrade_is_surgical_and_preserves_business_data(db_path) -> None:
    assert _alembic(db_path, "upgrade", "head").returncode == 0

    # Seed a business row (organizations) and a worker row.
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO organizations (id, name, slug) VALUES (?, ?, ?)",
            ("org-keep-0001", "Keep Co", "keep-co"),
        )
        con.execute(
            "INSERT INTO worker_registrations "
            "(id, worker_id, worker_type, status, concurrency, supported_job_types, "
            "queue_backend, application_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("wr-0001", "w-1", "durable-jobs", "ready", 4, "[]", "inprocess", "1.0.0"),
        )
        con.commit()
    finally:
        con.close()

    result = _alembic(db_path, "downgrade", "-1")
    assert result.returncode == 0, result.stderr

    tables = _tables(db_path)
    assert "worker_registrations" not in tables  # only the worker table is dropped
    assert "organizations" in tables and "jobs" in tables  # business schema intact

    con = sqlite3.connect(db_path)
    try:
        kept = con.execute(
            "SELECT name FROM organizations WHERE id = 'org-keep-0001'"
        ).fetchone()
        assert kept is not None and kept[0] == "Keep Co"  # business data preserved
    finally:
        con.close()


def test_reupgrade_restores_worker_table(db_path) -> None:
    assert _alembic(db_path, "upgrade", "head").returncode == 0
    assert _alembic(db_path, "downgrade", PREV).returncode == 0
    assert "worker_registrations" not in _tables(db_path)
    assert _alembic(db_path, "upgrade", "head").returncode == 0
    assert "worker_registrations" in _tables(db_path)
