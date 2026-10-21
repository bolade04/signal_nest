"""P6-CI-3 / Phase-6 E9: cross-ORGANIZATION isolation on every customer-facing route family.

Two organizations are built only through real HTTP flows (``POST /auth/register`` creates
each organization and its OWNER; a MARKETER joins each by invitation; workspaces,
onboarding, locations, geo-coverage, the nine context entities, scout requests and
schedules are created through their customer routes with the owner's real bearer token).
Rows that have no creation route (opportunities, durable jobs) are written directly through
a witness session and are the only hand-written tenant rows. The database is a temporary
SQLite file with foreign keys enforced; ``get_db`` is the ONLY overridden dependency, so
authentication (``get_authenticated_session``), workspace tenancy (``get_tenant_context``),
organization tenancy (``get_organization_context``) and every role checker run for real.

Every probe class runs under both perspectives (organization A acting against B, then B
against A), so a lost tenant predicate that would hand the first-built organization's row to
everyone is caught from the second organization's side; singleton reads (business profile,
brands) assert the caller's own identifiers, and every created row's ``brand_id`` is checked
against the caller's brand.

The three isolation limbs this module proves, per operation, with identifiers carried in
the path, the query string and the request body:

* positive control -- the organization-A owner performs the operation on A's own resources
  and gets the route's success outcome (so a later denial is not a broken route);
* foreign TENANT path -- the same token names organization B's workspace (or organization)
  in the path or query: ``403 permission_denied`` with the membership message. This limb is
  also proven for the whole matrix by ``test_e7_role_policy_matrix`` (an outsider on every
  route); it is repeated here only where a real B resource makes the probe concrete;
* foreign CURSORS (P6-PLAT-12): a paging cursor that organization B was issued on its own
  list is refused (``422 "Invalid cursor."``) when A replays it on A's list, and walking
  A's list page by page returns only A's rows with an ``X-Total-Count`` that counts only
  A's rows;
* foreign RESOURCE identifiers under A's own workspace/organization path (hidden IDOR):
  B's location/request/opportunity/job/item/member/invitation id answers the handler's
  ``404 "... not found ..."``, list filters naming B's ids return A's rows only, and after
  every rejected mutation B's row is byte-for-byte unchanged (witness snapshot).

What is deliberately NOT counted as isolation proof: 401s, 422s on malformed bodies, role
denials (the role matrix is E7's), and the dependency's ``404 Workspace not found.`` for a
nonexistent workspace. Schedule mutations and feedback are feature-gated and answer 503
while dark BEFORE scoping, so those probes run with the flag on.

Completeness is checked against the served application, not against a count: the route
contexts are walked, every operation whose dependency closure contains a tenancy
dependency (plus the two handler-checked GETs) is a customer-facing tenant-scoped
operation, and ``COVERAGE`` must name a test in this module for each of them. The two
limbs feedback needs an intelligence record for are proven in
``test_opportunity_feedback_api`` (each market there is its own organization) and are
cross-referenced, with the referenced tests verified to exist.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

import app.auth.dependencies as deps
from app.audit.models import AuditLog
from app.brands.models import Brand, BusinessProfile
from app.campaign_context.models import (
    AudienceProfile,
    BrandVoiceProfile,
    Campaign,
    ChannelPreference,
    ClaimsLibraryEntry,
    CompetitorProfile,
    OfferCalendarEntry,
    ProductProfile,
    SourcePreference,
)
from app.core.config import get_settings
from app.core.enums import OpportunityStatus, ScoutRequestStatus
from app.core.middleware import RateLimitMiddleware
from app.db.models import Base
from app.db.session import get_db
from app.feedback.models import OpportunityFeedback
from app.jobs.models import Job
from app.jobs.status import JobStatus
from app.locations.models import BusinessLocation, GeoCoverageRule
from app.main import app
from app.opportunities.models import Opportunity
from app.organizations.models import (
    OrganizationInvitation,
    OrganizationMember,
    Workspace,
)
from app.scouting_requests.models import ScoutRequest, ScoutSchedule

API = get_settings().api_prefix
W = "/workspaces/{workspace_id}"
ORG = "/organizations/{organization_id}"

NOT_A_MEMBER = "You are not a member of this organization."  # dependency seam
HANDLER_NOT_A_MEMBER = "Not a member of this organization."  # the two GET helpers
PASSWORD = "ci3-isolation-pw-123"

# (path segment, model, valid body, the body's identifier-list field if any)
CONTEXT = (
    ("products", ProductProfile, {"name": "Widget"}),
    ("audiences", AudienceProfile, {"label": "Owners"}),
    ("competitors", CompetitorProfile, {"name": "Rival"}),
    ("brand-voice", BrandVoiceProfile, {"tone": ["warm"]}),
    ("offers", OfferCalendarEntry, {"name": "Spring"}),
    ("claims", ClaimsLibraryEntry, {"text": "Fastest in town"}),
    ("source-preferences", SourcePreference, {"source_type": "news"}),
    ("channel-preferences", ChannelPreference, {"channel": "email"}),
    ("campaigns", Campaign, {"name": "Launch"}),
)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
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


def _rate_limiter() -> RateLimitMiddleware:
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()
    node = app.middleware_stack
    while node is not None and not isinstance(node, RateLimitMiddleware):
        node = getattr(node, "app", None)
    assert node is not None, "RateLimitMiddleware not found"
    return node


@dataclass
class Tenant:
    """Everything one organization owns, as created through the API (ids are real)."""

    key: str
    org_id: str
    ws_id: str
    owner_id: str
    owner_token: str
    marketer_id: str
    marketer_token: str
    brand_id: str = ""
    location_id: str = ""
    request_id: str = ""  # the request with a schedule: the foreign-id target
    life_request_id: str = ""  # a second request for lifecycle positives
    opportunity_id: str = ""
    job_id: str = ""
    invitation_id: str = ""
    items: dict[str, str] = field(default_factory=dict)  # context path -> item id


@dataclass
class World:
    client: TestClient
    witness: Engine
    a: Tenant
    b: Tenant
    build: dict[str, int] = field(default_factory=dict)  # positive outcomes seen while building

    # -- requests ----------------------------------------------------------------------
    def send(self, method: str, url: str, token: str, body=None, params=None):
        assert set(app.dependency_overrides) == {get_db}, "only get_db may be overridden"
        _rate_limiter()._hits.clear()
        headers = {"Authorization": f"Bearer {token}"}
        kwargs = {"headers": headers, "params": params}
        if body is not None:
            kwargs["json"] = body
        return self.client.request(method, url, **kwargs)

    def as_a(self, method: str, path: str, body=None, params=None):
        return self.send(method, API + path, self.a.owner_token, body, params)

    # -- witness -----------------------------------------------------------------------
    def row(self, model, row_id: str) -> dict | None:
        table = model.__table__
        with self.witness.connect() as conn:
            r = conn.execute(select(*table.c).where(table.c.id == row_id)).first()
            return dict(r._mapping) if r else None

    def count(self, model, **where) -> int:
        table = model.__table__
        stmt = select(func.count()).select_from(table)
        for k, v in where.items():
            stmt = stmt.where(table.c[k] == v)
        with self.witness.connect() as conn:
            return conn.execute(stmt).scalar_one()

    def membership(self, org_id: str, user_id: str) -> dict | None:
        table = OrganizationMember.__table__
        with self.witness.connect() as conn:
            r = conn.execute(
                select(*table.c).where(
                    table.c.organization_id == org_id, table.c.user_id == user_id
                )
            ).first()
            return dict(r._mapping) if r else None


def _error(r) -> tuple[int, str | None, str | None]:
    payload = r.json() if r.content else {}
    err = payload.get("error") if isinstance(payload, dict) else None
    return r.status_code, (err or {}).get("code"), (err or {}).get("message")


def _build_tenant(world: World, key: str, build: dict[str, int]) -> Tenant:
    c = world.client
    api = API

    def post(url, body, token=None, expect=201):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        _rate_limiter()._hits.clear()
        r = c.post(url, json=body, headers=headers)
        assert r.status_code == expect, (url, r.status_code, r.text)
        return r.json() if r.content else None

    owner = post(
        f"{api}/auth/register",
        {
            "email": f"ci3-{key}-owner@example.com",
            "full_name": f"{key} owner",
            "password": PASSWORD,
            "organization_name": f"CI3 organization {key}",
        },
    )
    org_id = owner["memberships"][0]["organization_id"]
    tok = owner["access_token"]
    ws = post(f"{api}/organizations/{org_id}/workspaces", {"name": f"WS {key}"}, tok)
    t = Tenant(key, org_id, ws["id"], owner["user"]["id"], tok, "", "")
    build[f"POST {ORG}/workspaces"] = 201
    # A second, non-owner member obtained through a real invitation (invited registration).
    inv = post(
        f"{api}/organizations/{org_id}/invitations",
        {"email": f"ci3-{key}-marketer@example.com", "role": "marketer"},
        tok,
    )
    build[f"POST {ORG}/invitations"] = 201
    joined = post(
        f"{api}/auth/invitations/register",
        {"token": inv["token"], "full_name": f"{key} marketer", "password": PASSWORD},
    )
    t.marketer_id, t.marketer_token = joined["user"]["id"], joined["access_token"]
    # A pending invitation that stays open (the foreign-id target for revoke).
    pending = post(
        f"{api}/organizations/{org_id}/invitations",
        {"email": f"ci3-{key}-pending@example.com", "role": "viewer"},
        tok,
    )
    t.invitation_id = pending["id"]
    w = f"{api}/workspaces/{t.ws_id}"
    ob = post(
        f"{w}/onboarding",
        {"brand_name": f"Brand {key}", "profile": {"company_name": f"Company {key}"}},
        tok,
    )
    t.brand_id = ob["brand"]["id"]
    build[f"POST {W}/onboarding"] = 201
    loc = post(f"{w}/locations", {"name": f"HQ {key}", "city": "Dallas", "country": "US"}, tok)
    t.location_id = loc["id"]
    build[f"POST {W}/locations"] = 201
    _rate_limiter()._hits.clear()
    geo = c.put(
        f"{w}/locations/{t.location_id}/geo-coverage",
        json={"coverage_type": "radius", "radius_miles": 25},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert geo.status_code == 200, geo.text
    build[f"PUT {W}/locations/{{location_id}}/geo-coverage"] = 200
    for path, _model, body in CONTEXT:
        item = post(f"{w}/{path}", body, tok)
        t.items[path] = item["id"]
        build[f"POST {W}/{path}"] = 201
    req = post(
        f"{w}/scout-requests",
        {"name": f"Scout {key}", "location_id": t.location_id, "keywords": ["coffee"]},
        tok,
    )
    t.request_id = req["id"]
    build[f"POST {W}/scout-requests"] = 201
    life = post(f"{w}/scout-requests", {"name": f"Scout life {key}", "keywords": ["tea"]}, tok)
    t.life_request_id = life["id"]
    # A schedule on the first request, through the feature-gated route with the flag ON.
    settings = get_settings()
    saved = settings.scout_scheduling_enabled
    settings.scout_scheduling_enabled = True
    try:
        post(f"{w}/scout-requests/{t.request_id}/schedule", {"interval": "daily"}, tok)
        build[f"POST {W}/scout-requests/{{request_id}}/schedule"] = 201
    finally:
        settings.scout_scheduling_enabled = saved
    return t


def _write_direct_rows(engine: Engine, t: Tenant) -> None:
    """The two row kinds no customer route creates: an opportunity and a durable job."""
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)() as s:
        opp = Opportunity(
            organization_id=t.org_id,
            workspace_id=t.ws_id,
            brand_id=t.brand_id,
            scout_request_id=t.request_id,
            location_id=t.location_id,
            title=f"Opportunity {t.key}",
            classification="lead",
            decision="pursue",
        )
        job = Job(
            organization_id=t.org_id,
            workspace_id=t.ws_id,
            location_id=t.location_id,
            scout_request_id=t.request_id,
            job_type="ci3.noop",
            payload={},
            payload_hash="0" * 64,
        )
        s.add_all([opp, job])
        s.commit()
        t.opportunity_id, t.job_id = opp.id, job.id


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Iterator[World]:
    db_file = tmp_path_factory.mktemp("ci3") / "ci3.db"
    request_engine, witness = _engine(db_file), _engine(db_file)
    Base.metadata.create_all(request_engine)
    factory = sessionmaker(
        bind=request_engine, autoflush=False, expire_on_commit=False, future=True
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
    client = TestClient(app)
    try:
        build: dict[str, int] = {}
        shell = World(client, witness, None, None, build)  # type: ignore[arg-type]
        a = _build_tenant(shell, "a", build)
        b = _build_tenant(shell, "b", build)
        _write_direct_rows(witness, a)
        _write_direct_rows(witness, b)
        yield World(client, witness, a, b, build)
    finally:
        app.dependency_overrides.clear()
        _rate_limiter()._hits.clear()
        request_engine.dispose()
        witness.dispose()


@dataclass(frozen=True)
class Pov:
    """One perspective: ``a`` is the acting tenant (its owner sends every request), ``b`` the
    foreign one. Every probe class runs under BOTH perspectives (``a-vs-b`` and ``b-vs-a``), so a
    predicate whose loss would return the first-built organization's row is caught from the
    second organization's side (Lane B finding F4)."""

    world: World
    a: Tenant
    b: Tenant

    def send(self, method: str, path: str, body=None, params=None):
        return self.world.send(method, API + path, self.a.owner_token, body, params)


