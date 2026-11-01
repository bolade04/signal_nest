"""Project Phase 5A: the recommendation-brief API and service (exit criteria 3, 4, 6, 8).

    GET  /api/v1/workspaces/{ws}/opportunities/{opp}/briefs
    POST /api/v1/workspaces/{ws}/opportunities/{opp}/briefs
    GET  /api/v1/workspaces/{ws}/opportunities/{opp}/briefs/{brief_id}
    GET  /api/v1/workspaces/{ws}/brief-capability  (the reflection; never 503)

Each test gets its own SQLite database (foreign keys on) with two organizations, Dallas and
London: owner, admin (Dallas), marketer, reviewer and viewer members, one opportunity with
its immutable intelligence record, validation evidence and a campaign; Dallas also has a
sibling opportunity. ``get_db`` is the only overridden dependency, so authentication, tenancy
and the role checkers run for real. No test deletes or updates a brief, approval or audit
row: the tables are append-only, and a fresh database replaces cleanup.

Model calls are offline: the pinned mock provider, or the real Anthropic adapter over
``httpx.MockTransport`` where the wire matters (``offline_llm`` refuses real HTTP).
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.briefs import routes as brief_routes
from app.briefs import service as brief_service
from app.briefs.models import RecommendationBrief
from app.briefs.schemas import BRIEF_BOUNDS
from app.capabilities.models import WorkspaceCapabilityOverride
from app.core.config import Settings, get_settings
from app.core.enums import Role
from app.db.append_only import AppendOnlyViolation
from app.db.models import Base
from app.db.session import get_db
from app.intelligence.records import SignalIntelligenceRecord
from app.llm.base import LLMProviderError, LLMResponse, LLMUsage
from app.llm.mock import MockLLMProvider
from app.llm.models import LLMUsageEvent
from app.llm.prompts import get_prompt
from app.llm.providers_real import AnthropicProvider
from app.llm.service import LLMService, compute_input_hash, get_llm_service
from app.main import app
from app.opportunities.models import Opportunity, ValidationEvidence
from app.tests._auth2_support import live_bearer
from app.tests._llm_offline import offline_llm, offline_settings

API = get_settings().api_prefix
KEY = "fixture-key"
MODEL = "claude-sonnet-4-6"
MARKETS = ("dallas", "london")
OUT_KEYS = {
    "id", "opportunity_id", "intelligence_record_id", "location_id", "campaign_id",
    "brief_version", "parent_brief_id", "analysis_version", "scoring_version", "prompt_name",
    "prompt_version", "model_identifier", "is_simulated", "created_by", "created_by_snapshot",
    "created_at", "summary", "rationale", "recommended_actions", "suggested_angles",
    "risk_notes", "evidence_refs",
}
HIDDEN_KEYS = {"organization_id", "workspace_id", "input_hash", "trace_id", "llm_usage_event_id"}
EXPLAINED = {
    "why_it_matters": "Commuters in Dallas report long morning waits.",
    "who_cares": "commuters",
    "ai_inference": "Inference: speed is becoming a deciding factor.",
    "recommended_action": "Publish a brand-safe post about faster service.",
    "risk_note": "Use only approved claims.",
    "suggested_angles": ["Speed you can count on", "Morning rush, handled"],
}


@dataclass
class Market:
    key: str
    org_id: str
    ws_id: str
    opp_id: str
    record_id: str
    campaign_id: str
    evidence_ids: list[str]
    auth: dict[str, dict]


@pytest.fixture(scope="module", autouse=True)
def _offline() -> Iterator[None]:
    with offline_llm():
        yield


def _seed_market(s, key: str) -> Market:
    from app.brands.models import Brand
    from app.campaign_context.models import Campaign
    from app.organizations.models import Organization, OrganizationMember, User, Workspace
    from app.scouting_requests.models import ScoutRequest
    from app.signals.models import NormalizedSignal, RawSignal

    org_id, ws_id = f"org-{key}", f"ws-{key}"
    s.add(Organization(id=org_id, name=f"Org {key}", slug=f"org-{key}"))
    s.flush()
    s.add(Workspace(id=ws_id, organization_id=org_id, name=f"WS {key}", slug=f"ws-{key}"))
    roles = [Role.OWNER, Role.MARKETER, Role.REVIEWER, Role.VIEWER, Role.COMPLIANCE_REVIEWER]
    if key == "dallas":
        roles.append(Role.ADMIN)
    for role in roles:
        uid = f"{role.value}-{key}"
        s.add(User(id=uid, email=f"{uid}@example.com", full_name=f"{role.value.title()} {key}",
                   hashed_password="x", is_active=True))
        s.flush()
        s.add(OrganizationMember(id=f"m-{uid}", organization_id=org_id, user_id=uid,
                                 role=role.value))
    s.add(Brand(id=f"br-{key}", organization_id=org_id, workspace_id=ws_id, name=f"Brand {key}"))
    s.flush()
    s.add(ScoutRequest(id=f"sr-{key}", organization_id=org_id, workspace_id=ws_id,
                       brand_id=f"br-{key}", name=f"scout {key}"))
    s.add(Campaign(id=f"camp-{key}", organization_id=org_id, workspace_id=ws_id,
                   brand_id=f"br-{key}", name=f"Campaign {key}"))
    s.flush()
    for suffix in ("", "-2"):
        s.add(RawSignal(id=f"raw-{key}{suffix}", organization_id=org_id, workspace_id=ws_id,
                        scout_request_id=f"sr-{key}", source_type="rss_news", content="c"))
        s.flush()
        s.add(NormalizedSignal(id=f"ns-{key}{suffix}", organization_id=org_id,
                               workspace_id=ws_id, scout_request_id=f"sr-{key}",
                               raw_signal_id=f"raw-{key}{suffix}", source_type="rss_news",
                               excerpt="e", content_hash=f"h-{key}{suffix}",
                               pain_point_dna="speed_complaint"))
        s.flush()
        s.add(Opportunity(id=f"opp-{key}{suffix}", organization_id=org_id, workspace_id=ws_id,
                          brand_id=f"br-{key}", scout_request_id=f"sr-{key}",
                          title=f"Opp {key}{suffix}", classification="emerging",
                          decision="monitor", opportunity_score=50,
                          resolved_market=key.title(), audience_fit="commuters"))
        s.flush()
        s.add(SignalIntelligenceRecord(
            id=f"sir-{key}{suffix}", organization_id=org_id, workspace_id=ws_id,
            scout_request_id=f"sr-{key}", normalized_signal_id=f"ns-{key}{suffix}",
            opportunity_id=f"opp-{key}{suffix}", analysis_version="3c", scoring_version="3c.1",
            fingerprint=f"fp-{key}{suffix}", enricher="deterministic", accepted=True,
            classification="emerging"))
        s.flush()
    evidence = []
    for n in range(2):
        s.add(ValidationEvidence(id=f"ev-{key}-{n}", workspace_id=ws_id,
                                 opportunity_id=f"opp-{key}", source_type="rss_news",
                                 detail=f"evidence {n}"))
        evidence.append(f"ev-{key}-{n}")
    s.flush()
    auth = {
        role.value: {"Authorization": f"Bearer {live_bearer(f'{role.value}-{key}', s)}"}
        for role in roles
    }
    return Market(key, org_id, ws_id, f"opp-{key}", f"sir-{key}", f"camp-{key}", evidence, auth)


@dataclass
class World:
    client: TestClient
    make: sessionmaker
    m: dict[str, Market]
    outsider: dict

    def url(self, key="dallas", *, ws=None, opp=None, brief=None) -> str:
        mk = self.m[key]
        base = f"{API}/workspaces/{ws or mk.ws_id}/opportunities/{opp or mk.opp_id}/briefs"
        return f"{base}/{brief}" if brief else base

    def create(self, key="dallas", *, role="owner", body=None, **url):
        mk = self.m[key]
        payload = {"intelligence_record_id": mk.record_id} if body is None else body
        return self.client.post(self.url(key, **url), json=payload, headers=mk.auth[role])

    def get(self, key="dallas", *, role="owner", params=None, **url):
        return self.client.get(self.url(key, **url), headers=self.m[key].auth[role],
                               params=params or {})

    def count(self, model) -> int:
        with self.make() as s:
            return int(s.scalar(select(func.count()).select_from(model)) or 0)

    def rows(self, model, *where):
        with self.make() as s:
            return list(s.scalars(select(model).where(*where)))


def _reset_rate_limiter() -> None:
    from app.core.middleware import RateLimitMiddleware

    stack = getattr(app, "middleware_stack", None)
    while stack is not None:
        if isinstance(stack, RateLimitMiddleware):
            stack._hits.clear()
            return
        stack = getattr(stack, "app", None)


@pytest.fixture
def world(tmp_path, monkeypatch) -> Iterator[World]:
    engine = create_engine(f"sqlite:///{tmp_path / 'briefs.db'}",
                           connect_args={"check_same_thread": False}, future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial pragma hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with make() as s:
        markets = {key: _seed_market(s, key) for key in MARKETS}
        from app.organizations.models import User

        s.add(User(id="outsider", email="outsider@example.com", full_name="Outsider",
                   hashed_password="x", is_active=True))
        s.flush()
        outsider = {"Authorization": f"Bearer {live_bearer('outsider', s)}"}
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

    monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", False)
    app.dependency_overrides[get_db] = _override_get_db
    _reset_rate_limiter()
    try:
        yield World(TestClient(app), make, markets, outsider)
    finally:
        app.dependency_overrides.clear()
        _reset_rate_limiter()
        engine.dispose()


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", True)


def _use_service(service: LLMService) -> None:
    app.dependency_overrides[get_llm_service] = lambda: service


class _Recorder:
    """The real Anthropic adapter over a mock transport; records every request body."""

    def __init__(self, output: dict | None = None, tokens=(1000, 500)):
        self.bodies: list[dict] = []
        self.output = output or EXPLAINED
        self.tokens = tokens

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": json.dumps(self.output)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": self.tokens[0], "output_tokens": self.tokens[1]},
        })

    def service(self, settings: Settings) -> LLMService:
        client = httpx.Client(transport=httpx.MockTransport(self._handler))
        provider = AnthropicProvider(KEY, MODEL, 5, client=client)
        return LLMService(provider, sleep=lambda _s: None, settings=settings)


def _real_settings(monkeypatch, **overrides) -> Settings:
    """Anthropic-priced settings for the route's metering (offline: the transport is mocked)."""
    s = offline_settings(llm_provider="anthropic", llm_api_key=KEY, llm_model=MODEL,
                         recommendation_briefs_enabled=True, **overrides)
    monkeypatch.setattr(brief_routes, "get_settings", lambda: s)
    return s


