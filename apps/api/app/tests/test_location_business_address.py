"""F-1: a location's business address is edited and persisted through the coverage PUT.

The coverage rule's ``business_address`` is what
:func:`app.jurisdiction.resolver.resolve_jurisdiction` reads (``_address_check``): an address
that states another country, or another state or province of a country that resolves at that
level, makes the location UNRESOLVED. The web dialog used to send
``coverage.business_address ?? form.address``, so after the first save the product could not
change it. The dialog now sends the value of a Business address field. These tests prove the API
side of that repair, unchanged by it, through real HTTP:

* the PUT stores the business address it is sent (including ``null``), GET returns it, every
  other field comes back as sent, the rule keeps its id and ``GEO_COVERAGE_UPDATED`` is audited;
* the location's own rule set, selected as ``jobs/pipeline.py`` and the coverage GET select it,
  resolves UNRESOLVED for an address in another country or state and the location's code after
  the PUT corrects it;
* editing the location and its business address leaves captured brief, creative-draft and
  content-review snapshots (``jurisdiction_code`` and every other column) unchanged;
* only owner, admin and marketer may PUT (``require_role(OWNER, ADMIN, MARKETER)``); a refused
  PUT changes nothing. GET admits every member of the workspace's organization
  (``get_tenant_context``) and refuses a non-member;
* a location of another workspace of the same organization, or of another organization, named
  under the caller's own workspace answers 404 on PUT and GET and changes nothing; naming the
  other organization's workspace answers the membership 403.

World (SQLite with foreign keys on, or PostgreSQL when ``TEST_POSTGRES_URL`` is set; one database
per test): org-a with ws-a1 and ws-a2 and one member of each of the six roles; org-b with ws-b1
and its owner; an outsider with no membership. One location with a stored rule per workspace.
Only ``get_db`` is overridden: authentication, tenancy and the role checks run for real.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.audit.actions import AuditAction
from app.audit.models import AuditLog
from app.briefs.models import RecommendationBrief
from app.core.config import get_settings
from app.core.enums import Role
from app.creative.models import ContentReview, CreativeDraft
from app.db.models import Base
from app.db.session import get_db
from app.jurisdiction.codes import UNRESOLVED
from app.jurisdiction.resolver import resolve_jurisdiction
from app.locations.models import BusinessLocation, GeoCoverageRule
from app.main import app
from app.tests._auth2_support import live_bearer, pg_database, require_postgres

API = get_settings().api_prefix
ROLES = [
    Role.OWNER,
    Role.ADMIN,
    Role.MARKETER,
    Role.REVIEWER,
    Role.COMPLIANCE_REVIEWER,
    Role.VIEWER,
]
EDITORS = {Role.OWNER, Role.ADMIN, Role.MARKETER}
CONFLICTING = "Toronto, Ontario, Canada"
CORRECTED = "1 Main Street, Dallas, TX"
NOT_FOUND_LOCATION = (404, "not_found", "Location not found in this workspace.")
NOT_FOUND_RULE = (404, "not_found", "No Scout Reach configured for this location yet.")
NOT_A_MEMBER = (403, "permission_denied", "You are not a member of this organization.")

#: The stored rule of every location before a test acts: the business address names another
#: country, and every other field is set, so a dropped or reset field shows.
STORED = {
    "coverage_type": "radius",
    "business_address": CONFLICTING,
    "center_latitude": 32.7767,
    "center_longitude": -96.797,
    "radius_miles": 40,
    "country": "United States",
    "state": "Texas",
    "included_markets": ["Dallas", "Fort Worth"],
    "excluded_markets": ["Houston"],
    "online_global": True,
}
SPACES = {"ws-a1": "org-a", "ws-a2": "org-a", "ws-b1": "org-b"}
#: What W21 ADDR adds to every answer: the structured business address (these tests never set
#: it, and a PUT that does not name it leaves it as it was) and what is computed about its town.
#: ``test_location_structured_address`` covers them.
ADDR_UNSET = {
    "business_address_street": None,
    "business_address_locality": None,
    "business_address_country": None,
    "business_address_region": None,
    "business_address_postcode": None,
    "locality_matches_city": None,
    "locality_confirmed_at": None,
    "locality_confirmation_applies": False,
}


def _seed(s) -> dict[str, dict[str, str]]:
    from app.brands.models import Brand
    from app.organizations.models import Organization, OrganizationMember, User, Workspace

    for org in ("org-a", "org-b"):
        s.add(Organization(id=org, name=org, slug=org))
    s.flush()
    for ws, org in SPACES.items():
        key = ws[3:]
        s.add(Workspace(id=ws, organization_id=org, name=ws, slug=ws))
        s.flush()
        s.add(Brand(id=f"br-{key}", organization_id=org, workspace_id=ws, name=ws))
        s.flush()
        # Dallas, TX: resolves to US-TX from its own fields.
        s.add(
            BusinessLocation(
                id=f"loc-{key}",
                organization_id=org,
                workspace_id=ws,
                brand_id=f"br-{key}",
                name=f"Dallas {key}",
                address="1 Main Street",
                city="Dallas",
                state_province="TX",
                country="US",
                latitude=32.7767,
                longitude=-96.797,
                timezone="America/Chicago",
            )
        )
        s.flush()
        s.add(
            GeoCoverageRule(
                id=f"geo-{key}",
                organization_id=org,
                workspace_id=ws,
                location_id=f"loc-{key}",
                **STORED,
            )
        )
        s.flush()

    def user(uid: str, org: str | None, role: str | None) -> None:
        s.add(
            User(
                id=uid,
                email=f"{uid}@example.com",
                full_name=f"Name {uid}",
                hashed_password="x",
                is_active=True,
            )
        )
        s.flush()
        if org:
            s.add(OrganizationMember(id=f"m-{uid}", organization_id=org, user_id=uid, role=role))
            s.flush()

    for role in ROLES:
        user(f"a-{role.value}", "org-a", role.value)
    user("b-owner", "org-b", "owner")
    user("outsider", None, None)
    uids = [f"a-{r.value}" for r in ROLES] + ["b-owner", "outsider"]
    return {uid: {"Authorization": f"Bearer {live_bearer(uid, s)}"} for uid in uids}


@dataclass
class W:
    client: TestClient
    make: sessionmaker
    auth: dict[str, dict[str, str]]

    def coverage(
        self, method: str, ws: str, loc: str, body: dict | None = None, who: str = "a-owner"
    ):
        _reset_rate_limiter()
        url = f"{API}/workspaces/{ws}/locations/{loc}/geo-coverage"
        return self.client.request(method, url, json=body, headers=self.auth[who])

    def row(self, model, row_id: str) -> dict | None:
        table = model.__table__
        with self.make() as s:
            r = s.execute(select(*table.c).where(table.c.id == row_id)).first()
            return dict(r._mapping) if r else None

    def rules_of(self, loc: str) -> list[dict]:
        table = GeoCoverageRule.__table__
        with self.make() as s:
            rows = s.execute(select(*table.c).where(table.c.location_id == loc)).all()
            return [dict(r._mapping) for r in rows]

    def audits(self) -> list[tuple]:
        with self.make() as s:
            return [
                tuple(r)
                for r in s.execute(
                    select(
                        AuditLog.organization_id,
                        AuditLog.workspace_id,
                        AuditLog.actor_user_id,
                        AuditLog.entity_type,
                        AuditLog.entity_id,
                    )
                    .where(AuditLog.action == AuditAction.GEO_COVERAGE_UPDATED.value)
                    .order_by(AuditLog.created_at)
                ).all()
            ]

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


@pytest.fixture(params=["sqlite", "postgresql"])
def w(request, tmp_path) -> Iterator[W]:
    # On PostgreSQL, the production engine, when TEST_POSTGRES_URL is set (require_postgres:
    # unset FAILS in CI and skips only on a developer machine). Each test gets its own database.
    if request.param == "sqlite":
        engine = create_engine(
            f"sqlite:///{tmp_path / 'f1loc.db'}",
            connect_args={"check_same_thread": False},
            future=True,
        )

        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        yield from _world(engine)
        return
    require_postgres()
    with pg_database("sn_f1loc") as url:  # pragma: no cover - gated on PostgreSQL
        yield from _world(create_engine(url, future=True))


def _world(engine) -> Iterator[W]:
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

    app.dependency_overrides[get_db] = _get_db
    _reset_rate_limiter()
    try:
        yield W(TestClient(app), make, tokens)
    finally:
        app.dependency_overrides.clear()
        _reset_rate_limiter()
        engine.dispose()


def _error(r) -> tuple[int, str | None, str | None]:
    err = (r.json() or {}).get("error") or {}
    return r.status_code, err.get("code"), err.get("message")


def _without(row: dict | None, *keys: str) -> dict:
    assert row is not None
    return {k: v for k, v in row.items() if k not in keys}


def _resolve(w: W, ws: str, loc: str):
    """The location's resolution from its own rule set, selected as the coverage GET selects it
    (workspace and location), checked equal to ``jobs/pipeline.py``'s selection (location)."""
    with w.make() as s:
        location = s.get(BusinessLocation, loc)
        rules = s.scalars(
            select(GeoCoverageRule).where(
                GeoCoverageRule.workspace_id == ws, GeoCoverageRule.location_id == loc
            )
        ).all()
        pipeline_rule = s.scalar(select(GeoCoverageRule).where(GeoCoverageRule.location_id == loc))
        assert [r.id for r in rules] == [pipeline_rule.id]
        return resolve_jurisdiction(location, rules)


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #
def test_put_stores_the_corrected_business_address_and_keeps_every_other_field(w: W) -> None:
    before = w.row(GeoCoverageRule, "geo-a1")
    audits_before = w.audits()

    r = w.coverage("PUT", "ws-a1", "loc-a1", {**STORED, "business_address": CORRECTED})
    assert r.status_code == 200, r.text
    assert r.json() == {
        **STORED,
        "business_address": CORRECTED,
        "id": "geo-a1",
        "location_id": "loc-a1",
        **ADDR_UNSET,
    }

    got = w.coverage("GET", "ws-a1", "loc-a1")
    assert got.status_code == 200
    assert got.json() == {
        **STORED,
        "business_address": CORRECTED,
        "id": "geo-a1",
        "location_id": "loc-a1",
        **ADDR_UNSET,
    }

    after = w.row(GeoCoverageRule, "geo-a1")
    assert after["business_address"] == CORRECTED
    assert _without(after, "business_address", "updated_at") == _without(
        before, "business_address", "updated_at"
    )
    assert w.rules_of("loc-a1") == [after]  # updated in place: the same row, no second rule
    assert w.audits() == [
        *audits_before,
        ("org-a", "ws-a1", "a-owner", "geo_coverage_rule", "geo-a1"),
    ]


