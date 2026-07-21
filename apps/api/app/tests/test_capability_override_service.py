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

import ast
import inspect
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.capabilities import service as override_service
from app.capabilities.errors import (
    CapabilityOverrideNotPermittedError,
    CapabilityTenantMismatchError,
)
from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import Capability, iter_capabilities
from app.capabilities.resolver import DecisionSource, resolve_capability
from app.capabilities.results import OverrideMutation, OverridePage
from app.capabilities.service import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_REASON_LEN,
    clear_capability_override,
    get_capability_override,
    list_capability_overrides,
    set_capability_override,
)
from app.core.config import Settings
from app.core.errors import NotFoundError, ValidationDomainError
from app.db.models import Base
from app.organizations.models import Organization, User, Workspace

_ORG_A, _WS_A = "org-a", "ws-a"
_ORG_B, _WS_B = "org-b", "ws-b"
_ACTOR = "operator-1"


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'override_service.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    # Enforce FKs for THIS engine only (SQLite is off by default) so tenant-scope
    # rows are real, and hand transaction control to SQLAlchemy so SAVEPOINTs are
    # truly nested. pysqlite emits its own implicit BEGIN, which breaks SAVEPOINT
    # atomicity (a savepoint row can survive an outer rollback); the documented fix
    # is to disable that implicit BEGIN (``isolation_level = None``) and emit BEGIN
    # ourselves via the engine ``begin`` event. Scoped to THIS engine only.
    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, _record):
        dbapi_connection.isolation_level = None
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    @event.listens_for(engine, "begin")
    def _emit_begin(conn):
        conn.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with make() as s:
        s.add(
            User(
                id=_ACTOR,
                email="op@example.com",
                full_name="Op",
                hashed_password="x",
                is_active=True,
                is_operator=True,
            )
        )
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
        s.add(
            WorkspaceCapabilityOverride(
                organization_id=org,
                workspace_id=ws,
                capability=capability.value,
                enabled=enabled,
                **kwargs,
            )
        )
        s.commit()


# --------------------------------------------------------------------------- #
# get_capability_override — success / absence (#13)
# --------------------------------------------------------------------------- #
def test_get_returns_none_when_no_override(factory) -> None:
    with factory() as s:
        row = get_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
        )
    assert row is None


def test_get_returns_the_row_when_present(factory) -> None:
    _seed_override(
        factory, org=_ORG_A, ws=_WS_A, capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True
    )
    with factory() as s:
        row = get_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
        )
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
            s,
            organization_id=_ORG_A,
            workspace_id="ws-does-not-exist",
            capability=Capability.OPPORTUNITY_FEEDBACK,
        )


def test_get_cross_tenant_raises_tenant_mismatch(factory) -> None:
    # ws-B belongs to org-B; reading it under org-A is a cross-tenant attempt.
    with factory() as s, pytest.raises(CapabilityTenantMismatchError):
        get_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_B,
            capability=Capability.OPPORTUNITY_FEEDBACK,
        )


def test_tenant_mismatch_is_envelope_indistinguishable_from_absent(factory) -> None:
    # Non-enumeration (§8.27): a cross-tenant workspace and a genuinely absent one
    # must present the SAME 404 status and the SAME generic ``not_found`` code, so a
    # caller cannot tell "exists but not yours" from "does not exist".
    with factory() as s:
        with pytest.raises(CapabilityTenantMismatchError) as mismatch:
            get_capability_override(
                s,
                organization_id=_ORG_A,
                workspace_id=_WS_B,
                capability=Capability.OPPORTUNITY_FEEDBACK,
            )
        with pytest.raises(NotFoundError) as absent:
            get_capability_override(
                s,
                organization_id=_ORG_A,
                workspace_id="ws-nope",
                capability=Capability.OPPORTUNITY_FEEDBACK,
            )
    assert mismatch.value.status_code == absent.value.status_code == 404
    assert mismatch.value.code == absent.value.code == "not_found"


def test_get_cross_tenant_does_not_leak_the_other_tenants_row(factory) -> None:
    # A real override exists in ws-B/org-B; reading it under org-A must raise a
    # tenant mismatch, never return the row.
    _seed_override(
        factory, org=_ORG_B, ws=_WS_B, capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True
    )
    with factory() as s, pytest.raises(CapabilityTenantMismatchError):
        get_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_B,
            capability=Capability.OPPORTUNITY_FEEDBACK,
        )


# --------------------------------------------------------------------------- #
# Isolation — per-workspace and per-capability (#15)
# --------------------------------------------------------------------------- #
def test_get_per_capability_isolation(factory) -> None:
    _seed_override(
        factory, org=_ORG_A, ws=_WS_A, capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True
    )
    with factory() as s:
        other = get_capability_override(
            s, organization_id=_ORG_A, workspace_id=_WS_A, capability=Capability.SCOUT_SCHEDULING
        )
    assert other is None


def test_get_per_workspace_isolation(factory) -> None:
    # An override in ws-A must never be visible when reading ws-B (own tenant).
    _seed_override(
        factory, org=_ORG_A, ws=_WS_A, capability=Capability.SCOUT_SCHEDULING, enabled=True
    )
    with factory() as s:
        row = get_capability_override(
            s, organization_id=_ORG_B, workspace_id=_WS_B, capability=Capability.SCOUT_SCHEDULING
        )
    assert row is None