# --------------------------------------------------------------------------- #
# Exit criterion 3: dark by default, fail-closed, resolver errors deny
# --------------------------------------------------------------------------- #
class TestCapabilityGate:
    def test_every_endpoint_answers_503_while_dark_and_writes_nothing(self, world):
        mk = world.m["dallas"]
        for r in (
            world.get(),
            world.create(),
            world.get(brief="0" * 32),
        ):
            assert r.status_code == 503, r.text
            assert r.json()["error"]["code"] == "capability_unavailable"
        assert world.count(RecommendationBrief) == 0
        assert world.count(LLMUsageEvent) == 0
        assert world.rows(AuditLog, AuditLog.workspace_id == mk.ws_id) == []

    def test_the_gate_runs_before_scope_so_dark_ids_are_not_probed(self, world):
        # A foreign or unknown opportunity answers the same 503 as the caller's own.
        for opp in ("opp-london", "no-such-opportunity"):
            r = world.get(opp=opp)
            assert r.status_code == 503 and r.json()["error"]["code"] == "capability_unavailable"

    def test_a_resolver_failure_denies_with_a_5xx_never_an_enable(self, world, flag_on,
                                                                 monkeypatch):
        def broken(**_kwargs):
            raise RuntimeError("override storage unavailable")

        monkeypatch.setattr(brief_routes, "resolve_capability", broken)
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post(world.url(), json={"intelligence_record_id": world.m["dallas"].record_id},
                        headers=world.m["dallas"].auth["owner"])
        assert r.status_code == 500
        assert world.count(RecommendationBrief) == 0 and world.count(LLMUsageEvent) == 0

    def test_an_honored_workspace_override_enables_one_workspace_only(self, world):
        # Global flag off (O-3: workspace-enableable); an enable override for Dallas only.
        with world.make() as s:
            s.add(WorkspaceCapabilityOverride(
                organization_id="org-dallas", workspace_id="ws-dallas",
                capability="recommendation_briefs", enabled=True, reason="pilot"))
            s.commit()
        assert world.create().status_code == 201
        assert world.get("london").status_code == 503

    def test_a_disable_override_closes_a_globally_enabled_workspace(self, world, flag_on):
        with world.make() as s:
            s.add(WorkspaceCapabilityOverride(
                organization_id="org-london", workspace_id="ws-london",
                capability="recommendation_briefs", enabled=False, reason="hold"))
            s.commit()
        assert world.get("london").status_code == 503
        assert world.get("dallas").status_code == 200