def test_put_stores_null_for_an_emptied_business_address(w: W) -> None:
    r = w.coverage("PUT", "ws-a1", "loc-a1", {**STORED, "business_address": None})
    assert r.status_code == 200, r.text
    got = w.coverage("GET", "ws-a1", "loc-a1").json()
    assert got == {
        **STORED, "business_address": None, "id": "geo-a1", "location_id": "loc-a1", **ADDR_UNSET
    }
    assert w.row(GeoCoverageRule, "geo-a1")["business_address"] is None


# --------------------------------------------------------------------------- #
# Resolution reads the stored value
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "conflicting",
    [CONFLICTING, "Sacramento, California, US"],
    ids=["another-country", "another-state"],
)
def test_resolution_uses_the_corrected_business_address(w: W, conflicting: str) -> None:
    put = w.coverage("PUT", "ws-a1", "loc-a1", {**STORED, "business_address": conflicting})
    assert put.status_code == 200, put.text
    refused = _resolve(w, "ws-a1", "loc-a1")
    assert refused.code == UNRESOLVED
    assert repr(conflicting) in refused.evidence[-1]

    put = w.coverage("PUT", "ws-a1", "loc-a1", {**STORED, "business_address": CORRECTED})
    assert put.status_code == 200, put.text
    resolved = _resolve(w, "ws-a1", "loc-a1")
    assert (resolved.code, resolved.codes) == ("US-TX", ("US-TX", "US"))
    assert f"coverage rule 0 business address {CORRECTED!r} agrees" in resolved.evidence