# --------------------------------------------------------------------------- #
# list_capability_overrides — shape, ordering, isolation, totals (#14, #15)
# --------------------------------------------------------------------------- #
def test_list_empty_returns_coherent_page(factory) -> None:
    with factory() as s:
        page = list_capability_overrides(s, organization_id=_ORG_A, workspace_id=_WS_A)
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
    _seed_override(
        factory, org=_ORG_A, ws=_WS_A, capability=Capability.OPPORTUNITY_FEEDBACK, created_at=t1
    )
    _seed_override(
        factory, org=_ORG_A, ws=_WS_A, capability=Capability.SCOUT_SCHEDULING, created_at=t2
    )
    _seed_override(
        factory, org=_ORG_A, ws=_WS_A, capability=Capability.CONNECTOR_RSS, created_at=t3
    )
    _seed_override(
        factory, org=_ORG_B, ws=_WS_B, capability=Capability.OPPORTUNITY_FEEDBACK, created_at=t3
    )
    with factory() as s:
        page = list_capability_overrides(s, organization_id=_ORG_A, workspace_id=_WS_A)
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
    _seed_override(
        factory, org=_ORG_A, ws=_WS_A, capability=Capability.OPPORTUNITY_FEEDBACK, created_at=t1
    )
    _seed_override(
        factory, org=_ORG_A, ws=_WS_A, capability=Capability.SCOUT_SCHEDULING, created_at=t2
    )
    _seed_override(
        factory, org=_ORG_A, ws=_WS_A, capability=Capability.CONNECTOR_RSS, created_at=t3
    )
    with factory() as s:
        first = list_capability_overrides(
            s, organization_id=_ORG_A, workspace_id=_WS_A, limit=1, offset=0
        )
        second = list_capability_overrides(
            s, organization_id=_ORG_A, workspace_id=_WS_A, limit=1, offset=1
        )
        third = list_capability_overrides(
            s, organization_id=_ORG_A, workspace_id=_WS_A, limit=1, offset=2
        )
    assert first.total == second.total == third.total == 3
    assert [p.items[0].capability for p in (first, second, third)] == [
        Capability.CONNECTOR_RSS.value,
        Capability.SCOUT_SCHEDULING.value,
        Capability.OPPORTUNITY_FEEDBACK.value,
    ]


def test_list_clamps_limit_and_offset(factory) -> None:
    _seed_override(factory, org=_ORG_A, ws=_WS_A, capability=Capability.OPPORTUNITY_FEEDBACK)
    with factory() as s:
        over = list_capability_overrides(
            s, organization_id=_ORG_A, workspace_id=_WS_A, limit=500, offset=-5
        )
        under = list_capability_overrides(
            s, organization_id=_ORG_A, workspace_id=_WS_A, limit=0, offset=0
        )
    assert over.limit == MAX_LIMIT
    assert over.offset == 0
    assert under.limit == 1
    # Totals are unaffected by clamping.
    assert over.total == under.total == 1


def test_list_unknown_workspace_raises_not_found(factory) -> None:
    with factory() as s, pytest.raises(NotFoundError):
        list_capability_overrides(s, organization_id=_ORG_A, workspace_id="ws-does-not-exist")


def test_list_cross_tenant_raises_tenant_mismatch_without_leak(factory) -> None:
    _seed_override(
        factory, org=_ORG_B, ws=_WS_B, capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True
    )
    with factory() as s, pytest.raises(CapabilityTenantMismatchError):
        list_capability_overrides(s, organization_id=_ORG_A, workspace_id=_WS_B)


# --------------------------------------------------------------------------- #
# Read-only posture — no mutation, no commit (caller owns the transaction)
# --------------------------------------------------------------------------- #
def test_reads_never_mutate_or_commit(factory) -> None:
    _seed_override(
        factory, org=_ORG_A, ws=_WS_A, capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True
    )
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
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
        )
        list_capability_overrides(s, organization_id=_ORG_A, workspace_id=_WS_A)
    assert writes == [], writes
    assert commits == []


# --------------------------------------------------------------------------- #
# set_capability_override — create / update / idempotent no-op (#1–#4)
# --------------------------------------------------------------------------- #
def _audits(session, action: str) -> list[AuditLog]:
    return list(session.scalars(select(AuditLog).where(AuditLog.action == action)))


def test_set_creates_row_first_time(factory) -> None:
    with factory() as s:
        result = set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
            enabled=True,
            actor_user_id=_ACTOR,
        )
        s.commit()
        assert isinstance(result, OverrideMutation)
        assert (result.created, result.changed, result.enabled) == (True, True, True)
        assert result.override_id is not None
        assert result.capability is Capability.OPPORTUNITY_FEEDBACK
        row = get_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
        )
        assert row is not None and row.enabled is True
        created = _audits(s, "workspace_capability_override.created")
        assert len(created) == 1
        assert created[0].new_state == {
            "capability": "opportunity_feedback",
            "enabled": True,
            "override_id": row.id,
        }
        assert created[0].actor_user_id == _ACTOR