@pytest.fixture(scope="module", params=["a-vs-b", "b-vs-a"])
def pov(request, world: World) -> Pov:
    if request.param == "a-vs-b":
        return Pov(world, world.a, world.b)
    return Pov(world, world.b, world.a)


@pytest.fixture
def scheduling_on() -> Iterator[None]:
    settings = get_settings()
    saved = settings.scout_scheduling_enabled
    settings.scout_scheduling_enabled = True
    try:
        yield
    finally:
        settings.scout_scheduling_enabled = saved


@pytest.fixture
def feedback_on() -> Iterator[None]:
    settings = get_settings()
    saved = settings.opportunity_feedback_enabled
    settings.opportunity_feedback_enabled = True
    try:
        yield
    finally:
        settings.opportunity_feedback_enabled = saved


# --------------------------------------------------------------------------- #
# Shared assertions
# --------------------------------------------------------------------------- #
def _assert_membership_403(r, message: str = NOT_A_MEMBER) -> None:
    assert _error(r) == (403, "permission_denied", message), r.text


def _assert_404(r, message: str) -> None:
    status, _code, msg = _error(r)
    assert (status, msg) == (404, message), r.text


def _ws_path(world: World, tenant: Tenant, suffix: str = "") -> str:
    return f"/workspaces/{tenant.ws_id}{suffix}"