# --------------------------------------------------------------------------- #
# Captured snapshots are left alone
# --------------------------------------------------------------------------- #
def _snapshots(w: W) -> None:
    """A brief, a creative draft of it and the draft's content review for loc-a1, each with a
    captured jurisdiction_code, written directly (nothing on main writes them)."""
    from app.intelligence.records import SignalIntelligenceRecord
    from app.opportunities.models import Opportunity
    from app.scouting_requests.models import ScoutRequest
    from app.signals.models import NormalizedSignal, RawSignal

    scope = {"organization_id": "org-a", "workspace_id": "ws-a1"}
    with w.make() as s:
        s.add(
            ScoutRequest(id="sr-a1", brand_id="br-a1", location_id="loc-a1", name="scout", **scope)
        )
        s.flush()
        req = {**scope, "scout_request_id": "sr-a1"}
        s.add(RawSignal(id="raw-a1", source_type="rss_news", content="c", **req))
        s.flush()
        s.add(
            NormalizedSignal(
                id="ns-a1",
                raw_signal_id="raw-a1",
                source_type="rss_news",
                excerpt="e",
                content_hash="0" * 64,
                **req,
            )
        )
        s.flush()
        s.add(
            Opportunity(
                id="opp-a1",
                brand_id="br-a1",
                location_id="loc-a1",
                title="t",
                classification="emerging",
                decision="monitor",
                opportunity_score=50,
                resolved_market="Dallas, TX",
                audience_fit="commuters",
                **req,
            )
        )
        s.flush()
        s.add(
            SignalIntelligenceRecord(
                id="sir-a1",
                normalized_signal_id="ns-a1",
                opportunity_id="opp-a1",
                analysis_version="3c",
                scoring_version="3c.1",
                fingerprint="f" * 64,
                enricher="deterministic",
                accepted=True,
                classification="emerging",
                **req,
            )
        )
        s.flush()
        s.add(
            RecommendationBrief(
                id="brief-a1",
                opportunity_id="opp-a1",
                intelligence_record_id="sir-a1",
                location_id="loc-a1",
                brief_version=1,
                analysis_version="3c",
                scoring_version="3c.1",
                prompt_name="explain_opportunity",
                prompt_version="1.2.0",
                model_identifier="mock-1",
                input_hash="0" * 64,
                is_simulated=True,
                created_by="a-marketer",
                created_by_snapshot="a-marketer Name <a-marketer@example.com>",
                summary="s",
                rationale="r",
                recommended_actions=["a"],
                suggested_angles=["b"],
                risk_notes=["c"],
                evidence_refs=[],
                jurisdiction_code="US-TX",
                **scope,
            )
        )
        s.flush()
        s.add(
            CreativeDraft(
                id="draft-a1",
                brief_id="brief-a1",
                opportunity_id="opp-a1",
                location_id="loc-a1",
                draft_version=1,
                channel="email",
                headline="h",
                body="Draft",
                call_to_action="Visit",
                prompt_name="generate_creative",
                prompt_version="1.0.0",
                model_identifier="mock-1",
                input_hash="0" * 64,
                is_simulated=True,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_usd=0,
                created_by="a-owner",
                created_by_snapshot="a-owner Name <a-owner@example.com>",
                jurisdiction_code="US-TX",
                **scope,
            )
        )
        s.flush()
        s.add(
            ContentReview(
                id="review-a1",
                draft_id="draft-a1",
                draft_version=1,
                risk_level="low",
                is_blocked=False,
                findings=[],
                warnings=[],
                blocked_claim_ids=[],
                required_disclaimers=[],
                disclaimers_resolved=True,
                passed=True,
                jurisdiction_code="US",
                **scope,
            )
        )
        s.commit()