# --------------------------------------------------------------------------- #
# The workspace-effective reflection the panel reads (GET .../brief-capability)
# --------------------------------------------------------------------------- #
def _capability(world: World, key="dallas", role="owner"):
    mk = world.m[key]
    return world.client.get(f"{API}/workspaces/{mk.ws_id}/brief-capability",
                            headers=mk.auth[role])


def _override(world: World, key: str, enabled: bool) -> None:
    with world.make() as s:
        s.add(WorkspaceCapabilityOverride(
            organization_id=f"org-{key}", workspace_id=f"ws-{key}",
            capability="recommendation_briefs", enabled=enabled, reason="test"))
        s.commit()


class TestCapabilityReflection:
    def test_dark_is_200_disabled_not_503(self, world):
        r = _capability(world)
        assert r.status_code == 200 and r.json() == {"enabled": False}

    @pytest.mark.parametrize(
        "global_on, override, expected",
        [
            (False, None, False),
            (True, None, True),
            (False, True, True),  # an honored enable override with the global flag off
            (True, False, False),  # a disable override with the global flag on
        ],
    )
    def test_the_reflection_agrees_with_the_gate(self, world, monkeypatch, global_on,
                                                 override, expected):
        monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", global_on)
        if override is not None:
            _override(world, "dallas", override)
        assert _capability(world).json() == {"enabled": expected}
        assert world.get().status_code == (200 if expected else 503)

    def test_it_is_per_workspace(self, world):
        _override(world, "dallas", True)
        assert _capability(world, "dallas").json() == {"enabled": True}
        assert _capability(world, "london").json() == {"enabled": False}

    @pytest.mark.parametrize("role", ["viewer", "reviewer", "compliance_reviewer", "marketer"])
    def test_any_member_reads_it_and_it_carries_no_governance_metadata(self, world, role):
        r = _capability(world, role=role)
        assert r.status_code == 200 and set(r.json()) == {"enabled"}

    def test_a_non_member_is_403_and_unauthenticated_401(self, world):
        url = f"{API}/workspaces/ws-dallas/brief-capability"
        assert world.client.get(url, headers=world.outsider).status_code == 403
        assert world.client.get(url).status_code == 401
        other_org = world.client.get(url, headers=world.m["london"].auth["owner"])
        assert other_org.status_code == 403


