"""Phase 4A-C.2: precedence + DB-backed tests for the capability resolver.

The pure precedence surface (:func:`decide_capability`) is exercised without a
database — mirroring how :func:`app.jobs.stuck.is_job_stuck` is unit-tested — and
the thin I/O wrapper (:func:`resolve_capability`) is exercised against a
self-contained throwaway SQLite database with foreign-key enforcement enabled
(mirroring ``test_workspace_capability_override_model.py``), so the single indexed
lookup, tenant validation, and per-workspace isolation are all really executed.

Nothing here activates a capability: every global flag stays ``False`` by default,
override rows are seeded into a temp DB and torn down, and the resolver is consumed
by nothing but this test module.
"""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import (
    Capability,
    get_policy,
    iter_capabilities,
)
from app.capabilities.resolver import (
    CapabilityResolution,
    DecisionSource,
    decide_capability,
    resolve_capability,
)
from app.core.config import Settings
from app.db.models import Base
from app.organizations.models import Organization, User, Workspace

_ORG_A, _WS_A = "org-a", "ws-a"
_ORG_B, _WS_B = "org-b", "ws-b"
_ACTOR = "operator-1"


def _settings(**overrides: bool) -> Settings:
    """A hermetic Settings (no ``.env``), flags off unless explicitly overridden."""
    return Settings(_env_file=None, **overrides)


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path/'resolver.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    # Enforce FKs for THIS engine only (SQLite is off by default) so tenant-scope
    # rows are real, without leaking the pragma to any other test's engine.
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


def _seed_override(factory, *, org, ws, capability, enabled) -> None:
    with factory() as s:
        s.add(WorkspaceCapabilityOverride(
            organization_id=org, workspace_id=ws,
            capability=capability.value, enabled=enabled))
        s.commit()


# --------------------------------------------------------------------------- #
# Pure precedence units (decide_capability — no DB, deterministic)
# --------------------------------------------------------------------------- #
def test_ceiling_blocks_unknown_capability_over_everything() -> None:
    # A forged/unregistered member is ceiling-blocked even with a would-be enable
    # override and a True global flag.
    forged = "not_a_capability"  # not a Capability member
    result = decide_capability(
        capability=forged,  # type: ignore[arg-type]
        workspace_id=_WS_A,
        global_flag=True,
        override_value=True,
    )
    assert result.effective_enabled is False
    assert result.decided_by is DecisionSource.SAFETY_CEILING
    assert result.has_override is False
    assert result.override_value is None


def test_override_enable_decides_over_false_global_flag() -> None:
    result = decide_capability(
        capability=Capability.OPPORTUNITY_FEEDBACK,
        workspace_id=_WS_A,
        global_flag=False,
        override_value=True,
    )
    assert result.effective_enabled is True
    assert result.decided_by is DecisionSource.WORKSPACE_OVERRIDE
    assert result.has_override is True
    assert result.override_value is True


def test_override_disable_decides_over_true_global_flag() -> None:
    result = decide_capability(
        capability=Capability.SCOUT_SCHEDULING,
        workspace_id=_WS_A,
        global_flag=True,
        override_value=False,
    )
    assert result.effective_enabled is False
    assert result.decided_by is DecisionSource.WORKSPACE_OVERRIDE
    assert result.has_override is True
    assert result.override_value is False


def test_rss_enable_override_is_not_honorable_falls_to_secure_default() -> None:
    # RSS is workspace_disableable but NOT workspace_enableable: an enable override
    # must never enable it — deny-biased fall-through to the secure default.
    assert get_policy(Capability.CONNECTOR_RSS).workspace_enableable is False
    result = decide_capability(
        capability=Capability.CONNECTOR_RSS,
        workspace_id=_WS_A,
        global_flag=False,
        override_value=True,
    )
    assert result.effective_enabled is False
    assert result.decided_by is DecisionSource.SECURE_DEFAULT
    assert result.has_override is False
    assert result.override_value is None


def test_rss_disable_override_is_honored() -> None:
    result = decide_capability(
        capability=Capability.CONNECTOR_RSS,
        workspace_id=_WS_A,
        global_flag=True,
        override_value=False,
    )
    assert result.effective_enabled is False
    assert result.decided_by is DecisionSource.WORKSPACE_OVERRIDE
    assert result.override_value is False


