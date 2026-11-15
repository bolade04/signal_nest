"""Project Phase 5E (lane 5E-W4): status analytics authorization, gate and field boundary.

``GET /workspaces/{ws}/analytics`` (5E plan §2; interface sheet P4, A; exit 2(b), 4):

* **Exact roles** (``require_analytics_reader``, frozen at CF-3; item 32 kept the analytics
  set): OWNER, ADMIN, MARKETER and REVIEWER read. VIEWER and COMPLIANCE_REVIEWER are refused
  403, whether the capability is on or off and whatever location they name (a refused role
  learns nothing about ids). The route's own dependency is the exact-role checker.
* **O-12(B)**, after the role: while ``notifications_analytics`` does not resolve enabled for
  the workspace, every reader gets 503 ``capability_unavailable`` ("Status analytics is not
  available yet."), also for a foreign or unknown location (nothing about ids while dark).
  An enable override opens one workspace with the flag off, a disable override closes one
  with it on, the safety ceiling outranks both, other capabilities never open it, and a
  resolver failure is raised, never a decision.
* **No attribution or advertising-performance field**: the response schema (walked through
  every ``$ref`` of the live OpenAPI document) and every response body have exactly the
  pinned key sets, and no key names spend, cost, clicks, impressions, conversions, revenue,
  attribution or a person. Figures with no basis in scope are ``null``, never 0; counts are
  never null; ``unassigned`` has no meaning in a location view and is ``null`` there.
* **Location narrowing**: a location view counts that location's rows only, exports by their
  live draft's location.
* The read writes nothing.

The world is the 5C harness on SQLite and, with ``TEST_POSTGRES_URL``, PostgreSQL at head.
"""

from __future__ import annotations

import inspect
import re
import uuid
from collections.abc import Iterator

import pytest
from fastapi.routing import iter_route_contexts
from sqlalchemy import delete

import app.capabilities.resolver as resolver_mod
from app.analytics import policy
from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.brands.service import get_primary_brand
from app.briefs.models import RecommendationBrief
from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import Capability
from app.core.actor_snapshot import actor_snapshot
from app.core.config import get_settings
from app.core.enums import Role
from app.creative.models import ContentReview, CreativeDraft
from app.exports.models import Export
from app.intelligence.records import SignalIntelligenceRecord
from app.locations.models import BusinessLocation
from app.main import app
from app.opportunities.models import Opportunity
from app.organizations.models import User
from app.scouting_requests.models import ScoutRequest
from app.signals.models import NormalizedSignal, RawSignal, SignalCluster
from app.tests._creative_support import API, CreativeWorld, creative_world, postgres_creative_world

CAP = "notifications_analytics"
DARK = {"code": "capability_unavailable", "message": "Status analytics is not available yet."}
READERS = ("owner", "admin", "marketer", "reviewer")
REFUSED = ("viewer", "compliance_reviewer")
ROUTE = f"{API}/workspaces/{{workspace_id}}/analytics"

_COUNTS = {"opportunities", "briefs", "drafts", "approvals", "exports"}
_LATENCY = {"decided", "mean_seconds", "median_seconds"}
_SUBJECT = {"total", "awaiting_decision", "approved", "rejected", "superseded"}
#: The exact response structure: a dict is an object's keys, a list is an array of objects.
SHAPE: dict = {
    "workspace_id": None,
    "location_id": None,
    "generated_at": None,
    "opportunities": {"total", "new", "saved", "monitoring", "ignored", "actioned"},
    "briefs": _SUBJECT,
    "drafts": _SUBJECT,
    "approvals": {"total", "approved", "rejected", "revoked", "on_briefs", "on_drafts"},
    "reviews": {"total", "passed", "blocked", "not_passed"},
    "exports": {"total", "plain_text", "markdown", "json_document"},
    "approval_latency": {"briefs": _LATENCY, "drafts": _LATENCY},
    "outcomes": {"drafts_generated", "revisions", "simulated", "review_pass_rate",
                 "review_block_rate"},  # fmt: skip
    "locations": [_COUNTS | {"location_id", "name", "is_active"}],
    "unassigned": _COUNTS,
}
#: Words no analytics key may contain: advertising performance, attribution, or a person.
FORBIDDEN = re.compile(
    r"attribut|conversion|convert|click|impression|revenue|spend|cost|roas|roi\b|\bcp[acm]\b|"
    r"\bctr\b|budget|\bbid|advert|\bads?\b|_ads?_|^ads?_|_ads?$|sale|purchase|income|profit|"
    r"author|decider|decided_by|exporter|exported_by|snapshot|email|user|token|name_of",
)


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def world(request, tmp_path_factory) -> Iterator[CreativeWorld]:
    if request.param == "sqlite":
        with creative_world(tmp_path_factory.mktemp("w4as") / "s.db", "w4as") as w:
            yield w
        return
    with postgres_creative_world("w4as") as w:  # skips unless TEST_POSTGRES_URL is set
        yield w