# --------------------------------------------------------------------------- #
# Exit criteria 3 and 4: create / list / fetch, complete persisted provenance
# --------------------------------------------------------------------------- #
class TestCreateAndRead:
    def test_create_returns_the_safe_projection_with_server_derived_fields(self, world, flag_on):
        mk = world.m["dallas"]
        r = world.create()
        assert r.status_code == 201, r.text
        body = r.json()
        assert set(body) == OUT_KEYS and not HIDDEN_KEYS & set(body)
        assert body["opportunity_id"] == mk.opp_id
        assert body["intelligence_record_id"] == mk.record_id
        assert body["brief_version"] == 1 and body["parent_brief_id"] is None
        assert (body["analysis_version"], body["scoring_version"]) == ("3c", "3c.1")
        prompt = get_prompt(brief_service.BRIEF_TASK)
        assert (body["prompt_name"], body["prompt_version"]) == (prompt.name, prompt.version)
        assert body["model_identifier"] == "mock-1" and body["is_simulated"] is True
        assert body["created_by"] == "owner-dallas"
        assert body["created_by_snapshot"] == "owner-dallas Owner dallas <owner-dallas@example.com>"
        assert body["campaign_id"] is None
        assert body["evidence_refs"] == [mk.record_id, *mk.evidence_ids]
        assert body["summary"] and body["rationale"] and body["recommended_actions"]

    def test_persisted_provenance_is_complete_and_linked_to_the_usage_ledger(self, world,
                                                                              flag_on):
        mk = world.m["dallas"]
        brief_id = world.create().json()["id"]
        (row,) = world.rows(RecommendationBrief, RecommendationBrief.id == brief_id)
        assert (row.organization_id, row.workspace_id) == (mk.org_id, mk.ws_id)
        # input_hash is the service's: task, prompt identity and the sanitized inputs.
        inputs = {
            "resolved_market": "Dallas",
            "audience_fit": "commuters",
            "pain_point_dna": "speed_complaint",
            "safe_positioning": brief_service.translate_competitor_weakness(
                "speed_complaint", None
            ),
        }
        prompt = get_prompt(brief_service.BRIEF_TASK)
        assert row.input_hash == compute_input_hash(brief_service.BRIEF_TASK, prompt, inputs)
        assert len(row.input_hash) == 64
        (usage,) = world.rows(LLMUsageEvent, LLMUsageEvent.id == row.llm_usage_event_id)
        assert (usage.organization_id, usage.workspace_id) == (mk.org_id, mk.ws_id)
        assert (usage.task, usage.provider, usage.outcome) == (
            brief_service.BRIEF_TASK, "mock", "ok"
        )
        assert usage.scout_request_id is None and usage.job_id is None

    def test_same_inputs_give_the_same_content_and_a_new_monotonic_version(self, world,
                                                                          flag_on):
        first = world.create().json()
        second = world.create().json()
        assert second["brief_version"] == 2 and second["parent_brief_id"] == first["id"]
        content = ("summary", "rationale", "recommended_actions", "suggested_angles",
                   "risk_notes", "evidence_refs")
        assert {k: first[k] for k in content} == {k: second[k] for k in content}
        rows = world.rows(RecommendationBrief)
        assert len({r.input_hash for r in rows}) == 1  # deterministic mock run
        # Each version links to its own ledger row.
        assert len({r.llm_usage_event_id for r in rows}) == 2

    def test_versions_are_per_opportunity(self, world, flag_on):
        world.create()
        r = world.create(opp="opp-dallas-2", body={"intelligence_record_id": "sir-dallas-2"})
        assert r.status_code == 201 and r.json()["brief_version"] == 1

    def test_list_is_newest_first_and_bounded(self, world, flag_on):
        ids = [world.create().json()["id"] for _ in range(3)]
        page = world.get().json()
        assert page["total"] == 3 and [b["id"] for b in page["items"]] == ids[::-1]
        page = world.get(params={"limit": 1, "offset": 1}).json()
        assert [b["id"] for b in page["items"]] == [ids[1]]
        assert (page["limit"], page["offset"], page["total"]) == (1, 1, 3)
        for bad in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
            assert world.get(params=bad).status_code == 422

    def test_fetch_one(self, world, flag_on):
        created = world.create().json()
        r = world.get(brief=created["id"])
        assert r.status_code == 200
        fetched = r.json()
        # SQLite hands created_at back naive (no "Z"); the instant is the same.
        assert fetched.pop("created_at") == created.pop("created_at").removesuffix("Z")
        assert fetched == created

    def test_a_campaign_of_the_workspace_is_recorded(self, world, flag_on):
        mk = world.m["dallas"]
        body = {"intelligence_record_id": mk.record_id, "campaign_id": mk.campaign_id}
        r = world.create(body=body)
        assert r.status_code == 201 and r.json()["campaign_id"] == mk.campaign_id

    def test_the_request_body_is_strict(self, world, flag_on):
        mk = world.m["dallas"]
        for body in (
            {},
            {"intelligence_record_id": mk.record_id, "brief_version": 7},
            {"intelligence_record_id": mk.record_id, "created_by": "someone"},
            {"intelligence_record_id": "x" * 33},
        ):
            assert world.create(body=body).status_code == 422, body
        assert world.count(RecommendationBrief) == 0