def test_set_updates_existing_row_in_place(factory) -> None:
    with factory() as s:
        first = set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.SCOUT_SCHEDULING,
            enabled=True,
            actor_user_id=_ACTOR,
        )
        second = set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.SCOUT_SCHEDULING,
            enabled=False,
            actor_user_id=_ACTOR,
        )
        s.commit()
        assert (second.created, second.changed, second.enabled) == (False, True, False)
        assert second.override_id == first.override_id  # same row, updated in place
        rows = list(s.scalars(select(WorkspaceCapabilityOverride)))
        assert len(rows) == 1 and rows[0].enabled is False
        updated = _audits(s, "workspace_capability_override.updated")
        assert len(updated) == 1
        assert updated[0].previous_state == {
            "capability": "scout_scheduling",
            "enabled": True,
            "override_id": first.override_id,
        }
        assert updated[0].new_state["enabled"] is False


def test_set_idempotent_noop_when_unchanged(factory) -> None:
    with factory() as s:
        set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
            enabled=True,
            actor_user_id=_ACTOR,
            reason="keep",
        )
        again = set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
            enabled=True,
            actor_user_id="operator-2",
            reason="keep",
        )
        s.commit()
        assert (again.created, again.changed) == (False, False)
        assert len(list(s.scalars(select(WorkspaceCapabilityOverride)))) == 1
        assert _audits(s, "workspace_capability_override.created") != []
        assert _audits(s, "workspace_capability_override.updated") == []


def test_set_strips_reason_and_blank_becomes_none(factory) -> None:
    with factory() as s:
        set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
            enabled=True,
            actor_user_id=_ACTOR,
            reason="  needs review  ",
        )
        set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.SCOUT_SCHEDULING,
            enabled=True,
            actor_user_id=_ACTOR,
            reason="   ",
        )
        s.commit()
        kept = get_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
        )
        blank = get_capability_override(
            s, organization_id=_ORG_A, workspace_id=_WS_A, capability=Capability.SCOUT_SCHEDULING
        )
    assert kept.reason == "needs review"
    assert blank.reason is None


# --------------------------------------------------------------------------- #
# set — policy denials (#5–#7)
# --------------------------------------------------------------------------- #
def test_rss_enable_is_rejected_and_writes_no_row(factory) -> None:
    with factory() as s:
        with pytest.raises(CapabilityOverrideNotPermittedError) as exc:
            set_capability_override(
                s,
                organization_id=_ORG_A,
                workspace_id=_WS_A,
                capability=Capability.CONNECTOR_RSS,
                enabled=True,
                actor_user_id=_ACTOR,
            )
        assert exc.value.status_code == 422
        assert exc.value.code == "capability_override_not_permitted"
        # No override row written; a .rejected audit records the attempt.
        assert list(s.scalars(select(WorkspaceCapabilityOverride))) == []
        rejected = _audits(s, "workspace_capability_override.rejected")
        assert len(rejected) == 1
        assert rejected[0].entity_id is None
        assert rejected[0].new_state == {
            "capability": "connector_rss",
            "enabled": True,
            "override_id": None,
        }


def test_rss_disable_is_permitted(factory) -> None:
    with factory() as s:
        result = set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.CONNECTOR_RSS,
            enabled=False,
            actor_user_id=_ACTOR,
        )
        s.commit()
        assert (result.created, result.enabled) == (True, False)
        row = get_capability_override(
            s, organization_id=_ORG_A, workspace_id=_WS_A, capability=Capability.CONNECTOR_RSS
        )
        assert row is not None and row.enabled is False


def test_over_long_reason_is_rejected_without_row_or_audit(factory) -> None:
    with factory() as s:
        with pytest.raises(ValidationDomainError):
            set_capability_override(
                s,
                organization_id=_ORG_A,
                workspace_id=_WS_A,
                capability=Capability.OPPORTUNITY_FEEDBACK,
                enabled=True,
                actor_user_id=_ACTOR,
                reason="x" * (MAX_REASON_LEN + 1),
            )
        assert list(s.scalars(select(WorkspaceCapabilityOverride))) == []
        # A length failure is not a policy rejection → no .rejected audit either.
        assert _audits(s, "workspace_capability_override.rejected") == []


# --------------------------------------------------------------------------- #
# set — tenant validation / non-enumeration
# --------------------------------------------------------------------------- #
def test_set_unknown_workspace_raises_not_found(factory) -> None:
    with factory() as s, pytest.raises(NotFoundError):
        set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id="ws-nope",
            capability=Capability.OPPORTUNITY_FEEDBACK,
            enabled=True,
            actor_user_id=_ACTOR,
        )


def test_set_cross_tenant_raises_tenant_mismatch_without_writing(factory) -> None:
    with factory() as s:
        with pytest.raises(CapabilityTenantMismatchError):
            set_capability_override(
                s,
                organization_id=_ORG_A,
                workspace_id=_WS_B,
                capability=Capability.OPPORTUNITY_FEEDBACK,
                enabled=True,
                actor_user_id=_ACTOR,
            )
        assert list(s.scalars(select(WorkspaceCapabilityOverride))) == []


