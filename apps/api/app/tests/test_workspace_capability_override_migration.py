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
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# apps/api (holds alembic.ini) — three levels up from this test file.
API_DIR = Path(__file__).resolve().parents[2]
# Revision immediately before the workspace_capability_overrides migration.
PREV = "4945b98229e6"
HEAD = "53fdde95354a"  # 5E-CF: the CHECK admits notifications_analytics (above D-N4 item 36)
TABLE = "workspace_capability_overrides"


def _downgrade_confirmation(db_path: Path, args: tuple[str, ...]) -> list[str]:
    """Mint the guard's confirmation for a downgrade against this temp database.

    Computed, not hard-coded: each test binds a fresh `tmp_path`, so the token
    differs per run. The digest algorithm itself is pinned by literal value in
    `test_migrate_downgrade_target_guard.py`, which is what keeps this from being
    a self-confirming oracle.
    """
    if not args or args[0] != "downgrade":
        return []
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine

    from app.db.downgrade_guard import (
        chain_identity,
        confirmation_token,
        live_identity,
        resolve_downgrade,
    )

    url = f"sqlite:///{db_path}"
    engine = create_engine(url)
    with engine.connect() as conn:
        heads = MigrationContext.configure(conn).get_current_heads()
        identity = live_identity(conn, url)
    if len(heads) != 1:
        return []
    script = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))
    resolved, chain = resolve_downgrade(script, heads[0], args[1])
    token = confirmation_token(
        database_url=url,
        live_identity=identity,
        source_revision=heads[0],
        requested_target=args[1],
        resolved_destination=resolved,
        chain_identity=chain_identity(script, chain),
    )
    return ["-x", f"confirm={token}"]


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    # `-x` is a GLOBAL alembic option and must precede the subcommand, so it is
    # prefixed here rather than appended to *args. Harmless for upgrade/check;
    # required for downgrade, which the target-bound guard refuses unconfirmed.
    confirm = _downgrade_confirmation(db_path, args)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *confirm, *args],
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
    assert settings.recommendation_briefs_enabled is False  # Project Phase 5 CF-1
    assert settings.creative_generation_enabled is False  # Project Phase 5 CF-2
    assert settings.content_export_enabled is False  # Project Phase 5 CF-3
    assert settings.notifications_analytics_enabled is False  # 5E-CF, operator ruling O-12(B)
    # And every registry-bound flag is dark.
    for capability in iter_capabilities():
        assert getattr(settings, get_policy(capability).global_flag_attr) is False


def test_resolver_ships_and_every_live_gate_consumes_it() -> None:
    # Phase 4A-C.2 shipped the resolver, so it now imports cleanly...
    resolver = importlib.import_module("app.capabilities.resolver")
    assert hasattr(resolver, "resolve_capability")
    assert hasattr(resolver, "decide_capability")

    # ...and every live gate decides through it: feedback since Phase 4B-A, scheduling
    # and RSS since P6-CAP-1, briefs since Phase 5A (this test asserted the opposite for
    # scheduling and RSS until then, which pinned the raw-flag bypass). The precise AST
    # guards — one decision call
    # site per capability, no raw-flag read outside the raw-global reflection — live in
    # ``test_capability_override_types.py``; this keeps the migration-era check honest.
    for module_path in (
        Path("app/feedback/routes.py"),
        Path("app/scouting_requests/schedules.py"),
        Path("app/scouting_requests/connectors.py"),
        Path("app/briefs/routes.py"),  # Project Phase 5A
    ):
        source = (API_DIR / module_path).read_text(encoding="utf-8")
        assert "resolve_capability(" in source, module_path


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


# --------------------------------------------------------------------------- #
# Project Phase 5E 5E-CF (operator ruling O-12(B)): the CHECK admits
# notifications_analytics (revision 53fdde95354a, PENDING the operator's
# migration-freeze answer, Q-1)
# --------------------------------------------------------------------------- #
#: The revision below the 5E-CF widening (D-N4 item 36's closed claim kinds).
CLAIM_KINDS = "1574637e56d3"
CHECK_NAME = "ck_workspace_capability_override_capability"
NEW_CAPABILITY = "notifications_analytics"
_INSERT_OVERRIDE = (
    f"INSERT INTO {TABLE} (id, organization_id, workspace_id, capability, enabled)"
    " VALUES (?, 'org-1', ?, ?, 0)"
)


def _sql(db_path: Path, statement: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(statement, params).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def _capability_check(db_path: Path) -> set[str]:
    """The capability values the migrated CHECK admits, read from the database."""
    from sqlalchemy import create_engine, inspect

    from app.tests.test_cf1_contract_migration import _in_list

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        checks = {c["name"]: c["sqltext"] for c in inspect(engine).get_check_constraints(TABLE)}
    finally:
        engine.dispose()
    return _in_list(checks[CHECK_NAME], "capability")


def _version(db_path: Path) -> list[str]:
    return [r[0] for r in _sql(db_path, "SELECT version_num FROM alembic_version")]


def test_the_5e_cf_widening_is_the_single_head_above_the_claim_kinds() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))
    assert [r.revision for r in script.get_revisions("heads")] == [HEAD]
    assert script.get_revision(HEAD).down_revision == CLAIM_KINDS


def test_the_head_check_admits_every_registered_capability(db_path) -> None:
    from app.capabilities.registry import persisted_values

    assert _alembic(db_path, "upgrade", "head").returncode == 0
    assert _capability_check(db_path) == set(persisted_values())
    for value in persisted_values():
        _sql(db_path, _INSERT_OVERRIDE, (f"ov-{value}", f"ws-{value}", value))
    with pytest.raises(sqlite3.IntegrityError):
        _sql(db_path, _INSERT_OVERRIDE, ("ov-bogus", "ws-bogus", "not_a_capability"))