# --------------------------------------------------------------------------- #
# 0. The world is what the tests assume
# --------------------------------------------------------------------------- #
class TestWorld:
    def test_two_organizations_two_workspaces_from_real_flows(self, world: World):
        a, b = world.a, world.b
        assert a.org_id != b.org_id and a.ws_id != b.ws_id
        assert world.row(Workspace, a.ws_id)["organization_id"] == a.org_id
        assert world.row(Workspace, b.ws_id)["organization_id"] == b.org_id
        # Each owner holds exactly one membership (their own organization); each marketer
        # joined through an invitation that is now accepted.
        for t in (a, b):
            assert world.membership(t.org_id, t.owner_id)["role"] == "owner"
            assert world.membership(t.org_id, t.marketer_id)["role"] == "marketer"
            assert world.count(OrganizationMember, user_id=t.owner_id) == 1
        assert world.membership(b.org_id, a.owner_id) is None
        assert world.membership(a.org_id, b.owner_id) is None

    def test_every_resource_kind_exists_in_both_organizations(self, world: World):
        for t in (world.a, world.b):
            assert world.row(Brand, t.brand_id)["organization_id"] == t.org_id
            assert world.row(BusinessLocation, t.location_id)["workspace_id"] == t.ws_id
            assert world.row(ScoutRequest, t.request_id)["workspace_id"] == t.ws_id
            assert world.row(ScoutRequest, t.life_request_id)["workspace_id"] == t.ws_id
            assert world.count(ScoutSchedule, scout_request_id=t.request_id) == 1
            assert world.count(GeoCoverageRule, location_id=t.location_id) == 1
            assert world.row(Opportunity, t.opportunity_id)["workspace_id"] == t.ws_id
            assert world.row(Job, t.job_id)["workspace_id"] == t.ws_id
            assert world.row(OrganizationInvitation, t.invitation_id)["accepted_at"] is None
            for path, model, _ in CONTEXT:
                row = world.row(model, t.items[path])
                assert (row["workspace_id"], row["brand_id"]) == (t.ws_id, t.brand_id)
            assert world.row(BusinessLocation, t.location_id)["brand_id"] == t.brand_id
            assert world.row(ScoutRequest, t.request_id)["brand_id"] == t.brand_id
            profile = next(r for r in _rows(world, BusinessProfile) if r["workspace_id"] == t.ws_id)
            assert profile["brand_id"] == t.brand_id

    def test_build_positive_outcomes_were_the_routes_success_statuses(self, world: World):
        assert world.build == {
            f"POST {ORG}/workspaces": 201,
            f"POST {ORG}/invitations": 201,
            f"POST {W}/onboarding": 201,
            f"POST {W}/locations": 201,
            f"PUT {W}/locations/{{location_id}}/geo-coverage": 200,
            **{f"POST {W}/{p}": 201 for p, _m, _b in CONTEXT},
            f"POST {W}/scout-requests": 201,
            f"POST {W}/scout-requests/{{request_id}}/schedule": 201,
        }

    def test_only_get_db_is_overridden(self, world: World):
        assert set(app.dependency_overrides) == {get_db}