# --------------------------------------------------------------------------- #
# set — idempotent upsert SAVEPOINT race backstop (#16)
# --------------------------------------------------------------------------- #
def test_set_integrity_error_backstop_updates_the_winning_row(factory, monkeypatch) -> None:
    # Simulate a concurrent insert winning the unique-constraint race: force the
    # initial existence check to miss (return None) while a real row already exists,
    # so the insert hits IntegrityError → SAVEPOINT rollback → re-read → update.
    _seed_override(
        factory, org=_ORG_A, ws=_WS_A, capability=Capability.OPPORTUNITY_FEEDBACK, enabled=False
    )
    real = override_service._load_override_row
    calls = {"n": 0}

    def _miss_once(db, *, workspace_id, capability):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real(db, workspace_id=workspace_id, capability=capability)

    monkeypatch.setattr(override_service, "_load_override_row", _miss_once)
    with factory() as s:
        result = set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
            enabled=True,
            actor_user_id=_ACTOR,
        )
        s.commit()
        assert calls["n"] == 2  # missed once, then re-read after IntegrityError
        assert (result.created, result.changed, result.enabled) == (False, True, True)
        rows = list(s.scalars(select(WorkspaceCapabilityOverride)))
        assert len(rows) == 1 and rows[0].enabled is True  # single row survives


# --------------------------------------------------------------------------- #
# set — actor, atomicity, no-commit, read-after-write
# --------------------------------------------------------------------------- #
def test_set_requires_explicit_keyword_only_actor() -> None:
    sig = inspect.signature(set_capability_override)
    actor = sig.parameters["actor_user_id"]
    assert actor.kind is inspect.Parameter.KEYWORD_ONLY
    assert actor.default is inspect.Parameter.empty  # no default/system actor


def test_set_and_audit_share_transaction_and_roll_back_together(factory) -> None:
    with factory() as s:
        set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
            enabled=True,
            actor_user_id=_ACTOR,
        )
        # Before commit both the row and its audit are pending in the same tx.
        assert list(s.scalars(select(WorkspaceCapabilityOverride))) != []
        assert _audits(s, "workspace_capability_override.created") != []
        s.rollback()
        # A caller rollback discards BOTH — never an audited change that didn't persist.
        assert list(s.scalars(select(WorkspaceCapabilityOverride))) == []
        assert _audits(s, "workspace_capability_override.created") == []


def test_set_never_commits_the_callers_transaction(factory) -> None:
    # Count only REAL DBAPI commits (engine ``commit`` event). The session
    # ``after_commit`` hook also fires on a SAVEPOINT release, which the set path
    # legitimately performs, so it cannot distinguish "released a savepoint" from
    # "committed the caller's transaction"; the engine-level event fires solely on a
    # true DBAPI commit.
    commits: list[int] = []
    with factory() as s:

        @event.listens_for(s.bind, "commit")
        def _count(conn):
            commits.append(1)

        set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
            enabled=True,
            actor_user_id=_ACTOR,
        )
        assert commits == []  # caller owns the commit
        s.rollback()


def test_set_is_visible_to_read_plane_within_the_same_transaction(factory) -> None:
    with factory() as s:
        set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.SCOUT_SCHEDULING,
            enabled=True,
            actor_user_id=_ACTOR,
        )
        # Read-after-write without committing (flushed pending state). Assert while
        # the row is still live in the session — a rollback expires the instance.
        row = get_capability_override(
            s, organization_id=_ORG_A, workspace_id=_WS_A, capability=Capability.SCOUT_SCHEDULING
        )
        page = list_capability_overrides(s, organization_id=_ORG_A, workspace_id=_WS_A)
        assert row is not None and row.enabled is True
        assert page.total == 1
        s.rollback()


# --------------------------------------------------------------------------- #
# clear_capability_override — delete existing / absent no-op (#11–#12)
# --------------------------------------------------------------------------- #
def test_clear_deletes_existing_row_and_audits_cleared(factory) -> None:
    _seed_override(
        factory, org=_ORG_A, ws=_WS_A, capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True
    )
    with factory() as s:
        before = get_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
        )
        prior_id = before.id
        result = clear_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
            actor_user_id=_ACTOR,
        )
        s.commit()
        # No override remains after a clear (§8.10/§8.14).
        assert isinstance(result, OverrideMutation)
        assert (result.created, result.changed) == (False, True)
        assert result.enabled is None and result.override_id is None
        assert list(s.scalars(select(WorkspaceCapabilityOverride))) == []
        cleared = _audits(s, "workspace_capability_override.cleared")
        assert len(cleared) == 1
        assert cleared[0].entity_id == prior_id
        assert cleared[0].actor_user_id == _ACTOR
        assert cleared[0].previous_state == {
            "capability": "opportunity_feedback",
            "enabled": True,
            "override_id": prior_id,
        }
        assert cleared[0].new_state is None


def test_clear_absent_is_idempotent_noop_without_audit(factory) -> None:
    with factory() as s:
        result = clear_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
            actor_user_id=_ACTOR,
        )
        s.commit()
        assert (result.created, result.changed) == (False, False)
        assert result.enabled is None and result.override_id is None
        assert list(s.scalars(select(WorkspaceCapabilityOverride))) == []
        assert _audits(s, "workspace_capability_override.cleared") == []


def test_clear_repeated_is_idempotent(factory) -> None:
    with factory() as s:
        set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.SCOUT_SCHEDULING,
            enabled=True,
            actor_user_id=_ACTOR,
        )
        first = clear_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.SCOUT_SCHEDULING,
            actor_user_id=_ACTOR,
        )
        second = clear_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.SCOUT_SCHEDULING,
            actor_user_id=_ACTOR,
        )
        s.commit()
        assert (first.created, first.changed) == (False, True)
        assert (second.created, second.changed) == (False, False)
        # Exactly one .cleared audit — the second clear is a benign no-op.
        assert len(_audits(s, "workspace_capability_override.cleared")) == 1


