"""Migration-lifecycle + dark-state regression tests for capability overrides.

Exercises the single additive ``workspace_capability_overrides`` migration against
a throwaway SQLite database via the real Alembic CLI (matching how CI validates
migrations, mirroring ``test_opportunity_feedback_migration.py``):

* **upgrade** to head creates the table and its FK indexes,
* **check** reports no drift between the ORM models and the migrations,
* the head stays **single** (no divergent branch),
* **downgrade** one step surgically drops only the new table, preserving business
  data,
* **re-upgrade** restores it.

Separate, ``TEST_POSTGRES_URL``-gated tests assert the two database-enforced
invariants on real PostgreSQL: the closed-vocabulary check constraint and the
workspace-deletion cascade.

Plus a regression guard that this foundation batch stays dark: the three global
capability flags remain ``False`` and no capability resolver module has been
introduced (the resolver is a later, separately-approved batch).
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# apps/api (holds alembic.ini) — three levels up from this test file.
API_DIR = Path(__file__).resolve().parents[2]
# Revision immediately before the workspace_capability_overrides migration.
PREV = "4945b98229e6"
HEAD = "98289430a3ec"
TABLE = "workspace_capability_overrides"


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


def test_upgrade_creates_table_and_indexes(db_path) -> None:
    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    assert TABLE in _tables(db_path)
    assert {
        "ix_workspace_capability_overrides_organization_id",
        "ix_workspace_capability_overrides_workspace_id",
        "ix_workspace_capability_overrides_set_by_user_id",
    }.issubset(_indexes(db_path))


def test_upgrade_leaves_no_model_drift(db_path) -> None:
    assert _alembic(db_path, "upgrade", "head").returncode == 0
    result = _alembic(db_path, "check")
    assert result.returncode == 0, result.stdout + result.stderr


def test_single_head(db_path) -> None:
    result = _alembic(db_path, "heads")
    assert result.returncode == 0, result.stderr
    heads = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(heads) == 1, result.stdout
    assert HEAD in heads[0]


def test_downgrade_is_surgical_and_preserves_business_data(db_path) -> None:
    assert _alembic(db_path, "upgrade", "head").returncode == 0

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO organizations (id, name, slug) VALUES (?, ?, ?)",
            ("org-keep-0001", "Keep Co", "keep-co"),
        )
        con.commit()
    finally:
        con.close()

    result = _alembic(db_path, "downgrade", PREV)
    assert result.returncode == 0, result.stderr

    tables = _tables(db_path)
    assert TABLE not in tables  # only the new table is dropped
    assert "organizations" in tables and "opportunity_feedback" in tables

    con = sqlite3.connect(db_path)
    try:
        kept = con.execute(
            "SELECT name FROM organizations WHERE id = 'org-keep-0001'"
        ).fetchone()
        assert kept is not None and kept[0] == "Keep Co"
    finally:
        con.close()


def test_reupgrade_restores_table(db_path) -> None:
    assert _alembic(db_path, "upgrade", "head").returncode == 0
    assert _alembic(db_path, "downgrade", PREV).returncode == 0
    assert TABLE not in _tables(db_path)
    assert _alembic(db_path, "upgrade", "head").returncode == 0
    assert TABLE in _tables(db_path)


# --------------------------------------------------------------------------- #
# Dark-state regression: foundation batch turns nothing on and adds no resolver
# --------------------------------------------------------------------------- #
def test_all_capability_flags_remain_dark() -> None:
    from app.capabilities.registry import get_policy, iter_capabilities
    from app.core.config import get_settings

    settings = get_settings()
    assert settings.opportunity_feedback_enabled is False
    assert settings.scout_scheduling_enabled is False
    assert settings.connector_rss_enabled is False
    # And every registry-bound flag is dark.
    for capability in iter_capabilities():
        assert getattr(settings, get_policy(capability).global_flag_attr) is False


def test_no_resolver_module_shipped_in_this_batch() -> None:
    # The precedence resolver is an explicitly-deferred, separately-approved batch.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.capabilities.resolver")


# --------------------------------------------------------------------------- #
# PostgreSQL-gated: the DB is the real backstop for the closed vocabulary + cascade
# --------------------------------------------------------------------------- #
def _pg_seed_workspace(factory) -> tuple[str, str, str]:  # pragma: no cover - gated
    from app.organizations.models import Organization, User, Workspace

    with factory() as s:
        s.add(User(id="op-1", email="op@example.com", full_name="Op",
                   hashed_password="x", is_active=True, is_operator=True))
        s.add(Organization(id="org-1", name="Org", slug="org-1"))
        s.add(Workspace(id="ws-1", organization_id="org-1", name="WS", slug="ws-1"))
        s.commit()
    return "org-1", "ws-1", "op-1"


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set; skipping live PostgreSQL invariants test",
)
def test_postgres_capability_check_constraint_enforced() -> None:  # pragma: no cover - gated
    from sqlalchemy import create_engine
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import sessionmaker

    from app.capabilities.models import WorkspaceCapabilityOverride
    from app.db.models import Base

    url = os.environ["TEST_POSTGRES_URL"]
    engine = create_engine(url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    try:
        org_id, ws_id, _ = _pg_seed_workspace(factory)
        with factory() as s:  # noqa: SIM117
            s.add(WorkspaceCapabilityOverride(
                organization_id=org_id, workspace_id=ws_id,
                capability="not_a_capability", enabled=True))
            with pytest.raises(IntegrityError):
                s.commit()
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set; skipping live PostgreSQL cascade test",
)
def test_postgres_workspace_deletion_cascades_override() -> None:  # pragma: no cover - gated
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import sessionmaker

    from app.capabilities.models import WorkspaceCapabilityOverride
    from app.db.models import Base
    from app.organizations.models import Workspace

    url = os.environ["TEST_POSTGRES_URL"]
    engine = create_engine(url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    try:
        org_id, ws_id, actor = _pg_seed_workspace(factory)
        with factory() as s:
            s.add(WorkspaceCapabilityOverride(
                organization_id=org_id, workspace_id=ws_id,
                capability="opportunity_feedback", enabled=True,
                set_by_user_id=actor, reason="pilot"))
            s.commit()
        with factory() as s:
            assert s.scalar(
                select(func.count()).select_from(WorkspaceCapabilityOverride)
            ) == 1
            s.delete(s.get(Workspace, ws_id))
            s.commit()
        with factory() as s:
            assert s.scalar(
                select(func.count()).select_from(WorkspaceCapabilityOverride)
            ) == 0
    finally:
        engine.dispose()
