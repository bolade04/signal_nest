"""Project Phase 5A exit criterion 6, adversarially: brief isolation and the dark gate.

Adopted from the 5A-W4 adversarial battery of review W21-REV-P5A-FEATURE r1 (condition C1;
``adversarial/test_rev_p5a_briefs_adversarial.py``, sha256 2f23ccfa...f1a3), classes
``TestSameOrgCrossWorkspace`` and ``TestDark`` as written by the reviewer.

``test_briefs_api.py`` puts each market in its own organization; this module adds the case EC6
names - another workspace of the SAME organization - and the dark-before-lookup ordering:

* same-organization cross-workspace and cross-organization opportunity, brief, intelligence
  record and campaign ids each answer a 404 byte-identical (request id aside) to a missing
  id, with no model call and no usage row;
* with the flag off, every brief endpoint answers 503 ``capability_unavailable`` before any
  scope lookup (so a dark deployment cannot be probed by id), for every role; the overrides
  are honoured exactly.

5A-W4 additions (W21 lane ADV5AB), each pinned by the mutant it kills (lane evidence
P6-W21-ADV5AB-01):

* every test of the world below also runs on PostgreSQL when ``TEST_POSTGRES_URL`` is set;
* a brief's ``evidence_refs`` name only its own opportunity's validation evidence, never a
  sibling opportunity's or another tenant's;
* on PostgreSQL, a capture in flight (holding its opportunity's row lock) never blocks a
  capture of another opportunity, workspace or organization.

World (SQLite with FKs on, or PostgreSQL; one database per test):
  org-a: ws-a1 and ws-a2 (SAME organization, so every org-a member reaches both workspaces)
  org-b: ws-b1
  org-a has one member of each of the six roles; org-b an owner; plus an outsider.
  Opportunities: opp-a1, opp-a1s (sibling in ws-a1), opp-a2, opp-b1; one intelligence record
  each (sir-*), one campaign per workspace (camp-a1, camp-a2, camp-b1).

Only get_db and get_llm_service are overridden; authentication, tenancy and the role checkers
run for real. A spy provider counts every model call. No brief, approval or audit row is ever
updated or deleted: each test has a fresh database.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.briefs.models import RecommendationBrief
from app.capabilities.models import WorkspaceCapabilityOverride
from app.core.config import get_settings
from app.core.enums import Role
from app.db.models import Base
from app.db.session import get_db
from app.llm.mock import MockLLMProvider
from app.llm.models import LLMUsageEvent
from app.llm.service import LLMService, get_llm_service
from app.main import app
from app.tests._auth2_support import live_bearer, pg_database, require_postgres
from app.tests._llm_offline import offline_llm, offline_settings
from app.tests.fixtures.briefs import HOLD_SECONDS, add_validation_evidence, capture_hold

API = get_settings().api_prefix
ROLES = [Role.OWNER, Role.ADMIN, Role.MARKETER, Role.REVIEWER, Role.COMPLIANCE_REVIEWER,
         Role.VIEWER]
EDITOR_ROLES = {"owner", "admin", "marketer"}


@pytest.fixture(scope="module", autouse=True)
def _offline() -> Iterator[None]:
    with offline_llm():
        yield


class SpyMock(MockLLMProvider):
    """The deterministic mock, counting calls."""

    calls = 0

    def generate(self, request, prompt_name, prompt_version):
        SpyMock.calls += 1
        return super().generate(request, prompt_name, prompt_version)



def _seed(s) -> dict:
    from app.brands.models import Brand
    from app.campaign_context.models import Campaign
    from app.intelligence.records import SignalIntelligenceRecord
    from app.opportunities.models import Opportunity
    from app.organizations.models import Organization, OrganizationMember, User, Workspace
    from app.scouting_requests.models import ScoutRequest
    from app.signals.models import NormalizedSignal, RawSignal

    tokens: dict[str, dict] = {}

    def user(uid: str, org: str | None, role: str | None):
        s.add(User(id=uid, email=f"{uid}@example.com", full_name=f"Name {uid}",
                   hashed_password="x", is_active=True))
        s.flush()
        if org:
            s.add(OrganizationMember(id=f"m-{uid}-{org}", organization_id=org, user_id=uid,
                                     role=role))
            s.flush()

    for org in ("org-a", "org-b"):
        s.add(Organization(id=org, name=org, slug=org))
    s.flush()
    spaces = {"ws-a1": "org-a", "ws-a2": "org-a", "ws-b1": "org-b"}
    for ws, org in spaces.items():
        s.add(Workspace(id=ws, organization_id=org, name=ws, slug=ws))
        s.flush()
        s.add(Brand(id=f"br-{ws}", organization_id=org, workspace_id=ws, name=ws))
        s.flush()
        s.add(ScoutRequest(id=f"sr-{ws}", organization_id=org, workspace_id=ws,
                           brand_id=f"br-{ws}", name="scout"))
        s.add(Campaign(id=f"camp-{ws[3:]}", organization_id=org, workspace_id=ws,
                       brand_id=f"br-{ws}", name="c"))
        s.flush()
    opps = {"a1": "ws-a1", "a1s": "ws-a1", "a2": "ws-a2", "b1": "ws-b1"}
    for key, ws in opps.items():
        org = spaces[ws]
        s.add(RawSignal(id=f"raw-{key}", organization_id=org, workspace_id=ws,
                        scout_request_id=f"sr-{ws}", source_type="rss_news", content="c"))
        s.flush()
        s.add(NormalizedSignal(id=f"ns-{key}", organization_id=org, workspace_id=ws,
                               scout_request_id=f"sr-{ws}", raw_signal_id=f"raw-{key}",
                               source_type="rss_news", excerpt="e", content_hash=f"h-{key}",
                               pain_point_dna="speed_complaint"))
        s.flush()
        s.add(Opportunity(id=f"opp-{key}", organization_id=org, workspace_id=ws,
                          brand_id=f"br-{ws}", scout_request_id=f"sr-{ws}", title=key,
                          classification="emerging", decision="monitor",
                          opportunity_score=50, resolved_market="Dallas",
                          audience_fit="commuters"))
        s.flush()
        s.add(SignalIntelligenceRecord(
            id=f"sir-{key}", organization_id=org, workspace_id=ws,
            scout_request_id=f"sr-{ws}", normalized_signal_id=f"ns-{key}",
            opportunity_id=f"opp-{key}", analysis_version="3c", scoring_version="3c.1",
            fingerprint=f"fp-{key}", enricher="deterministic", accepted=True,
            classification="emerging"))
        s.flush()
    for role in ROLES:
        user(f"a-{role.value}", "org-a", role.value)
    user("b-owner", "org-b", "owner")
    user("outsider", None, None)
    for uid in [f"a-{r.value}" for r in ROLES] + ["b-owner", "outsider"]:
        tokens[uid] = {"Authorization": f"Bearer {live_bearer(uid, s)}"}
    return tokens


@dataclass
class W:
    client: TestClient
    make: sessionmaker
    auth: dict

    def url(self, ws="ws-a1", opp="opp-a1", brief=None):
        base = f"{API}/workspaces/{ws}/opportunities/{opp}/briefs"
        return f"{base}/{brief}" if brief else base

    def create(self, who="a-owner", ws="ws-a1", opp="opp-a1", record="sir-a1", campaign=None):
        body = {"intelligence_record_id": record}
        if campaign is not None:
            body["campaign_id"] = campaign
        return self.client.post(self.url(ws, opp), json=body, headers=self.auth[who])

    def count(self, model) -> int:
        with self.make() as s:
            return int(s.scalar(select(func.count()).select_from(model)) or 0)

    def override(self, org, ws, capability, enabled):
        with self.make() as s:
            s.add(WorkspaceCapabilityOverride(organization_id=org, workspace_id=ws,
                                              capability=capability, enabled=enabled,
                                              reason="rev"))
            s.commit()


def _reset_rate_limiter() -> None:
    from app.core.middleware import RateLimitMiddleware

    stack = getattr(app, "middleware_stack", None)
    while stack is not None:
        if isinstance(stack, RateLimitMiddleware):
            stack._hits.clear()
            return
        stack = getattr(stack, "app", None)


@pytest.fixture(params=["sqlite", "postgresql"])
def w(request, tmp_path, monkeypatch) -> Iterator[W]:
    # 5A-W4 (W21 lane ADV5AB): every isolation and dark-gate test also runs on PostgreSQL,
    # the production engine, when TEST_POSTGRES_URL is set (require_postgres: unset FAILS in
    # CI and skips only on a developer machine). Each test gets a database of its own.
    if request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'rev.db'}",
                               connect_args={"check_same_thread": False}, future=True)

        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        yield from _world(engine, monkeypatch)
        return
    require_postgres()
    with pg_database("sn_w4bi") as url:  # pragma: no cover - gated on PostgreSQL
        yield from _world(create_engine(url, future=True), monkeypatch)


def _world(engine, monkeypatch) -> Iterator[W]:
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with make() as s:
        tokens = _seed(s)
        s.commit()

    def _get_db():
        s = make()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", False)
    app.dependency_overrides[get_db] = _get_db
    SpyMock.calls = 0
    app.dependency_overrides[get_llm_service] = lambda: LLMService(
        SpyMock(seed="rev"), sleep=lambda _s: None, settings=offline_settings())
    _reset_rate_limiter()
    try:
        yield W(TestClient(app), make, tokens)
    finally:
        app.dependency_overrides.clear()
        _reset_rate_limiter()
        engine.dispose()


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", True)


def _normalised(r) -> bytes:
    """The raw body with this response's own request id replaced (the only per-request value)."""
    rid = r.json()["error"].get("request_id")
    body = r.content
    return body.replace(rid.encode(), b"<RID>") if rid else body