# --------------------------------------------------------------------------- #
# clear — actor / no-reason contract, tenant validation
# --------------------------------------------------------------------------- #
def test_clear_requires_explicit_keyword_only_actor_and_takes_no_reason() -> None:
    sig = inspect.signature(clear_capability_override)
    actor = sig.parameters["actor_user_id"]
    assert actor.kind is inspect.Parameter.KEYWORD_ONLY
    assert actor.default is inspect.Parameter.empty  # no default/system actor
    assert "reason" not in sig.parameters  # clear carries no reason (plan §8.7)


def test_clear_unknown_workspace_raises_not_found(factory) -> None:
    with factory() as s, pytest.raises(NotFoundError):
        clear_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id="ws-nope",
            capability=Capability.OPPORTUNITY_FEEDBACK,
            actor_user_id=_ACTOR,
        )


def test_clear_cross_tenant_raises_tenant_mismatch_without_deleting(factory) -> None:
    # A real override exists in ws-B/org-B; clearing it under org-A must raise a
    # tenant mismatch and never delete the other tenant's row.
    _seed_override(
        factory, org=_ORG_B, ws=_WS_B, capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True
    )
    with factory() as s:
        with pytest.raises(CapabilityTenantMismatchError):
            clear_capability_override(
                s,
                organization_id=_ORG_A,
                workspace_id=_WS_B,
                capability=Capability.OPPORTUNITY_FEEDBACK,
                actor_user_id=_ACTOR,
            )
        s.rollback()
    with factory() as s:
        survivor = get_capability_override(
            s,
            organization_id=_ORG_B,
            workspace_id=_WS_B,
            capability=Capability.OPPORTUNITY_FEEDBACK,
        )
        assert survivor is not None and survivor.enabled is True


# --------------------------------------------------------------------------- #
# clear — atomicity, no-commit, read-after-clear, set-after-clear
# --------------------------------------------------------------------------- #
def test_clear_and_audit_roll_back_together(factory) -> None:
    _seed_override(
        factory, org=_ORG_A, ws=_WS_A, capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True
    )
    with factory() as s:
        clear_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
            actor_user_id=_ACTOR,
        )
        # Pending in the same tx: the row is gone and the audit is present.
        assert list(s.scalars(select(WorkspaceCapabilityOverride))) == []
        assert _audits(s, "workspace_capability_override.cleared") != []
        s.rollback()
        # A caller rollback restores the row and discards the audit — never an
        # audited deletion that didn't persist.
        assert list(s.scalars(select(WorkspaceCapabilityOverride))) != []
        assert _audits(s, "workspace_capability_override.cleared") == []


def test_clear_never_commits_the_callers_transaction(factory) -> None:
    _seed_override(
        factory, org=_ORG_A, ws=_WS_A, capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True
    )
    commits: list[int] = []
    with factory() as s:

        @event.listens_for(s.bind, "commit")
        def _count(conn):
            commits.append(1)

        clear_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
            actor_user_id=_ACTOR,
        )
        assert commits == []  # caller owns the commit
        s.rollback()


def test_clear_is_visible_to_read_plane_within_the_same_transaction(factory) -> None:
    with factory() as s:
        set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.SCOUT_SCHEDULING,
            enabled=True,
            actor_user_id=_ACTOR,
        )
        s.commit()
        clear_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.SCOUT_SCHEDULING,
            actor_user_id=_ACTOR,
        )
        # Read-after-clear without committing: the flushed delete is visible.
        row = get_capability_override(
            s, organization_id=_ORG_A, workspace_id=_WS_A, capability=Capability.SCOUT_SCHEDULING
        )
        page = list_capability_overrides(s, organization_id=_ORG_A, workspace_id=_WS_A)
        assert row is None
        assert page.total == 0
        s.rollback()


def test_set_after_clear_recreates_the_override(factory) -> None:
    with factory() as s:
        first = set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
            enabled=True,
            actor_user_id=_ACTOR,
        )
        clear_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
            actor_user_id=_ACTOR,
        )
        recreated = set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
            enabled=False,
            actor_user_id=_ACTOR,
        )
        s.commit()
        # A set after a clear is a fresh create (a new row), not an update.
        assert (recreated.created, recreated.changed, recreated.enabled) == (True, True, False)
        assert recreated.override_id != first.override_id
        rows = list(s.scalars(select(WorkspaceCapabilityOverride)))
        assert len(rows) == 1 and rows[0].enabled is False