def test_editing_the_location_and_its_business_address_keeps_captured_snapshots(w: W) -> None:
    _snapshots(w)
    rows = {
        model: w.row(model, rid)
        for model, rid in (
            (RecommendationBrief, "brief-a1"),
            (CreativeDraft, "draft-a1"),
            (ContentReview, "review-a1"),
        )
    }
    assert [r["jurisdiction_code"] for r in rows.values()] == ["US-TX", "US-TX", "US"]

    # The location moves to Tulsa, OK, and its business address follows.
    moved = {
        "name": "Tulsa",
        "address": "2 Elm Street",
        "city": "Tulsa",
        "state_province": "OK",
        "country": "US",
        "latitude": 36.154,
        "longitude": -95.9928,
        "timezone": "America/Chicago",
    }
    _reset_rate_limiter()
    put = w.client.put(
        f"{API}/workspaces/ws-a1/locations/loc-a1", json=moved, headers=w.auth["a-owner"]
    )
    assert put.status_code == 200, put.text
    put = w.coverage(
        "PUT", "ws-a1", "loc-a1", {**STORED, "business_address": "2 Elm Street, Tulsa, OK"}
    )
    assert put.status_code == 200, put.text
    assert _resolve(w, "ws-a1", "loc-a1").code == "US-OK"  # what a new capture would read

    for model, before in rows.items():
        assert w.row(model, before["id"]) == before, model.__tablename__


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ROLES, ids=[r.value for r in ROLES])
def test_put_admits_owner_admin_and_marketer_only(w: W, role: Role) -> None:
    who = f"a-{role.value}"
    before = w.row(GeoCoverageRule, "geo-a1")
    audits_before = w.audits()
    r = w.coverage("PUT", "ws-a1", "loc-a1", {**STORED, "business_address": CORRECTED}, who=who)
    if role in EDITORS:
        assert r.status_code == 200, r.text
        assert w.row(GeoCoverageRule, "geo-a1")["business_address"] == CORRECTED
        assert w.audits() == [
            *audits_before,
            ("org-a", "ws-a1", who, "geo_coverage_rule", "geo-a1"),
        ]
    else:
        assert _error(r) == (
            403,
            "permission_denied",
            f"Role '{role.value}' is not permitted for this action.",
        )
        assert w.row(GeoCoverageRule, "geo-a1") == before
        assert w.audits() == audits_before


