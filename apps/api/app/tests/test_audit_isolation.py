"""Phase 5B-W4 adversarial battery: audit isolation (W21 lane ADV5AB).

Source: ``docs/project-phase-5b-approval-audit-spine.md`` §4 exit criteria 5 and 7(a) ("an
approval ``subject_id``, audit filter, or member ``user_id`` that exists in another tenant
... is indistinguishable from 'does not exist'") and the §5 W4 row.

Already proven elsewhere, and not repeated here: the six-role gate, the organization and
workspace predicates over fixture rows and the reciprocal NULL-workspace rows
(``test_audit_logs_role_matrix.py``); typed rows, each filter, the time range, the cursor's
binding to its filters, and a foreign *organization's* ``entity_id`` answering like a missing
one (``test_audit_read_api.py``). Exit criterion 7(a) is read as recorded in review
W21-REV-P5B r1 D-1: a filter has no natural 404, so "indistinguishable" is an empty page
with ``X-Total-Count: 0`` and no next cursor, exactly as for an id that exists nowhere.

This module adds, with the mutant each class kills (lane evidence ``P6-W21-ADV5AB-01``):

* **Real decisions, real trail.** Approval decisions recorded through the API in a sibling
  workspace and in another organization: every filter that names them (entity id, entity
  type, action, their conjunction, a time window) answers from this workspace exactly as a
  filter naming nothing, for every role that may read the trail.
* **Filters are exact, never patterns.** ``%``, ``_`` and prefixes of a real id match
  nothing, so a filter cannot be used to sweep for ids.
* **A cursor is bound to its workspace.** A cursor issued in one workspace is refused in a
  sibling workspace of the same organization and in another organization's workspace.

Every test runs on SQLite and, when ``TEST_POSTGRES_URL`` is set, on PostgreSQL;
``require_postgres`` makes an unset URL FAIL in CI and skip only on a developer machine.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.enums import Role
from app.core.pagination import NEXT_CURSOR_HEADER, TOTAL_COUNT_HEADER
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.tests._approvals_support import add_brief, add_evidence, add_org, add_user
from app.tests._auth2_support import (
    active_rate_limiter,
    live_bearer,
    pg_database,
    require_postgres,
)

API = get_settings().api_prefix

ORG_A, WS_A1, WS_A2 = "org-w4au-a", "ws-w4au-a1", "ws-w4au-a2"
ORG_B, WS_B = "org-w4au-b", "ws-w4au-b"

A_OWNER, A_ADMIN, A_COMPLIANCE = "w4au-a-owner", "w4au-a-admin", "w4au-a-compliance"
A_MARKETER, A_REVIEWER, A_REVIEWER2 = "w4au-a-marketer", "w4au-a-reviewer", "w4au-a-rev2"
B_OWNER, B_MARKETER, B_REVIEWER = "w4au-b-owner", "w4au-b-marketer", "w4au-b-reviewer"

#: Every role the trail admits (``require_exact_roles(OWNER, ADMIN, COMPLIANCE_REVIEWER)``).
A_READERS = (A_OWNER, A_ADMIN, A_COMPLIANCE)

BRIEF_A1, BRIEF_A1_OTHER = "w4au-brief-a1", "w4au-brief-a1o"
BRIEF_A2, BRIEF_B = "w4au-brief-a2", "w4au-brief-b"

INVALID_CURSOR = "Invalid cursor."


@dataclass
class World:
    kind: str
    client: TestClient
    ev: dict
    tokens: dict = field(default_factory=dict)

    def send(self, method: str, url: str, user_id: str, body=None, params=None):
        if user_id not in self.tokens:
            self.tokens[user_id] = live_bearer(user_id)
        active_rate_limiter()._hits.clear()
        kwargs: dict = {
            "headers": {"Authorization": f"Bearer {self.tokens[user_id]}"},
            "params": params,
        }
        if body is not None:
            kwargs["json"] = body
        return self.client.request(method, url, **kwargs)

    def decide(self, user_id: str, key: str, brief_id: str, decision: str = "approved"):
        ev = self.ev[key]
        url = (
            f"{API}/workspaces/{ev.ws_id}/opportunities/{ev.opportunity_id}"
            f"/briefs/{brief_id}/approvals"
        )
        r = self.send("POST", url, user_id, {"decision": decision, "subject_version": 1})
        assert r.status_code == 201, r.text
        return r.json()

    def trail(self, user_id: str, ws: str, params: dict | None = None):
        return self.send("GET", f"{API}/workspaces/{ws}/audit-logs", user_id, params=params)


def _sqlite(db_file: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}, future=True
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial pragma hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def _seed(s) -> dict:
    add_org(s, ORG_A, WS_A1, WS_A2)
    add_org(s, ORG_B, WS_B)
    users = {
        uid: add_user(s, uid, org_id=ORG_A, role=role)
        for uid, role in (
            (A_OWNER, Role.OWNER),
            (A_ADMIN, Role.ADMIN),
            (A_COMPLIANCE, Role.COMPLIANCE_REVIEWER),
            (A_MARKETER, Role.MARKETER),
            (A_REVIEWER, Role.REVIEWER),
            (A_REVIEWER2, Role.REVIEWER),
        )
    }
    for uid, role in (
        (B_OWNER, Role.OWNER),
        (B_MARKETER, Role.MARKETER),
        (B_REVIEWER, Role.REVIEWER),
    ):
        users[uid] = add_user(s, uid, org_id=ORG_B, role=role)
    ev = {
        "a1": add_evidence(s, org_id=ORG_A, ws_id=WS_A1, key="w4au-a1"),
        "a1o": add_evidence(s, org_id=ORG_A, ws_id=WS_A1, key="w4au-a1o"),
        "a2": add_evidence(s, org_id=ORG_A, ws_id=WS_A2, key="w4au-a2"),
        "b": add_evidence(s, org_id=ORG_B, ws_id=WS_B, key="w4au-b"),
    }
    add_brief(s, ev["a1"], brief_id=BRIEF_A1, author=users[A_MARKETER])
    add_brief(s, ev["a1o"], brief_id=BRIEF_A1_OTHER, author=users[A_MARKETER])
    add_brief(s, ev["a2"], brief_id=BRIEF_A2, author=users[A_MARKETER])
    add_brief(s, ev["b"], brief_id=BRIEF_B, author=users[B_MARKETER])
    return ev


def _build(kind: str, engine: Engine) -> Iterator[World]:
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    with factory() as s:
        ev = _seed(s)
        s.commit()

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
        yield World(kind, TestClient(app), ev)
    finally:
        app.dependency_overrides.clear()
        active_rate_limiter()._hits.clear()
        engine.dispose()


@pytest.fixture(params=["sqlite", "postgresql"])
def world(request, tmp_path, monkeypatch) -> Iterator[World]:
    # Decisions are the audited events under test, so briefs are on for this module.
    monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", True)
    if request.param == "sqlite":
        yield from _build("sqlite", _sqlite(tmp_path / "w4-audit.db"))
        return
    require_postgres()
    with pg_database("sn_w4au") as url:  # pragma: no cover - gated on PostgreSQL
        yield from _build("postgresql", create_engine(url, future=True))


def _answer(r) -> tuple:
    """Everything a caller can observe of a listing: status, body, count, next cursor."""
    return (
        r.status_code,
        r.json(),
        r.headers.get(TOTAL_COUNT_HEADER),
        r.headers.get(NEXT_CURSOR_HEADER),
    )


#: The answer to a filter that matches nothing.
NOTHING = (200, [], "0", None)


# --------------------------------------------------------------------------- #
# Real decisions: their trail never crosses a workspace or an organization
# --------------------------------------------------------------------------- #
class TestDecisionTrailIsolation:
    """Kills W4B-A01 (the trail scoped by organization only) and W4B-A02 (decision audit
    rows written organization-level, ``workspace_id`` NULL): either makes the sibling
    workspace's decision visible from WS_A1."""

    def test_every_filter_naming_another_tenants_decision_answers_like_nothing(self, world: World):
        since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        a2 = world.decide(A_REVIEWER, "a2", BRIEF_A2)
        world.decide(B_REVIEWER, "b", BRIEF_B)
        # Positive control: the sibling workspace's own trail holds the decision.
        own = world.trail(A_OWNER, WS_A2, {"entity_id": BRIEF_A2})
        assert own.status_code == 200, own.text
        assert [(r["action"], r["workspace_id"], r["entity_id"]) for r in own.json()] == [
            ("approval.approved", WS_A2, BRIEF_A2)
        ]
        assert own.json()[0]["new_state"]["approval_id"] == a2["id"]
        b_own = world.trail(B_OWNER, WS_B, {"entity_id": BRIEF_B})
        assert [r["entity_id"] for r in b_own.json()] == [BRIEF_B]

        probes = {
            "sibling entity_id": {"entity_id": BRIEF_A2},
            "other-org entity_id": {"entity_id": BRIEF_B},
            "entity_type": {"entity_type": "recommendation_brief"},
            "action": {"action": "approval.approved"},
            "entity_id + action": {"entity_id": BRIEF_A2, "action": "approval.approved"},
            "entity_type + time window": {
                "entity_type": "recommendation_brief",
                "created_from": since,
            },
            "missing id (the reference)": {"entity_id": "w4au-never-existed"},
        }
        for reader in A_READERS:
            for label, params in probes.items():
                answer = _answer(world.trail(reader, WS_A1, params))
                assert answer == NOTHING, (reader, label, answer)

    def test_the_decision_shows_in_its_own_workspace_only_after_it_is_made_there(
        self, world: World
    ):
        world.decide(A_REVIEWER, "a2", BRIEF_A2)
        before = _answer(world.trail(A_COMPLIANCE, WS_A1, {"entity_type": "recommendation_brief"}))
        assert before == NOTHING
        world.decide(A_REVIEWER, "a1", BRIEF_A1, "rejected")
        after = world.trail(A_COMPLIANCE, WS_A1, {"entity_type": "recommendation_brief"})
        assert [(r["action"], r["entity_id"]) for r in after.json()] == [
            ("approval.rejected", BRIEF_A1)
        ]
        assert after.headers[TOTAL_COUNT_HEADER] == "1"