# --------------------------------------------------------------------------- #
# Roles (coordinator-accepted: create = editors; reads = any workspace member)
# --------------------------------------------------------------------------- #
class TestRoles:
    @pytest.mark.parametrize("role", ["owner", "admin", "marketer"])
    def test_editors_create(self, world, flag_on, role):
        assert world.create(role=role).status_code == 201

    @pytest.mark.parametrize("role", ["reviewer", "viewer", "compliance_reviewer"])
    def test_non_editors_cannot_create_but_can_read(self, world, flag_on, role):
        r = world.create(role=role)
        assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied"
        assert world.count(RecommendationBrief) == 0 and world.count(LLMUsageEvent) == 0
        brief_id = world.create().json()["id"]
        assert world.get(role=role).status_code == 200
        assert world.get(role=role, brief=brief_id).status_code == 200

    def test_a_non_member_is_refused_everywhere_and_unauthenticated_is_401(self, world,
                                                                         flag_on):
        brief_id = world.create().json()["id"]
        for method, url in (
            ("GET", world.url()),
            ("POST", world.url()),
            ("GET", world.url(brief=brief_id)),
        ):
            body = {"intelligence_record_id": world.m["dallas"].record_id}
            r = world.client.request(method, url, headers=world.outsider,
                                     json=body if method == "POST" else None)
            assert r.status_code == 403, (method, url)
            r = world.client.request(method, url, json=body if method == "POST" else None)
            assert r.status_code == 401, (method, url)