@pytest.fixture(autouse=True)
def flag_off(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "notifications_analytics_enabled", False)


def set_cap(world: CreativeWorld, ws: str, enabled: bool | None, capability: str = CAP) -> None:
    with world.session() as s:
        s.execute(
            delete(WorkspaceCapabilityOverride).where(
                WorkspaceCapabilityOverride.workspace_id == ws,
                WorkspaceCapabilityOverride.capability == capability,
            )
        )
        if enabled is not None:
            s.add(WorkspaceCapabilityOverride(
                organization_id=world.org_of(ws), workspace_id=ws, capability=capability,
                enabled=enabled, set_by_user_id=world.owner_id(ws), reason="5E-W4 test",
            ))


@pytest.fixture
def ws(world: CreativeWorld, request) -> str:
    """A fresh workspace with analytics ON by override."""
    ws = world.workspace(f"s {request.node.name[:40]}", enabled=False)
    set_cap(world, ws, True)
    return ws


def location(world: CreativeWorld, ws: str, name: str) -> str:
    with world.session() as s:
        row = BusinessLocation(organization_id=world.org_of(ws), workspace_id=ws,
                               brand_id=get_primary_brand(s, ws).id, name=name)  # fmt: skip
        s.add(row)
        s.flush()
        return row.id


def family(world: CreativeWorld, ws: str, location_id: str | None = None, *,
           draft_location: str | None = "same") -> None:  # fmt: skip
    """One opportunity, approved brief, approved draft (passed review) and one export, at
    ``location_id`` (the draft at ``draft_location`` when given otherwise)."""
    draft_at = location_id if draft_location == "same" else draft_location
    org, tag = world.org_of(ws), uuid.uuid4().hex[:12]
    actor = world.users["reviewer"]
    with world.session() as s:
        user = s.get(User, actor)
        snap = actor_snapshot(user_id=user.id, full_name=user.full_name, email=user.email)
        brand = get_primary_brand(s, ws)
        scoped = {"organization_id": org, "workspace_id": ws}
        s.add(ScoutRequest(id=f"sr{tag}", name="Scout", brand_id=brand.id, **scoped))
        s.flush()
        req = {**scoped, "scout_request_id": f"sr{tag}"}
        s.add(RawSignal(id=f"rs{tag}", source_type="rss", content="parking", **req))
        s.add(SignalCluster(id=f"cl{tag}", label="parking", workspace_id=ws,
                            scout_request_id=f"sr{tag}"))  # fmt: skip
        s.flush()
        s.add(NormalizedSignal(id=f"ns{tag}", raw_signal_id=f"rs{tag}", cluster_id=f"cl{tag}",
                               source_type="rss", excerpt="parking", content_hash="0" * 64,
                               **req))  # fmt: skip
        s.flush()
        s.add(Opportunity(id=f"op{tag}", brand_id=brand.id, title="Parking",
                          scout_request_id=f"sr{tag}", cluster_id=f"cl{tag}",
                          classification="x", decision="act_now", location_id=location_id,
                          **scoped))  # fmt: skip
        s.flush()
        s.add(SignalIntelligenceRecord(
            id=f"in{tag}", normalized_signal_id=f"ns{tag}", opportunity_id=f"op{tag}",
            analysis_version="1", scoring_version="1", fingerprint="f" * 64, enricher="test",
            accepted=True, classification="x", **req,
        ))
        s.flush()
        s.add(RecommendationBrief(
            id=f"br{tag}", opportunity_id=f"op{tag}", intelligence_record_id=f"in{tag}",
            brief_version=1, analysis_version="1", scoring_version="1",
            prompt_name="explain_opportunity", prompt_version="1.2.0",
            model_identifier="mock-1", input_hash="0" * 64, is_simulated=True,
            created_by=actor, created_by_snapshot=snap, summary="s", rationale="r",
            recommended_actions=[], suggested_angles=[], risk_notes=[], evidence_refs=[],
            jurisdiction_code="US-TX", location_id=location_id, **scoped,
        ))
        s.flush()
        s.add(CreativeDraft(
            id=f"dr{tag}", brief_id=f"br{tag}", opportunity_id=f"op{tag}", draft_version=1,
            channel="social_post", body="Weekend parking is easy.",
            prompt_name="generate_creative", prompt_version="1.0.0", model_identifier="mock-1",
            input_hash="0" * 64, is_simulated=True, input_tokens=1, output_tokens=1,
            estimated_cost_usd=0, jurisdiction_code="US-TX", created_by=actor,
            created_by_snapshot=snap, location_id=draft_at, **scoped,
        ))
        s.flush()
        s.add(ContentReview(
            draft_id=f"dr{tag}", draft_version=1, risk_level="low", is_blocked=False,
            findings=[], warnings=[], blocked_claim_ids=[], required_disclaimers=[],
            disclaimers_resolved=True, passed=True, jurisdiction_code="US-TX", **scoped,
        ))
        for subject, kind, link in ((f"br{tag}", "recommendation_brief", "brief_id"),
                                    (f"dr{tag}", "creative_draft", "draft_id")):  # fmt: skip
            s.add(Approval(
                id=f"ap{kind[0]}{tag}", subject_type=kind, subject_id=subject, subject_version=1,
                decision="approved", decision_seq=1, decided_by=actor, decided_by_snapshot=snap,
                decided_role="reviewer", is_simulated=False, **{link: subject}, **scoped,
            ))
        s.flush()
        s.add(Export(
            draft_id=f"dr{tag}", draft_version=1, approval_id=f"apc{tag}", format="text/plain",
            jurisdiction_code="US-TX", exported_by=actor, exported_by_snapshot=snap,
            is_simulated=True, **scoped,
        ))