# --------------------------------------------------------------------------- #
# Concurrency — workspace SELECT … FOR UPDATE lock (4A-C.3.5, #17–#18)
# --------------------------------------------------------------------------- #
def test_workspace_lock_select_compiles_to_for_update_on_postgres() -> None:
    """Always-run compile proof: the lock statement emits ``FOR UPDATE`` on PG.

    Mirrors ``test_scout_schedule_api.py::TestCapLockCompiles`` — proves the locking
    SQL compiles against the PostgreSQL dialect without a live database, so the row
    lock is real where it matters even though SQLite renders it a no-op (§8.22).
    """
    from sqlalchemy.dialects import postgresql

    from app.capabilities.service import _workspace_lock_select

    sql = str(_workspace_lock_select("ws-1").compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE" in sql
    assert "WORKSPACES" in sql


# The row lock only truly blocks on PostgreSQL; on SQLite ``FOR UPDATE`` is a no-op
# and the engine serializes writers on the single file. These threaded convergence
# tests are gated on ``TEST_POSTGRES_URL`` (skipped otherwise); the always-run compile
# proof above stands in locally. Each worker drives its own session/transaction
# against a single workspace, so the transactions genuinely overlap (§8.22, #17).
_pg_only = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set; skipping live PostgreSQL override-contention test",
)


def _fresh_pg_engine():  # pragma: no cover - gated on live PG
    engine = create_engine(os.environ["TEST_POSTGRES_URL"], future=True)
    assert engine.dialect.name == "postgresql"
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return engine


def _seed_pg_tenant(make) -> None:  # pragma: no cover - gated on live PG
    with make() as s:
        s.add(
            User(
                id=_ACTOR,
                email="op@example.com",
                full_name="Op",
                hashed_password="x",
                is_active=True,
                is_operator=True,
            )
        )
        s.add(Organization(id=_ORG_A, name="A", slug="a"))
        s.add(Workspace(id=_WS_A, organization_id=_ORG_A, name="WA", slug="wa"))
        s.commit()


def _override_count(make) -> int:  # pragma: no cover - gated on live PG
    with make() as s:
        return int(
            s.scalar(
                select(func.count())
                .select_from(WorkspaceCapabilityOverride)
                .where(WorkspaceCapabilityOverride.workspace_id == _WS_A)
            )
            or 0
        )


def _audit_count(make, action: str) -> int:  # pragma: no cover - gated on live PG
    with make() as s:
        return int(
            s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == action))
            or 0
        )


@_pg_only
def test_pg_concurrent_identical_creates_converge_to_one_row() -> (
    None
):  # pragma: no cover - gated on live PG
    """Many concurrent identical ``set`` callers converge to exactly one row (#17)."""
    engine = _fresh_pg_engine()
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    try:
        _seed_pg_tenant(make)

        def _set(_i: int) -> str:
            s = make()
            try:
                set_capability_override(
                    s,
                    organization_id=_ORG_A,
                    workspace_id=_WS_A,
                    capability=Capability.OPPORTUNITY_FEEDBACK,
                    enabled=True,
                    actor_user_id=_ACTOR,
                )
                s.commit()
                return "ok"
            finally:
                s.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(_set, range(8)))

        # Every call succeeded, exactly one row survives, and exactly one insert won:
        # the lock serialized the check-then-act so no duplicate row and no lost update.
        assert outcomes == ["ok"] * 8
        assert _override_count(make) == 1
        assert _audit_count(make, "workspace_capability_override.created") == 1
    finally:
        engine.dispose()


@_pg_only
def test_pg_concurrent_sets_converge_without_duplicate_or_lost_update() -> (
    None
):  # pragma: no cover - gated on live PG
    """Concurrent ``set`` callers with differing values converge to one row (#17)."""
    engine = _fresh_pg_engine()
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    try:
        _seed_pg_tenant(make)

        def _set(i: int) -> str:
            s = make()
            try:
                set_capability_override(
                    s,
                    organization_id=_ORG_A,
                    workspace_id=_WS_A,
                    capability=Capability.SCOUT_SCHEDULING,
                    enabled=(i % 2 == 0),
                    actor_user_id=_ACTOR,
                )
                s.commit()
                return "ok"
            finally:
                s.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(_set, range(8)))

        # One row, one create; the rest updated/no-op'd the same row in place under the
        # lock — never a duplicate, never a lost update. Final value is whichever set
        # committed last (a valid boolean either way).
        assert outcomes == ["ok"] * 8
        assert _override_count(make) == 1
        assert _audit_count(make, "workspace_capability_override.created") == 1
        with make() as s:
            row = s.scalar(
                select(WorkspaceCapabilityOverride).where(
                    WorkspaceCapabilityOverride.workspace_id == _WS_A
                )
            )
            assert row is not None and isinstance(row.enabled, bool)
    finally:
        engine.dispose()


@_pg_only
def test_pg_concurrent_set_and_clear_converge_to_terminal_state() -> (
    None
):  # pragma: no cover - gated on live PG
    """Concurrent ``set``/``clear`` callers converge to a single terminal state (§8.22)."""
    engine = _fresh_pg_engine()
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    try:
        _seed_pg_tenant(make)

        def _set(_i: int) -> str:
            s = make()
            try:
                set_capability_override(
                    s,
                    organization_id=_ORG_A,
                    workspace_id=_WS_A,
                    capability=Capability.OPPORTUNITY_FEEDBACK,
                    enabled=True,
                    actor_user_id=_ACTOR,
                )
                s.commit()
                return "ok"
            finally:
                s.close()

        def _clear(_i: int) -> str:
            s = make()
            try:
                clear_capability_override(
                    s,
                    organization_id=_ORG_A,
                    workspace_id=_WS_A,
                    capability=Capability.OPPORTUNITY_FEEDBACK,
                    actor_user_id=_ACTOR,
                )
                s.commit()
                return "ok"
            finally:
                s.close()

        tasks = [_set if i % 2 == 0 else _clear for i in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda fn: fn(0), tasks))

        # No call errored and no duplicate row survives: the terminal state is a single
        # row (a set committed last) or none (a clear committed last).
        assert outcomes == ["ok"] * 8
        assert _override_count(make) in (0, 1)
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# Dark-state coupling (#21): service writes persist intent but never *activate*
# a capability — resolution stays dark in production (shipped defaults).
# --------------------------------------------------------------------------- #
def _shipped_settings() -> Settings:
    """Hermetic Settings (no ``.env``): every global capability flag stays ``False``."""
    return Settings(_env_file=None)


