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

import inspect
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.capabilities import service as override_service
from app.capabilities.errors import (
    CapabilityOverrideNotPermittedError,
    CapabilityTenantMismatchError,
)
from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import Capability
from app.capabilities.results import OverrideMutation, OverridePage
from app.capabilities.service import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_REASON_LEN,
    get_capability_override,
    list_capability_overrides,
    set_capability_override,
)
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