# --------------------------------------------------------------------------- #
# 1. Workspace and organization reads/creation
# --------------------------------------------------------------------------- #
class TestWorkspaceAndOrganization:
    def test_get_workspace_own_200_foreign_403(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        own = pov.send("GET", _ws_path(world, a))
        assert own.status_code == 200 and own.json()["organization_id"] == a.org_id
        _assert_membership_403(pov.send("GET", _ws_path(world, b)), HANDLER_NOT_A_MEMBER)

    def test_list_workspaces_own_only_foreign_org_403(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        own = pov.send("GET", f"/organizations/{a.org_id}/workspaces")
        assert own.status_code == 200
        ids = {w["id"] for w in own.json()}
        assert a.ws_id in ids and b.ws_id not in ids
        assert all(world.row(Workspace, i)["organization_id"] == a.org_id for i in ids)
        _assert_membership_403(
            pov.send("GET", f"/organizations/{b.org_id}/workspaces"), HANDLER_NOT_A_MEMBER
        )

    def test_list_organizations_is_the_callers_memberships_only(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        assert [o["id"] for o in pov.send("GET", "/organizations").json()] == [a.org_id]
        other = world.send("GET", f"{API}/organizations", b.owner_token)
        assert [o["id"] for o in other.json()] == [b.org_id]
        marketer = world.send("GET", f"{API}/organizations", a.marketer_token)
        assert [o["id"] for o in marketer.json()] == [a.org_id]

    def test_create_workspace_in_foreign_org_403_and_nothing_created(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        before = world.count(Workspace, organization_id=b.org_id)
        r = pov.send("POST", f"/organizations/{b.org_id}/workspaces", {"name": "Intruder"})
        _assert_membership_403(r)
        assert world.count(Workspace, organization_id=b.org_id) == before
        own = pov.send("POST", f"/organizations/{a.org_id}/workspaces", {"name": "Second"})
        assert own.status_code == 201 and own.json()["organization_id"] == a.org_id


# --------------------------------------------------------------------------- #
# 2. Invitations and members (organization scope)
# --------------------------------------------------------------------------- #
class TestInvitationsAndMembers:
    def test_invitations_list_and_create_foreign_org_403(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        own = pov.send("GET", f"/organizations/{a.org_id}/invitations")
        assert own.status_code == 200
        pending = {
            r["id"]
            for r in _rows(world, OrganizationInvitation)
            if r["organization_id"] == a.org_id
            and r["accepted_at"] is None
            and r["revoked_at"] is None
        }
        assert {i["id"] for i in own.json()} == pending and a.invitation_id in pending
        assert b.invitation_id not in pending
        assert not {i["email"] for i in own.json()} & {
            r["email"]
            for r in _rows(world, OrganizationInvitation)
            if r["organization_id"] == b.org_id
        }
        _assert_membership_403(pov.send("GET", f"/organizations/{b.org_id}/invitations"))
        before = world.count(OrganizationInvitation, organization_id=b.org_id)
        r = pov.send(
            "POST",
            f"/organizations/{b.org_id}/invitations",
            {"email": "intruder@example.com", "role": "viewer"},
        )
        _assert_membership_403(r)
        assert world.count(OrganizationInvitation, organization_id=b.org_id) == before

    def test_revoke_foreign_invitation_id_404_stays_pending(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        before = world.row(OrganizationInvitation, b.invitation_id)
        r = pov.send("DELETE", f"/organizations/{a.org_id}/invitations/{b.invitation_id}")
        _assert_404(r, "Invitation not found.")
        assert world.row(OrganizationInvitation, b.invitation_id) == before
        _assert_membership_403(
            pov.send("DELETE", f"/organizations/{b.org_id}/invitations/{b.invitation_id}")
        )
        assert world.row(OrganizationInvitation, b.invitation_id) == before
        # Positive: the owner revokes a pending invitation of their own organization.
        mine = pov.send(
            "POST",
            f"/organizations/{a.org_id}/invitations",
            {"email": f"ci3-{a.key}-revoke-me@example.com", "role": "viewer"},
        )
        assert mine.status_code == 201
        done = pov.send("DELETE", f"/organizations/{a.org_id}/invitations/{mine.json()['id']}")
        assert done.status_code == 204
        assert world.row(OrganizationInvitation, mine.json()["id"])["revoked_at"] is not None

    def test_members_list_own_200_foreign_403(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        own = pov.send("GET", f"/organizations/{a.org_id}/members")
        assert own.status_code == 200
        assert {m["user_id"] for m in own.json()} == {a.owner_id, a.marketer_id}
        _assert_membership_403(pov.send("GET", f"/organizations/{b.org_id}/members"))

    def test_role_change_and_removal_foreign_user_id_404_unchanged(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        before = world.membership(b.org_id, b.marketer_id)
        r = pov.send(
            "PUT", f"/organizations/{a.org_id}/members/{b.marketer_id}/role", {"role": "viewer"}
        )
        _assert_404(r, "Member not found.")
        r = pov.send("DELETE", f"/organizations/{a.org_id}/members/{b.marketer_id}")
        _assert_404(r, "Member not found.")
        # The same identifiers through B's organization path: refused by membership.
        _assert_membership_403(
            pov.send(
                "PUT",
                f"/organizations/{b.org_id}/members/{b.marketer_id}/role",
                {"role": "viewer"},
            )
        )
        _assert_membership_403(
            pov.send("DELETE", f"/organizations/{b.org_id}/members/{b.marketer_id}")
        )
        assert world.membership(b.org_id, b.marketer_id) == before
        # Positive: the owner changes and restores their own marketer's role.
        r = pov.send(
            "PUT", f"/organizations/{a.org_id}/members/{a.marketer_id}/role", {"role": "viewer"}
        )
        assert r.status_code == 200 and r.json()["role"] == "viewer"
        r = pov.send(
            "PUT",
            f"/organizations/{a.org_id}/members/{a.marketer_id}/role",
            {"role": "marketer"},
        )
        assert r.status_code == 200 and r.json()["role"] == "marketer"

    def test_remove_member_positive_on_own_organization(self, world: World, pov: Pov):
        a = pov.a
        # Invite + register a throwaway member, then remove them.
        inv = pov.send(
            "POST",
            f"/organizations/{a.org_id}/invitations",
            {"email": f"ci3-{a.key}-removable@example.com", "role": "viewer"},
        )
        assert inv.status_code == 201
        _rate_limiter()._hits.clear()
        joined = world.client.post(
            f"{API}/auth/invitations/register",
            json={"token": inv.json()["token"], "full_name": "removable", "password": PASSWORD},
        )
        assert joined.status_code == 201
        uid = joined.json()["user"]["id"]
        assert world.membership(a.org_id, uid) is not None
        r = pov.send("DELETE", f"/organizations/{a.org_id}/members/{uid}")
        assert r.status_code == 204
        assert world.membership(a.org_id, uid) is None


# --------------------------------------------------------------------------- #
# 3. Onboarding, brands, business profile
# --------------------------------------------------------------------------- #
class TestBrandsAndProfile:
    def test_brands_and_profile_reads_own_only_foreign_403(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        brands = pov.send("GET", _ws_path(world, a, "/brands"))
        assert brands.status_code == 200 and [x["id"] for x in brands.json()] == [a.brand_id]
        profile = pov.send("GET", _ws_path(world, a, "/business-profile"))
        assert profile.status_code == 200
        assert (profile.json()["workspace_id"], profile.json()["brand_id"]) == (a.ws_id, a.brand_id)
        assert profile.json()["company_name"].startswith(f"Company {a.key}")
        assert all(x["id"] == a.brand_id for x in brands.json())
        _assert_membership_403(pov.send("GET", _ws_path(world, b, "/brands")))
        _assert_membership_403(pov.send("GET", _ws_path(world, b, "/business-profile")))

    def test_profile_update_foreign_403_unchanged_own_200(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        b_profile_id = next(
            r["id"] for r in _rows(world, BusinessProfile) if r["workspace_id"] == b.ws_id
        )
        before = world.row(BusinessProfile, b_profile_id)
        r = pov.send("PUT", _ws_path(world, b, "/business-profile"), {"company_name": "Hijacked"})
        _assert_membership_403(r)
        assert world.row(BusinessProfile, b_profile_id) == before
        own = pov.send(
            "PUT", _ws_path(world, a, "/business-profile"), {"company_name": f"Company {a.key} v2"}
        )
        assert own.status_code == 200 and own.json()["company_name"] == f"Company {a.key} v2"
        assert own.json()["workspace_id"] == a.ws_id
        assert world.row(BusinessProfile, b_profile_id) == before  # the foreign row stays put
        own_row = next(r for r in _rows(world, BusinessProfile) if r["workspace_id"] == a.ws_id)
        assert own_row["company_name"] == f"Company {a.key} v2"

    def test_onboarding_foreign_403_creates_no_brand(self, world: World, pov: Pov):
        b = pov.b
        before = world.count(Brand, workspace_id=b.ws_id)
        r = pov.send(
            "POST",
            _ws_path(world, b, "/onboarding"),
            {"brand_name": "Intruder", "profile": {"company_name": "Intruder Co"}},
        )
        _assert_membership_403(r)
        assert world.count(Brand, workspace_id=b.ws_id) == before


def _rows(world: World, model) -> list[dict]:
    table = model.__table__
    with world.witness.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(*table.c))]


# --------------------------------------------------------------------------- #
# 4. The nine context entities
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,model,body", CONTEXT, ids=[c[0] for c in CONTEXT])
class TestContextEntities:
    def test_list_returns_own_rows_only_and_foreign_ws_403(self, world, pov, path, model, body):
        a, b = pov.a, pov.b
        own = pov.send("GET", _ws_path(world, a, f"/{path}"))
        assert own.status_code == 200
        ids = {x["id"] for x in own.json()}
        assert a.items[path] in ids and b.items[path] not in ids
        assert all(x["workspace_id"] == a.ws_id for x in own.json())
        _assert_membership_403(pov.send("GET", _ws_path(world, b, f"/{path}")))

    def test_create_in_foreign_ws_403_nothing_written_own_201(self, world, pov, path, model, body):
        a, b = pov.a, pov.b
        before = world.count(model, workspace_id=b.ws_id)
        _assert_membership_403(pov.send("POST", _ws_path(world, b, f"/{path}"), body))
        assert world.count(model, workspace_id=b.ws_id) == before
        own = pov.send("POST", _ws_path(world, a, f"/{path}"), body)
        assert own.status_code == 201 and own.json()["workspace_id"] == a.ws_id
        assert own.json()["brand_id"] == a.brand_id  # resolved from the caller's own workspace

    def test_delete_with_foreign_item_id_404_and_row_survives(self, world, pov, path, model, body):
        a, b = pov.a, pov.b
        before = world.row(model, b.items[path])
        r = pov.send("DELETE", _ws_path(world, a, f"/{path}/{b.items[path]}"))
        _assert_404(r, "Item not found in this workspace.")
        _assert_membership_403(pov.send("DELETE", _ws_path(world, b, f"/{path}/{b.items[path]}")))
        assert world.row(model, b.items[path]) == before
        # Positive: deleting one of A's own items works.
        mine = pov.send("POST", _ws_path(world, a, f"/{path}"), body).json()["id"]
        assert pov.send("DELETE", _ws_path(world, a, f"/{path}/{mine}")).status_code == 204
        assert world.row(model, mine) is None


# --------------------------------------------------------------------------- #
# 5. Locations, geo-coverage, geocode
# --------------------------------------------------------------------------- #
class TestLocations:
    def test_list_and_create(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        own = pov.send("GET", _ws_path(world, a, "/locations"))
        assert own.status_code == 200
        ids = {x["id"] for x in own.json()}
        assert a.location_id in ids and b.location_id not in ids
        assert all(x["workspace_id"] == a.ws_id for x in own.json())
        _assert_membership_403(pov.send("GET", _ws_path(world, b, "/locations")))
        before = world.count(BusinessLocation, workspace_id=b.ws_id)
        _assert_membership_403(
            pov.send("POST", _ws_path(world, b, "/locations"), {"name": "Intruder"})
        )
        assert world.count(BusinessLocation, workspace_id=b.ws_id) == before
        created = pov.send("POST", _ws_path(world, a, "/locations"), {"name": f"Branch {a.key}"})
        assert created.status_code == 201
        assert (created.json()["workspace_id"], created.json()["brand_id"]) == (a.ws_id, a.brand_id)

    def test_update_with_foreign_location_id_404_unchanged(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        before = world.row(BusinessLocation, b.location_id)
        r = pov.send("PUT", _ws_path(world, a, f"/locations/{b.location_id}"), {"name": "Hijacked"})
        _assert_404(r, "Location not found in this workspace.")
        _assert_membership_403(
            pov.send("PUT", _ws_path(world, b, f"/locations/{b.location_id}"), {"name": "Hijacked"})
        )
        assert world.row(BusinessLocation, b.location_id) == before
        own = pov.send(
            "PUT", _ws_path(world, a, f"/locations/{a.location_id}"), {"name": f"HQ {a.key} v2"}
        )
        assert own.status_code == 200 and own.json()["name"] == f"HQ {a.key} v2"

    def test_geo_coverage_with_foreign_location_id_404_unchanged(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        rule_id = next(
            r["id"] for r in _rows(world, GeoCoverageRule) if r["location_id"] == b.location_id
        )
        before = world.row(GeoCoverageRule, rule_id)
        _assert_404(
            pov.send("GET", _ws_path(world, a, f"/locations/{b.location_id}/geo-coverage")),
            "No Scout Reach configured for this location yet.",
        )
        r = pov.send(
            "PUT",
            _ws_path(world, a, f"/locations/{b.location_id}/geo-coverage"),
            {"coverage_type": "radius", "radius_miles": 1},
        )
        _assert_404(r, "Location not found in this workspace.")
        _assert_membership_403(
            pov.send("GET", _ws_path(world, b, f"/locations/{b.location_id}/geo-coverage"))
        )
        _assert_membership_403(
            pov.send(
                "PUT",
                _ws_path(world, b, f"/locations/{b.location_id}/geo-coverage"),
                {"coverage_type": "radius", "radius_miles": 1},
            )
        )
        assert world.row(GeoCoverageRule, rule_id) == before
        assert world.count(GeoCoverageRule, workspace_id=a.ws_id, location_id=b.location_id) == 0
        own = pov.send("GET", _ws_path(world, a, f"/locations/{a.location_id}/geo-coverage"))
        assert own.status_code == 200 and own.json()["radius_miles"] == 25

    def test_deactivate_and_reactivate_with_foreign_location_id_404_unchanged(
        self, world: World, pov: Pov
    ):
        a, b = pov.a, pov.b
        before = world.row(BusinessLocation, b.location_id)
        for verb in ("deactivate", "reactivate"):
            r = pov.send("POST", _ws_path(world, a, f"/locations/{b.location_id}/{verb}"))
            _assert_404(r, "Location not found in this workspace.")
            _assert_membership_403(
                pov.send("POST", _ws_path(world, b, f"/locations/{b.location_id}/{verb}"))
            )
        assert world.row(BusinessLocation, b.location_id) == before
        # Positive: A retires and restores a location of its own (not the shared fixture one).
        mine = pov.send("POST", _ws_path(world, a, "/locations"), {"name": f"Pop-up {a.key}"})
        mine_id = mine.json()["id"]
        off = pov.send("POST", _ws_path(world, a, f"/locations/{mine_id}/deactivate"))
        assert off.status_code == 200 and off.json()["is_active"] is False
        on = pov.send("POST", _ws_path(world, a, f"/locations/{mine_id}/reactivate"))
        assert on.status_code == 200 and on.json()["is_active"] is True

    def test_geocode_workspace_id_in_query(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        own = pov.send("POST", "/geocode", {"query": "Dallas"}, params={"workspace_id": a.ws_id})
        assert own.status_code == 200, own.text
        _assert_membership_403(
            pov.send("POST", "/geocode", {"query": "Dallas"}, params={"workspace_id": b.ws_id})
        )


# --------------------------------------------------------------------------- #
# 6. Scout requests: list filters, body identifiers, lifecycle, runs
# --------------------------------------------------------------------------- #
class TestScoutRequests:
    def test_list_own_only_and_foreign_location_filter_is_empty(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        own = pov.send("GET", _ws_path(world, a, "/scout-requests"))
        assert own.status_code == 200
        assert {x["id"] for x in own.json()} == {a.request_id, a.life_request_id}
        filtered = pov.send(
            "GET", _ws_path(world, a, "/scout-requests"), params={"location_id": b.location_id}
        )
        assert filtered.status_code == 200 and filtered.json() == []
        _assert_membership_403(pov.send("GET", _ws_path(world, b, "/scout-requests")))

    def test_create_with_foreign_location_in_body_404_nothing_created(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        before_a = world.count(ScoutRequest, workspace_id=a.ws_id)
        before_b = world.count(ScoutRequest, workspace_id=b.ws_id)
        r = pov.send(
            "POST",
            _ws_path(world, a, "/scout-requests"),
            {"name": "Borrowed location", "location_id": b.location_id},
        )
        _assert_404(r, "Location not found in this workspace.")
        _assert_membership_403(
            pov.send("POST", _ws_path(world, b, "/scout-requests"), {"name": "Intruder"})
        )
        assert world.count(ScoutRequest, workspace_id=a.ws_id) == before_a
        assert world.count(ScoutRequest, workspace_id=b.ws_id) == before_b

    def test_get_and_update_with_foreign_request_id_404_unchanged(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        before = world.row(ScoutRequest, b.request_id)
        _assert_404(
            pov.send("GET", _ws_path(world, a, f"/scout-requests/{b.request_id}")),
            "Scout request not found in this workspace.",
        )
        r = pov.send(
            "PUT", _ws_path(world, a, f"/scout-requests/{b.request_id}"), {"name": "Hijacked"}
        )
        _assert_404(r, "Scout request not found in this workspace.")
        _assert_membership_403(
            pov.send("GET", _ws_path(world, b, f"/scout-requests/{b.request_id}"))
        )
        _assert_membership_403(
            pov.send(
                "PUT", _ws_path(world, b, f"/scout-requests/{b.request_id}"), {"name": "Hijacked"}
            )
        )
        assert world.row(ScoutRequest, b.request_id) == before
        own = pov.send("GET", _ws_path(world, a, f"/scout-requests/{a.request_id}"))
        assert own.status_code == 200 and own.json()["organization_id"] == a.org_id
        upd = pov.send(
            "PUT", _ws_path(world, a, f"/scout-requests/{a.request_id}"), {"notes": "mine"}
        )
        assert upd.status_code == 200 and upd.json()["notes"] == "mine"

    @pytest.mark.parametrize("verb", ["pause", "resume", "run"])
    def test_lifecycle_with_foreign_request_id_404_unchanged(
        self, world: World, pov: Pov, verb: str
    ):
        a, b = pov.a, pov.b
        before = world.row(ScoutRequest, b.request_id)
        jobs_before = world.count(Job, workspace_id=b.ws_id)
        r = pov.send("POST", _ws_path(world, a, f"/scout-requests/{b.request_id}/{verb}"))
        _assert_404(r, "Scout request not found in this workspace.")
        _assert_membership_403(
            pov.send("POST", _ws_path(world, b, f"/scout-requests/{b.request_id}/{verb}"))
        )
        assert world.row(ScoutRequest, b.request_id) == before
        assert world.count(Job, workspace_id=b.ws_id) == jobs_before

    def test_lifecycle_positive_on_own_request(self, world: World, pov: Pov):
        a = pov.a
        base = _ws_path(world, a, f"/scout-requests/{a.life_request_id}")
        paused = pov.send("POST", base + "/pause")
        assert paused.status_code == 200
        assert paused.json()["status"] == ScoutRequestStatus.PAUSED.value
        resumed = pov.send("POST", base + "/resume")
        assert resumed.status_code == 200
        assert resumed.json()["status"] == ScoutRequestStatus.DRAFT.value
        jobs_before = world.count(Job, workspace_id=a.ws_id)
        ran = pov.send("POST", base + "/run")
        assert ran.status_code == 200, ran.text
        assert ran.json()["status"] == ScoutRequestStatus.QUEUED.value
        assert world.count(Job, workspace_id=a.ws_id) == jobs_before + 1
        job = world.row(Job, ran.json()["stats"]["job_id"])
        assert (job["organization_id"], job["workspace_id"]) == (a.org_id, a.ws_id)

    def test_runs_history_with_foreign_request_id_404(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        _assert_404(
            pov.send("GET", _ws_path(world, a, f"/scout-requests/{b.request_id}/runs")),
            "Scout request not found in this workspace.",
        )
        _assert_membership_403(
            pov.send("GET", _ws_path(world, b, f"/scout-requests/{b.request_id}/runs"))
        )
        own = pov.send("GET", _ws_path(world, a, f"/scout-requests/{a.request_id}/runs"))
        assert own.status_code == 200 and own.json()["items"] == []


# --------------------------------------------------------------------------- #
# 7. Scout schedules (reads open while dark; mutations probed with the feature ON)
# --------------------------------------------------------------------------- #
class TestScoutSchedules:
    def test_get_schedule_with_foreign_request_id_404(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        _assert_404(
            pov.send("GET", _ws_path(world, a, f"/scout-requests/{b.request_id}/schedule")),
            "Scout request not found in this workspace.",
        )
        _assert_membership_403(
            pov.send("GET", _ws_path(world, b, f"/scout-requests/{b.request_id}/schedule"))
        )
        own = pov.send("GET", _ws_path(world, a, f"/scout-requests/{a.request_id}/schedule"))
        assert own.status_code == 200 and own.json()["scout_request_id"] == a.request_id

    def test_mutations_with_foreign_request_id_404_and_schedule_unchanged(
        self, world: World, pov: Pov, scheduling_on
    ):
        a, b = pov.a, pov.b
        sched_id = next(
            r["id"] for r in _rows(world, ScoutSchedule) if r["scout_request_id"] == b.request_id
        )
        before = world.row(ScoutSchedule, sched_id)
        base = _ws_path(world, a, f"/scout-requests/{b.request_id}/schedule")
        for method, path, body in (
            ("POST", base, {"interval": "daily"}),
            ("POST", base + "/pause", None),
            ("POST", base + "/resume", None),
            ("DELETE", base, None),
        ):
            _assert_404(pov.send(method, path, body), "Scout request not found in this workspace.")
        foreign = _ws_path(world, b, f"/scout-requests/{b.request_id}/schedule")
        for method, path, body in (
            ("POST", foreign, {"interval": "daily"}),
            ("POST", foreign + "/pause", None),
            ("POST", foreign + "/resume", None),
            ("DELETE", foreign, None),
        ):
            _assert_membership_403(pov.send(method, path, body))
        assert world.row(ScoutSchedule, sched_id) == before
        assert world.count(ScoutSchedule, workspace_id=b.ws_id) == 1

    def test_mutations_positive_on_own_request(self, world: World, pov: Pov, scheduling_on):
        a = pov.a
        base = _ws_path(world, a, f"/scout-requests/{a.life_request_id}/schedule")
        created = pov.send("POST", base, {"interval": "daily"})
        assert created.status_code == 201, created.text
        assert pov.send("POST", base + "/pause").status_code == 200
        assert pov.send("POST", base + "/resume").status_code == 200
        assert pov.send("DELETE", base).status_code == 204
        assert world.count(ScoutSchedule, scout_request_id=a.life_request_id) == 0


# --------------------------------------------------------------------------- #
# 8. Opportunities (+ intelligence, feedback, feedback-capability)
# --------------------------------------------------------------------------- #
class TestOpportunities:
    def test_list_own_only_and_foreign_query_filters_empty(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        own = pov.send("GET", _ws_path(world, a, "/opportunities"))
        assert own.status_code == 200 and [x["id"] for x in own.json()] == [a.opportunity_id]
        for params in (
            {"location_id": b.location_id},
            {"scout_request_id": b.request_id},
            {"campaign_id": b.items["campaigns"]},
        ):
            r = pov.send("GET", _ws_path(world, a, "/opportunities"), params=params)
            assert r.status_code == 200 and r.json() == [], params
        _assert_membership_403(pov.send("GET", _ws_path(world, b, "/opportunities")))

    def test_detail_and_intelligence_with_foreign_opportunity_id_404(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        for suffix in ("", "/intelligence"):
            _assert_404(
                pov.send("GET", _ws_path(world, a, f"/opportunities/{b.opportunity_id}{suffix}")),
                "Opportunity not found in this workspace.",
            )
            _assert_membership_403(
                pov.send("GET", _ws_path(world, b, f"/opportunities/{b.opportunity_id}{suffix}"))
            )
        own = pov.send("GET", _ws_path(world, a, f"/opportunities/{a.opportunity_id}"))
        assert own.status_code == 200 and own.json()["id"] == a.opportunity_id
        intel = pov.send(
            "GET", _ws_path(world, a, f"/opportunities/{a.opportunity_id}/intelligence")
        )
        assert intel.status_code == 200 and intel.json()["opportunity_id"] == a.opportunity_id

    def test_status_update_with_foreign_opportunity_id_404_unchanged(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        before = world.row(Opportunity, b.opportunity_id)
        own_before = world.row(Opportunity, a.opportunity_id)["status"]
        target = next(
            st.value for st in OpportunityStatus if st.value not in (before["status"], own_before)
        )
        r = pov.send(
            "PUT",
            _ws_path(world, a, f"/opportunities/{b.opportunity_id}/status"),
            {"status": target},
        )
        _assert_404(r, "Opportunity not found in this workspace.")
        _assert_membership_403(
            pov.send(
                "PUT",
                _ws_path(world, b, f"/opportunities/{b.opportunity_id}/status"),
                {"status": target},
            )
        )
        assert world.row(Opportunity, b.opportunity_id) == before
        own = pov.send(
            "PUT",
            _ws_path(world, a, f"/opportunities/{a.opportunity_id}/status"),
            {"status": target},
        )
        assert own.status_code == 200 and own.json()["status"] == target

    def test_feedback_capability_and_feedback_routes(self, world: World, pov: Pov, feedback_on):
        a, b = pov.a, pov.b
        own = pov.send("GET", _ws_path(world, a, "/feedback-capability"))
        assert own.status_code == 200 and own.json() == {"enabled": True}
        _assert_membership_403(pov.send("GET", _ws_path(world, b, "/feedback-capability")))
        # Foreign opportunity id under the caller's own workspace: scoped 404 on both operations;
        # nothing is written for either organization's opportunity.
        feedback_before = world.count(OpportunityFeedback)
        opp_before = world.row(Opportunity, b.opportunity_id)
        _assert_404(
            pov.send("GET", _ws_path(world, a, f"/opportunities/{b.opportunity_id}/feedback")),
            "Opportunity not found in this workspace.",
        )
        _assert_404(
            pov.send(
                "POST",
                _ws_path(world, a, f"/opportunities/{b.opportunity_id}/feedback"),
                {"intelligence_record_id": "x" * 32, "is_useful": True},
            ),
            "Opportunity not found in this workspace.",
        )
        probe_body = {"intelligence_record_id": "x" * 32, "is_useful": True}
        for method, body in (("GET", None), ("POST", probe_body)):
            foreign = _ws_path(world, b, f"/opportunities/{b.opportunity_id}/feedback")
            _assert_membership_403(pov.send(method, foreign, body))
        own_history = pov.send(
            "GET", _ws_path(world, a, f"/opportunities/{a.opportunity_id}/feedback")
        )
        assert own_history.status_code == 200 and own_history.json()["items"] == []
        assert world.count(OpportunityFeedback) == feedback_before
        assert world.row(Opportunity, b.opportunity_id) == opp_before


# --------------------------------------------------------------------------- #
# 9. Jobs
# --------------------------------------------------------------------------- #
class TestJobs:
    def test_list_own_only_and_foreign_query_filters_empty(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        own = pov.send("GET", _ws_path(world, a, "/jobs"))
        assert own.status_code == 200
        ids = {x["id"] for x in own.json()["items"]}
        assert a.job_id in ids and b.job_id not in ids
        for params in ({"location_id": b.location_id}, {"scout_request_id": b.request_id}):
            r = pov.send("GET", _ws_path(world, a, "/jobs"), params=params)
            assert r.status_code == 200 and r.json()["items"] == [], params
        _assert_membership_403(pov.send("GET", _ws_path(world, b, "/jobs")))

    def test_get_events_cancel_with_foreign_job_id_404_unchanged(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        before = world.row(Job, b.job_id)
        for method, suffix in (("GET", ""), ("GET", "/events"), ("POST", "/cancel")):
            _assert_404(
                pov.send(method, _ws_path(world, a, f"/jobs/{b.job_id}{suffix}")),
                "Job not found in this workspace.",
            )
            _assert_membership_403(
                pov.send(method, _ws_path(world, b, f"/jobs/{b.job_id}{suffix}"))
            )
        assert world.row(Job, b.job_id) == before
        own = pov.send("GET", _ws_path(world, a, f"/jobs/{a.job_id}"))
        assert own.status_code == 200 and own.json()["id"] == a.job_id
        events = pov.send("GET", _ws_path(world, a, f"/jobs/{a.job_id}/events"))
        assert events.status_code == 200
        assert world.row(Job, a.job_id)["status"] == JobStatus.PENDING.value
        cancelled = pov.send("POST", _ws_path(world, a, f"/jobs/{a.job_id}/cancel"))
        assert cancelled.status_code == 200
        assert world.row(Job, a.job_id)["status"] == JobStatus.CANCELLED.value
        assert world.row(Job, b.job_id) == before


# --------------------------------------------------------------------------- #
# 10. Audit logs (the full two-organization matrix lives in test_audit_logs_role_matrix)
# --------------------------------------------------------------------------- #
class TestAuditLogs:
    def test_own_200_own_organization_rows_only_foreign_403(self, world: World, pov: Pov):
        a, b = pov.a, pov.b
        own = pov.send("GET", _ws_path(world, a, "/audit-logs"))
        assert own.status_code == 200 and own.json()
        # Every returned row is an organization-A row (witness), none was written by B's actors.
        returned = {r["id"] for r in own.json()}
        a_rows = {r["id"] for r in _rows(world, AuditLog) if r["organization_id"] == a.org_id}
        b_rows = {r["id"] for r in _rows(world, AuditLog) if r["organization_id"] == b.org_id}
        assert returned <= a_rows and not (returned & b_rows) and b_rows
        assert not {r["actor_user_id"] for r in own.json()} & {b.owner_id, b.marketer_id}
        _assert_membership_403(pov.send("GET", _ws_path(world, b, "/audit-logs")))


# --------------------------------------------------------------------------- #
# 10b. Paginated lists: cursors and totals stay inside the organization (P6-PLAT-12)
# --------------------------------------------------------------------------- #
PAGED_LISTS = ("opportunities", "audit-logs", "scout-requests", *(c[0] for c in CONTEXT))


def _own_row_ids(world: World, tenant: Tenant, path: str) -> set[str]:
    """Every row the tenant's list may show, read by the witness (not through the API)."""
    if path == "audit-logs":
        return {
            r["id"]
            for r in _rows(world, AuditLog)
            if r["organization_id"] == tenant.org_id and r["workspace_id"] in (tenant.ws_id, None)
        }
    model = {
        "opportunities": Opportunity,
        "scout-requests": ScoutRequest,
        **{p: m for p, m, _b in CONTEXT},
    }[path]
    return {r["id"] for r in _rows(world, model) if r["workspace_id"] == tenant.ws_id}


def _ensure_two_rows(world: World, tenant: Tenant, path: str) -> list[tuple[type, str]]:
    """A cursor exists only when a second page does: give the tenant at least two rows.

    Returns the rows added, which the caller removes again: other probes assume the world
    exactly as it was built (one opportunity per tenant, for example).
    """
    if len(_own_row_ids(world, tenant, path)) >= 2:
        return []
    if path == "opportunities":
        with sessionmaker(bind=world.witness, expire_on_commit=False, future=True)() as s:
            row = Opportunity(
                organization_id=tenant.org_id,
                workspace_id=tenant.ws_id,
                brand_id=tenant.brand_id,
                scout_request_id=tenant.request_id,
                location_id=tenant.location_id,
                title=f"Second opportunity {tenant.key}",
                classification="lead",
                decision="pursue",
            )
            s.add(row)
            s.commit()
            return [(Opportunity, row.id)]
    model, body = next((m, b) for p, m, b in CONTEXT if p == path)
    r = world.send("POST", API + _ws_path(world, tenant, f"/{path}"), tenant.owner_token, body)
    assert r.status_code == 201, r.text
    return [(model, r.json()["id"])]


def _remove(world: World, added: list[tuple[type, str]]) -> None:
    with world.witness.begin() as conn:
        for model, row_id in added:
            conn.execute(model.__table__.delete().where(model.__table__.c.id == row_id))


@pytest.mark.parametrize("path", PAGED_LISTS)
class TestPaginatedLists:
    def test_foreign_cursor_refused_and_pages_and_total_are_own_rows_only(
        self, world: World, pov: Pov, path: str
    ):
        a, b = pov.a, pov.b
        added = _ensure_two_rows(world, a, path) + _ensure_two_rows(world, b, path)
        try:
            self._probe(world, pov, a, b, path)
        finally:
            _remove(world, added)

    @staticmethod
    def _probe(world: World, pov: Pov, a: Tenant, b: Tenant, path: str) -> None:
        # B's cursor, issued to B's owner on B's own list.
        issued = world.send(
            "GET", API + _ws_path(world, b, f"/{path}"), b.owner_token, params={"limit": 1}
        )
        assert issued.status_code == 200, issued.text
        foreign_cursor = issued.headers["x-next-cursor"]
        # Replayed by A on A's own list: refused, and nothing of B's is returned.
        r = pov.send(
            "GET", _ws_path(world, a, f"/{path}"), params={"limit": 1, "cursor": foreign_cursor}
        )
        assert _error(r) == (422, "validation_error", "Invalid cursor."), r.text
        # Replayed on B's list by A: the tenancy dependency refuses first.
        _assert_membership_403(
            pov.send(
                "GET",
                _ws_path(world, b, f"/{path}"),
                params={"limit": 1, "cursor": foreign_cursor},
            )
        )
        # Positive: A walks its own list one row per page; only A's rows, total = A's count.
        own, seen, totals, cursor = _own_row_ids(world, a, path), [], set(), None
        for _ in range(500):
            params = {"limit": 1, **({"cursor": cursor} if cursor else {})}
            page = pov.send("GET", _ws_path(world, a, f"/{path}"), params=params)
            assert page.status_code == 200, page.text
            seen.extend(row["id"] for row in page.json())
            totals.add(page.headers["x-total-count"])
            cursor = page.headers.get("x-next-cursor")
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) and set(seen) == own
        assert totals == {str(len(own))}
        assert not set(seen) & _own_row_ids(world, b, path)


# --------------------------------------------------------------------------- #
# 11. Completeness against the served application
# --------------------------------------------------------------------------- #
#: Every customer-facing tenant-scoped operation -> the tests in THIS module that prove it.
_CTX_TESTS = (
    "TestContextEntities::test_list_returns_own_rows_only_and_foreign_ws_403",
    "TestContextEntities::test_create_in_foreign_ws_403_nothing_written_own_201",
    "TestContextEntities::test_delete_with_foreign_item_id_404_and_row_survives",
)
_PAGED_TEST = (
    "TestPaginatedLists::test_foreign_cursor_refused_and_pages_and_total_are_own_rows_only",
)
COVERAGE: dict[tuple[str, str], tuple[str, ...]] = {
    ("GET", W): ("TestWorkspaceAndOrganization::test_get_workspace_own_200_foreign_403",),
    ("GET", f"{ORG}/workspaces"): (
        "TestWorkspaceAndOrganization::test_list_workspaces_own_only_foreign_org_403",
    ),
    ("POST", f"{ORG}/workspaces"): (
        "TestWorkspaceAndOrganization::test_create_workspace_in_foreign_org_403_and_nothing_created",
    ),
    ("GET", f"{ORG}/invitations"): (
        "TestInvitationsAndMembers::test_invitations_list_and_create_foreign_org_403",
    ),
    ("POST", f"{ORG}/invitations"): (
        "TestInvitationsAndMembers::test_invitations_list_and_create_foreign_org_403",
    ),
    ("DELETE", f"{ORG}/invitations/{{invitation_id}}"): (
        "TestInvitationsAndMembers::test_revoke_foreign_invitation_id_404_stays_pending",
    ),
    ("GET", f"{ORG}/members"): (
        "TestInvitationsAndMembers::test_members_list_own_200_foreign_403",
    ),
    ("PUT", f"{ORG}/members/{{user_id}}/role"): (
        "TestInvitationsAndMembers::test_role_change_and_removal_foreign_user_id_404_unchanged",
    ),
    ("DELETE", f"{ORG}/members/{{user_id}}"): (
        "TestInvitationsAndMembers::test_role_change_and_removal_foreign_user_id_404_unchanged",
        "TestInvitationsAndMembers::test_remove_member_positive_on_own_organization",
    ),
    ("POST", f"{W}/onboarding"): (
        "TestBrandsAndProfile::test_onboarding_foreign_403_creates_no_brand",
    ),
    ("GET", f"{W}/brands"): (
        "TestBrandsAndProfile::test_brands_and_profile_reads_own_only_foreign_403",
    ),
    ("GET", f"{W}/business-profile"): (
        "TestBrandsAndProfile::test_brands_and_profile_reads_own_only_foreign_403",
    ),
    ("PUT", f"{W}/business-profile"): (
        "TestBrandsAndProfile::test_profile_update_foreign_403_unchanged_own_200",
    ),
    **{("GET", f"{W}/{p}"): _CTX_TESTS[:1] + _PAGED_TEST for p, _m, _b in CONTEXT},
    **{("POST", f"{W}/{p}"): _CTX_TESTS[1:2] for p, _m, _b in CONTEXT},
    **{("DELETE", f"{W}/{p}/{{item_id}}"): _CTX_TESTS[2:] for p, _m, _b in CONTEXT},
    ("GET", f"{W}/locations"): ("TestLocations::test_list_and_create",),
    ("POST", f"{W}/locations"): ("TestLocations::test_list_and_create",),
    ("PUT", f"{W}/locations/{{location_id}}"): (
        "TestLocations::test_update_with_foreign_location_id_404_unchanged",
    ),
    ("GET", f"{W}/locations/{{location_id}}/geo-coverage"): (
        "TestLocations::test_geo_coverage_with_foreign_location_id_404_unchanged",
    ),
    ("PUT", f"{W}/locations/{{location_id}}/geo-coverage"): (
        "TestLocations::test_geo_coverage_with_foreign_location_id_404_unchanged",
    ),
    **{
        ("POST", f"{W}/locations/{{location_id}}/{verb}"): (
            "TestLocations::test_deactivate_and_reactivate_with_foreign_location_id_404_unchanged",
        )
        for verb in ("deactivate", "reactivate")
    },
    ("POST", "/geocode"): ("TestLocations::test_geocode_workspace_id_in_query",),
    ("GET", f"{W}/scout-requests"): (
        "TestScoutRequests::test_list_own_only_and_foreign_location_filter_is_empty",
        *_PAGED_TEST,
    ),
    ("POST", f"{W}/scout-requests"): (
        "TestScoutRequests::test_create_with_foreign_location_in_body_404_nothing_created",
    ),
    ("GET", f"{W}/scout-requests/{{request_id}}"): (
        "TestScoutRequests::test_get_and_update_with_foreign_request_id_404_unchanged",
    ),
    ("PUT", f"{W}/scout-requests/{{request_id}}"): (
        "TestScoutRequests::test_get_and_update_with_foreign_request_id_404_unchanged",
    ),
    **{
        ("POST", f"{W}/scout-requests/{{request_id}}/{v}"): (
            "TestScoutRequests::test_lifecycle_with_foreign_request_id_404_unchanged",
            "TestScoutRequests::test_lifecycle_positive_on_own_request",
        )
        for v in ("pause", "resume", "run")
    },
    ("GET", f"{W}/scout-requests/{{request_id}}/runs"): (
        "TestScoutRequests::test_runs_history_with_foreign_request_id_404",
    ),
    ("GET", f"{W}/scout-requests/{{request_id}}/schedule"): (
        "TestScoutSchedules::test_get_schedule_with_foreign_request_id_404",
    ),
    **{
        (m, f"{W}/scout-requests/{{request_id}}/schedule{s}"): (
            "TestScoutSchedules::test_mutations_with_foreign_request_id_404_and_schedule_unchanged",
            "TestScoutSchedules::test_mutations_positive_on_own_request",
        )
        for m, s in (("POST", ""), ("POST", "/pause"), ("POST", "/resume"), ("DELETE", ""))
    },
    ("GET", f"{W}/opportunities"): (
        "TestOpportunities::test_list_own_only_and_foreign_query_filters_empty",
        *_PAGED_TEST,
    ),
    ("GET", f"{W}/opportunities/{{opportunity_id}}"): (
        "TestOpportunities::test_detail_and_intelligence_with_foreign_opportunity_id_404",
    ),
    ("GET", f"{W}/opportunities/{{opportunity_id}}/intelligence"): (
        "TestOpportunities::test_detail_and_intelligence_with_foreign_opportunity_id_404",
    ),
    ("PUT", f"{W}/opportunities/{{opportunity_id}}/status"): (
        "TestOpportunities::test_status_update_with_foreign_opportunity_id_404_unchanged",
    ),
    ("GET", f"{W}/feedback-capability"): (
        "TestOpportunities::test_feedback_capability_and_feedback_routes",
    ),
    ("GET", f"{W}/opportunities/{{opportunity_id}}/feedback"): (
        "TestOpportunities::test_feedback_capability_and_feedback_routes",
    ),
    ("POST", f"{W}/opportunities/{{opportunity_id}}/feedback"): (
        "TestOpportunities::test_feedback_capability_and_feedback_routes",
    ),
    ("GET", f"{W}/jobs"): ("TestJobs::test_list_own_only_and_foreign_query_filters_empty",),
    ("GET", f"{W}/jobs/{{job_id}}"): (
        "TestJobs::test_get_events_cancel_with_foreign_job_id_404_unchanged",
    ),
    ("GET", f"{W}/jobs/{{job_id}}/events"): (
        "TestJobs::test_get_events_cancel_with_foreign_job_id_404_unchanged",
    ),
    ("POST", f"{W}/jobs/{{job_id}}/cancel"): (
        "TestJobs::test_get_events_cancel_with_foreign_job_id_404_unchanged",
    ),
    ("GET", f"{W}/audit-logs"): (
        "TestAuditLogs::test_own_200_own_organization_rows_only_foreign_403",
        *_PAGED_TEST,
    ),
}

#: Limbs proven in another module whose fixtures own the needed rows (each market there is
#: its own organization); the referenced tests are verified to exist by name.
PROVEN_ELSEWHERE: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {
    ("POST", f"{W}/opportunities/{{opportunity_id}}/feedback"): (
        ("test_opportunity_feedback_api.py", "test_cross_workspace_record_404"),
        ("test_opportunity_feedback_api.py", "test_cross_market_record_is_rejected"),
    ),
    ("GET", f"{W}/audit-logs"): (
        ("test_audit_logs_role_matrix.py", "test_allowed_role_cannot_reach_another_orgs_workspace"),
        ("test_audit_logs_role_matrix.py", "test_foreign_org_wide_row_is_excluded_from_org_a"),
    ),
}

#: Membership is checked inside these two handlers, so the dependency walk sees only
#: ``get_current_user``; by contract they are customer-facing and tenant-scoped.
HANDLER_CHECKED = {("GET", f"{API}{W}"), ("GET", f"{API}{ORG}/workspaces")}


def _dependency_calls(dependant) -> list:
    seen: set[int] = set()
    out: list = []

    def walk(d) -> None:
        if d.call is not None and id(d.call) not in seen:
            seen.add(id(d.call))
            out.append(d.call)
        for sub in d.dependencies:
            walk(sub)

    walk(dependant)
    return out


def _is_role_checker(call) -> bool:
    return getattr(call, "__module__", "") == deps.__name__ and getattr(
        call, "__qualname__", ""
    ).endswith("<locals>._checker")


def _served_customer_operations() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for rc in iter_route_contexts(app.routes):
        dependant = getattr(rc, "dependant", None)
        if dependant is None:
            continue
        methods = sorted(set(rc.methods) - {"HEAD", "OPTIONS"})
        assert len(methods) == 1, (rc.path, methods)
        calls = _dependency_calls(dependant)
        tenant_scoped = any(
            c in (deps.get_tenant_context, deps.get_organization_context) or _is_role_checker(c)
            for c in calls
        )
        if tenant_scoped or (methods[0], rc.path) in HANDLER_CHECKED:
            assert deps.require_operator not in calls, rc.path
            out.add((methods[0], rc.path))
    return out


def _tests_defined_in(module_path: Path) -> set[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            names.add(node.name)
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name.startswith("test_"):
                    names.add(f"{node.name}::{sub.name}")
    return names


class TestCoverageCompleteness:
    def test_every_served_customer_operation_has_a_proof_in_this_module(self):
        served = _served_customer_operations()
        covered = {(m, f"{API}{p}") for m, p in COVERAGE}
        assert served - covered == set(), sorted(served - covered)
        assert covered - served == set(), sorted(covered - served)
        # 71 -> 73 (P6-UI-023): location deactivate and reactivate; the equalities above
        # prove no other operation came or went.
        assert len(served) == 73

    def test_every_named_proof_exists(self):
        here = _tests_defined_in(Path(__file__))
        missing = {t for tests in COVERAGE.values() for t in tests if t not in here}
        assert missing == set(), sorted(missing)
        for (_m, _p), refs in PROVEN_ELSEWHERE.items():
            for module, name in refs:
                defined = _tests_defined_in(Path(__file__).with_name(module))
                assert any(d == name or d.endswith(f"::{name}") for d in defined), (module, name)

    def test_the_role_checker_detector_sees_a_checker(self):
        # Positive control for the detector: a route guarded by require_exact_roles is found.
        found = [
            rc.path
            for rc in iter_route_contexts(app.routes)
            if getattr(rc, "dependant", None) is not None
            and any(_is_role_checker(c) for c in _dependency_calls(rc.dependant))
        ]
        assert f"{API}{W}/audit-logs" in found

    def test_operator_public_and_account_routes_are_excluded_by_classification(self):
        served = _served_customer_operations()
        assert not any(p.startswith(f"{API}/internal/") for _m, p in served)
        assert not any(p.startswith(f"{API}/auth/") for _m, p in served)
        assert ("GET", f"{API}/organizations") not in served
        assert ("GET", f"{API}/system/capabilities") not in served