def test_the_5e_cf_downgrade_refuses_while_an_override_names_the_capability(db_path) -> None:
    assert _alembic(db_path, "upgrade", "head").returncode == 0
    _sql(db_path, _INSERT_OVERRIDE, ("ov-na", "ws-1", NEW_CAPABILITY))

    refused = _alembic(db_path, "downgrade", "-1")

    assert refused.returncode != 0
    assert f"name '{NEW_CAPABILITY}'; clear them before downgrading" in (
        refused.stdout + refused.stderr
    )
    assert _version(db_path) == [HEAD]  # nothing changed
    assert NEW_CAPABILITY in _capability_check(db_path)
    assert _sql(db_path, f"SELECT capability FROM {TABLE}") == [(NEW_CAPABILITY,)]


def test_the_5e_cf_downgrade_narrows_to_the_cf3_set_and_the_upgrade_restores_it(db_path) -> None:
    from app.capabilities.registry import persisted_values

    assert _alembic(db_path, "upgrade", "head").returncode == 0
    _sql(db_path, _INSERT_OVERRIDE, ("ov-export", "ws-1", "content_export"))

    down = _alembic(db_path, "downgrade", "-1")

    assert down.returncode == 0, down.stdout + down.stderr
    assert _version(db_path) == [CLAIM_KINDS]
    assert _capability_check(db_path) == set(persisted_values()) - {NEW_CAPABILITY}
    assert _sql(db_path, f"SELECT id, capability FROM {TABLE}") == [("ov-export", "content_export")]
    with pytest.raises(sqlite3.IntegrityError):
        _sql(db_path, _INSERT_OVERRIDE, ("ov-na", "ws-2", NEW_CAPABILITY))

    up = _alembic(db_path, "upgrade", "head")

    assert up.returncode == 0, up.stdout + up.stderr
    assert _version(db_path) == [HEAD]
    assert _capability_check(db_path) == set(persisted_values())
    _sql(db_path, _INSERT_OVERRIDE, ("ov-na", "ws-2", NEW_CAPABILITY))
    check = _alembic(db_path, "check")
    assert check.returncode == 0, check.stdout + check.stderr


def test_postgres_5e_cf_widening_round_trips() -> None:
    """On PostgreSQL (a database of its own): the head CHECK admits the capability, the
    downgrade refuses before any DDL while an override names it, then narrows the CHECK to
    the CF-3 set, and the upgrade restores it with no drift."""
    import sqlalchemy as sa
    from sqlalchemy import create_engine
    from sqlalchemy.exc import IntegrityError

    from app.capabilities.registry import persisted_values
    from app.tests._auth2_support import pg_database, require_postgres
    from app.tests.test_postgres_migration_proof import _alembic as _pg_alembic
    from app.tests.test_postgres_migration_proof import _confirmation, _run
    from app.tests.test_postgres_migration_proof import _version as _pg_version

    require_postgres()
    overrides = sa.table(TABLE, *(sa.column(c) for c in (
        "id", "organization_id", "workspace_id", "capability", "enabled")))

    def check_values(url: str) -> set[str]:
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                definition = conn.execute(
                    sa.select(sa.func.pg_get_constraintdef(sa.column("oid")))
                    .select_from(sa.table("pg_constraint", sa.column("oid"), sa.column("conname")))
                    .where(sa.column("conname") == CHECK_NAME)
                ).scalar_one()
        finally:
            engine.dispose()
        return set(re.findall(r"'([^']*)'", definition))

    def insert(url: str, row_id: str, workspace: str, capability: str) -> None:
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(overrides.insert().values(
                    id=row_id, organization_id="org-1", workspace_id=workspace,
                    capability=capability, enabled=False))
        finally:
            engine.dispose()

    with pg_database("sn_5ecf") as url:
        _pg_alembic(url, "upgrade", "head")
        assert _pg_version(url) == [HEAD]
        assert check_values(url) == set(persisted_values())
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(sa.table("organizations", sa.column("id"), sa.column("name"),
                                      sa.column("slug")).insert().values(
                    id="org-1", name="org-1", slug="org-1"))
                conn.execute(sa.table("workspaces", *(sa.column(c) for c in (
                    "id", "organization_id", "name", "slug", "onboarding_completed"))).insert(),
                    [{"id": w, "organization_id": "org-1", "name": w, "slug": w,
                      "onboarding_completed": True} for w in ("ws-1", "ws-2")])
        finally:
            engine.dispose()
        insert(url, "ov-na", "ws-1", NEW_CAPABILITY)

        token = _confirmation(url, "-1")
        refused = _run(["-x", f"confirm={token}", "downgrade", "-1"], url)
        assert refused.returncode != 0
        assert f"name '{NEW_CAPABILITY}'; clear them before downgrading" in (
            refused.stdout + refused.stderr
        )
        assert _pg_version(url) == [HEAD] and NEW_CAPABILITY in check_values(url)

        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(overrides.delete().where(overrides.c.id == "ov-na"))
        finally:
            engine.dispose()
        token = _confirmation(url, "-1")
        _pg_alembic(url, "-x", f"confirm={token}", "downgrade", "-1")
        assert _pg_version(url) == [CLAIM_KINDS]
        assert check_values(url) == set(persisted_values()) - {NEW_CAPABILITY}
        with pytest.raises(IntegrityError):
            insert(url, "ov-na", "ws-2", NEW_CAPABILITY)

        _pg_alembic(url, "upgrade", "head")
        assert _pg_version(url) == [HEAD]
        assert check_values(url) == set(persisted_values())
        insert(url, "ov-na", "ws-2", NEW_CAPABILITY)
        _pg_alembic(url, "check")