def read(world: CreativeWorld, ws: str, role: str = "owner", location_id: str | None = None):
    query = f"?location_id={location_id}" if location_id is not None else ""
    return world.send("GET", f"/workspaces/{ws}/analytics{query}", role)


def error(r) -> dict:
    return {k: v for k, v in r.json()["error"].items() if k != "request_id"}


def assert_shape(value, shape, where: str = "AnalyticsOut") -> None:
    """``value`` has exactly ``shape``'s keys, recursively, and no forbidden word in any."""
    if isinstance(shape, list):
        assert isinstance(value, list), where
        for item in value:
            assert_shape(item, shape[0], f"{where}[]")
        return
    keys = set(shape)
    if value is None:
        return
    assert isinstance(value, dict), where
    assert set(value) == keys, (where, sorted(set(value) ^ keys))
    for key in value:
        assert not FORBIDDEN.search(key), (where, key)
        if isinstance(shape, dict) and shape[key] is not None:
            assert_shape(value[key], shape[key], f"{where}.{key}")


# --------------------------------------------------------------------------- #
# Exact roles
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("capability", [True, None])
def test_viewer_and_compliance_reviewer_are_refused_403_on_or_dark(world, ws, capability):
    set_cap(world, ws, capability)
    own = location(world, ws, "Own")
    foreign = location(world, world.ws_b, "Foreign")
    for role in REFUSED:
        for location_id in (None, own, foreign, uuid.uuid4().hex):
            r = read(world, ws, role, location_id)
            assert r.status_code == 403, (role, location_id, r.text)
            assert error(r) == {
                "code": "permission_denied",
                "message": f"Role '{role}' is not permitted for this action.",
            }


def test_every_reader_reads_when_on(world, ws):
    family(world, ws)
    for role in READERS:
        r = read(world, ws, role)
        assert r.status_code == 200, (role, r.text)
        assert r.json()["opportunities"]["total"] == 1