# --------------------------------------------------------------------------- #
# Exit criterion 6: isolation — non-enumerating 404s; campaign validated
# --------------------------------------------------------------------------- #
class TestIsolation:
    def test_a_foreign_opportunity_under_the_callers_workspace_is_a_404(self, world, flag_on):
        for r in (world.get(opp="opp-london"), world.create(opp="opp-london")):
            assert r.status_code == 404
            assert r.json()["error"]["message"] == "Opportunity not found in this workspace."
        assert world.count(LLMUsageEvent) == 0

    def test_a_foreign_brief_id_is_indistinguishable_from_a_missing_one(self, world, flag_on):
        london = world.create("london").json()["id"]
        sibling = world.create(opp="opp-dallas-2",
                               body={"intelligence_record_id": "sir-dallas-2"}).json()["id"]
        answers = [
            world.get(brief=london),          # another organization's brief
            world.get(brief=sibling),         # another opportunity's brief, same workspace
            world.get(brief="0" * 32),        # no such brief
        ]
        assert {r.status_code for r in answers} == {404}
        errors = [{k: v for k, v in r.json()["error"].items() if k != "request_id"}
                  for r in answers]
        assert errors[0] == errors[1] == errors[2]

    def test_a_foreign_record_is_a_404_and_a_sibling_record_a_422(self, world, flag_on):
        r = world.create(body={"intelligence_record_id": "sir-london"})
        assert r.status_code == 404
        assert r.json()["error"]["message"] == "Intelligence record not found in this workspace."
        r = world.create(body={"intelligence_record_id": "sir-dallas-2"})
        assert r.status_code == 422
        assert world.count(RecommendationBrief) == 0 and world.count(LLMUsageEvent) == 0

    def test_the_campaign_is_validated_against_the_workspace(self, world, flag_on):
        mk = world.m["dallas"]
        for campaign in ("camp-london", "no-such-campaign"):
            body = {"intelligence_record_id": mk.record_id, "campaign_id": campaign}
            r = world.create(body=body)
            assert r.status_code == 404, campaign
            assert r.json()["error"]["message"] == "Campaign not found in this workspace."
        assert world.count(RecommendationBrief) == 0 and world.count(LLMUsageEvent) == 0

    def test_another_organizations_workspace_path_is_a_membership_403(self, world, flag_on):
        london = world.m["london"]
        r = world.client.get(world.url("london"), headers=world.m["dallas"].auth["owner"])
        assert r.status_code == 403
        r = world.client.post(world.url("london"), headers=world.m["dallas"].auth["owner"],
                              json={"intelligence_record_id": london.record_id})
        assert r.status_code == 403 and world.count(RecommendationBrief) == 0