def test_dark_state_shipped_defaults_resolve_every_capability_disabled(factory) -> None:
    """With shipped defaults and no real override row, every registered capability
    resolves **disabled** via the global-configuration rule (§8.29 #21, §8.34 #45).

    This is the production baseline: the override table holds no real row, all three
    global flags are ``False``, so ``resolve_capability`` resolves every capability in
    a sample workspace to disabled.
    """
    settings = _shipped_settings()
    with factory() as s:
        for capability in iter_capabilities():
            res = resolve_capability(
                session=s,
                settings=settings,
                capability=capability,
                organization_id=_ORG_A,
                workspace_id=_WS_A,
            )
            assert res.effective_enabled is False, capability
            assert res.decided_by is DecisionSource.GLOBAL_CONFIGURATION, capability
            assert res.global_flag is False, capability


def test_dark_state_service_write_is_inert_without_a_live_consumer(factory) -> None:
    """The service can *persist* an enable override, but that intent only *would* take
    effect if a live gate consumed the resolver — none does (§8.29 #21, the
    persistence-vs-activation split).

    Setting an enable override for a ``workspace_enableable`` capability is honored by
    the resolver alone (``WORKSPACE_OVERRIDE``) while the bound global flag stays
    ``False``; because no production module consumes the resolver
    (``test_no_production_module_imports_the_override_service``) the capability is
    never globally activated. Clearing the override returns resolution to the dark
    global default.
    """
    settings = _shipped_settings()
    capability = Capability.OPPORTUNITY_FEEDBACK  # workspace_enableable

    with factory() as s:
        set_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=capability,
            enabled=True,
            actor_user_id=_ACTOR,
        )
        s.commit()

    with factory() as s:
        after_set = resolve_capability(
            session=s,
            settings=settings,
            capability=capability,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
        )
    # Persisted intent is honored only by the resolver; the global flag is still False,
    # so nothing globally activates the capability — persistence is not activation.
    assert after_set.decided_by is DecisionSource.WORKSPACE_OVERRIDE
    assert after_set.global_flag is False

    with factory() as s:
        clear_capability_override(
            s,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
            capability=capability,
            actor_user_id=_ACTOR,
        )
        s.commit()

    with factory() as s:
        after_clear = resolve_capability(
            session=s,
            settings=settings,
            capability=capability,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
        )
    assert after_clear.effective_enabled is False
    assert after_clear.decided_by is DecisionSource.GLOBAL_CONFIGURATION
    assert after_clear.global_flag is False


def test_dark_state_rss_stays_disabled_across_service_mutations(factory) -> None:
    """RSS is not ``workspace_enableable``: the service rejects an enable set (no row)
    and RSS resolves disabled regardless of any set/clear (§8.29 #21).

    Proves the deny-biased split holds even for a capability an override can never
    raise: an enable attempt is refused with no row written, and resolution stays
    disabled.
    """
    with factory() as s:
        with pytest.raises(CapabilityOverrideNotPermittedError):
            set_capability_override(
                s,
                organization_id=_ORG_A,
                workspace_id=_WS_A,
                capability=Capability.CONNECTOR_RSS,
                enabled=True,
                actor_user_id=_ACTOR,
            )
        s.rollback()

    with factory() as s:
        res = resolve_capability(
            session=s,
            settings=_shipped_settings(),
            capability=Capability.CONNECTOR_RSS,
            organization_id=_ORG_A,
            workspace_id=_WS_A,
        )
    assert res.effective_enabled is False


# --------------------------------------------------------------------------- #
# Live-gate / single-consumer allow-list guard (#22, §26, reframed in 4A-C.4.2).
# Phase 4A-C.4 intentionally adds the FIRST sanctioned production consumer of the
# capability control plane: the operator router (``app/system/
# internal_capabilities_routes.py``). So the 4A-C.3.6 "no production import at all"
# guard is reframed into an allow-list of exactly that one module — every OTHER
# production module (every live gate / worker / scheduler / connector included) must
# still import neither ``app.capabilities.service`` nor ``app.capabilities.resolver``.
# Precise AST import-boundary scan (not brittle whole-file string matching): a
# docstring/comment mention is ignored; only a real ``import`` binds a consumer.
# --------------------------------------------------------------------------- #
_APP_DIR = Path(__file__).resolve().parents[1]  # apps/api/app
_API_DIR = _APP_DIR.parent  # apps/api (the import root that holds the ``app`` package)
_SERVICE_MODULE = "app.capabilities.service"
_RESOLVER_MODULE = "app.capabilities.resolver"
#: The capability control-plane modules whose only sanctioned consumer is the operator
#: router; a live gate importing either re-arms an enforcement path and must fail here.
_CONTROL_PLANE_MODULES = (_SERVICE_MODULE, _RESOLVER_MODULE)
_SERVICE_FILE = _APP_DIR / "capabilities" / "service.py"
_RESOLVER_FILE = _APP_DIR / "capabilities" / "resolver.py"
#: The single allow-listed production consumer (relative to ``app/``).
_ALLOWED_CONSUMER_REL = "system/internal_capabilities_routes.py"


