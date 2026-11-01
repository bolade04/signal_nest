"""Phase 5B approvals at the HTTP boundary (W21 lane P5B).

    POST /api/v1/workspaces/{ws}/opportunities/{opp}/briefs/{brief_id}/approvals
    GET  /api/v1/workspaces/{ws}/opportunities/{opp}/briefs/{brief_id}/approvals
    GET  /api/v1/workspaces/{ws}/approval-queue

Real dependency resolution: only ``get_db`` is overridden, onto a fresh SQLite file per test
with foreign keys enforced, so authentication, membership, the exact-set role checkers and
the capability resolver all run. Every bearer token belongs to a live session.

Phase 5B exit criteria proven here (``docs/project-phase-5b-approval-audit-spine.md`` §4):

* 3 -- an organization holds a ``reviewer`` granted through the real role endpoint, and the
  grant is organization-wide: the same token decides in two workspaces of the organization;
* 4 -- approvals are append-only (a change of mind is a new row; the earlier row is
  byte-for-byte unchanged), bind the exact brief version, and refuse author = approver;
* 7 -- (a) a brief id that exists in another workspace, organization or opportunity answers
  the same 404 as one that does not exist; (b) a caller with no role in the organization
  answers 403;
* 8 -- with ``recommendation_briefs_enabled`` off (the shipped default) every approval
  surface answers 503 ``capability_unavailable`` and writes nothing; only the resolver's
  explicit enable opens it (a workspace override enables exactly one workspace).

Plus the latest-decision rule, the revoke rule, the queue, and the decision + audit row in
one transaction.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

import app.approvals.service as approvals_service
from app.approvals.models import Approval
from app.approvals.routes import APPROVALS_DARK
from app.audit.models import AuditLog
from app.capabilities.models import WorkspaceCapabilityOverride
from app.core.config import get_settings
from app.core.enums import Role
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.organizations.models import User
from app.tests._approvals_support import add_brief, add_evidence, add_org, add_user
from app.tests._auth2_support import active_rate_limiter, live_bearer

API = get_settings().api_prefix

ORG_A, WS_A1, WS_A2 = "org-ap-a", "ws-ap-a1", "ws-ap-a2"
ORG_B, WS_B = "org-ap-b", "ws-ap-b"

OWNER, ADMIN, MARKETER = "ap-a-owner", "ap-a-admin", "ap-a-marketer"
REVIEWER, REVIEWER2 = "ap-a-reviewer", "ap-a-reviewer2"
COMPLIANCE, VIEWER, PROMOTED = "ap-a-compliance", "ap-a-viewer", "ap-a-promoted"
B_OWNER, B_REVIEWER, B_MARKETER = "ap-b-owner", "ap-b-reviewer", "ap-b-marketer"
OUTSIDER = "ap-outsider"

ROLE_OF = {
    OWNER: Role.OWNER,
    ADMIN: Role.ADMIN,
    MARKETER: Role.MARKETER,
    REVIEWER: Role.REVIEWER,
    REVIEWER2: Role.REVIEWER,
    COMPLIANCE: Role.COMPLIANCE_REVIEWER,
    VIEWER: Role.VIEWER,
    PROMOTED: Role.MARKETER,
}
# One user per role, for the role matrices.
BY_ROLE = {
    Role.OWNER: OWNER,
    Role.ADMIN: ADMIN,
    Role.MARKETER: MARKETER,
    Role.REVIEWER: REVIEWER,
    Role.COMPLIANCE_REVIEWER: COMPLIANCE,
    Role.VIEWER: VIEWER,
}

# Briefs: A1 (opportunity 1, versions 1 and -- added by some tests -- 2), A1 second
# opportunity, A2, B; plus a brief authored by REVIEWER.
BRIEF_A1, BRIEF_A1_OTHER, BRIEF_A2, BRIEF_B = "brief-a1", "brief-a1-other", "brief-a2", "brief-b"
BRIEF_BY_REVIEWER = "brief-a1-by-reviewer"

ROLE_DENIAL = "Role '{role}' is not permitted for this action."
NOT_A_MEMBER = "You are not a member of this organization."
NOT_FOUND = "Brief not found in this workspace."


@dataclass
class Env:
    client: TestClient
    witness: Engine
    ev: dict  # key -> Evidence
    tokens: dict

    def token(self, user_id: str) -> str:
        if user_id not in self.tokens:
            self.tokens[user_id] = live_bearer(user_id)
        return self.tokens[user_id]

    def send(self, method: str, url: str, user_id: str | None, body=None, params=None):
        active_rate_limiter()._hits.clear()
        headers = {} if user_id is None else {"Authorization": f"Bearer {self.token(user_id)}"}
        kwargs = {"headers": headers, "params": params}
        if body is not None:
            kwargs["json"] = body
        return self.client.request(method, url, **kwargs)

    def decide(self, user_id: str, brief: str = BRIEF_A1, *, key: str = "a1", body=None, **fields):
        ev = self.ev[key]
        payload = body if body is not None else {"decision": "approved", "subject_version": 1}
        payload = {**payload, **fields}
        return self.send(
            "POST", decisions_url(ev.ws_id, ev.opportunity_id, brief), user_id, payload
        )

    def history(self, user_id: str, brief: str = BRIEF_A1, *, key: str = "a1", params=None):
        ev = self.ev[key]
        return self.send(
            "GET", decisions_url(ev.ws_id, ev.opportunity_id, brief), user_id, params=params
        )

    def queue(self, user_id: str, ws: str = WS_A1, params=None):
        return self.send("GET", f"{API}/workspaces/{ws}/approval-queue", user_id, params=params)

    # -- witness ----------------------------------------------------------------------
    def approvals(self) -> list[dict]:
        t = Approval.__table__
        with self.witness.connect() as c:
            rows = c.execute(select(*t.c).order_by(t.c.subject_id, t.c.decision_seq)).all()
        return [dict(r._mapping) for r in rows]

    def audit(self, action: str | None = None) -> list[dict]:
        t = AuditLog.__table__
        stmt = select(*t.c)
        if action is not None:
            stmt = stmt.where(t.c.action == action)
        with self.witness.connect() as c:
            return [dict(r._mapping) for r in c.execute(stmt.order_by(t.c.created_at)).all()]

    def count(self, model) -> int:
        with self.witness.connect() as c:
            return c.execute(select(func.count()).select_from(model.__table__)).scalar_one()

    def seed(self, fn) -> None:
        """Run ``fn(session)`` in a committed witness transaction (extra fixture rows)."""
        with sessionmaker(bind=self.witness, expire_on_commit=False, future=True)() as s:
            fn(s)
            s.commit()


def decisions_url(ws: str, opportunity: str, brief: str) -> str:
    return f"{API}/workspaces/{ws}/opportunities/{opportunity}/briefs/{brief}/approvals"


def _engine(db_file: Path) -> Engine:
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
    users = {uid: add_user(s, uid, org_id=ORG_A, role=role) for uid, role in ROLE_OF.items()}
    for uid, role in (
        (B_OWNER, Role.OWNER),
        (B_REVIEWER, Role.REVIEWER),
        (B_MARKETER, Role.MARKETER),
    ):
        users[uid] = add_user(s, uid, org_id=ORG_B, role=role)
    add_user(s, OUTSIDER)
    ev = {
        "a1": add_evidence(s, org_id=ORG_A, ws_id=WS_A1, key="ap-a1"),
        "a1-other": add_evidence(s, org_id=ORG_A, ws_id=WS_A1, key="ap-a1o"),
        "a2": add_evidence(s, org_id=ORG_A, ws_id=WS_A2, key="ap-a2"),
        "b": add_evidence(s, org_id=ORG_B, ws_id=WS_B, key="ap-b"),
    }
    add_brief(s, ev["a1"], brief_id=BRIEF_A1, author=users[MARKETER])
    add_brief(s, ev["a1-other"], brief_id=BRIEF_A1_OTHER, author=users[MARKETER])
    add_brief(s, ev["a2"], brief_id=BRIEF_A2, author=users[MARKETER])
    add_brief(s, ev["b"], brief_id=BRIEF_B, author=users[B_MARKETER])
    return ev


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    request_engine, witness = _engine(tmp_path / "approvals.db"), _engine(tmp_path / "approvals.db")
    Base.metadata.create_all(request_engine)
    factory = sessionmaker(
        bind=request_engine, autoflush=False, expire_on_commit=False, future=True
    )
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
        yield Env(TestClient(app), witness, ev, {})
    finally:
        app.dependency_overrides.clear()
        active_rate_limiter()._hits.clear()
        request_engine.dispose()
        witness.dispose()


@pytest.fixture(autouse=True)
def _dark_by_default(monkeypatch):
    # The shipped default; a test opts in with ``briefs_on``.
    monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", False)


@pytest.fixture
def briefs_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", True)


def _error(r) -> tuple[int, str | None, str | None]:
    payload = r.json() if r.content else {}
    err = payload.get("error") if isinstance(payload, dict) else None
    return r.status_code, (err or {}).get("code"), (err or {}).get("message")


# --------------------------------------------------------------------------- #
# Exit criterion 8: dark by default, fail-closed, resolver-decided
# --------------------------------------------------------------------------- #
class TestDark:
    def test_every_approval_surface_is_503_while_dark_and_writes_nothing(self, env: Env):
        before = (env.count(Approval), env.count(AuditLog))
        answers = [
            env.decide(REVIEWER),
            env.history(OWNER),
            env.history(REVIEWER),
            env.queue(REVIEWER),
        ]
        for r in answers:
            assert _error(r) == (503, "capability_unavailable", APPROVALS_DARK), r.text
        assert (env.count(Approval), env.count(AuditLog)) == before

    def test_a_workspace_override_enables_exactly_that_workspace(self, env: Env):
        # The global flag stays off; an honoured override enables WS_A1 only.
        env.seed(
            lambda s: s.add(
                WorkspaceCapabilityOverride(
                    organization_id=ORG_A,
                    workspace_id=WS_A1,
                    capability="recommendation_briefs",
                    enabled=True,
                )
            )
        )
        assert env.decide(REVIEWER).status_code == 201
        assert env.queue(REVIEWER, WS_A1).status_code == 200
        dark = env.decide(REVIEWER, BRIEF_A2, key="a2")
        assert _error(dark)[:2] == (503, "capability_unavailable")
        assert _error(env.queue(REVIEWER, WS_A2))[:2] == (503, "capability_unavailable")

    def test_a_resolver_failure_denies_and_writes_nothing(self, env: Env, monkeypatch):
        import app.approvals.routes as routes

        def boom(**_kwargs):
            raise RuntimeError("override storage unavailable")

        monkeypatch.setattr(routes, "resolve_capability", boom)
        monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", True)
        client = TestClient(app, raise_server_exceptions=False)
        active_rate_limiter()._hits.clear()
        ev = env.ev["a1"]
        r = client.post(
            decisions_url(ev.ws_id, ev.opportunity_id, BRIEF_A1),
            json={"decision": "approved", "subject_version": 1},
            headers={"Authorization": f"Bearer {env.token(REVIEWER)}"},
        )
        assert r.status_code == 500
        assert env.count(Approval) == 0 and env.audit("approval.approved") == []


# --------------------------------------------------------------------------- #
# Recording a decision (exit criterion 4)
# --------------------------------------------------------------------------- #
class TestDecide:
    def test_a_reviewer_approves_the_exact_version(self, env: Env, briefs_on):
        r = env.decide(REVIEWER, reason_code="evidence_sufficient", note="Looks right.")
        assert r.status_code == 201, r.text
        body = r.json()
        assert set(body) == {
            "id",
            "workspace_id",
            "subject_type",
            "subject_id",
            "subject_version",
            "brief_id",
            "decision",
            "decision_seq",
            "reason_code",
            "note",
            "decided_by",
            "decided_by_snapshot",
            "decided_role",
            "is_simulated",
            "created_at",
        }
        assert body["subject_type"] == "recommendation_brief"
        assert body["subject_id"] == body["brief_id"] == BRIEF_A1
        assert (body["subject_version"], body["decision_seq"]) == (1, 1)
        assert (body["decision"], body["reason_code"], body["note"]) == (
            "approved",
            "evidence_sufficient",
            "Looks right.",
        )
        assert body["decided_by"] == REVIEWER and body["decided_role"] == "reviewer"
        assert body["decided_by_snapshot"] == (
            f"{REVIEWER} Name {REVIEWER} <{REVIEWER}@example.test>"
        )
        assert body["is_simulated"] is True  # copied from the (simulated) brief
        stored = env.approvals()
        assert len(stored) == 1 and stored[0]["organization_id"] == ORG_A
        assert stored[0]["workspace_id"] == WS_A1

    def test_the_decision_and_its_audit_row_commit_together(self, env: Env, briefs_on):
        r = env.decide(REVIEWER, reason_code="evidence_sufficient")
        rows = env.audit("approval.approved")
        assert len(rows) == 1
        row = rows[0]
        assert (row["organization_id"], row["workspace_id"], row["actor_user_id"]) == (
            ORG_A,
            WS_A1,
            REVIEWER,
        )
        assert (row["entity_type"], row["entity_id"]) == ("recommendation_brief", BRIEF_A1)
        assert row["previous_state"] is None
        assert row["new_state"] == {
            "approval_id": r.json()["id"],
            "decision": "approved",
            "decision_seq": 1,
            "subject_type": "recommendation_brief",
            "subject_version": 1,
            "reason_code": "evidence_sufficient",
        }
        # A change of mind records the formerly controlling decision as previous_state.
        env.decide(REVIEWER2, decision="rejected", reason_code="off_strategy")
        rejected = env.audit("approval.rejected")
        assert rejected[0]["previous_state"] == {"decision": "approved", "decision_seq": 1}

    def test_a_failing_audit_write_rolls_the_decision_back(self, env: Env, briefs_on, monkeypatch):
        def refuse(*_a, **_k):
            raise RuntimeError("audit store unavailable")

        monkeypatch.setattr(approvals_service, "record_audit", refuse)
        client = TestClient(app, raise_server_exceptions=False)
        active_rate_limiter()._hits.clear()
        ev = env.ev["a1"]
        r = client.post(
            decisions_url(ev.ws_id, ev.opportunity_id, BRIEF_A1),
            json={"decision": "approved", "subject_version": 1},
            headers={"Authorization": f"Bearer {env.token(REVIEWER)}"},
        )
        assert r.status_code == 500
        assert env.count(Approval) == 0

    def test_the_note_and_identity_never_reach_the_audit_row(self, env: Env, briefs_on):
        env.decide(REVIEWER, note="private remark 7f3a")
        text = repr(env.audit("approval.approved"))
        assert "private remark 7f3a" not in text
        assert f"{REVIEWER}@example.test" not in text and f"Name {REVIEWER}" not in text

    def test_the_author_cannot_decide_on_their_own_brief(self, env: Env, briefs_on):
        def seed(s):
            reviewer = s.get(User, REVIEWER)
            add_brief(
                s,
                env.ev["a1-other"],
                brief_id=BRIEF_BY_REVIEWER,
                author=reviewer,
                version=2,
                parent_id=BRIEF_A1_OTHER,
            )

        env.seed(seed)
        r = env.decide(REVIEWER, BRIEF_BY_REVIEWER, key="a1-other", subject_version=2)
        assert _error(r) == (
            403,
            "approval_author_conflict",
            approvals_service.AUTHOR_CANNOT_DECIDE,
        )
        for decision in ("rejected",):
            r = env.decide(
                REVIEWER, BRIEF_BY_REVIEWER, key="a1-other", subject_version=2, decision=decision
            )
            assert r.status_code == 403
        assert env.count(Approval) == 0 and env.audit("approval.approved") == []
        # Another reviewer may decide on it.
        ok = env.decide(REVIEWER2, BRIEF_BY_REVIEWER, key="a1-other", subject_version=2)
        assert ok.status_code == 201

    def test_the_author_is_recognised_by_the_snapshot_after_the_fk_is_nulled(
        self, env: Env, briefs_on
    ):
        def seed(s):
            reviewer = s.get(User, REVIEWER)
            add_brief(
                s,
                env.ev["a1-other"],
                brief_id=BRIEF_BY_REVIEWER,
                author=reviewer,
                version=2,
                parent_id=BRIEF_A1_OTHER,
                created_by=None,
            )

        env.seed(seed)
        r = env.decide(REVIEWER, BRIEF_BY_REVIEWER, key="a1-other", subject_version=2)
        assert _error(r)[:2] == (403, "approval_author_conflict")

    def test_an_erased_snapshot_names_nobody(self, env: Env, briefs_on):
        env.seed(
            lambda s: add_brief(
                s,
                env.ev["a1-other"],
                brief_id=BRIEF_BY_REVIEWER,
                author=None,
                version=2,
                parent_id=BRIEF_A1_OTHER,
            )
        )
        r = env.decide(REVIEWER, BRIEF_BY_REVIEWER, key="a1-other", subject_version=2)
        assert r.status_code == 201

    @pytest.mark.parametrize("version", [2, 7])
    def test_a_version_other_than_the_briefs_is_refused(self, env: Env, briefs_on, version):
        r = env.decide(REVIEWER, subject_version=version)
        assert _error(r) == (409, "approval_stale_version", approvals_service.STALE_VERSION)
        assert env.count(Approval) == 0

    def test_reason_polarity_is_enforced(self, env: Env, briefs_on):
        r = env.decide(REVIEWER, reason_code="off_strategy")  # a rejection reason
        assert r.status_code == 422
        r = env.decide(REVIEWER, decision="rejected", reason_code="evidence_sufficient")
        assert r.status_code == 422
        assert env.count(Approval) == 0

    @pytest.mark.parametrize(
        "smuggled",
        ["decision_seq", "decided_by", "decided_role", "workspace_id", "brief_id", "is_simulated"],
    )
    def test_server_derived_fields_are_refused(self, env: Env, briefs_on, smuggled):
        r = env.decide(REVIEWER, **{smuggled: "x"})
        assert r.status_code == 422
        assert env.count(Approval) == 0

    def test_the_note_is_bounded(self, env: Env, briefs_on):
        assert env.decide(REVIEWER, note="n" * 1001).status_code == 422
        assert env.decide(REVIEWER, note="n" * 1000).status_code == 201


# --------------------------------------------------------------------------- #
# The latest-decision rule, append-only history, the revoke rule
# --------------------------------------------------------------------------- #
class TestLatestDecision:
    def test_the_highest_decision_seq_controls_and_history_is_newest_first(
        self, env: Env, briefs_on
    ):
        assert env.decide(REVIEWER).json()["decision_seq"] == 1
        assert env.decide(REVIEWER2, decision="rejected").json()["decision_seq"] == 2
        assert env.decide(REVIEWER).json()["decision_seq"] == 3
        r = env.history(OWNER)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 3 and (body["limit"], body["offset"]) == (20, 0)
        assert [(i["decision_seq"], i["decision"]) for i in body["items"]] == [
            (3, "approved"),
            (2, "rejected"),
            (1, "approved"),
        ]
        page = env.history(OWNER, params={"limit": 1, "offset": 1}).json()
        assert [i["decision_seq"] for i in page["items"]] == [2] and page["total"] == 3

    def test_the_service_reads_the_controlling_decision(self, env: Env, briefs_on):
        from app.briefs.models import RecommendationBrief

        env.decide(REVIEWER)
        env.decide(REVIEWER2, decision="rejected")
        with sessionmaker(bind=env.witness, future=True)() as s:
            brief = s.get(RecommendationBrief, BRIEF_A1)
            latest = approvals_service.controlling_decision(
                s, subject_type="recommendation_brief", subject_id=BRIEF_A1, subject_version=1
            )
            assert (latest.decision, latest.decision_seq) == ("rejected", 2)
            # An approved row exists, and the brief is NOT approved: the latest row decides.
            assert approvals_service.is_approved(s, brief=brief) is False

    def test_a_change_of_mind_is_a_new_row_and_the_first_is_unchanged(self, env: Env, briefs_on):
        env.decide(REVIEWER, reason_code="evidence_sufficient", note="first")
        first = env.approvals()[0]
        env.decide(REVIEWER, decision="rejected", reason_code="inaccurate", note="second")
        rows = env.approvals()
        assert len(rows) == 2 and rows[0] == first

    def test_revoke_withdraws_only_a_controlling_approval(self, env: Env, briefs_on):
        nothing = env.decide(REVIEWER, decision="revoked")
        assert _error(nothing) == (
            409,
            "approval_nothing_to_revoke",
            approvals_service.NOTHING_TO_REVOKE,
        )
        env.decide(REVIEWER, decision="rejected")
        assert env.decide(REVIEWER, decision="revoked").status_code == 409
        env.decide(REVIEWER, decision="approved")
        revoked = env.decide(REVIEWER2, decision="revoked", reason_code="approved_in_error")
        assert revoked.status_code == 201 and revoked.json()["decision_seq"] == 3
        assert env.decide(REVIEWER, decision="revoked").status_code == 409
        assert [r["decision"] for r in env.approvals()] == ["rejected", "approved", "revoked"]
        assert len(env.audit("approval.revoked")) == 1

    def test_a_decision_binds_its_version_and_not_its_successor(self, env: Env, briefs_on):
        env.decide(REVIEWER)

        def seed(s):
            marketer = s.get(User, MARKETER)
            add_brief(
                s,
                env.ev["a1"],
                brief_id="brief-a1-v2",
                author=marketer,
                version=2,
                parent_id=BRIEF_A1,
            )

        env.seed(seed)
        # The successor has no decision; v1's approval is not v2's.
        assert env.history(OWNER, "brief-a1-v2").json()["total"] == 0
        assert env.decide(REVIEWER, "brief-a1-v2", subject_version=1).status_code == 409
        v2 = env.decide(REVIEWER, "brief-a1-v2", subject_version=2)
        assert v2.status_code == 201
        assert (v2.json()["subject_version"], v2.json()["decision_seq"]) == (2, 1)
        assert env.history(OWNER).json()["total"] == 1


# --------------------------------------------------------------------------- #
# Roles (exact sets) and organization-wide approver authority (exit criterion 3)
# --------------------------------------------------------------------------- #
class TestRoles:
    @pytest.mark.parametrize("role", [r for r in Role if r is not Role.REVIEWER])
    def test_only_the_reviewer_decides(self, env: Env, briefs_on, role: Role):
        r = env.decide(BY_ROLE[role])
        assert _error(r) == (403, "permission_denied", ROLE_DENIAL.format(role=role.value))
        assert env.count(Approval) == 0

    @pytest.mark.parametrize("role", list(Role))
    def test_history_readers_are_every_role_but_viewer(self, env: Env, briefs_on, role: Role):
        r = env.history(BY_ROLE[role])
        if role is Role.VIEWER:
            assert _error(r) == (403, "permission_denied", ROLE_DENIAL.format(role="viewer"))
        else:
            assert r.status_code == 200, (role, r.text)

    @pytest.mark.parametrize("role", list(Role))
    def test_the_queue_is_the_reviewers_alone(self, env: Env, briefs_on, role: Role):
        r = env.queue(BY_ROLE[role])
        if role is Role.REVIEWER:
            assert r.status_code == 200
        else:
            assert _error(r) == (403, "permission_denied", ROLE_DENIAL.format(role=role.value))

    def test_anonymous_is_401_everywhere(self, env: Env, briefs_on):
        for r in (env.decide(None), env.history(None), env.queue(None)):  # type: ignore[arg-type]
            assert r.status_code == 401

    def test_a_reviewer_grant_is_organization_wide(self, env: Env, briefs_on):
        # Before: PROMOTED is a marketer, refused in both workspaces of the organization.
        assert env.decide(PROMOTED).status_code == 403
        assert env.decide(PROMOTED, BRIEF_A2, key="a2").status_code == 403
        # The owner grants REVIEWER through the real role-assignment endpoint (audited).
        grant = env.send(
            "PUT",
            f"{API}/organizations/{ORG_A}/members/{PROMOTED}/role",
            OWNER,
            {"role": "reviewer"},
        )
        assert grant.status_code == 200 and grant.json()["role"] == "reviewer"
        changed = env.audit("organization_member.role_changed")
        assert [(c["entity_id"], c["new_state"]) for c in changed] == [
            (PROMOTED, {"role": "reviewer"})
        ]
        # After: the same token decides in BOTH workspaces, and reads both queues.
        one = env.decide(PROMOTED)
        two = env.decide(PROMOTED, BRIEF_A2, key="a2")
        assert (one.status_code, two.status_code) == (201, 201)
        assert {one.json()["workspace_id"], two.json()["workspace_id"]} == {WS_A1, WS_A2}
        assert {r["decided_role"] for r in env.approvals()} == {"reviewer"}
        assert env.queue(PROMOTED, WS_A2).status_code == 200
        # The grant stops at the organization boundary.
        assert _error(env.decide(PROMOTED, BRIEF_B, key="b"))[2] == NOT_A_MEMBER

    def test_a_demoted_reviewer_loses_authority_with_the_same_token(self, env: Env, briefs_on):
        assert env.decide(REVIEWER).status_code == 201
        demote = env.send(
            "PUT", f"{API}/organizations/{ORG_A}/members/{REVIEWER}/role", OWNER, {"role": "viewer"}
        )
        assert demote.status_code == 200
        assert _error(env.decide(REVIEWER, decision="rejected"))[:2] == (403, "permission_denied")


# --------------------------------------------------------------------------- #
# Isolation (exit criterion 7: two classes)
# --------------------------------------------------------------------------- #
class TestIsolation:
    def test_entity_level_mismatch_is_one_non_enumerating_404(self, env: Env, briefs_on):
        a1, a2, b = env.ev["a1"], env.ev["a2"], env.ev["b"]
        probes = {
            "another workspace of the organization": decisions_url(
                WS_A1, a1.opportunity_id, BRIEF_A2
            ),
            "another organization": decisions_url(WS_A1, a1.opportunity_id, BRIEF_B),
            "another opportunity of the workspace": decisions_url(
                WS_A1, a1.opportunity_id, BRIEF_A1_OTHER
            ),
            "another workspace's opportunity": decisions_url(WS_A1, a2.opportunity_id, BRIEF_A2),
            "another organization's opportunity": decisions_url(WS_A1, b.opportunity_id, BRIEF_B),
            "does not exist": decisions_url(WS_A1, a1.opportunity_id, "brief-never-created"),
        }
        before = env.approvals()
        for label, url in probes.items():
            post = env.send("POST", url, REVIEWER, {"decision": "approved", "subject_version": 1})
            get = env.send("GET", url, OWNER)
            assert _error(post) == (404, "not_found", NOT_FOUND), label
            assert _error(get) == (404, "not_found", NOT_FOUND), label
        assert env.approvals() == before == []
        assert env.audit("approval.approved") == []

    def test_the_organization_membership_gate_is_403(self, env: Env, briefs_on):
        b = env.ev["b"]
        url = decisions_url(WS_B, b.opportunity_id, BRIEF_B)
        for user in (REVIEWER, OWNER, OUTSIDER):
            post = env.send("POST", url, user, {"decision": "approved", "subject_version": 1})
            assert _error(post) == (403, "permission_denied", NOT_A_MEMBER), user
            assert _error(env.send("GET", url, user)) == (403, "permission_denied", NOT_A_MEMBER)
        assert _error(env.queue(REVIEWER, WS_B)) == (403, "permission_denied", NOT_A_MEMBER)
        # B's own reviewer reaches it (positive control) and A's queue stays A's.
        assert (
            env.send(
                "POST", url, B_REVIEWER, {"decision": "approved", "subject_version": 1}
            ).status_code
            == 201
        )
        assert [r["subject_id"] for r in env.approvals()] == [BRIEF_B]

    def test_the_queue_lists_only_its_own_workspace(self, env: Env, briefs_on):
        ids = {i["id"] for i in env.queue(REVIEWER, WS_A1).json()["items"]}
        assert ids == {BRIEF_A1, BRIEF_A1_OTHER}
        assert {i["id"] for i in env.queue(REVIEWER, WS_A2).json()["items"]} == {BRIEF_A2}
        assert {i["id"] for i in env.queue(B_REVIEWER, WS_B).json()["items"]} == {BRIEF_B}


# --------------------------------------------------------------------------- #
# The approval queue
# --------------------------------------------------------------------------- #
class TestQueue:
    def _ids(self, env: Env, ws: str = WS_A1) -> list[str]:
        r = env.queue(REVIEWER, ws)
        assert r.status_code == 200, r.text
        return [i["id"] for i in r.json()["items"]]

    def test_approved_and_rejected_leave_the_queue_revoked_returns(self, env: Env, briefs_on):
        assert set(self._ids(env)) == {BRIEF_A1, BRIEF_A1_OTHER}
        env.decide(REVIEWER)
        assert self._ids(env) == [BRIEF_A1_OTHER]
        env.decide(REVIEWER, BRIEF_A1_OTHER, key="a1-other", decision="rejected")
        assert self._ids(env) == []
        env.decide(REVIEWER2, decision="revoked")
        assert self._ids(env) == [BRIEF_A1]

    def test_a_superseded_version_leaves_the_queue_for_its_successor(self, env: Env, briefs_on):
        def seed(s):
            marketer = s.get(User, MARKETER)
            add_brief(
                s,
                env.ev["a1"],
                brief_id="brief-a1-v2",
                author=marketer,
                version=2,
                parent_id=BRIEF_A1,
            )

        env.seed(seed)
        assert set(self._ids(env)) == {"brief-a1-v2", BRIEF_A1_OTHER}

    def test_the_queue_is_oldest_first_and_paged(self, env: Env, briefs_on):
        r = env.queue(REVIEWER, WS_A1, params={"limit": 1})
        body = r.json()
        assert body["total"] == 2 and len(body["items"]) == 1
        assert body["items"][0]["id"] == BRIEF_A1  # seeded first
        second = env.queue(REVIEWER, WS_A1, params={"limit": 1, "offset": 1}).json()
        assert second["items"][0]["id"] == BRIEF_A1_OTHER
        assert env.queue(REVIEWER, WS_A1, params={"limit": 101}).status_code == 422

    def test_queue_items_are_the_frozen_brief_projection(self, env: Env, briefs_on):
        item = env.queue(REVIEWER).json()["items"][0]
        for hidden in (
            "organization_id",
            "workspace_id",
            "input_hash",
            "trace_id",
            "llm_usage_event_id",
        ):
            assert hidden not in item
        assert item["created_by"] == MARKETER and item["brief_version"] == 1

    def test_reads_never_write(self, env: Env, briefs_on):
        env.decide(REVIEWER)
        before = (env.approvals(), env.count(AuditLog))
        env.history(OWNER)
        env.queue(REVIEWER)
        assert (env.approvals(), env.count(AuditLog)) == before


# --------------------------------------------------------------------------- #
# decision_seq: the lock precedes the read; a clash is a 409 that writes nothing
# --------------------------------------------------------------------------- #
class TestSequence:
    def test_the_subject_lock_is_select_for_update_on_postgresql(self):
        from sqlalchemy.dialects import postgresql

        sql = str(
            approvals_service.subject_lock_select("brief-x").compile(dialect=postgresql.dialect())
        )
        assert "FROM recommendation_briefs" in sql and sql.rstrip().endswith("FOR UPDATE")

    def test_the_lock_is_taken_before_the_sequence_and_the_latest_decision_are_read(
        self, env: Env, briefs_on, monkeypatch
    ):
        order: list[str] = []
        real = {
            name: getattr(approvals_service, name)
            for name in ("_lock_subject", "_next_decision_seq", "controlling_decision")
        }

        def spy(name):
            def wrapper(*args, **kwargs):
                order.append(name)
                return real[name](*args, **kwargs)

            return wrapper

        for name in real:
            monkeypatch.setattr(approvals_service, name, spy(name))
        assert env.decide(REVIEWER).status_code == 201
        assert order == ["_lock_subject", "controlling_decision", "_next_decision_seq"]

    def test_a_sequence_clash_is_a_409_and_writes_nothing(self, env: Env, briefs_on, monkeypatch):
        assert env.decide(REVIEWER).status_code == 201
        before = (env.approvals(), env.audit())
        # A stale sequence read (what a REPEATABLE READ snapshot or a lockless writer sees).
        monkeypatch.setattr(approvals_service, "_next_decision_seq", lambda *_a, **_k: 1)
        r = env.decide(REVIEWER2, decision="rejected")
        assert _error(r) == (
            409,
            "approval_concurrent_decision",
            approvals_service.CONCURRENT_DECISION,
        )
        assert (env.approvals(), env.audit()) == before

    @pytest.mark.parametrize(
        ("message", "clash"),
        [
            ('duplicate key value violates unique constraint "uq_approval_decision_seq"', True),
            (
                "UNIQUE constraint failed: approvals.subject_type, approvals.subject_id, "
                "approvals.subject_version, approvals.decision_seq",
                True,
            ),
            ('new row violates check constraint "ck_approval_reason_polarity"', False),
            ("FOREIGN KEY constraint failed", False),
            ("UNIQUE constraint failed: audit_logs.id", False),
        ],
    )
    def test_only_the_sequence_key_counts_as_a_clash(self, message, clash):
        from sqlalchemy.exc import IntegrityError

        exc = IntegrityError("INSERT ...", {}, Exception(message))
        assert approvals_service._is_sequence_clash(exc) is clash