# --------------------------------------------------------------------------- #
# Audit: recommendation_brief.created in the brief's own transaction
# --------------------------------------------------------------------------- #
class TestAudit:
    def test_one_audit_row_per_brief_with_identifiers_only(self, world, flag_on):
        mk = world.m["dallas"]
        brief = world.create(role="marketer").json()
        (log,) = world.rows(AuditLog, AuditLog.action == "recommendation_brief.created")
        assert (log.organization_id, log.workspace_id, log.actor_user_id) == (
            mk.org_id, mk.ws_id, "marketer-dallas"
        )
        assert (log.entity_type, log.entity_id) == ("recommendation_brief", brief["id"])
        assert log.new_state == {
            "opportunity_id": mk.opp_id,
            "intelligence_record_id": mk.record_id,
            "brief_version": 1,
            "parent_brief_id": None,
            "prompt": f"{brief['prompt_name']}@{brief['prompt_version']}",
            "is_simulated": True,
        }
        stored = json.dumps([log.new_state, log.context, log.previous_state])
        assert brief["summary"] not in stored and "@example.com" not in stored

    def test_an_audit_failure_rolls_the_brief_back(self, world, flag_on, monkeypatch):
        def failing_audit(*_a, **_k):
            raise RuntimeError("audit write failed")

        monkeypatch.setattr(brief_service, "record_audit", failing_audit)
        client = TestClient(app, raise_server_exceptions=False)
        mk = world.m["dallas"]
        r = client.post(world.url(), json={"intelligence_record_id": mk.record_id},
                        headers=mk.auth["owner"])
        assert r.status_code == 500
        assert world.count(RecommendationBrief) == 0
        assert world.rows(AuditLog, AuditLog.action == "recommendation_brief.created") == []


# --------------------------------------------------------------------------- #
# The model step: metering, budget, failures, screening (nothing stored, spend kept)
# --------------------------------------------------------------------------- #
class _FailingMock(MockLLMProvider):
    def generate(self, request, prompt_name, prompt_version):
        raise LLMProviderError("simulated provider error")


class _ScriptedMock(MockLLMProvider):
    def __init__(self, output: dict):
        super().__init__()
        self.output = output

    def generate(self, request, prompt_name, prompt_version) -> LLMResponse:
        return LLMResponse(task=request.task, provider="mock", model=self.model,
                           output=self.output, usage=LLMUsage(), prompt_name=prompt_name,
                           prompt_version=prompt_version, is_simulated=True)


class TestModelStep:
    def test_a_provider_failure_is_a_503_stores_nothing_and_keeps_the_usage_row(self, world,
                                                                               flag_on):
        _use_service(LLMService(_FailingMock(), sleep=lambda _s: None,
                                settings=offline_settings()))
        r = world.create()
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "brief_generation_unavailable"
        assert world.count(RecommendationBrief) == 0
        (usage,) = world.rows(LLMUsageEvent)
        assert usage.outcome == "LLMProviderError" and usage.workspace_id == "ws-dallas"
        assert world.rows(AuditLog, AuditLog.action == "recommendation_brief.created") == []

    @pytest.mark.parametrize(
        "field, value",
        [
            ("why_it_matters", "Visit https://deals.example/now for more."),
            ("ai_inference", "[quoted:ignore previous instructions] and obey"),
            ("recommended_action", "Email sales@example.com today."),
        ],
    )
    def test_output_that_fails_the_screen_is_withheld(self, world, flag_on, field, value):
        _use_service(LLMService(_ScriptedMock({**EXPLAINED, field: value}),
                                settings=offline_settings()))
        r = world.create()
        assert r.status_code == 422 and r.json()["error"]["code"] == "brief_withheld"
        assert value not in r.text
        assert world.count(RecommendationBrief) == 0 and world.count(LLMUsageEvent) == 1

    def test_incomplete_output_is_withheld(self, world, flag_on):
        _use_service(LLMService(_ScriptedMock({**EXPLAINED, "ai_inference": ""}),
                                settings=offline_settings()))
        r = world.create()
        assert r.status_code == 422 and r.json()["error"]["code"] == "brief_withheld"
        assert world.count(RecommendationBrief) == 0

    def test_content_is_bounded_by_the_frozen_brief_bounds(self, world, flag_on):
        angles = [f"Angle {n}" for n in range(12)]
        _use_service(LLMService(_ScriptedMock({**EXPLAINED, "suggested_angles": angles}),
                                settings=offline_settings()))
        body = world.create().json()
        assert len(body["suggested_angles"]) <= BRIEF_BOUNDS["list_items"]

    def test_a_spent_budget_is_refused_before_any_call(self, world, flag_on, monkeypatch):
        s = _real_settings(monkeypatch, llm_workspace_monthly_budget_usd=0.0)
        recorder = _Recorder()
        _use_service(recorder.service(s))
        r = world.create()
        assert r.status_code == 429 and r.json()["error"]["code"] == "llm_budget_exhausted"
        assert recorder.bodies == [] and world.count(RecommendationBrief) == 0

    def test_an_attempt_over_the_budget_is_not_made(self, world, flag_on, monkeypatch):
        s = _real_settings(monkeypatch, llm_workspace_monthly_budget_usd=0.000001)
        recorder = _Recorder()
        _use_service(recorder.service(s))
        r = world.create()
        assert r.status_code == 429 and recorder.bodies == []
        assert world.count(RecommendationBrief) == 0

    def test_a_real_provider_call_is_metered_priced_and_tenant_referenced(self, world, flag_on,
                                                                          monkeypatch):
        from app.llm.metering import tenant_reference

        s = _real_settings(monkeypatch)
        recorder = _Recorder()
        _use_service(recorder.service(s))
        r = world.create()
        assert r.status_code == 201, r.text
        assert r.json()["model_identifier"] == MODEL and r.json()["is_simulated"] is False
        (body,) = recorder.bodies
        assert body["metadata"] == {"user_id": tenant_reference(s, "ws-dallas")}
        (row,) = world.rows(RecommendationBrief)
        (usage,) = world.rows(LLMUsageEvent, LLMUsageEvent.id == row.llm_usage_event_id)
        assert (usage.provider, usage.input_tokens, usage.output_tokens) == (
            "anthropic", 1000, 500
        )
        assert usage.cost_usd > 0

    def test_a_version_clash_is_a_409_that_stores_nothing(self, world, flag_on, monkeypatch):
        world.create()
        monkeypatch.setattr(brief_service, "_next_version", lambda _db, _opp: (1, None))
        r = world.create()
        assert r.status_code == 409
        assert world.count(RecommendationBrief) == 1
        assert world.count(LLMUsageEvent) == 2  # the clashing capture's call is still counted