def _production_modules() -> list[Path]:
    """Every production module under ``app/`` except the test package and the two
    control-plane modules themselves (a module trivially "imports" itself)."""
    return [
        path
        for path in sorted(_APP_DIR.rglob("*.py"))
        if "tests" not in path.relative_to(_APP_DIR).parts
        and path not in (_SERVICE_FILE, _RESOLVER_FILE)
    ]


def _absolute_from_base(path: Path, level: int, module: str | None) -> str:
    """Resolve an ``ast.ImportFrom`` base (handling relative imports) to a dotted
    absolute module rooted at the ``app`` package."""
    if level == 0:
        return module or ""
    pkg_parts = list(path.relative_to(_API_DIR).with_suffix("").parts)
    if path.name != "__init__.py":
        pkg_parts = pkg_parts[:-1]  # the containing package
    if level - 1 > 0:  # each extra level climbs one more package
        pkg_parts = pkg_parts[: len(pkg_parts) - (level - 1)]
    base = ".".join(pkg_parts)
    if module:
        base = f"{base}.{module}" if base else module
    return base


def _module_imports_target(path: Path, tree: ast.AST, target: str) -> bool:
    """Whether ``path`` really imports ``target`` (e.g. ``app.capabilities.service`` or
    ``app.capabilities.resolver``) — via absolute *or* relative import. A docstring or
    comment mention is ignored; only a binding ``import`` counts."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):  # import <target>[.x] [as y]
            for alias in node.names:
                if alias.name == target or alias.name.startswith(target + "."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_from_base(path, node.level, node.module)
            # from <target> import x  /  from ..<leaf> import x
            if base == target or base.startswith(target + "."):
                return True
            # from <parent> import <leaf>  /  from . import <leaf>
            for alias in node.names:
                if f"{base}.{alias.name}" == target:
                    return True
    return False


def _control_plane_imports(path: Path, tree: ast.AST) -> list[str]:
    """The control-plane modules (service / resolver) that ``path`` imports."""
    return [t for t in _CONTROL_PLANE_MODULES if _module_imports_target(path, tree, t)]


def test_only_the_operator_router_consumes_the_service_or_resolver() -> None:
    """The capability control plane has exactly ONE production consumer (§26, #22).

    Reframed from the 4A-C.3.6 absolute prohibition: 4A-C.4 adds the operator router as
    the first sanctioned consumer of the resolver (4A-C.4.2) — and later the service
    (4A-C.4.3+). This guard parses every production module under ``app/`` and asserts the
    **only** one importing ``app.capabilities.service`` or ``app.capabilities.resolver``
    (via absolute *or* relative import) is ``system/internal_capabilities_routes.py``.
    Any other production module — every live gate / worker / scheduler / connector — that
    re-arms an enforcement path by importing either module fails CI here.
    """
    offenders = {}
    for path in _production_modules():
        rel = str(path.relative_to(_APP_DIR))
        if rel == _ALLOWED_CONSUMER_REL:
            continue  # the single sanctioned consumer (allow-list of one)
        hits = _control_plane_imports(
            path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "only the operator router may consume the capability control plane; found "
        f"unauthorized imports in: {offenders}"
    )


def test_consumer_allow_list_covers_live_gates_and_the_operator_router() -> None:
    """Self-check: the scan really covers the live-gate set and the allow-listed router,
    the two control-plane modules exclude themselves, and the allow-list is not vacuous —
    the operator router genuinely imports BOTH control-plane modules (§26, #22, #24).

    As of 4A-C.4.3 the operator router consumes the resolver (effective read, 4A-C.4.2)
    AND the override service's read plane (list read, 4A-C.4.3). Asserting both bindings
    keeps the allow-list grounded for each module: were either dropped from the allow-list
    above, ``test_only_the_operator_router_consumes_the_service_or_resolver`` would fail.
    """
    scanned = {str(p.relative_to(_APP_DIR)) for p in _production_modules()}
    for expected in (
        "feedback/routes.py",
        "scouting_requests/routes.py",
        "scouting_requests/schedules.py",
        "connectors/registry.py",
        _ALLOWED_CONSUMER_REL,  # the allow-listed consumer must actually be in scope
    ):
        assert expected in scanned, expected
    # The two control-plane modules exclude themselves (they cannot consume themselves).
    assert "capabilities/service.py" not in scanned
    assert "capabilities/resolver.py" not in scanned
    # The allow-list is grounded, not vacuous: the operator router really imports BOTH the
    # resolver and the service, so removing it from the allow-list above would fail the
    # single-consumer guard for either module.
    router_path = _APP_DIR / _ALLOWED_CONSUMER_REL
    router_tree = ast.parse(router_path.read_text(encoding="utf-8"), filename=str(router_path))
    router_imports = _control_plane_imports(router_path, router_tree)
    assert _RESOLVER_MODULE in router_imports
    assert _SERVICE_MODULE in router_imports