@pytest.mark.parametrize("flag", [True, False])
def test_global_flag_decides_when_no_override(flag: bool) -> None:
    result = decide_capability(
        capability=Capability.OPPORTUNITY_FEEDBACK,
        workspace_id=_WS_A,
        global_flag=flag,
        override_value=None,
    )
    assert result.effective_enabled is flag
    assert result.decided_by is DecisionSource.GLOBAL_CONFIGURATION
    assert result.has_override is False
    assert result.override_value is None


# --------------------------------------------------------------------------- #
# DB-backed resolve_capability
# --------------------------------------------------------------------------- #
def test_resolve_happy_path_enable_via_override(factory) -> None:
    _seed_override(factory, org=_ORG_A, ws=_WS_A,
                   capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True)
    with factory() as s:
        result = resolve_capability(
            session=s, settings=_settings(),
            capability=Capability.OPPORTUNITY_FEEDBACK,
            organization_id=_ORG_A, workspace_id=_WS_A)
    assert result.effective_enabled is True
    assert result.decided_by is DecisionSource.WORKSPACE_OVERRIDE
    assert result.global_flag is False  # enabled despite the dark global flag


def test_resolve_happy_path_disable_over_enabled_global_flag(factory) -> None:
    _seed_override(factory, org=_ORG_A, ws=_WS_A,
                   capability=Capability.SCOUT_SCHEDULING, enabled=False)
    with factory() as s:
        result = resolve_capability(
            session=s, settings=_settings(scout_scheduling_enabled=True),
            capability=Capability.SCOUT_SCHEDULING,
            organization_id=_ORG_A, workspace_id=_WS_A)
    assert result.effective_enabled is False
    assert result.decided_by is DecisionSource.WORKSPACE_OVERRIDE
    assert result.global_flag is True


def test_resolve_no_row_falls_through_to_global_flag(factory) -> None:
    with factory() as s:
        enabled = resolve_capability(
            session=s, settings=_settings(opportunity_feedback_enabled=True),
            capability=Capability.OPPORTUNITY_FEEDBACK,
            organization_id=_ORG_A, workspace_id=_WS_A)
        disabled = resolve_capability(
            session=s, settings=_settings(),
            capability=Capability.OPPORTUNITY_FEEDBACK,
            organization_id=_ORG_A, workspace_id=_WS_A)
    assert enabled.effective_enabled is True
    assert enabled.decided_by is DecisionSource.GLOBAL_CONFIGURATION
    assert disabled.effective_enabled is False
    assert disabled.decided_by is DecisionSource.GLOBAL_CONFIGURATION


def test_resolve_tenant_mismatch_treated_as_absent(factory) -> None:
    # Row is stored under org B for workspace B; resolving that workspace under
    # the WRONG org must treat the override as absent (deny-biased) and fall to the
    # global flag — never honor a cross-tenant row as an enable.
    _seed_override(factory, org=_ORG_B, ws=_WS_B,
                   capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True)
    with factory() as s:
        result = resolve_capability(
            session=s, settings=_settings(),
            capability=Capability.OPPORTUNITY_FEEDBACK,
            organization_id=_ORG_A, workspace_id=_WS_B)
    assert result.effective_enabled is False
    assert result.decided_by is DecisionSource.GLOBAL_CONFIGURATION
    assert result.has_override is False


def test_resolve_per_workspace_isolation(factory) -> None:
    # Only workspace A has an enable override; workspace B must resolve independently
    # to the (dark) global flag.
    _seed_override(factory, org=_ORG_A, ws=_WS_A,
                   capability=Capability.SCOUT_SCHEDULING, enabled=True)
    with factory() as s:
        a = resolve_capability(
            session=s, settings=_settings(),
            capability=Capability.SCOUT_SCHEDULING,
            organization_id=_ORG_A, workspace_id=_WS_A)
        b = resolve_capability(
            session=s, settings=_settings(),
            capability=Capability.SCOUT_SCHEDULING,
            organization_id=_ORG_B, workspace_id=_WS_B)
    assert a.effective_enabled is True
    assert a.decided_by is DecisionSource.WORKSPACE_OVERRIDE
    assert b.effective_enabled is False
    assert b.decided_by is DecisionSource.GLOBAL_CONFIGURATION