def test_the_route_depends_on_the_exact_reader_set():
    assert policy.ANALYTICS_READER_ROLES == {Role.OWNER, Role.ADMIN, Role.MARKETER, Role.REVIEWER}
    calls = [
        [d.call for d in rc.dependant.dependencies]
        for rc in iter_route_contexts(app.routes)
        if getattr(rc, "dependant", None) is not None and rc.path == ROUTE
    ]
    assert len(calls) == 1 and policy.require_analytics_reader in calls[0]
    permitted = inspect.getclosurevars(policy.require_analytics_reader).nonlocals["permitted"]
    assert permitted == policy.ANALYTICS_READER_ROLES


# --------------------------------------------------------------------------- #
# O-12(B)
# --------------------------------------------------------------------------- #
def assert_dark(world: CreativeWorld, ws: str) -> None:
    own = location(world, ws, "Dark own")
    sibling = location(world, world.ws2, "Dark sibling")
    for role in READERS:
        for location_id in (None, own, sibling, uuid.uuid4().hex):
            r = read(world, ws, role, location_id)
            assert r.status_code == 503, (role, location_id, r.text)
            assert error(r) == DARK
        reflected = world.send("GET", f"/workspaces/{ws}/notifications-analytics-capability", role)
        assert reflected.status_code == 200 and reflected.json() == {"enabled": False}
    for role in REFUSED:  # the role still decides first
        assert read(world, ws, role).status_code == 403


def assert_open(world: CreativeWorld, ws: str) -> None:
    for role in READERS:
        assert read(world, ws, role).status_code == 200, role
    reflected = world.send("GET", f"/workspaces/{ws}/notifications-analytics-capability", "viewer")
    assert reflected.json() == {"enabled": True}


def test_dark_every_reader_gets_503_whatever_the_location(world, ws):
    set_cap(world, ws, None)
    assert_dark(world, ws)


def test_an_enable_override_opens_one_workspace_while_the_flag_is_off(world, ws):
    assert_open(world, ws)
    assert_dark(world, world.workspace("s neighbour", enabled=False))


def test_a_disable_override_closes_one_workspace_while_the_flag_is_on(world, ws, monkeypatch):
    monkeypatch.setattr(get_settings(), "notifications_analytics_enabled", True)
    neighbour = world.workspace("s neighbour on", enabled=False)
    set_cap(world, ws, False)
    assert_dark(world, ws)
    assert_open(world, neighbour)


def test_the_safety_ceiling_outranks_an_enable_override_and_the_flag(world, ws, monkeypatch):
    monkeypatch.setattr(get_settings(), "notifications_analytics_enabled", True)
    assert_open(world, ws)
    real = resolver_mod._ceiling_blocks
    monkeypatch.setattr(
        resolver_mod, "_ceiling_blocks",
        lambda c, **kw: c is Capability.NOTIFICATIONS_ANALYTICS or real(c, **kw),
    )
    assert_dark(world, ws)


def test_other_capabilities_never_open_analytics(world, ws):
    set_cap(world, ws, None)
    for other in ("content_export", "creative_generation", "opportunity_feedback"):
        set_cap(world, ws, True, capability=other)
    assert_dark(world, ws)


def test_a_resolver_failure_is_raised_never_a_decision(world, ws, monkeypatch):
    import app.analytics.routes as routes

    def broken(**_kw):
        raise RuntimeError("resolver unavailable")

    monkeypatch.setattr(routes, "resolve_capability", broken)
    with pytest.raises(RuntimeError, match="resolver unavailable"):
        read(world, ws)


# --------------------------------------------------------------------------- #
# The field boundary
# --------------------------------------------------------------------------- #
def _resolve(schema: dict, components: dict) -> dict:
    while "$ref" in schema:
        schema = components[schema["$ref"].rsplit("/", 1)[1]]
    return schema


def _schema_shape(schema: dict, components: dict):
    """The key structure of a JSON schema, in ``SHAPE``'s notation."""
    schema = _resolve(schema, components)
    if "anyOf" in schema:
        shapes = [_schema_shape(s, components) for s in schema["anyOf"]
                  if s.get("type") != "null"]  # fmt: skip
        assert len(shapes) == 1, schema
        return shapes[0]
    if schema.get("type") == "array":
        return [_schema_shape(schema["items"], components)]
    if schema.get("type") == "object" or "properties" in schema:
        assert schema.get("additionalProperties") is False, schema.get("title")
        props = schema["properties"]
        nested = {k: _schema_shape(v, components) for k, v in props.items()}
        if all(v is None for v in nested.values()):
            return set(props)
        return nested
    return None