# --------------------------------------------------------------------------- #
# Filters are exact, never patterns
# --------------------------------------------------------------------------- #
class TestFiltersAreExact:
    """Kills W4B-A03 (``entity_id`` compared with LIKE), W4B-A04 (``entity_id`` as a
    prefix) and W4B-A05 (``entity_type`` compared with LIKE). All three survive
    ``test_audit_read_api.py`` and ``test_audit_logs_role_matrix.py``."""

    @pytest.mark.parametrize(
        "params",
        [
            {"entity_id": "%"},
            {"entity_id": "w4au-%"},
            {"entity_id": "_" * len(BRIEF_A1)},
            {"entity_id": BRIEF_A1[:-1]},
            {"entity_id": "w4au-brief"},
            {"entity_type": "%"},
            {"entity_type": "recommendation_%"},
            {"entity_type": "recommendation"},
        ],
        ids=lambda p: "-".join(f"{k}={v}" for k, v in p.items()),
    )
    def test_wildcards_and_prefixes_match_nothing(self, world: World, params: dict):
        world.decide(A_REVIEWER, "a1", BRIEF_A1)
        world.decide(A_REVIEWER, "a1o", BRIEF_A1_OTHER)
        exact = world.trail(A_OWNER, WS_A1, {"entity_id": BRIEF_A1})
        assert [r["entity_id"] for r in exact.json()] == [BRIEF_A1]  # positive control
        assert _answer(world.trail(A_OWNER, WS_A1, params)) == NOTHING