def test_resolve_override_for_one_capability_does_not_affect_another(factory) -> None:
    _seed_override(factory, org=_ORG_A, ws=_WS_A,
                   capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True)
    with factory() as s:
        other = resolve_capability(
            session=s, settings=_settings(),
            capability=Capability.SCOUT_SCHEDULING,
            organization_id=_ORG_A, workspace_id=_WS_A)
    assert other.effective_enabled is False
    assert other.decided_by is DecisionSource.GLOBAL_CONFIGURATION
    assert other.has_override is False


def test_resolve_rss_enable_override_never_enables_via_db(factory) -> None:
    # Even a persisted enable row for RSS cannot enable it (not workspace-enableable).
    _seed_override(factory, org=_ORG_A, ws=_WS_A,
                   capability=Capability.CONNECTOR_RSS, enabled=True)
    with factory() as s:
        result = resolve_capability(
            session=s, settings=_settings(),
            capability=Capability.CONNECTOR_RSS,
            organization_id=_ORG_A, workspace_id=_WS_A)
    assert result.effective_enabled is False
    assert result.decided_by is DecisionSource.SECURE_DEFAULT


def test_resolve_issues_single_query(factory) -> None:
    _seed_override(factory, org=_ORG_A, ws=_WS_A,
                   capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True)
    statements: list[str] = []
    with factory() as s:
        @event.listens_for(s.bind, "before_cursor_execute")
        def _count(conn, cursor, statement, params, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        resolve_capability(
            session=s, settings=_settings(),
            capability=Capability.OPPORTUNITY_FEEDBACK,
            organization_id=_ORG_A, workspace_id=_WS_A)
    assert len(statements) == 1, statements


# --------------------------------------------------------------------------- #
# Dark-by-default regression + registry coupling
# --------------------------------------------------------------------------- #
def test_dark_by_default_every_capability_disabled(factory) -> None:
    # Shipped defaults: all flags False, zero override rows → every registered
    # capability resolves disabled in a sample workspace.
    with factory() as s:
        for capability in iter_capabilities():
            result = resolve_capability(
                session=s, settings=_settings(),
                capability=capability,
                organization_id=_ORG_A, workspace_id=_WS_A)
            assert result.effective_enabled is False, capability
            assert result.decided_by is DecisionSource.GLOBAL_CONFIGURATION, capability


def test_resolver_reads_flag_via_registry_binding() -> None:
    # The resolver must derive the bound flag from the registry, never a hardcoded
    # name: every capability's global_flag_attr resolves on Settings and is dark.
    settings = _settings()
    for capability in iter_capabilities():
        attr = get_policy(capability).global_flag_attr
        assert getattr(settings, attr) is False


# --------------------------------------------------------------------------- #
# Result shape / secret-free
# --------------------------------------------------------------------------- #
def test_resolution_is_frozen_and_immutable() -> None:
    result = decide_capability(
        capability=Capability.OPPORTUNITY_FEEDBACK,
        workspace_id=_WS_A, global_flag=False, override_value=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.effective_enabled = True  # type: ignore[misc]


def test_resolution_fields_are_exactly_the_documented_set() -> None:
    fields = {f.name for f in dataclasses.fields(CapabilityResolution)}
    assert fields == {
        "capability",
        "workspace_id",
        "effective_enabled",
        "decided_by",
        "global_flag",
        "has_override",
        "override_value",
    }


def test_override_value_none_iff_no_override() -> None:
    with_override = decide_capability(
        capability=Capability.OPPORTUNITY_FEEDBACK,
        workspace_id=_WS_A, global_flag=False, override_value=True)
    without = decide_capability(
        capability=Capability.OPPORTUNITY_FEEDBACK,
        workspace_id=_WS_A, global_flag=True, override_value=None)
    assert with_override.has_override is True and with_override.override_value is True
    assert without.has_override is False and without.override_value is None


def test_decision_source_is_bounded() -> None:
    assert {d.value for d in DecisionSource} == {
        "safety_ceiling",
        "workspace_override",
        "global_configuration",
        "secure_default",
    }
