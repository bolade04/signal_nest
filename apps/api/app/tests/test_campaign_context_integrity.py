"""P6-PLAT-8 and P6-PLAT-9: campaign-context location integrity and partial update.

P6-PLAT-8. A campaign's ``location_ids`` and an offer's ``eligible_location_ids`` are JSON
lists with no foreign key. Every write of either list -- create (POST) and partial update
(PATCH) -- must admit only locations of the request's own workspace that exist and are
active. The cases that matter are the ones a member legitimately reaches: a location of
ANOTHER WORKSPACE OF THE SAME ORGANIZATION (the caller passes the tenancy check for both
workspaces, so only the handler can refuse it), a location of another organization, and an
opaque id that names nothing. The last two must answer identically, so the response is no
oracle for whether an id exists elsewhere. Rows written before the check are not rewritten:
they are reported, read-only, by ``app.campaign_context.location_refs``.

P6-PLAT-9. Each of the nine context collections gains ``PATCH /{item_id}``: the same role
gate as POST (OWNER/ADMIN/MARKETER), the same tenant scoping, the same field validation, a
stable id, and only the fields sent are changed. For every collection: success, a
validation failure that leaves the row unchanged, cross-workspace denial (same organization
and foreign organization) and role denial.

The world is built only through real HTTP flows (registration, invitations, workspaces,
onboarding, locations, context rows) on a temporary SQLite file with foreign keys on;
``get_db`` is the only overridden dependency. Two kinds of row are written directly through
a witness engine because no route can produce them: a deactivated location (no deactivate
route exists in this tranche) and a pre-check "legacy" row that already violates the rule.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.core.config as config_module
from app.campaign_context import location_refs
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
from app.campaign_context.schemas import (
    MAX_LOCATION_IDS,
    AudienceIn,
    AudiencePatch,
    BrandVoiceIn,
    BrandVoicePatch,
    CampaignIn,
    CampaignPatch,
    ChannelPrefIn,
    ChannelPrefPatch,
    ClaimIn,
    ClaimPatch,
    CompetitorIn,
    CompetitorPatch,
    OfferIn,
    OfferPatch,
    ProductIn,
    ProductPatch,
    SourcePrefIn,
    SourcePrefPatch,
)
from app.core.config import get_settings
from app.core.middleware import RateLimitMiddleware
from app.db.models import Base
from app.db.session import get_db
from app.locations.models import BusinessLocation
from app.main import app
from app.organizations.models import Workspace

API = get_settings().api_prefix
PASSWORD = "plat8-integrity-pw-123"

LOCATION_404 = (404, "not_found", "Location not found in this workspace.")
DEACTIVATED_422 = (422, "validation_error", "A deactivated location cannot be added.")
ITEM_404 = (404, "not_found", "Item not found in this workspace.")
NOT_A_MEMBER = (403, "permission_denied", "You are not a member of this organization.")
BODY_422 = (422, "validation_error", "Request validation failed")

#: The two collections that carry a location-id list: (path, model, list field, base body).
LOCATION_LISTS = (
    ("campaigns", Campaign, "location_ids", {"name": "Spring push"}),
    ("offers", OfferCalendarEntry, "eligible_location_ids", {"name": "Spring offer"}),
)

#: Every collection: (path, model, create body, valid patch, invalid patch).
COLLECTIONS = (
    (
        "products",
        ProductProfile,
        {"name": "Widget", "keywords": ["widget"]},
        {"name": "Widget Pro"},
        {"name": ""},
    ),
    (
        "audiences",
        AudienceProfile,
        {"label": "Owners"},
        {"label": "Renters"},
        {"label": None},
    ),
    (
        "competitors",
        CompetitorProfile,
        {"name": "Rival", "notes": "keep"},
        {"website": "https://rival.example"},
        {"name": ""},
    ),
    (
        "brand-voice",
        BrandVoiceProfile,
        {"tone": ["warm"]},
        {"tone": ["bold", "direct"]},
        {"tone": "bold"},
    ),
    (
        "offers",
        OfferCalendarEntry,
        {"name": "Spring", "percentage_discount": 10.0},
        {"percentage_discount": 15.0},
        {"percentage_discount": "lots"},
    ),
    (
        "claims",
        ClaimsLibraryEntry,
        {"text": "Fastest in town"},
        {"text": "Among the fastest in town"},
        {"text": ""},
    ),
    (
        "source-preferences",
        SourcePreference,
        {"source_type": "news"},
        {"enabled": False},
        {"source_type": None},
    ),
    (
        "channel-preferences",
        ChannelPreference,
        {"channel": "email", "weekly_volume": 2},
        {"weekly_volume": 3},
        {"weekly_volume": "many"},
    ),
    (
        "campaigns",
        Campaign,
        {"name": "Launch", "goal": "Awareness"},
        {"goal": "Grow repeat visits"},
        {"name": None},
    ),
)
COLLECTION_IDS = [c[0] for c in COLLECTIONS]

PATCH_PAIRS = (
    (ProductIn, ProductPatch),
    (AudienceIn, AudiencePatch),
    (CompetitorIn, CompetitorPatch),
    (BrandVoiceIn, BrandVoicePatch),
    (OfferIn, OfferPatch),
    (ClaimIn, ClaimPatch),
    (SourcePrefIn, SourcePrefPatch),
    (ChannelPrefIn, ChannelPrefPatch),
    (CampaignIn, CampaignPatch),
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


def _error(r) -> tuple[int, str | None, str | None]:
    payload = r.json() if r.content else {}
    err = payload.get("error") if isinstance(payload, dict) else None
    return r.status_code, (err or {}).get("code"), (err or {}).get("message")


@dataclass
class World:
    client: TestClient
    witness: Engine
    db_file: Path
    org_a: str = ""
    org_b: str = ""
    ws_a1: str = ""  # the acting workspace
    ws_a2: str = ""  # a second workspace of the SAME organization
    ws_b: str = ""  # a workspace of another organization
    owner_a: str = ""
    owner_b: str = ""
    tokens: dict[str, str] = field(default_factory=dict)  # role -> token, organization A
    loc_a1: str = ""
    loc_a1_off: str = ""  # deactivated, workspace A1
    loc_a2: str = ""
    loc_b: str = ""

    def send(self, method: str, path: str, token: str, body=None):
        assert set(app.dependency_overrides) == {get_db}, "only get_db may be overridden"
        _rate_limiter()._hits.clear()
        kwargs: dict = {"headers": {"Authorization": f"Bearer {token}"}}
        if body is not None:
            kwargs["json"] = body
        return self.client.request(method, API + path, **kwargs)

    def a(self, method: str, path: str, body=None):
        return self.send(method, path, self.owner_a, body)

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


def _post(client: TestClient, url: str, body: dict, token: str | None = None) -> dict:
    _rate_limiter()._hits.clear()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = client.post(API + url, json=body, headers=headers)
    assert r.status_code == 201, (url, r.status_code, r.text)
    return r.json()


def _register(client: TestClient, key: str) -> tuple[str, str]:
    owner = _post(
        client,
        "/auth/register",
        {
            "email": f"plat8-{key}-owner@example.com",
            "full_name": f"{key} owner",
            "password": PASSWORD,
            "organization_name": f"PLAT8 organization {key}",
        },
    )
    return owner["memberships"][0]["organization_id"], owner["access_token"]


def _workspace(client: TestClient, org_id: str, token: str, name: str) -> str:
    ws = _post(client, f"/organizations/{org_id}/workspaces", {"name": name}, token)
    _post(
        client,
        f"/workspaces/{ws['id']}/onboarding",
        {"brand_name": f"Brand {name}", "profile": {"company_name": f"Company {name}"}},
        token,
    )
    return ws["id"]


def _location(client: TestClient, ws_id: str, token: str, name: str) -> str:
    return _post(client, f"/workspaces/{ws_id}/locations", {"name": name}, token)["id"]


def _invite(client: TestClient, org_id: str, token: str, role: str) -> str:
    inv = _post(
        client,
        f"/organizations/{org_id}/invitations",
        {"email": f"plat8-{role}@example.com", "role": role},
        token,
    )
    joined = _post(
        client,
        "/auth/invitations/register",
        {"token": inv["token"], "full_name": f"{role} member", "password": PASSWORD},
    )
    return joined["access_token"]


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Iterator[World]:
    db_file = tmp_path_factory.mktemp("plat8") / "plat8.db"
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
        w = World(client, witness, db_file)
        w.org_a, w.owner_a = _register(client, "a")
        w.org_b, w.owner_b = _register(client, "b")
        w.ws_a1 = _workspace(client, w.org_a, w.owner_a, "A1")
        w.ws_a2 = _workspace(client, w.org_a, w.owner_a, "A2")
        w.ws_b = _workspace(client, w.org_b, w.owner_b, "B")
        w.loc_a1 = _location(client, w.ws_a1, w.owner_a, "A1 HQ")
        w.loc_a1_off = _location(client, w.ws_a1, w.owner_a, "A1 closed branch")
        w.loc_a2 = _location(client, w.ws_a2, w.owner_a, "A2 HQ")
        w.loc_b = _location(client, w.ws_b, w.owner_b, "B HQ")
        # No route deactivates a location in this tranche; the flag is set directly.
        with witness.begin() as conn:
            conn.execute(
                update(BusinessLocation)
                .where(BusinessLocation.id == w.loc_a1_off)
                .values(is_active=False)
            )
        w.tokens = {
            role: _invite(client, w.org_a, w.owner_a, role)
            for role in ("admin", "marketer", "reviewer", "compliance_reviewer", "viewer")
        }
        yield w
    finally:
        app.dependency_overrides.clear()
        _rate_limiter()._hits.clear()
        request_engine.dispose()
        witness.dispose()


def _create(world: World, ws_id: str, path: str, body: dict, token: str | None = None) -> str:
    r = world.send("POST", f"/workspaces/{ws_id}/{path}", token or world.owner_a, body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --------------------------------------------------------------------------- #
# 0. The world is what the tests assume
# --------------------------------------------------------------------------- #
class TestWorld:
    def test_workspaces_and_locations(self, world: World):
        assert world.row(Workspace, world.ws_a1)["organization_id"] == world.org_a
        assert world.row(Workspace, world.ws_a2)["organization_id"] == world.org_a
        assert world.row(Workspace, world.ws_b)["organization_id"] == world.org_b
        expected = {
            world.loc_a1: (world.ws_a1, True),
            world.loc_a1_off: (world.ws_a1, False),
            world.loc_a2: (world.ws_a2, True),
            world.loc_b: (world.ws_b, True),
        }
        for loc_id, (ws_id, active) in expected.items():
            row = world.row(BusinessLocation, loc_id)
            assert (row["workspace_id"], row["is_active"]) == (ws_id, active)

    def test_the_organization_a_owner_is_a_member_of_both_its_workspaces(self, world: World):
        # The cross-workspace probes below are only meaningful if the tenancy dependency
        # admits the caller to BOTH workspaces -- so only the handler can refuse.
        for ws in (world.ws_a1, world.ws_a2):
            assert world.a("GET", f"/workspaces/{ws}/locations").status_code == 200


# --------------------------------------------------------------------------- #
# 1. P6-PLAT-8: location-id lists on create and update
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path,model,list_field,base", LOCATION_LISTS, ids=[c[0] for c in LOCATION_LISTS]
)
class TestLocationListOnCreate:
    def test_own_active_locations_are_stored_in_order(self, world, path, model, list_field, base):
        second = _location(world.client, world.ws_a1, world.owner_a, f"A1 {path} second")
        body = {**base, list_field: [second, world.loc_a1]}
        r = world.a("POST", f"/workspaces/{world.ws_a1}/{path}", body)
        assert r.status_code == 201, r.text
        assert r.json()[list_field] == [second, world.loc_a1]
        assert world.row(model, r.json()["id"])[list_field] == [second, world.loc_a1]

    def test_empty_list_is_accepted(self, world, path, model, list_field, base):
        r = world.a("POST", f"/workspaces/{world.ws_a1}/{path}", {**base, list_field: []})
        assert r.status_code == 201 and r.json()[list_field] == []

    @pytest.mark.parametrize("which", ["same_org_other_workspace", "other_org", "opaque"])
    def test_foreign_or_unknown_location_404_nothing_written(
        self, world, path, model, list_field, base, which
    ):
        foreign = {
            "same_org_other_workspace": world.loc_a2,
            "other_org": world.loc_b,
            "opaque": "not-a-location-id",
        }[which]
        before = world.count(model, workspace_id=world.ws_a1)
        for ids in ([foreign], [world.loc_a1, foreign]):  # alone, and mixed with a valid one
            r = world.a("POST", f"/workspaces/{world.ws_a1}/{path}", {**base, list_field: ids})
            assert _error(r) == LOCATION_404, r.text
        assert world.count(model, workspace_id=world.ws_a1) == before

    def test_a_foreign_id_and_an_unknown_id_answer_identically(
        self, world, path, model, list_field, base
    ):
        url = f"/workspaces/{world.ws_a1}/{path}"
        foreign = world.a("POST", url, {**base, list_field: [world.loc_b]})
        unknown = world.a("POST", url, {**base, list_field: ["0" * 32]})
        assert _error(foreign) == _error(unknown) == LOCATION_404

    def test_deactivated_location_422_nothing_written(self, world, path, model, list_field, base):
        before = world.count(model, workspace_id=world.ws_a1)
        r = world.a(
            "POST", f"/workspaces/{world.ws_a1}/{path}", {**base, list_field: [world.loc_a1_off]}
        )
        assert _error(r) == DEACTIVATED_422, r.text
        assert world.count(model, workspace_id=world.ws_a1) == before

    def test_duplicate_or_oversized_list_422_nothing_written(
        self, world, path, model, list_field, base
    ):
        before = world.count(model, workspace_id=world.ws_a1)
        url = f"/workspaces/{world.ws_a1}/{path}"
        dup = world.a("POST", url, {**base, list_field: [world.loc_a1, world.loc_a1]})
        assert _error(dup) == BODY_422, dup.text
        big = world.a("POST", url, {**base, list_field: [f"{i:032x}" for i in range(501)]})
        assert _error(big) == BODY_422, big.text
        assert MAX_LOCATION_IDS == 500
        assert world.count(model, workspace_id=world.ws_a1) == before

    def test_the_second_workspace_may_use_its_own_location(
        self, world, path, model, list_field, base
    ):
        # Positive control for the cross-workspace probe: A2's location is valid in A2.
        r = world.a(
            "POST", f"/workspaces/{world.ws_a2}/{path}", {**base, list_field: [world.loc_a2]}
        )
        assert r.status_code == 201 and r.json()[list_field] == [world.loc_a2]


@pytest.mark.parametrize(
    "path,model,list_field,base", LOCATION_LISTS, ids=[c[0] for c in LOCATION_LISTS]
)
class TestLocationListOnUpdate:
    def test_replace_with_own_locations(self, world, path, model, list_field, base):
        item = _create(world, world.ws_a1, path, {**base, list_field: [world.loc_a1]})
        extra = _location(world.client, world.ws_a1, world.owner_a, f"A1 {path} extra")
        r = world.a("PATCH", f"/workspaces/{world.ws_a1}/{path}/{item}", {list_field: [extra]})
        assert r.status_code == 200, r.text
        assert world.row(model, item)[list_field] == [extra]

    @pytest.mark.parametrize("which", ["same_org_other_workspace", "other_org", "opaque"])
    def test_foreign_or_unknown_location_404_row_unchanged(
        self, world, path, model, list_field, base, which
    ):
        foreign = {
            "same_org_other_workspace": world.loc_a2,
            "other_org": world.loc_b,
            "opaque": "not-a-location-id",
        }[which]
        item = _create(world, world.ws_a1, path, {**base, list_field: [world.loc_a1]})
        before = world.row(model, item)
        r = world.a(
            "PATCH",
            f"/workspaces/{world.ws_a1}/{path}/{item}",
            {"name": "Renamed", list_field: [world.loc_a1, foreign]},
        )
        assert _error(r) == LOCATION_404, r.text
        assert world.row(model, item) == before  # the rename was not applied either

    def test_a_deactivated_location_may_be_kept_but_not_added(
        self, world, path, model, list_field, base
    ):
        # Attached while active, then deactivated: keeping it is history, not a new reference.
        loc = _location(world.client, world.ws_a1, world.owner_a, f"A1 {path} closing")
        item = _create(world, world.ws_a1, path, {**base, list_field: [loc]})
        with world.witness.begin() as conn:
            conn.execute(
                update(BusinessLocation).where(BusinessLocation.id == loc).values(is_active=False)
            )
        url = f"/workspaces/{world.ws_a1}/{path}/{item}"
        kept = world.a("PATCH", url, {list_field: [loc, world.loc_a1]})
        assert kept.status_code == 200, kept.text
        before = world.row(model, item)
        added = world.a("PATCH", url, {list_field: [loc, world.loc_a1, world.loc_a1_off]})
        assert _error(added) == DEACTIVATED_422, added.text
        assert world.row(model, item) == before

    def test_an_update_that_omits_the_list_leaves_it_alone(
        self, world, path, model, list_field, base
    ):
        item = _create(world, world.ws_a1, path, {**base, list_field: [world.loc_a1]})
        r = world.a("PATCH", f"/workspaces/{world.ws_a1}/{path}/{item}", {"name": "Renamed"})
        assert r.status_code == 200 and r.json()[list_field] == [world.loc_a1]


# --------------------------------------------------------------------------- #
# 2. Rows written before the check: reported, never rewritten
# --------------------------------------------------------------------------- #
def _write_direct(world: World, *rows) -> list[str]:
    with sessionmaker(bind=world.witness, expire_on_commit=False, future=True)() as s:
        s.add_all(rows)
        s.commit()
        return [r.id for r in rows]


def _a1_scope(world: World) -> dict[str, str]:
    brand_a1 = world.a("GET", f"/workspaces/{world.ws_a1}/brands").json()[0]["id"]
    return {"organization_id": world.org_a, "workspace_id": world.ws_a1, "brand_id": brand_a1}


def _legacy_rows(world: World) -> dict[str, str]:
    """Pre-check rows that already break the rule, written directly (no route can now)."""
    common = _a1_scope(world)
    rows = {
        "foreign_campaign": Campaign(
            name="Legacy foreign", location_ids=[world.loc_a1, world.loc_b], **common
        ),
        "missing_offer": OfferCalendarEntry(
            name="Legacy missing", eligible_location_ids=["gone-location"], **common
        ),
        "malformed_campaign": Campaign(name="Legacy malformed", location_ids={"x": 1}, **common),
        "inactive_campaign": Campaign(
            name="Legacy inactive", location_ids=[world.loc_a1_off], **common
        ),
        "clean_offer": OfferCalendarEntry(
            name="Legacy clean", eligible_location_ids=[world.loc_a1], **common
        ),
    }
    return dict(zip(rows, _write_direct(world, *rows.values()), strict=True))


@pytest.fixture(scope="module")
def legacy(world: World) -> dict[str, str]:
    return _legacy_rows(world)


class TestLegacyRowsAreReportedNotRewritten:
    def test_findings_name_each_legacy_violation(self, world: World, legacy):
        with Session(bind=world.witness) as s:
            findings = location_refs.find_location_reference_findings(s)
        mine = {
            (f.collection, f.row_id, f.location_id, f.kind)
            for f in findings
            if f.row_id in legacy.values()
        }
        assert mine == {
            ("campaigns", legacy["foreign_campaign"], world.loc_b, "foreign_workspace"),
            ("offers", legacy["missing_offer"], "gone-location", "missing"),
            ("campaigns", legacy["malformed_campaign"], None, "malformed"),
            ("campaigns", legacy["inactive_campaign"], world.loc_a1_off, "inactive"),
        }

    def test_the_report_reads_only(self, world: World, legacy):
        before = world.db_file.read_bytes()
        with Session(bind=world.witness) as s:
            report = location_refs.build_report(s)
            s.rollback()
        assert world.db_file.read_bytes() == before
        assert report["violation_count"] == len(report["violations"]) >= 3
        assert {v["kind"] for v in report["violations"]} <= set(location_refs.VIOLATION_KINDS)
        assert report["inactive_reference_count"] == len(report["inactive_references"]) >= 1
        assert world.row(Campaign, legacy["foreign_campaign"])["location_ids"] == [
            world.loc_a1,
            world.loc_b,
        ]

    def test_a_rename_does_not_touch_a_legacy_list_but_resending_it_is_refused(self, world: World):
        (row_id,) = _write_direct(
            world,
            Campaign(
                name="Legacy to repair",
                location_ids=[world.loc_a1, world.loc_b],
                **_a1_scope(world),
            ),
        )
        url = f"/workspaces/{world.ws_a1}/campaigns/{row_id}"
        renamed = world.a("PATCH", url, {"name": "Legacy renamed"})
        assert renamed.status_code == 200
        assert renamed.json()["location_ids"] == [world.loc_a1, world.loc_b]
        resent = world.a("PATCH", url, {"location_ids": [world.loc_a1, world.loc_b]})
        assert _error(resent) == LOCATION_404
        fixed = world.a("PATCH", url, {"location_ids": [world.loc_a1]})
        assert fixed.status_code == 200 and fixed.json()["location_ids"] == [world.loc_a1]

    def test_command_exit_codes_and_json_report(
        self, world: World, legacy, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            config_module,
            "get_settings",
            lambda: SimpleNamespace(database_url=f"sqlite:///{world.db_file}"),
        )
        with Session(bind=world.witness) as s:
            expected = location_refs.build_report(s)
        assert location_refs.main([]) == location_refs.EXIT_VIOLATIONS
        report = json.loads(capsys.readouterr().out)
        assert report == json.loads(json.dumps(expected))
        assert report["violation_count"] >= 3
        assert report["rows_checked"] == {
            "campaigns": world.count(Campaign),
            "offers": world.count(OfferCalendarEntry),
        }

        clean = tmp_path / "clean.db"
        clean_engine = _engine(clean)
        Base.metadata.create_all(clean_engine)
        clean_engine.dispose()
        monkeypatch.setattr(
            config_module,
            "get_settings",
            lambda: SimpleNamespace(database_url=f"sqlite:///{clean}"),
        )
        assert location_refs.main([]) == location_refs.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["violation_count"] == 0 and out["rows_checked"] == {"campaigns": 0, "offers": 0}

    def test_command_failures_print_a_fixed_token_only(self, tmp_path, monkeypatch, capsys):
        secret_path = tmp_path / "no-such-dir" / "secret-name.db"
        monkeypatch.setattr(
            config_module,
            "get_settings",
            lambda: SimpleNamespace(database_url=f"sqlite:///{secret_path}"),
        )
        code = location_refs.main([])
        captured = capsys.readouterr()
        assert code == location_refs.EXIT_CONFIG_OR_CONNECT_FAILURE
        assert captured.out == ""
        assert captured.err.strip() == "location-refs: connection-failure"
        assert location_refs.main(["--extra"]) == location_refs.EXIT_CONFIG_OR_CONNECT_FAILURE
        assert "secret-name" not in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 3. P6-PLAT-9: PATCH on every context collection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,model,create,valid,invalid", COLLECTIONS, ids=COLLECTION_IDS)
class TestPatch:
    def test_success_changes_only_the_sent_fields_and_keeps_the_id(
        self, world, path, model, create, valid, invalid
    ):
        item = _create(world, world.ws_a1, path, create)
        before = world.row(model, item)
        r = world.a("PATCH", f"/workspaces/{world.ws_a1}/{path}/{item}", valid)
        assert r.status_code == 200, r.text
        after = world.row(model, item)
        assert r.json()["id"] == after["id"] == item
        for key, value in valid.items():
            assert after[key] == value and r.json()[key] == value
        unchanged = set(before) - set(valid) - {"updated_at"}
        assert {k: after[k] for k in unchanged} == {k: before[k] for k in unchanged}
        assert after["updated_at"] >= before["updated_at"]
        assert world.count(model, id=item) == 1

    def test_empty_patch_is_a_no_op(self, world, path, model, create, valid, invalid):
        item = _create(world, world.ws_a1, path, create)
        before = world.row(model, item)
        r = world.a("PATCH", f"/workspaces/{world.ws_a1}/{path}/{item}", {})
        assert r.status_code == 200
        assert world.row(model, item) == before

    def test_tenant_and_identity_fields_in_the_body_are_ignored(
        self, world, path, model, create, valid, invalid
    ):
        item = _create(world, world.ws_a1, path, create)
        before = world.row(model, item)
        hostile = {
            "id": "f" * 32,
            "organization_id": world.org_b,
            "workspace_id": world.ws_b,
            "brand_id": "b" * 32,
            "created_at": "2001-01-01T00:00:00Z",
        }
        r = world.a("PATCH", f"/workspaces/{world.ws_a1}/{path}/{item}", hostile)
        assert r.status_code == 200, r.text
        after = world.row(model, item)
        for key in hostile:
            assert after[key] == before[key], key

    def test_validation_failure_422_row_unchanged(self, world, path, model, create, valid, invalid):
        item = _create(world, world.ws_a1, path, create)
        before = world.row(model, item)
        r = world.a("PATCH", f"/workspaces/{world.ws_a1}/{path}/{item}", invalid)
        assert _error(r) == BODY_422, r.text
        assert world.row(model, item) == before
        not_an_object = world.a("PATCH", f"/workspaces/{world.ws_a1}/{path}/{item}", [])
        assert _error(not_an_object) == BODY_422
        assert world.row(model, item) == before

    def test_cross_workspace_same_organization_404_row_unchanged(
        self, world, path, model, create, valid, invalid
    ):
        # The caller is a member of both workspaces; only the handler's scoping refuses.
        other = _create(world, world.ws_a2, path, create)
        before = world.row(model, other)
        r = world.a("PATCH", f"/workspaces/{world.ws_a1}/{path}/{other}", valid)
        assert _error(r) == ITEM_404, r.text
        assert world.row(model, other) == before

    def test_cross_organization_403_and_404_row_unchanged(
        self, world, path, model, create, valid, invalid
    ):
        foreign = _create(world, world.ws_b, path, create, token=world.owner_b)
        before = world.row(model, foreign)
        # B's workspace in the path: the tenancy dependency refuses A's owner.
        r = world.a("PATCH", f"/workspaces/{world.ws_b}/{path}/{foreign}", valid)
        assert _error(r) == NOT_A_MEMBER, r.text
        # B's item id under A's own workspace: the handler refuses (hidden IDOR).
        r = world.a("PATCH", f"/workspaces/{world.ws_a1}/{path}/{foreign}", valid)
        assert _error(r) == ITEM_404, r.text
        assert world.row(model, foreign) == before

    def test_unknown_item_404(self, world, path, model, create, valid, invalid):
        r = world.a("PATCH", f"/workspaces/{world.ws_a1}/{path}/{'0' * 32}", valid)
        assert _error(r) == ITEM_404

    @pytest.mark.parametrize("role", ["reviewer", "compliance_reviewer", "viewer"])
    def test_role_denial_403_row_unchanged(self, world, path, model, create, valid, invalid, role):
        item = _create(world, world.ws_a1, path, create)
        before = world.row(model, item)
        r = world.send(
            "PATCH", f"/workspaces/{world.ws_a1}/{path}/{item}", world.tokens[role], valid
        )
        assert _error(r) == (
            403,
            "permission_denied",
            f"Role '{role}' is not permitted for this action.",
        ), r.text
        assert world.row(model, item) == before

    @pytest.mark.parametrize("role", ["admin", "marketer"])
    def test_admitted_non_owner_roles(self, world, path, model, create, valid, invalid, role):
        item = _create(world, world.ws_a1, path, create)
        r = world.send(
            "PATCH", f"/workspaces/{world.ws_a1}/{path}/{item}", world.tokens[role], valid
        )
        assert r.status_code == 200, r.text


def test_nullable_field_can_be_cleared_and_required_field_cannot(world: World):
    item = _create(world, world.ws_a1, "campaigns", {"name": "Clearable", "goal": "Old goal"})
    url = f"/workspaces/{world.ws_a1}/campaigns/{item}"
    cleared = world.a("PATCH", url, {"goal": None})
    assert cleared.status_code == 200 and cleared.json()["goal"] is None
    assert _error(world.a("PATCH", url, {"name": None})) == BODY_422
    assert world.row(Campaign, item)["name"] == "Clearable"


@pytest.mark.parametrize("create_schema,patch_schema", PATCH_PAIRS, ids=COLLECTION_IDS)
def test_patch_schema_mirrors_the_create_schema(create_schema, patch_schema):
    """Same fields, same types and constraints, none required, no published default."""
    assert set(patch_schema.model_fields) == set(create_schema.model_fields)
    for name, info in create_schema.model_fields.items():
        patched = patch_schema.model_fields[name]
        assert patched.annotation == info.annotation, name
        assert patched.metadata == info.metadata, name
        assert not patched.is_required(), name
    published = patch_schema.model_json_schema()
    assert "required" not in published
    assert all("default" not in p for p in published["properties"].values())


def test_every_collection_has_a_patch_route():
    paths = app.openapi()["paths"]
    for path in COLLECTION_IDS:
        op = paths[f"{API}/workspaces/{{workspace_id}}/{path}/{{item_id}}"]
        assert set(op) == {"patch", "delete"}, path