def _same_404(*responses):
    assert {r.status_code for r in responses} == {404}, [r.status_code for r in responses]
    bodies = {_normalised(r) for r in responses}
    assert len(bodies) == 1, bodies
    lengths = {r.headers.get("content-length") for r in responses}
    assert len(lengths) == 1, lengths
    ctypes = {r.headers.get("content-type") for r in responses}
    assert len(ctypes) == 1, ctypes


class TestSameOrgCrossWorkspace:
    def test_foreign_opportunity_is_byte_identical_to_missing(self, w, on):
        h = w.auth["a-owner"]
        missing = w.client.get(w.url(opp="no-such-opp"), headers=h)
        same_org = w.client.get(w.url(opp="opp-a2"), headers=h)
        other_org = w.client.get(w.url(opp="opp-b1"), headers=h)
        _same_404(missing, same_org, other_org)
        for opp in ("no-such-opp", "opp-a2", "opp-b1"):
            r = w.client.get(w.url(opp=opp, brief="0" * 32), headers=h)
            assert r.status_code == 404
        posts = [w.create(opp=o, record="sir-a1") for o in ("no-such-opp", "opp-a2", "opp-b1")]
        _same_404(*posts)
        assert SpyMock.calls == 0 and w.count(LLMUsageEvent) == 0

    def test_foreign_brief_is_byte_identical_to_missing(self, w, on):
        # Briefs that exist elsewhere: same-org other workspace, other org, sibling opportunity.
        a2 = w.create(ws="ws-a2", opp="opp-a2", record="sir-a2").json()["id"]
        b1 = w.create("b-owner", ws="ws-b1", opp="opp-b1", record="sir-b1").json()["id"]
        sib = w.create(opp="opp-a1s", record="sir-a1s").json()["id"]
        own = w.create().json()["id"]
        h = w.auth["a-viewer"]
        assert w.client.get(w.url(brief=own), headers=h).status_code == 200
        answers = [w.client.get(w.url(brief=b), headers=h)
                   for b in (a2, b1, sib, "0" * 32, "x")]
        _same_404(*answers)
        # The brief's own workspace path with the wrong opportunity is 404 too.
        r = w.client.get(w.url(ws="ws-a2", opp="opp-a1", brief=a2), headers=h)
        assert r.status_code == 404
        # And the right workspace + opportunity path for a2 works for an org-a member.
        assert w.client.get(w.url(ws="ws-a2", opp="opp-a2", brief=a2), headers=h).status_code \
            == 200
        # The list never carries another opportunity's or workspace's briefs.
        items = w.client.get(w.url(), headers=h).json()["items"]
        assert [b["id"] for b in items] == [own]

    def test_foreign_record_is_byte_identical_to_missing_and_calls_nothing(self, w, on):
        answers = [w.create(record=r) for r in ("sir-a2", "sir-b1", "no-such-record")]
        _same_404(*answers)
        assert SpyMock.calls == 0
        assert w.count(LLMUsageEvent) == 0 and w.count(RecommendationBrief) == 0

    def test_campaign_of_another_workspace_in_the_same_org_is_refused(self, w, on):
        answers = [w.create(campaign=c) for c in ("camp-a2", "camp-b1", "no-such-campaign")]
        _same_404(*answers)
        assert SpyMock.calls == 0
        assert w.count(LLMUsageEvent) == 0 and w.count(RecommendationBrief) == 0
        r = w.create(campaign="camp-a1")
        assert r.status_code == 201 and r.json()["campaign_id"] == "camp-a1"

    def test_a_record_of_a_sibling_opportunity_is_422_and_calls_nothing(self, w, on):
        r = w.create(record="sir-a1s")
        assert r.status_code == 422
        assert SpyMock.calls == 0 and w.count(LLMUsageEvent) == 0