# --------------------------------------------------------------------------- #
# A cursor is bound to its workspace
# --------------------------------------------------------------------------- #
class TestCursorIsBoundToItsWorkspace:
    """Kills W4B-A06 (the audit route binds its cursor to the organization instead of the
    workspace): a WS_A1 cursor would then be honoured in WS_A2."""

    def test_a_cursor_is_refused_in_another_workspace(self, world: World):
        world.decide(A_REVIEWER, "a1", BRIEF_A1)
        world.decide(A_REVIEWER2, "a1", BRIEF_A1, "rejected")
        world.decide(A_REVIEWER, "a2", BRIEF_A2)
        params = {"entity_type": "recommendation_brief", "limit": 1}
        first = world.trail(A_OWNER, WS_A1, params)
        assert first.status_code == 200 and first.headers[TOTAL_COUNT_HEADER] == "2"
        cursor = first.headers[NEXT_CURSOR_HEADER]
        # Positive control: the cursor resumes its own listing.
        second = world.trail(A_OWNER, WS_A1, {**params, "cursor": cursor})
        assert second.status_code == 200 and len(second.json()) == 1
        assert second.json()[0]["id"] != first.json()[0]["id"]
        for reader, ws in ((A_OWNER, WS_A2), (A_COMPLIANCE, WS_A2), (B_OWNER, WS_B)):
            r = world.trail(reader, ws, {**params, "cursor": cursor})
            assert (r.status_code, r.json()["error"]["message"]) == (422, INVALID_CURSOR), (
                reader,
                ws,
            )