def _normalise(shape):
    if isinstance(shape, list):
        return [_normalise(shape[0])]
    if isinstance(shape, set):
        return {k: None for k in shape}
    if isinstance(shape, dict):
        return {k: _normalise(v) for k, v in shape.items()}
    return shape


def test_the_response_schema_has_exactly_the_pinned_fields_and_no_attribution():
    document = app.openapi()
    components = document["components"]["schemas"]
    operation = document["paths"][ROUTE]["get"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    walked = _schema_shape(schema, components)
    assert _normalise(walked) == _normalise(SHAPE)

    def keys(shape) -> set[str]:
        if isinstance(shape, list):
            return keys(shape[0])
        if isinstance(shape, dict):
            return set(shape) | {k for v in shape.values() for k in keys(v)}
        return set(shape or ())

    named = keys(walked)
    assert named and not [k for k in named if FORBIDDEN.search(k)]
    # The only inputs: the path workspace, an optional location, and the bearer header.
    assert {p["name"] for p in operation["parameters"]} == {
        "workspace_id", "location_id", "authorization"
    }


def test_every_response_body_has_exactly_the_pinned_fields(world, ws):
    here = location(world, ws, "Here")
    family(world, ws, here)
    family(world, ws, None)
    for location_id in (None, here):
        body = read(world, ws, location_id=location_id).json()
        assert_shape(body, SHAPE)
        assert len(body["locations"]) == 1


def test_figures_without_a_basis_are_null_and_counts_are_zero(world, ws):
    empty = location(world, ws, "Empty")
    family(world, ws, None)  # the workspace has data; the location has none
    for location_id in (None, empty):
        body = read(world, ws, location_id=location_id).json()
        assert_shape(body, SHAPE)
        if location_id is None:
            continue
        for family_name in ("opportunities", "briefs", "drafts", "approvals", "reviews",
                            "exports"):  # fmt: skip
            assert all(v == 0 for v in body[family_name].values()), family_name
        for subject in ("briefs", "drafts"):
            latency = body["approval_latency"][subject]
            assert latency == {"decided": 0, "mean_seconds": None, "median_seconds": None}
        assert body["outcomes"] == {
            "drafts_generated": 0, "revisions": 0, "simulated": 0,
            "review_pass_rate": None, "review_block_rate": None,
        }
        assert body["unassigned"] is None  # no meaning in a location view
    whole = read(world, ws).json()
    assert whole["unassigned"] is not None
    assert whole["outcomes"]["review_pass_rate"] == 1.0  # a basis exists in the workspace view
    bare = world.workspace("s bare", enabled=False)
    set_cap(world, bare, True)
    nothing = read(world, bare).json()
    assert nothing["unassigned"] == dict.fromkeys(_COUNTS, 0)  # an object, never null here
    assert nothing["locations"] == []
    assert nothing["outcomes"]["review_pass_rate"] is None
    assert nothing["approval_latency"]["briefs"]["mean_seconds"] is None


def test_a_location_view_counts_exports_by_their_drafts_location(world, ws):
    here, there = location(world, ws, "Here"), location(world, ws, "There")
    family(world, ws, here)
    family(world, ws, None)
    family(world, ws, here, draft_location=there)  # its export follows the draft, not the brief
    at_here = read(world, ws, location_id=here).json()
    at_there = read(world, ws, location_id=there).json()
    whole = read(world, ws).json()
    assert at_here["exports"]["total"] == 1
    assert at_there["exports"]["total"] == 1
    assert whole["exports"]["total"] == 3
    assert at_here["drafts"]["total"] == 1 and at_there["drafts"]["total"] == 1


def test_the_read_writes_nothing(world, ws):
    family(world, ws, location(world, ws, "Here"))

    def counts() -> dict[str, int]:
        return {m.__tablename__: world.count(m) for m in (Export, AuditLog, Approval,
                                                           CreativeDraft)}  # fmt: skip

    before = counts()
    for role in READERS:
        read(world, ws, role)
    assert counts() == before