class TestDark:
    @pytest.mark.parametrize("role", [r.value for r in ROLES])
    def test_flag_off_every_role_every_endpoint_503_nothing_written_no_call(self, w, role):
        h = w.auth[f"a-{role}"]
        for method, url, body in (
            ("GET", w.url(), None),
            ("POST", w.url(), {"intelligence_record_id": "sir-a1"}),
            ("POST", w.url(), {"intelligence_record_id": "sir-a1", "campaign_id": "camp-a2"}),
            ("GET", w.url(brief="0" * 32), None),
            ("GET", w.url(opp="opp-b1"), None),
            ("GET", w.url(opp="opp-b1", brief="0" * 32), None),
            ("GET", w.url(opp="no-such-opp", brief="0" * 32), None),
            ("POST", w.url(opp="opp-a2"), {"intelligence_record_id": "sir-a2"}),
            ("POST", w.url(opp="no-such-opp"), {"intelligence_record_id": "sir-a1"}),
            ("POST", w.url(), {"intelligence_record_id": "no-such-record"}),
        ):
            r = w.client.request(method, url, json=body, headers=h)
            if method == "POST" and role not in EDITOR_ROLES:
                assert r.status_code == 403  # the role check is a dependency: it runs first
            else:
                assert r.status_code == 503, (method, url, r.status_code)
                assert r.json()["error"]["code"] == "capability_unavailable"
        cap = w.client.get(f"{API}/workspaces/ws-a1/brief-capability", headers=h)
        assert cap.status_code == 200 and cap.json() == {"enabled": False}
        assert SpyMock.calls == 0
        assert w.count(RecommendationBrief) == 0 and w.count(LLMUsageEvent) == 0
        with w.make() as s:
            assert s.scalar(select(func.count()).select_from(AuditLog)) == 0

    def test_flag_off_malformed_requests_answer_422_before_the_gate(self, w):
        """Observation: request validation (FastAPI) precedes the in-handler gate."""
        h = w.auth["a-owner"]
        r1 = w.client.post(w.url(), json={"intelligence_record_id": "sir-a1", "x": 1}, headers=h)
        r2 = w.client.get(w.url(), params={"limit": 0}, headers=h)
        assert (r1.status_code, r2.status_code) == (422, 422)
        assert SpyMock.calls == 0 and w.count(LLMUsageEvent) == 0

    def test_override_is_honoured_exactly(self, w):
        w.override("org-a", "ws-a1", "recommendation_briefs", True)
        assert w.create().status_code == 201
        # Same organization, other workspace: still dark.
        assert w.create(ws="ws-a2", opp="opp-a2", record="sir-a2").status_code == 503
        assert w.create("b-owner", ws="ws-b1", opp="opp-b1", record="sir-b1").status_code \
            == 503
        caps = {ws: w.client.get(f"{API}/workspaces/{ws}/brief-capability",
                                 headers=w.auth["a-viewer" if ws != "ws-b1" else "b-owner"]
                                 ).json()["enabled"] for ws in ("ws-a1", "ws-a2", "ws-b1")}
        assert caps == {"ws-a1": True, "ws-a2": False, "ws-b1": False}

    def test_an_override_for_another_capability_does_not_enable_briefs(self, w):
        w.override("org-a", "ws-a1", "opportunity_feedback", True)
        assert w.create().status_code == 503
        assert w.client.get(f"{API}/workspaces/ws-a1/brief-capability",
                            headers=w.auth["a-owner"]).json() == {"enabled": False}
        assert SpyMock.calls == 0

    def test_a_tenant_inconsistent_override_does_not_enable(self, w):
        # A row for ws-a1 that names org-b: the resolver must not honour it.
        w.override("org-b", "ws-a1", "recommendation_briefs", True)
        assert w.create().status_code == 503
        assert w.client.get(f"{API}/workspaces/ws-a1/brief-capability",
                            headers=w.auth["a-owner"]).json() == {"enabled": False}

    def test_a_disable_override_beats_the_global_flag_for_that_workspace_only(self, w, on):
        w.override("org-a", "ws-a2", "recommendation_briefs", False)
        assert w.create(ws="ws-a2", opp="opp-a2", record="sir-a2").status_code == 503
        assert w.create().status_code == 201
        assert w.client.get(f"{API}/workspaces/ws-a2/brief-capability",
                            headers=w.auth["a-owner"]).json() == {"enabled": False}


