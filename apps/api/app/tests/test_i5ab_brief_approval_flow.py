"""I-5A + I-5B integration (W21 INT5AB): a brief captured through the 5A route is queued,
decided and read through the 5B routes.

Each feature's own modules prove its half: ``test_briefs_api`` writes briefs through the 5A
service, while the 5B modules (``test_approvals_api``, ``test_cross_organization_isolation``)
decide on briefs inserted as witness rows, because 5B was built before the 5A service was on
its base. This module joins the two halves through HTTP only, so the seam itself is under
test: the snapshot and author the 5A service writes are the ones 5B's separation-of-duties
check reads, one capability switch (and one workspace override) opens or closes both
features, the read sets are the coordinator's rulings (briefs: every member; decision
history: every member but viewer; queue: reviewer only), and the audit trail and the
account exports carry both features' records for the same brief.

One SQLite database per test (foreign keys on); ``get_db`` is the only overridden
dependency, so authentication, tenancy, role checks and the capability resolver run for
real. Model calls use the offline mock provider (``offline_llm``).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.approvals.models import Approval
from app.capabilities.models import WorkspaceCapabilityOverride
from app.core.config import get_settings
from app.core.enums import Role
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.tests._approvals_support import add_evidence, add_org, add_user
from app.tests._auth2_support import live_bearer
from app.tests._llm_offline import offline_llm

API = get_settings().api_prefix
ORG, WS, WS_OTHER = "org-i5", "ws-i5", "ws-i5-other"
ROLES = (
    Role.OWNER,
    Role.ADMIN,
    Role.MARKETER,
    Role.REVIEWER,
    Role.COMPLIANCE_REVIEWER,
    Role.VIEWER,
)
HISTORY_READERS = {r for r in ROLES if r is not Role.VIEWER}


@pytest.fixture(scope="module", autouse=True)
def _offline() -> Iterator[None]:
    with offline_llm():
        yield


@dataclass
class Flow:
    client: TestClient
    make: sessionmaker
    auth: dict[Role, dict]
    users: dict[Role, str]
    opp: dict[str, str]
    record: dict[str, str]

    def briefs_url(self, ws: str = WS, brief: str | None = None) -> str:
        base = f"{API}/workspaces/{ws}/opportunities/{self.opp[ws]}/briefs"
        return f"{base}/{brief}" if brief else base

    def approvals_url(self, brief: str, ws: str = WS) -> str:
        return f"{self.briefs_url(ws, brief)}/approvals"

    def capture(self, role: Role = Role.MARKETER, ws: str = WS):
        return self.client.post(
            self.briefs_url(ws),
            json={"intelligence_record_id": self.record[ws]},
            headers=self.auth[role],
        )

    def decide(self, brief: str, decision: str, version: int, role: Role = Role.REVIEWER):
        return self.client.post(
            self.approvals_url(brief),
            json={"decision": decision, "subject_version": version},
            headers=self.auth[role],
        )

    def history(self, brief: str, role: Role, ws: str = WS):
        return self.client.get(self.approvals_url(brief, ws), headers=self.auth[role])

    def queue(self, role: Role = Role.REVIEWER, ws: str = WS):
        return self.client.get(f"{API}/workspaces/{ws}/approval-queue", headers=self.auth[role])

    def count(self, model) -> int:
        with self.make() as s:
            return int(s.scalar(select(func.count()).select_from(model)) or 0)


def _reset_rate_limiter() -> None:
    from app.core.middleware import RateLimitMiddleware

    stack = getattr(app, "middleware_stack", None)
    while stack is not None:
        if isinstance(stack, RateLimitMiddleware):
            stack._hits.clear()
            return
        stack = getattr(stack, "app", None)


@pytest.fixture
def flow(tmp_path, monkeypatch) -> Iterator[Flow]:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'i5ab.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial pragma hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with make() as s:
        add_org(s, ORG, WS, WS_OTHER)
        users = {role: add_user(s, f"i5-{role.value}", org_id=ORG, role=role).id for role in ROLES}
        evidence = {ws: add_evidence(s, org_id=ORG, ws_id=ws, key=ws) for ws in (WS, WS_OTHER)}
        auth = {
            role: {"Authorization": f"Bearer {live_bearer(uid, s)}"} for role, uid in users.items()
        }
        s.commit()

    def _override_get_db():
        s = make()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", True)
    app.dependency_overrides[get_db] = _override_get_db
    _reset_rate_limiter()
    try:
        yield Flow(
            TestClient(app),
            make,
            auth,
            users,
            opp={ws: e.opportunity_id for ws, e in evidence.items()},
            record={ws: e.record_id for ws, e in evidence.items()},
        )
    finally:
        app.dependency_overrides.clear()
        _reset_rate_limiter()
        engine.dispose()


def _ids(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


class TestBriefToDecision:
    def test_a_captured_brief_is_queued_decided_and_its_decision_read_back(self, flow: Flow):
        created = flow.capture()
        assert created.status_code == 201, created.text
        brief = created.json()
        assert brief["brief_version"] == 1
        assert brief["created_by"] == flow.users[Role.MARKETER]

        # The reviewer's work list holds exactly the new brief; nobody else may read it.
        assert _ids(flow.queue()) == [brief["id"]]
        for role in set(ROLES) - {Role.REVIEWER}:
            refused = flow.queue(role)
            assert refused.status_code == 403, (role, refused.text)

        approved = flow.decide(brief["id"], "approved", 1)
        assert approved.status_code == 201, approved.text
        decision = approved.json()
        assert (decision["subject_id"], decision["subject_version"]) == (brief["id"], 1)
        assert decision["decided_by"] == flow.users[Role.REVIEWER]
        assert decision["is_simulated"] is True  # a decision on mock output is simulated
        assert _ids(flow.queue()) == []

        # Decision history: every member but the viewer; the brief itself: every member.
        for role in ROLES:
            history = flow.history(brief["id"], role)
            if role in HISTORY_READERS:
                assert history.status_code == 200, (role, history.text)
                first = history.json()["items"][0]
                assert (first["id"], first["decision"]) == (decision["id"], "approved")
            else:
                assert history.status_code == 403, (role, history.text)
            read = flow.client.get(flow.briefs_url(brief=brief["id"]), headers=flow.auth[role])
            assert read.status_code == 200, (role, read.text)

    def test_a_new_version_needs_its_own_decision(self, flow: Flow):
        first = flow.capture().json()
        assert flow.decide(first["id"], "approved", 1).status_code == 201
        second = flow.capture()
        assert second.status_code == 201, second.text
        second = second.json()
        assert second["brief_version"] == 2

        # Only the latest version waits; the earlier approval stays on version 1 alone.
        assert _ids(flow.queue()) == [second["id"]]
        assert flow.history(second["id"], Role.OWNER).json()["items"] == []
        assert flow.history(first["id"], Role.OWNER).json()["items"][0]["decision"] == "approved"
        # A decision names the version the reviewer read: version 1 under version 2's id is stale.
        stale = flow.decide(second["id"], "approved", 1)
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "approval_stale_version"


class TestSeparationOfDutiesAcrossTheSeam:
    def test_the_5a_author_cannot_approve_after_being_made_a_reviewer(self, flow: Flow):
        brief = flow.capture(Role.MARKETER).json()
        promoted = flow.client.put(
            f"{API}/organizations/{ORG}/members/{flow.users[Role.MARKETER]}/role",
            json={"role": Role.REVIEWER.value},
            headers=flow.auth[Role.OWNER],
        )
        assert promoted.status_code == 200, promoted.text

        # The author, now holding the approver role, still sees the brief in the queue ...
        assert _ids(flow.queue(Role.MARKETER)) == [brief["id"]]
        # ... and is refused on it: 5B reads the author that the 5A service wrote.
        refused = flow.decide(brief["id"], "approved", 1, role=Role.MARKETER)
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] == "approval_author_conflict"
        assert flow.count(Approval) == 0
        # Another reviewer may decide.
        assert flow.decide(brief["id"], "approved", 1).status_code == 201


class TestOneSwitch:
    def test_a_workspace_override_opens_both_features_in_that_workspace_only(
        self, flow: Flow, monkeypatch
    ):
        monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", False)
        with flow.make() as s:
            s.add(
                WorkspaceCapabilityOverride(
                    organization_id=ORG,
                    workspace_id=WS,
                    capability="recommendation_briefs",
                    enabled=True,
                    reason="I-5A/I-5B integration test",
                )
            )
            s.commit()

        def reflected(ws: str) -> bool:
            r = flow.client.get(
                f"{API}/workspaces/{ws}/brief-capability", headers=flow.auth[Role.VIEWER]
            )
            assert r.status_code == 200, r.text
            return r.json()["enabled"]

        assert reflected(WS) is True
        assert reflected(WS_OTHER) is False

        brief = flow.capture()
        assert brief.status_code == 201, brief.text
        brief_id = brief.json()["id"]
        assert _ids(flow.queue()) == [brief_id]
        assert flow.decide(brief_id, "approved", 1).status_code == 201
        assert flow.history(brief_id, Role.MARKETER).status_code == 200

        # The other workspace stays dark for both features, before any lookup.
        for r in (
            flow.capture(ws=WS_OTHER),
            flow.client.get(flow.briefs_url(WS_OTHER), headers=flow.auth[Role.OWNER]),
            flow.queue(ws=WS_OTHER),
            flow.history(brief_id, Role.MARKETER, ws=WS_OTHER),
        ):
            assert r.status_code == 503, r.text
            assert r.json()["error"]["code"] == "capability_unavailable"


class TestAuditAndExport:
    def test_the_brief_trail_and_both_accounts_exports_carry_both_features(self, flow: Flow):
        brief = flow.capture().json()
        decision = flow.decide(brief["id"], "approved", 1).json()

        trail = flow.client.get(
            f"{API}/workspaces/{WS}/audit-logs",
            params={"entity_type": "recommendation_brief", "entity_id": brief["id"]},
            headers=flow.auth[Role.COMPLIANCE_REVIEWER],
        )
        assert trail.status_code == 200, trail.text
        rows = {(row["action"], row["actor_user_id"]) for row in trail.json()}
        assert rows == {
            ("recommendation_brief.created", flow.users[Role.MARKETER]),
            ("approval.approved", flow.users[Role.REVIEWER]),
        }

        def export(role: Role) -> dict:
            r = flow.client.get(f"{API}/account/export", headers=flow.auth[role])
            assert r.status_code == 200, r.text
            return r.json()

        author, reviewer = export(Role.MARKETER), export(Role.REVIEWER)
        assert [row["id"] for row in author["briefs_authored"]] == [brief["id"]]
        assert author["approvals_decided"] == []
        assert reviewer["briefs_authored"] == []
        assert [row["id"] for row in reviewer["approvals_decided"]] == [decision["id"]]
