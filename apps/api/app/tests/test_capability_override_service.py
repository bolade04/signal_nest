"""Phase 4A-C.3.2: read-plane tests for the capability override service.

Exercises the two read accessors (:func:`get_capability_override`,
:func:`list_capability_overrides`) and their shared authoritative tenant
validation against a self-contained throwaway SQLite database with foreign-key
enforcement enabled (mirroring ``test_capability_resolver.py``). Covers plan
§8.29 read/tenant cases #8–#10 and #13–#15, plus read-only/no-commit and
pagination-clamp properties specific to this sub-batch.

Nothing here activates a capability: no global flag flips, override rows are
seeded into a temp DB and torn down, and the service is consumed by nothing but
this test module. Every capability remains dark.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.capabilities.errors import CapabilityTenantMismatchError
from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import Capability
from app.capabilities.results import OverridePage
from app.capabilities.service import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    get_capability_override,
    list_capability_overrides,
)
from app.core.errors import NotFoundError
from app.db.models import Base
from app.organizations.models import Organization, User, Workspace

_ORG_A, _WS_A = "org-a", "ws-a"
_ORG_B, _WS_B = "org-b", "ws-b"
_ACTOR = "operator-1"


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path/'override_service.db'}",
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


def _seed_override(
    factory, *, org, ws, capability, enabled=False, created_at=None, override_id=None
) -> None:
    with factory() as s:
        kwargs = {}
        if created_at is not None:
            kwargs["created_at"] = created_at
        if override_id is not None:
            kwargs["id"] = override_id
        s.add(WorkspaceCapabilityOverride(
            organization_id=org, workspace_id=ws,
            capability=capability.value, enabled=enabled, **kwargs))
        s.commit()


# --------------------------------------------------------------------------- #
# get_capability_override — success / absence (#13)
# --------------------------------------------------------------------------- #
def test_get_returns_none_when_no_override(factory) -> None:
    with factory() as s:
        row = get_capability_override(
            s, organization_id=_ORG_A, workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK)
    assert row is None


def test_get_returns_the_row_when_present(factory) -> None:
    _seed_override(factory, org=_ORG_A, ws=_WS_A,
                   capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True)
    with factory() as s:
        row = get_capability_override(
            s, organization_id=_ORG_A, workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK)
    assert row is not None
    assert row.capability == Capability.OPPORTUNITY_FEEDBACK.value
    assert row.enabled is True
    assert row.workspace_id == _WS_A


# --------------------------------------------------------------------------- #
# Tenant validation — unknown workspace (#8) and cross-tenant (#9)
# --------------------------------------------------------------------------- #
def test_get_unknown_workspace_raises_not_found(factory) -> None:
    with factory() as s, pytest.raises(NotFoundError):
        get_capability_override(
            s, organization_id=_ORG_A, workspace_id="ws-does-not-exist",
            capability=Capability.OPPORTUNITY_FEEDBACK)


def test_get_cross_tenant_raises_tenant_mismatch(factory) -> None:
    # ws-B belongs to org-B; reading it under org-A is a cross-tenant attempt.
    with factory() as s, pytest.raises(CapabilityTenantMismatchError):
        get_capability_override(
            s, organization_id=_ORG_A, workspace_id=_WS_B,
            capability=Capability.OPPORTUNITY_FEEDBACK)


def test_tenant_mismatch_is_envelope_indistinguishable_from_absent(factory) -> None:
    # Non-enumeration (§8.27): a cross-tenant workspace and a genuinely absent one
    # must present the SAME 404 status and the SAME generic ``not_found`` code, so a
    # caller cannot tell "exists but not yours" from "does not exist".
    with factory() as s:
        with pytest.raises(CapabilityTenantMismatchError) as mismatch:
            get_capability_override(
                s, organization_id=_ORG_A, workspace_id=_WS_B,
                capability=Capability.OPPORTUNITY_FEEDBACK)
        with pytest.raises(NotFoundError) as absent:
            get_capability_override(
                s, organization_id=_ORG_A, workspace_id="ws-nope",
                capability=Capability.OPPORTUNITY_FEEDBACK)
    assert mismatch.value.status_code == absent.value.status_code == 404
    assert mismatch.value.code == absent.value.code == "not_found"


def test_get_cross_tenant_does_not_leak_the_other_tenants_row(factory) -> None:
    # A real override exists in ws-B/org-B; reading it under org-A must raise a
    # tenant mismatch, never return the row.
    _seed_override(factory, org=_ORG_B, ws=_WS_B,
                   capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True)
    with factory() as s, pytest.raises(CapabilityTenantMismatchError):
        get_capability_override(
            s, organization_id=_ORG_A, workspace_id=_WS_B,
            capability=Capability.OPPORTUNITY_FEEDBACK)


# --------------------------------------------------------------------------- #
# Isolation — per-workspace and per-capability (#15)
# --------------------------------------------------------------------------- #
def test_get_per_capability_isolation(factory) -> None:
    _seed_override(factory, org=_ORG_A, ws=_WS_A,
                   capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True)
    with factory() as s:
        other = get_capability_override(
            s, organization_id=_ORG_A, workspace_id=_WS_A,
            capability=Capability.SCOUT_SCHEDULING)
    assert other is None


def test_get_per_workspace_isolation(factory) -> None:
    # An override in ws-A must never be visible when reading ws-B (own tenant).
    _seed_override(factory, org=_ORG_A, ws=_WS_A,
                   capability=Capability.SCOUT_SCHEDULING, enabled=True)
    with factory() as s:
        row = get_capability_override(
            s, organization_id=_ORG_B, workspace_id=_WS_B,
            capability=Capability.SCOUT_SCHEDULING)
    assert row is None


# --------------------------------------------------------------------------- #
# list_capability_overrides — shape, ordering, isolation, totals (#14, #15)
# --------------------------------------------------------------------------- #
def test_list_empty_returns_coherent_page(factory) -> None:
    with factory() as s:
        page = list_capability_overrides(
            s, organization_id=_ORG_A, workspace_id=_WS_A)
    assert isinstance(page, OverridePage)
    assert page.items == ()
    assert page.total == 0
    assert page.limit == DEFAULT_LIMIT
    assert page.offset == 0


def test_list_returns_only_workspace_rows_newest_first(factory) -> None:
    # Three overrides in ws-A with strictly increasing created_at; expect newest
    # first (created_at DESC). A ws-B row must not appear.
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 2, 1, tzinfo=UTC)
    t3 = datetime(2026, 3, 1, tzinfo=UTC)
    _seed_override(factory, org=_ORG_A, ws=_WS_A,
                   capability=Capability.OPPORTUNITY_FEEDBACK, created_at=t1)
    _seed_override(factory, org=_ORG_A, ws=_WS_A,
                   capability=Capability.SCOUT_SCHEDULING, created_at=t2)
    _seed_override(factory, org=_ORG_A, ws=_WS_A,
                   capability=Capability.CONNECTOR_RSS, created_at=t3)
    _seed_override(factory, org=_ORG_B, ws=_WS_B,
                   capability=Capability.OPPORTUNITY_FEEDBACK, created_at=t3)
    with factory() as s:
        page = list_capability_overrides(
            s, organization_id=_ORG_A, workspace_id=_WS_A)
    assert page.total == 3
    assert [r.capability for r in page.items] == [
        Capability.CONNECTOR_RSS.value,
        Capability.SCOUT_SCHEDULING.value,
        Capability.OPPORTUNITY_FEEDBACK.value,
    ]
    assert all(r.workspace_id == _WS_A for r in page.items)


def test_list_pages_deterministically_with_stable_total(factory) -> None:
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 2, 1, tzinfo=UTC)
    t3 = datetime(2026, 3, 1, tzinfo=UTC)
    _seed_override(factory, org=_ORG_A, ws=_WS_A,
                   capability=Capability.OPPORTUNITY_FEEDBACK, created_at=t1)
    _seed_override(factory, org=_ORG_A, ws=_WS_A,
                   capability=Capability.SCOUT_SCHEDULING, created_at=t2)
    _seed_override(factory, org=_ORG_A, ws=_WS_A,
                   capability=Capability.CONNECTOR_RSS, created_at=t3)
    with factory() as s:
        first = list_capability_overrides(
            s, organization_id=_ORG_A, workspace_id=_WS_A, limit=1, offset=0)
        second = list_capability_overrides(
            s, organization_id=_ORG_A, workspace_id=_WS_A, limit=1, offset=1)
        third = list_capability_overrides(
            s, organization_id=_ORG_A, workspace_id=_WS_A, limit=1, offset=2)
    assert first.total == second.total == third.total == 3
    assert [p.items[0].capability for p in (first, second, third)] == [
        Capability.CONNECTOR_RSS.value,
        Capability.SCOUT_SCHEDULING.value,
        Capability.OPPORTUNITY_FEEDBACK.value,
    ]


def test_list_clamps_limit_and_offset(factory) -> None:
    _seed_override(factory, org=_ORG_A, ws=_WS_A,
                   capability=Capability.OPPORTUNITY_FEEDBACK)
    with factory() as s:
        over = list_capability_overrides(
            s, organization_id=_ORG_A, workspace_id=_WS_A, limit=500, offset=-5)
        under = list_capability_overrides(
            s, organization_id=_ORG_A, workspace_id=_WS_A, limit=0, offset=0)
    assert over.limit == MAX_LIMIT
    assert over.offset == 0
    assert under.limit == 1
    # Totals are unaffected by clamping.
    assert over.total == under.total == 1


def test_list_unknown_workspace_raises_not_found(factory) -> None:
    with factory() as s, pytest.raises(NotFoundError):
        list_capability_overrides(
            s, organization_id=_ORG_A, workspace_id="ws-does-not-exist")


def test_list_cross_tenant_raises_tenant_mismatch_without_leak(factory) -> None:
    _seed_override(factory, org=_ORG_B, ws=_WS_B,
                   capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True)
    with factory() as s, pytest.raises(CapabilityTenantMismatchError):
        list_capability_overrides(
            s, organization_id=_ORG_A, workspace_id=_WS_B)


# --------------------------------------------------------------------------- #
# Read-only posture — no mutation, no commit (caller owns the transaction)
# --------------------------------------------------------------------------- #
def test_reads_never_mutate_or_commit(factory) -> None:
    _seed_override(factory, org=_ORG_A, ws=_WS_A,
                   capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True)
    writes: list[str] = []
    commits: list[int] = []
    with factory() as s:
        @event.listens_for(s.bind, "before_cursor_execute")
        def _count_writes(conn, cursor, statement, params, context, executemany):
            head = statement.lstrip().upper()
            if head.startswith(("INSERT", "UPDATE", "DELETE")):
                writes.append(statement)

        @event.listens_for(s, "after_commit")
        def _count_commits(session):
            commits.append(1)

        get_capability_override(
            s, organization_id=_ORG_A, workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK)
        list_capability_overrides(
            s, organization_id=_ORG_A, workspace_id=_WS_A)
    assert writes == [], writes
    assert commits == []