# --------------------------------------------------------------------------- #
# 5A-W4 additions (W21 lane ADV5AB). Each class names the mutant it kills; the mutation
# record is in the lane evidence P6-W21-ADV5AB-01.
# --------------------------------------------------------------------------- #
class TestEvidenceStaysWithItsOpportunity:
    """Kills W4A-M01 (``_evidence_refs`` drops its opportunity filter) and W4A-M02 (it drops
    both its filters). W4A-M01 survives ``test_briefs_api.py``, whose markets each hold one
    opportunity with evidence, and every other brief module."""

    def test_evidence_refs_name_only_the_opportunitys_own_evidence(self, w, on):
        with w.make() as s:
            own = add_validation_evidence(
                s, workspace_id="ws-a1", opportunity_id="opp-a1", key="a1", count=2
            )
            sibling = add_validation_evidence(
                s, workspace_id="ws-a1", opportunity_id="opp-a1s", key="a1s", count=1
            )
            add_validation_evidence(
                s, workspace_id="ws-a2", opportunity_id="opp-a2", key="a2", count=1
            )
            add_validation_evidence(
                s, workspace_id="ws-b1", opportunity_id="opp-b1", key="b1", count=1
            )
            s.commit()
        mine = w.create()
        assert mine.status_code == 201, mine.text
        assert mine.json()["evidence_refs"] == ["sir-a1", *own]
        theirs = w.create(opp="opp-a1s", record="sir-a1s")
        assert theirs.status_code == 201, theirs.text
        assert theirs.json()["evidence_refs"] == ["sir-a1s", *sibling]
        other_ws = w.create(ws="ws-a2", opp="opp-a2", record="sir-a2")
        assert other_ws.json()["evidence_refs"] == ["sir-a2", "ev-a2-0"]


