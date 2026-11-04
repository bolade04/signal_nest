"""Phase 5B typed audit read API (W21 lane P5B): typed rows, filters, cursor pagination.

    GET /api/v1/workspaces/{workspace_id}/audit-logs

Exit criterion 5 (``docs/project-phase-5b-approval-audit-spine.md`` §4): "the audit read
route returns typed, filtered, paginated results and appears in the OpenAPI contract". The
role gate and the organization/workspace scope are proven in
``test_audit_logs_role_matrix.py``; this module proves the typed shape, each filter, their
conjunction, the time-range semantics, cursor binding to the filters and the OpenAPI
surface. Exit criterion 7(a) for the audit filter: an ``entity_id`` that exists in another
organization answers exactly like one that exists nowhere.

Rows are inserted with Core ``INSERT`` on the table (explicit, distinct ``created_at``), so
ordering and time ranges are deterministic; every action is a registry member.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.audit.actions import AuditAction
from app.audit.models import AuditLog
from app.audit.routes import EMPTY_RANGE
from app.core.config import get_settings
from app.core.enums import Role
from app.core.pagination import NEXT_CURSOR_HEADER, TOTAL_COUNT_HEADER
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.tests._approvals_support import add_org, add_user
from app.tests._auth2_support import active_rate_limiter, live_bearer

API = get_settings().api_prefix
ORG_A, WS_A1, WS_A2 = "org-ar-a", "ws-ar-a1", "ws-ar-a2"
ORG_B, WS_B = "org-ar-b", "ws-ar-b"
OWNER_A, COMPLIANCE_A, OWNER_B = "ar-a-owner", "ar-a-compliance", "ar-b-owner"
T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

# id, organization, workspace, action, entity_type, entity_id, hours after T0, prev, new
ROWS = (
    ("ar-r1", ORG_A, WS_A1, "location.created", "business_location", "loc-1", 0, None, None),
    (
        "ar-r2",
        ORG_A,
        WS_A1,
        "location.deactivated",
        "business_location",
        "loc-1",
        1,
        {"is_active": True},
        {"is_active": False},
    ),
    (
        "ar-r3",
        ORG_A,
        None,
        "organization_member.role_changed",
        "organization_member",
        "u-x",
        2,
        {"role": "marketer"},
        {"role": "reviewer"},
    ),
    ("ar-r4", ORG_A, WS_A2, "location.created", "business_location", "loc-2", 3, None, None),
    ("ar-r5", ORG_A, WS_A1, "scout_request.created", "scout_request", "sr-1", 4, None, None),
    ("ar-b1", ORG_B, WS_B, "location.created", "business_location", "loc-b", 1, None, None),
    (
        "ar-b2",
        ORG_B,
        None,
        "organization_member.role_changed",
        "organization_member",
        "u-b",
        2,
        {"role": "viewer"},
        {"role": "admin"},
    ),
)
FIELDS = {
    "id", "workspace_id", "action", "actor_user_id", "entity_type", "entity_id", "reason",
    "previous_state", "new_state", "context", "created_at",
}  # fmt: skip


@dataclass
class Env:
    client: TestClient
    tokens: dict

    def get(self, params=None, *, user: str = OWNER_A, ws: str = WS_A1):
        if user not in self.tokens:
            self.tokens[user] = live_bearer(user)
        active_rate_limiter()._hits.clear()
        return self.client.get(
            f"{API}/workspaces/{ws}/audit-logs",
            params=params or {},
            headers={"Authorization": f"Bearer {self.tokens[user]}"},
        )


def _ids(r) -> list[str]:
    assert r.status_code == 200, r.text
    return [row["id"] for row in r.json()]


def _engine(db_file) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}, future=True
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial pragma hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


@pytest.fixture(scope="module")
def env(tmp_path_factory) -> Iterator[Env]:
    engine = _engine(tmp_path_factory.mktemp("audit_read") / "audit.db")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    with factory() as s:
        add_org(s, ORG_A, WS_A1, WS_A2)
        add_org(s, ORG_B, WS_B)
        add_user(s, OWNER_A, org_id=ORG_A, role=Role.OWNER)
        add_user(s, COMPLIANCE_A, org_id=ORG_A, role=Role.COMPLIANCE_REVIEWER)
        add_user(s, OWNER_B, org_id=ORG_B, role=Role.OWNER)
        s.commit()
    with engine.begin() as c:
        for row_id, org, ws, action, etype, eid, hours, prev, new in ROWS:
            c.execute(
                insert(AuditLog.__table__).values(
                    id=row_id,
                    organization_id=org,
                    workspace_id=ws,
                    actor_user_id=None,
                    action=action,
                    entity_type=etype,
                    entity_id=eid,
                    previous_state=prev,
                    new_state=new,
                    context={"k": row_id},
                    created_at=T0 + timedelta(hours=hours),
                    updated_at=T0 + timedelta(hours=hours),
                )
            )

    def _override_get_db():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield Env(TestClient(app), {})
    finally:
        app.dependency_overrides.clear()
        active_rate_limiter()._hits.clear()
        engine.dispose()


class TestTypedRows:
    def test_rows_are_audit_log_out_with_states_surfaced(self, env: Env):
        r = env.get()
        assert _ids(r) == ["ar-r5", "ar-r3", "ar-r2", "ar-r1"]
        rows = {row["id"]: row for row in r.json()}
        assert all(set(row) == FIELDS for row in rows.values())
        assert rows["ar-r2"]["previous_state"] == {"is_active": True}
        assert rows["ar-r2"]["new_state"] == {"is_active": False}
        assert rows["ar-r3"]["workspace_id"] is None and rows["ar-r2"]["workspace_id"] == WS_A1
        assert rows["ar-r1"]["context"] == {"k": "ar-r1"}
        assert ORG_A not in r.text  # organization_id is never returned

    def test_the_compliance_reviewer_reads_the_same_typed_rows(self, env: Env):
        assert _ids(env.get(user=COMPLIANCE_A)) == _ids(env.get())


class TestFilters:
    def test_action(self, env: Env):
        r = env.get({"action": "location.created"})
        assert _ids(r) == ["ar-r1"] and r.headers[TOTAL_COUNT_HEADER] == "1"
        assert _ids(env.get({"action": "location.created"}, ws=WS_A2)) == ["ar-r4"]

    def test_entity_type_and_entity_id(self, env: Env):
        assert _ids(env.get({"entity_type": "business_location"})) == ["ar-r2", "ar-r1"]
        assert _ids(env.get({"entity_id": "loc-1"})) == ["ar-r2", "ar-r1"]
        assert _ids(env.get({"entity_type": "scout_request", "entity_id": "loc-1"})) == []
        assert _ids(env.get({"entity_type": "organization_member", "entity_id": "u-x"})) == [
            "ar-r3"
        ]

    def test_filters_are_a_conjunction(self, env: Env):
        r = env.get({"action": "location.deactivated", "entity_id": "loc-1"})
        assert _ids(r) == ["ar-r2"]
        assert _ids(env.get({"action": "location.created", "entity_id": "loc-2"})) == []

    def test_time_range_is_from_inclusive_before_exclusive(self, env: Env):
        params = {
            "created_from": (T0 + timedelta(hours=1)).isoformat(),
            "created_before": (T0 + timedelta(hours=4)).isoformat(),
        }
        r = env.get(params)
        assert _ids(r) == ["ar-r3", "ar-r2"] and r.headers[TOTAL_COUNT_HEADER] == "2"
        assert _ids(env.get({"created_from": (T0 + timedelta(hours=4)).isoformat()})) == ["ar-r5"]
        assert _ids(env.get({"created_before": (T0 + timedelta(hours=1)).isoformat()})) == ["ar-r1"]

    def test_offsets_are_converted_and_a_naive_time_is_utc(self, env: Env):
        plus_two = timezone(timedelta(hours=2))
        aware = (T0 + timedelta(hours=1)).astimezone(plus_two).isoformat()  # 12:00+02:00
        naive = (T0 + timedelta(hours=1)).replace(tzinfo=None).isoformat()  # 10:00
        expected = ["ar-r5", "ar-r3", "ar-r2"]
        assert _ids(env.get({"created_from": aware})) == expected
        assert _ids(env.get({"created_from": naive})) == expected

    def test_an_empty_range_is_refused(self, env: Env):
        for before in (T0, T0 - timedelta(seconds=1)):
            r = env.get({"created_from": T0.isoformat(), "created_before": before.isoformat()})
            assert r.status_code == 422 and r.json()["error"]["message"] == EMPTY_RANGE

    @pytest.mark.parametrize(
        "params",
        [
            {"action": "auth.login"},  # the plan's word, not a registry member
            {"action": "opportunity.archived"},
            {"entity_type": ""},
            {"entity_type": "t" * 81},
            {"entity_id": "i" * 33},
            {"created_from": "yesterday"},
        ],
    )
    def test_malformed_filters_are_422(self, env: Env, params):
        assert env.get(params).status_code == 422

    def test_a_foreign_entity_id_answers_exactly_like_a_missing_one(self, env: Env):
        foreign = env.get({"entity_id": "loc-b"})  # exists, in organization B
        missing = env.get({"entity_id": "loc-never"})
        assert foreign.status_code == missing.status_code == 200
        assert foreign.json() == missing.json() == []
        assert foreign.headers[TOTAL_COUNT_HEADER] == missing.headers[TOTAL_COUNT_HEADER] == "0"
        assert (
            NEXT_CURSOR_HEADER not in foreign.headers and NEXT_CURSOR_HEADER not in missing.headers
        )
        # ...and organization B's own owner does see it: the filter, not the row, was scoped.
        assert _ids(env.get({"entity_id": "loc-b"}, user=OWNER_B, ws=WS_B)) == ["ar-b1"]

    def test_organization_level_rows_of_another_organization_never_match(self, env: Env):
        assert _ids(env.get({"entity_type": "organization_member"})) == ["ar-r3"]
        assert _ids(env.get({"entity_type": "organization_member"}, user=OWNER_B, ws=WS_B)) == [
            "ar-b2"
        ]


class TestCursorPagination:
    def test_cursor_pages_walk_the_filtered_listing(self, env: Env):
        params = {"entity_type": "business_location", "limit": 1}
        first = env.get(params)
        assert _ids(first) == ["ar-r2"] and first.headers[TOTAL_COUNT_HEADER] == "2"
        cursor = first.headers[NEXT_CURSOR_HEADER]
        second = env.get({**params, "cursor": cursor})
        assert _ids(second) == ["ar-r1"] and second.headers[TOTAL_COUNT_HEADER] == "2"
        assert NEXT_CURSOR_HEADER not in second.headers

    def test_a_cursor_is_bound_to_its_filters(self, env: Env):
        first = env.get({"entity_type": "business_location", "limit": 1})
        cursor = first.headers[NEXT_CURSOR_HEADER]
        for other in (
            {"entity_type": "scout_request"},
            {"entity_type": "business_location", "action": "location.created"},
            {"entity_type": "business_location", "created_from": T0.isoformat()},
            {"entity_type": "business_location", "created_before": T0.isoformat()},
            # Each filter is bound on its own: entity_id alone changes the listing.
            {"entity_type": "business_location", "entity_id": "loc-1"},
            {},
        ):
            r = env.get({**other, "limit": 1, "cursor": cursor})
            assert r.status_code == 422, other
            assert r.json()["error"]["message"] == "Invalid cursor."

    def test_a_time_filter_cursor_survives_an_equivalent_offset(self, env: Env):
        # The bound filter is the normalised UTC instant, not the client's spelling.
        utc = {"created_from": T0.isoformat(), "limit": 1}
        first = env.get(utc)
        cursor = first.headers[NEXT_CURSOR_HEADER]
        shifted = T0.astimezone(timezone(timedelta(hours=-5))).isoformat()
        r = env.get({"created_from": shifted, "limit": 1, "cursor": cursor})
        assert r.status_code == 200 and _ids(r) == ["ar-r3"]


class TestOpenApi:
    def test_the_route_publishes_the_typed_array_and_its_filters(self):
        doc = app.openapi()
        op = doc["paths"][f"{API}/workspaces/{{workspace_id}}/audit-logs"]["get"]
        schema = op["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema["type"] == "array"
        assert schema["items"] == {"$ref": "#/components/schemas/AuditLogOut"}
        names = {p["name"] for p in op["parameters"]}
        assert names == {
            "workspace_id", "limit", "cursor", "action", "entity_type", "entity_id",
            "created_from", "created_before", "authorization",
        }  # fmt: skip
        action = next(p for p in op["parameters"] if p["name"] == "action")
        assert {"$ref": "#/components/schemas/AuditAction"} in action["schema"]["anyOf"]
        enum = doc["components"]["schemas"]["AuditAction"]["enum"]
        assert set(enum) == {a.value for a in AuditAction}
        out = doc["components"]["schemas"]["AuditLogOut"]
        assert set(out["properties"]) == FIELDS
        assert out["properties"]["action"] == {"$ref": "#/components/schemas/AuditAction"}