@pytest.mark.parametrize("role", ROLES, ids=[r.value for r in ROLES])
def test_get_admits_every_member_of_the_organization(w: W, role: Role) -> None:
    r = w.coverage("GET", "ws-a1", "loc-a1", who=f"a-{role.value}")
    assert r.status_code == 200, r.text
    assert r.json()["business_address"] == CONFLICTING


def test_get_and_put_refuse_a_non_member(w: W) -> None:
    before = w.row(GeoCoverageRule, "geo-a1")
    assert _error(w.coverage("GET", "ws-a1", "loc-a1", who="outsider")) == NOT_A_MEMBER
    put = w.coverage(
        "PUT", "ws-a1", "loc-a1", {**STORED, "business_address": CORRECTED}, who="outsider"
    )
    assert _error(put) == NOT_A_MEMBER
    assert w.row(GeoCoverageRule, "geo-a1") == before


# --------------------------------------------------------------------------- #
# Tenant isolation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("foreign", ["a2", "b1"], ids=["other-workspace", "other-organization"])
def test_a_foreign_location_under_the_callers_workspace_answers_404_and_changes_nothing(
    w: W, foreign: str
) -> None:
    loc, rule = f"loc-{foreign}", f"geo-{foreign}"
    before = w.rules_of(loc)
    rules_before, audits_before = w.count(GeoCoverageRule), w.audits()

    put = w.coverage("PUT", "ws-a1", loc, {**STORED, "business_address": CORRECTED})
    assert _error(put) == NOT_FOUND_LOCATION
    assert _error(w.coverage("GET", "ws-a1", loc)) == NOT_FOUND_RULE

    assert w.rules_of(loc) == before
    assert [r["id"] for r in before] == [rule]
    assert w.count(GeoCoverageRule) == rules_before
    assert w.audits() == audits_before


def test_the_other_organizations_workspace_answers_403_and_its_rule_is_unchanged(w: W) -> None:
    before = w.row(GeoCoverageRule, "geo-b1")
    put = w.coverage("PUT", "ws-b1", "loc-b1", {**STORED, "business_address": CORRECTED})
    assert _error(put) == NOT_A_MEMBER
    assert _error(w.coverage("GET", "ws-b1", "loc-b1")) == NOT_A_MEMBER
    assert w.row(GeoCoverageRule, "geo-b1") == before
    # Its own owner still reads it as stored.
    own = w.coverage("GET", "ws-b1", "loc-b1", who="b-owner")
    assert own.status_code == 200 and own.json()["business_address"] == CONFLICTING


def test_positive_control_the_same_owner_edits_the_other_workspace_through_its_own_path(
    w: W,
) -> None:
    put = w.coverage("PUT", "ws-a2", "loc-a2", {**STORED, "business_address": CORRECTED})
    assert put.status_code == 200, put.text
    assert w.row(GeoCoverageRule, "geo-a2")["business_address"] == CORRECTED
    assert w.row(GeoCoverageRule, "geo-a1")["business_address"] == CONFLICTING