def _pg_capture(make, llm, opp: str, record: str, author: str) -> tuple[str, int]:
    """One capture through the brief service in its own session, as the route makes it."""
    from app.briefs.service import create_brief
    from app.intelligence.records import SignalIntelligenceRecord
    from app.opportunities.models import Opportunity
    from app.organizations.models import User

    with make() as s:
        brief = create_brief(
            s,
            opportunity=s.get(Opportunity, opp),
            intelligence_record=s.get(SignalIntelligenceRecord, record),
            campaign_id=None,
            author=s.get(User, author),
            llm=llm,
            settings=offline_settings(),
        )
        s.commit()
        return brief.workspace_id, brief.brief_version


class _Runner(threading.Thread):
    def __init__(self, name: str, fn, *args):
        super().__init__(name=name, daemon=True)
        self._call = (fn, args)
        self.result = None
        self.error: BaseException | None = None

    def run(self) -> None:  # pragma: no cover - exercised through join
        fn, args = self._call
        try:
            self.result = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
            self.error = exc

    def outcome(self):
        self.join(HOLD_SECONDS)
        assert not self.is_alive(), f"{self.name} did not finish"
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def pg_make() -> Iterator[sessionmaker]:
    require_postgres()
    with pg_database("sn_w4bc") as url:  # pragma: no cover - gated on PostgreSQL
        engine = create_engine(
            url,
            future=True,
            pool_size=10,
            max_overflow=5,
            connect_args={"options": "-c lock_timeout=30000 -c statement_timeout=60000"},
        )
        Base.metadata.create_all(engine)
        make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
        with make() as s:
            _seed(s)
            s.commit()
        try:
            yield make
        finally:
            engine.dispose()