# --------------------------------------------------------------------------- #
# Exit criterion 4: no update or delete path exists
# --------------------------------------------------------------------------- #
class TestImmutability:
    def test_no_route_updates_or_deletes_a_brief(self):
        from fastapi.routing import iter_route_contexts

        methods = {
            m
            for rc in iter_route_contexts(app.routes)
            if "/briefs" in rc.path
            for m in rc.methods
        }
        assert methods <= {"GET", "POST", "HEAD"}

    def test_the_service_exposes_no_update_or_delete(self):
        public = {n for n in brief_service.__all__ if callable(getattr(brief_service, n))}
        writers = {n for n in public if not n[0].isupper()}
        assert writers == {"create_brief"}
        for module in (brief_service, brief_routes):
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            names = {
                n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", None)
                for n in ast.walk(tree)
                if isinstance(n, ast.Call)
            }
            assert not names & {"update", "delete", "merge", "bulk_update_mappings"}, module

    def test_an_orm_update_of_a_stored_brief_raises(self, world, flag_on):
        brief_id = world.create().json()["id"]
        with world.make() as s:
            row = s.get(RecommendationBrief, brief_id)
            row.summary = "rewritten"
            with pytest.raises(AppendOnlyViolation):
                s.flush()
            s.rollback()
        (row,) = world.rows(RecommendationBrief)
        assert row.summary != "rewritten"


# --------------------------------------------------------------------------- #
# Exit criterion 8: no new flag defaults true anywhere
# --------------------------------------------------------------------------- #
#: The boolean settings that already defaulted to True at main N (102393fa), none of them a
#: capability flag. A new True default anywhere fails this test; adding to this set is a
#: reviewed decision, never a 5A change.
PRE_EXISTING_TRUE_DEFAULTS = frozenset(
    {"debug", "s3_use_ssl", "connector_fixture_fallback_enabled"}
)


class TestNoFlagDefaultsTrue:
    def test_no_boolean_setting_newly_defaults_true(self):
        true_defaults = {
            name
            for name, field in Settings.model_fields.items()
            if field.annotation is bool and field.get_default(call_default_factory=True) is True
        }
        assert true_defaults == PRE_EXISTING_TRUE_DEFAULTS

    def test_the_briefs_flag_and_every_capability_flag_default_off(self):
        from app.capabilities.registry import get_policy, iter_capabilities

        fields = Settings.model_fields
        assert fields["recommendation_briefs_enabled"].get_default() is False
        for capability in iter_capabilities():
            attr = get_policy(capability).global_flag_attr
            assert fields[attr].get_default() is False, attr

    def test_the_shipped_configuration_resolves_briefs_dark(self, world):
        r = world.get()
        assert r.status_code == 503 and r.json()["error"]["code"] == "capability_unavailable"
