"""Phase 4A-C.1: model-metadata + persistence tests for capability overrides.

Runs against a self-contained throwaway SQLite database with foreign-key
enforcement enabled, so it can assert the real DB-level invariants of the additive
``workspace_capability_overrides`` table: the closed-vocabulary check constraint,
the one-override-per-(workspace, capability) uniqueness, not-null intent, and the
CASCADE / SET NULL foreign-key lifecycle. The equivalent PostgreSQL-enforced
invariants are additionally covered (gated) in the migration test.

Nothing here activates a capability: rows are pure recorded *intent*, read by no
resolver and no live gate in this batch.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import Capability, persisted_values
from app.db.models import Base
from app.organizations.models import Organization, User, Workspace

_ORG_A, _WS_A = "org-a", "ws-a"
_ORG_B, _WS_B = "org-b", "ws-b"
_ACTOR = "operator-1"


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path/'ovr.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    # Enforce foreign keys for THIS engine only (SQLite is off by default), so the
    # CASCADE / SET NULL lifecycle is really exercised without leaking the pragma
    # to any other test's engine.
    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with make() as s:
        s.add(User(id=_ACTOR, email="op@example.com", full_name="Op",
                   hashed_password="x", is_active=True, is_operator=True))
        s.add(Organization(id=_ORG_A, name="A", slug="a"))
        s.add(Organization(id=_ORG_B, name="B", slug="b"))
        s.add(Workspace(id=_WS_A, organization_id=_ORG_A, name="WA", slug="wa"))
        s.add(Workspace(id=_WS_B, organization_id=_ORG_B, name="WB", slug="wb"))
        s.commit()
    try:
        yield make
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# Model / table metadata
# --------------------------------------------------------------------------- #
def test_tablename_and_columns() -> None:
    table = WorkspaceCapabilityOverride.__table__
    assert table.name == "workspace_capability_overrides"
    assert set(table.columns.keys()) == {
        "id",
        "organization_id",
        "workspace_id",
        "capability",
        "enabled",
        "set_by_user_id",
        "reason",
        "created_at",
        "updated_at",
    }


def test_nullability_matches_design() -> None:
    cols = WorkspaceCapabilityOverride.__table__.columns
    for required in ("id", "organization_id", "workspace_id", "capability", "enabled"):
        assert cols[required].nullable is False, required
    for optional in ("set_by_user_id", "reason"):
        assert cols[optional].nullable is True, optional


def test_unique_and_check_constraints_declared() -> None:
    names = {c.name for c in WorkspaceCapabilityOverride.__table__.constraints}
    assert "uq_workspace_capability_override" in names
    assert "ck_workspace_capability_override_capability" in names


def test_foreign_key_ondelete_semantics() -> None:
    fks = {
        tuple(fk.parent.name for fk in c.elements): c
        for c in WorkspaceCapabilityOverride.__table__.foreign_key_constraints
    }
    assert fks[("organization_id",)].ondelete == "CASCADE"
    assert fks[("workspace_id",)].ondelete == "CASCADE"
    assert fks[("set_by_user_id",)].ondelete == "SET NULL"


def test_check_constraint_covers_exact_registry_set() -> None:
    ck = next(
        c for c in WorkspaceCapabilityOverride.__table__.constraints
        if c.name == "ck_workspace_capability_override_capability"
    )
    sqltext = str(ck.sqltext)
    for value in persisted_values():
        assert f"'{value}'" in sqltext


# --------------------------------------------------------------------------- #
# Persistence + DB-enforced invariants (SQLite with FK enforcement)
# --------------------------------------------------------------------------- #
def test_happy_path_insert_and_read_back(factory) -> None:
    with factory() as s:
        s.add(WorkspaceCapabilityOverride(
            organization_id=_ORG_A, workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK.value,
            enabled=True, set_by_user_id=_ACTOR, reason="pilot workspace"))
        s.commit()
    with factory() as s:
        row = s.scalar(select(WorkspaceCapabilityOverride))
        assert row.capability == "opportunity_feedback"
        assert row.enabled is True
        assert row.set_by_user_id == _ACTOR
        assert row.reason == "pilot workspace"
        assert row.id and len(row.id) == 32  # uuid4 hex default
        assert row.created_at is not None and row.updated_at is not None


def test_reason_and_actor_are_optional(factory) -> None:
    with factory() as s:
        s.add(WorkspaceCapabilityOverride(
            organization_id=_ORG_A, workspace_id=_WS_A,
            capability=Capability.SCOUT_SCHEDULING.value, enabled=False))
        s.commit()
    with factory() as s:
        row = s.scalar(select(WorkspaceCapabilityOverride))
        assert row.reason is None and row.set_by_user_id is None


def test_unique_one_override_per_workspace_capability(factory) -> None:
    with factory() as s:
        s.add(WorkspaceCapabilityOverride(
            organization_id=_ORG_A, workspace_id=_WS_A,
            capability=Capability.CONNECTOR_RSS.value, enabled=False))
        s.commit()
    with factory() as s:  # noqa: SIM117
        s.add(WorkspaceCapabilityOverride(
            organization_id=_ORG_A, workspace_id=_WS_A,
            capability=Capability.CONNECTOR_RSS.value, enabled=True))
        with pytest.raises(IntegrityError):
            s.commit()


def test_same_capability_allowed_across_distinct_workspaces(factory) -> None:
    with factory() as s:
        s.add(WorkspaceCapabilityOverride(
            organization_id=_ORG_A, workspace_id=_WS_A,
            capability=Capability.SCOUT_SCHEDULING.value, enabled=True))
        s.add(WorkspaceCapabilityOverride(
            organization_id=_ORG_B, workspace_id=_WS_B,
            capability=Capability.SCOUT_SCHEDULING.value, enabled=True))
        s.commit()
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(WorkspaceCapabilityOverride)) == 2


def test_check_constraint_rejects_unknown_capability(factory) -> None:
    with factory() as s:  # noqa: SIM117
        s.add(WorkspaceCapabilityOverride(
            organization_id=_ORG_A, workspace_id=_WS_A,
            capability="totally_made_up", enabled=True))
        with pytest.raises(IntegrityError):
            s.commit()


def test_enabled_is_not_null(factory) -> None:
    with factory() as s:  # noqa: SIM117
        s.add(WorkspaceCapabilityOverride(
            organization_id=_ORG_A, workspace_id=_WS_A,
            capability=Capability.CONNECTOR_RSS.value, enabled=None))
        with pytest.raises(IntegrityError):
            s.commit()


def test_workspace_deletion_cascades_override(factory) -> None:
    with factory() as s:
        s.add(WorkspaceCapabilityOverride(
            organization_id=_ORG_A, workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK.value, enabled=True))
        s.commit()
    with factory() as s:
        s.delete(s.get(Workspace, _WS_A))
        s.commit()
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(WorkspaceCapabilityOverride)) == 0


def test_organization_deletion_cascades_override(factory) -> None:
    with factory() as s:
        s.add(WorkspaceCapabilityOverride(
            organization_id=_ORG_B, workspace_id=_WS_B,
            capability=Capability.SCOUT_SCHEDULING.value, enabled=True))
        s.commit()
    with factory() as s:
        # Remove the workspace first (its own FK to the org is CASCADE), then org.
        s.delete(s.get(Workspace, _WS_B))
        s.delete(s.get(Organization, _ORG_B))
        s.commit()
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(WorkspaceCapabilityOverride)) == 0


def test_actor_deletion_sets_null_and_preserves_override(factory) -> None:
    with factory() as s:
        s.add(WorkspaceCapabilityOverride(
            organization_id=_ORG_A, workspace_id=_WS_A,
            capability=Capability.CONNECTOR_RSS.value, enabled=False,
            set_by_user_id=_ACTOR, reason="ceiling off"))
        s.commit()
    with factory() as s:
        s.delete(s.get(User, _ACTOR))
        s.commit()
    with factory() as s:
        row = s.scalar(select(WorkspaceCapabilityOverride))
        assert row is not None  # override survives
        assert row.set_by_user_id is None  # authorship forgotten
        assert row.reason == "ceiling off"