class TestCaptureLockIsPerOpportunity:
    """PostgreSQL, READ COMMITTED. Kills W4A-M03 (``_next_version`` locks every opportunity
    row, not this one): one tenant's capture in flight would then stall every other
    tenant's. That mutant survives ``test_briefs_postgres.py``, whose captures share one
    opportunity, so a global lock still yields the same linear chain."""

    @pytest.mark.parametrize(
        ("opp", "record", "author", "ws"),
        [
            ("opp-a2", "sir-a2", "a-owner", "ws-a2"),
            ("opp-b1", "sir-b1", "b-owner", "ws-b1"),
            ("opp-a1s", "sir-a1s", "a-marketer", "ws-a1"),
        ],
        ids=["sibling-workspace", "other-organization", "sibling-opportunity"],
    )
    def test_a_capture_in_flight_never_blocks_another_opportunitys(
        self, pg_make, mock_llm_recorder, opp, record, author, ws
    ):
        llm = LLMService(mock_llm_recorder, sleep=lambda _s: None, settings=offline_settings())
        with capture_hold("held-capture") as hold:
            held = _Runner("held-capture", _pg_capture, pg_make, llm, "opp-a1", "sir-a1",
                           "a-owner")
            held.start()
            assert hold.holding.wait(HOLD_SECONDS), "the held capture never flushed its brief"
            other = _Runner("other-capture", _pg_capture, pg_make, llm, opp, record, author)
            other.start()
            # It completes while the held capture still holds opp-a1's lock.
            other.join(20.0)
            assert not other.is_alive(), "another opportunity's capture waited on this one"
            hold.release.set()
        assert other.outcome() == (ws, 1)
        assert held.outcome() == ("ws-a1", 1)
        # Each capture reached the provider once, under its own tenant's opaque reference.
        refs = [r.tenant_ref for r in mock_llm_recorder.requests]
        assert len(refs) == 2 and all(refs)
        if ws != "ws-a1":
            assert len(set(refs)) == 2
