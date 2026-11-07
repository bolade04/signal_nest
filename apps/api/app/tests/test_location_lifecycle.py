"""P6-UI-023 (backend): deactivate and reactivate a location, never delete it.

The choice is recorded in ``app.locations.service``. Opportunities, scout requests,
schedules, jobs and intelligence records point at ``business_locations.id`` with
``ON DELETE SET NULL``, and geo-coverage with ``CASCADE``, so a hard delete would quietly
detach a market's history and drop its Scout Reach. These tests prove, through real HTTP
on a temporary SQLite file (``get_db`` is the only override):

* deactivate and reactivate flip ``is_active``, are idempotent, and audit only a real
  change;
* every row that referenced the location still references it and stays readable;
* new scouting work for a deactivated location is refused: creating a request, running
  one, and creating or resuming a schedule;
* deactivation is refused while an enabled schedule or a queued/running scout would keep
  working for the location;
* the routes have the editor gate (viewer/reviewer denied) and the handler's workspace
  scoping (another workspace of the same organization answers 404).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.core.config import get_settings
from app.core.middleware import RateLimitMiddleware
from app.db.models import Base
from app.db.session import get_db
from app.jobs.models import Job
from app.locations.models import BusinessLocation, GeoCoverageRule
from app.locations.service import (
    HAS_ENABLED_SCHEDULE,
    HAS_WORK_IN_FLIGHT,
    LOCATION_DEACTIVATED,
)
from app.main import app
from app.opportunities.models import Opportunity
from app.scouting_requests.models import ScoutRequest, ScoutSchedule

API = get_settings().api_prefix
PASSWORD = "ui023-lifecycle-pw-123"
DEACTIVATED = (422, "validation_error", LOCATION_DEACTIVATED)
NOT_FOUND = (404, "not_found", "Location not found in this workspace.")


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
    org: str = ""
    owner: str = ""
    ws: str = ""
    ws_other: str = ""
    brand: str = ""
    tokens: dict[str, str] = field(default_factory=dict)

    def send(self, method: str, path: str, body=None, token: str | None = None):
        assert set(app.dependency_overrides) == {get_db}, "only get_db may be overridden"
        _rate_limiter()._hits.clear()
        kwargs: dict = {"headers": {"Authorization": f"Bearer {token or self.owner}"}}
        if body is not None:
            kwargs["json"] = body
        return self.client.request(method, API + path, **kwargs)

    def created(self, path: str, body: dict, token: str | None = None) -> dict:
        r = self.send("POST", path, body, token)
        assert r.status_code == 201, (path, r.status_code, r.text)
        return r.json()

    def location(self, name: str, ws: str | None = None) -> str:
        return self.created(f"/workspaces/{ws or self.ws}/locations", {"name": name})["id"]

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


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Iterator[World]:
    db_file = tmp_path_factory.mktemp("ui023") / "ui023.db"
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
        w = World(client, witness)
        _rate_limiter()._hits.clear()
        owner = client.post(
            f"{API}/auth/register",
            json={
                "email": "ui023-owner@example.com",
                "full_name": "Owner",
                "password": PASSWORD,
                "organization_name": "UI023 organization",
            },
        ).json()
        w.org, w.owner = owner["memberships"][0]["organization_id"], owner["access_token"]
        for name in ("Main", "Other"):
            ws = w.created(f"/organizations/{w.org}/workspaces", {"name": name})["id"]
            ob = w.created(
                f"/workspaces/{ws}/onboarding",
                {"brand_name": f"Brand {name}", "profile": {"company_name": name}},
            )
            if name == "Main":
                w.ws, w.brand = ws, ob["brand"]["id"]
            else:
                w.ws_other = ws
        for role in ("marketer", "reviewer", "viewer"):
            inv = w.created(
                f"/organizations/{w.org}/invitations",
                {"email": f"ui023-{role}@example.com", "role": role},
            )
            _rate_limiter()._hits.clear()
            joined = client.post(
                f"{API}/auth/invitations/register",
                json={"token": inv["token"], "full_name": role, "password": PASSWORD},
            ).json()
            w.tokens[role] = joined["access_token"]
        yield w
    finally:
        app.dependency_overrides.clear()
        _rate_limiter()._hits.clear()
        request_engine.dispose()
        witness.dispose()


def _path(world: World, location_id: str, verb: str, ws: str | None = None) -> str:
    return f"/workspaces/{ws or world.ws}/locations/{location_id}/{verb}"


def _audits(world: World, location_id: str, action: str) -> int:
    return world.count(AuditLog, entity_id=location_id, action=action)


# --------------------------------------------------------------------------- #
# 1. The flag, idempotence and audit
# --------------------------------------------------------------------------- #
def test_deactivate_and_reactivate_flip_the_flag_and_audit_once(world: World):
    loc = world.location("Kiosk")
    off = world.send("POST", _path(world, loc, "deactivate"))
    assert off.status_code == 200 and off.json()["is_active"] is False
    again = world.send("POST", _path(world, loc, "deactivate"))
    assert again.status_code == 200 and again.json()["is_active"] is False
    assert _audits(world, loc, "location.deactivated") == 1
    on = world.send("POST", _path(world, loc, "reactivate"))
    assert on.status_code == 200 and on.json()["is_active"] is True
    assert world.send("POST", _path(world, loc, "reactivate")).status_code == 200
    assert _audits(world, loc, "location.reactivated") == 1
    assert world.row(BusinessLocation, loc)["is_active"] is True


def test_a_deactivated_location_stays_listed_with_its_flag(world: World):
    loc = world.location("Listed after retirement")
    world.send("POST", _path(world, loc, "deactivate"))
    listed = {x["id"]: x for x in world.send("GET", f"/workspaces/{world.ws}/locations").json()}
    assert listed[loc]["is_active"] is False


# --------------------------------------------------------------------------- #
# 2. History is kept, and stays readable
# --------------------------------------------------------------------------- #
def test_every_reference_survives_and_stays_readable(world: World):
    loc = world.location("Dallas branch")
    geo = world.send(
        "PUT",
        f"/workspaces/{world.ws}/locations/{loc}/geo-coverage",
        {"coverage_type": "radius", "radius_miles": 10},
    )
    assert geo.status_code == 200
    request = world.created(
        f"/workspaces/{world.ws}/scout-requests", {"name": "Branch scout", "location_id": loc}
    )
    with sessionmaker(bind=world.witness, expire_on_commit=False, future=True)() as s:
        opp = Opportunity(
            organization_id=world.org,
            workspace_id=world.ws,
            brand_id=world.brand,
            scout_request_id=request["id"],
            location_id=loc,
            title="Historic opportunity",
            classification="validated",
            decision="monitor",
        )
        s.add(opp)
        s.commit()
    assert world.send("POST", _path(world, loc, "deactivate")).status_code == 200

    assert world.row(BusinessLocation, loc) is not None
    assert world.row(Opportunity, opp.id)["location_id"] == loc
    assert world.row(ScoutRequest, request["id"])["location_id"] == loc
    assert world.count(GeoCoverageRule, location_id=loc) == 1
    feed = world.send("GET", f"/workspaces/{world.ws}/opportunities?location_id={loc}")
    assert [o["id"] for o in feed.json()] == [opp.id]
    reach = world.send("GET", f"/workspaces/{world.ws}/locations/{loc}/geo-coverage")
    assert reach.status_code == 200 and reach.json()["radius_miles"] == 10
    detail = world.send("GET", f"/workspaces/{world.ws}/scout-requests/{request['id']}")
    assert detail.status_code == 200


# --------------------------------------------------------------------------- #
# 3. New scouting work is refused while deactivated
# --------------------------------------------------------------------------- #
def test_no_new_scout_request_for_a_deactivated_location(world: World):
    loc = world.location("Closed store")
    world.send("POST", _path(world, loc, "deactivate"))
    before = world.count(ScoutRequest, workspace_id=world.ws)
    r = world.send(
        "POST", f"/workspaces/{world.ws}/scout-requests", {"name": "Nope", "location_id": loc}
    )
    assert _error(r) == DEACTIVATED, r.text
    assert world.count(ScoutRequest, workspace_id=world.ws) == before
    # Reactivated, it may be scouted again.
    world.send("POST", _path(world, loc, "reactivate"))
    world.created(f"/workspaces/{world.ws}/scout-requests", {"name": "Yes", "location_id": loc})


def test_an_existing_request_cannot_run_while_its_location_is_deactivated(world: World):
    loc = world.location("Seasonal store")
    request = world.created(
        f"/workspaces/{world.ws}/scout-requests", {"name": "Seasonal", "location_id": loc}
    )
    world.send("POST", _path(world, loc, "deactivate"))
    jobs_before = world.count(Job, scout_request_id=request["id"])
    r = world.send("POST", f"/workspaces/{world.ws}/scout-requests/{request['id']}/run")
    assert _error(r) == DEACTIVATED, r.text
    assert world.row(ScoutRequest, request["id"])["status"] == "draft"
    assert world.count(Job, scout_request_id=request["id"]) == jobs_before


def test_no_schedule_create_or_resume_while_deactivated(world: World, monkeypatch):
    monkeypatch.setattr(get_settings(), "scout_scheduling_enabled", True)
    # Live scheduling needs the environment ceiling lifted as well (FU-267-C1).
    monkeypatch.setattr(get_settings(), "scout_scheduling_ceiling_lifted", True)
    loc = world.location("Weekend market")
    request = world.created(
        f"/workspaces/{world.ws}/scout-requests", {"name": "Weekend", "location_id": loc}
    )
    schedule_path = f"/workspaces/{world.ws}/scout-requests/{request['id']}/schedule"
    world.send("POST", _path(world, loc, "deactivate"))
    r = world.send("POST", schedule_path, {"interval": "daily"})
    assert _error(r) == DEACTIVATED, r.text
    assert world.count(ScoutSchedule, scout_request_id=request["id"]) == 0
    # Reactivate, create and pause a schedule, deactivate, then try to resume it.
    world.send("POST", _path(world, loc, "reactivate"))
    assert world.send("POST", schedule_path, {"interval": "daily"}).status_code == 201
    assert world.send("POST", f"{schedule_path}/pause").status_code == 200
    assert world.send("POST", _path(world, loc, "deactivate")).status_code == 200
    r = world.send("POST", f"{schedule_path}/resume")
    assert _error(r) == DEACTIVATED, r.text
    sched = next(s for s in _schedules(world) if s["scout_request_id"] == request["id"])
    assert sched["enabled"] is False


def _schedules(world: World) -> list[dict]:
    table = ScoutSchedule.__table__
    with world.witness.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(*table.c))]


# --------------------------------------------------------------------------- #
# 4. Deactivation is refused while work would keep running
# --------------------------------------------------------------------------- #
def test_refused_while_an_enabled_schedule_exists(world: World, monkeypatch):
    monkeypatch.setattr(get_settings(), "scout_scheduling_enabled", True)
    # Live scheduling needs the environment ceiling lifted as well (FU-267-C1).
    monkeypatch.setattr(get_settings(), "scout_scheduling_ceiling_lifted", True)
    loc = world.location("Busy store")
    request = world.created(
        f"/workspaces/{world.ws}/scout-requests", {"name": "Busy", "location_id": loc}
    )
    schedule_path = f"/workspaces/{world.ws}/scout-requests/{request['id']}/schedule"
    assert world.send("POST", schedule_path, {"interval": "weekly"}).status_code == 201
    with world.witness.begin() as conn:  # an active schedule, whatever its starting state
        conn.execute(
            update(ScoutSchedule)
            .where(ScoutSchedule.scout_request_id == request["id"])
            .values(enabled=True)
        )
    r = world.send("POST", _path(world, loc, "deactivate"))
    assert _error(r) == (409, "conflict", HAS_ENABLED_SCHEDULE), r.text
    assert world.row(BusinessLocation, loc)["is_active"] is True
    assert world.send("POST", f"{schedule_path}/pause").status_code == 200
    assert world.send("POST", _path(world, loc, "deactivate")).status_code == 200


@pytest.mark.parametrize("status", ["queued", "running"])
def test_refused_while_a_scout_is_queued_or_running(world: World, status):
    loc = world.location(f"Store with a {status} scout")
    request = world.created(
        f"/workspaces/{world.ws}/scout-requests", {"name": status, "location_id": loc}
    )
    with world.witness.begin() as conn:
        conn.execute(
            update(ScoutRequest).where(ScoutRequest.id == request["id"]).values(status=status)
        )
    r = world.send("POST", _path(world, loc, "deactivate"))
    assert _error(r) == (409, "conflict", HAS_WORK_IN_FLIGHT), r.text
    assert world.row(BusinessLocation, loc)["is_active"] is True
    with world.witness.begin() as conn:
        conn.execute(
            update(ScoutRequest).where(ScoutRequest.id == request["id"]).values(status="completed")
        )
    assert world.send("POST", _path(world, loc, "deactivate")).status_code == 200


# --------------------------------------------------------------------------- #
# 5. Authorization and scoping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ["reviewer", "viewer"])
def test_non_editors_are_denied_and_nothing_changes(world: World, role):
    loc = world.location(f"Guarded from {role}")
    for verb in ("deactivate", "reactivate"):
        r = world.send("POST", _path(world, loc, verb), token=world.tokens[role])
        assert _error(r) == (
            403,
            "permission_denied",
            f"Role '{role}' is not permitted for this action.",
        )
    assert world.row(BusinessLocation, loc)["is_active"] is True
    assert _audits(world, loc, "location.deactivated") == 0


def test_a_marketer_may_deactivate_and_reactivate(world: World):
    loc = world.location("Marketer-managed")
    token = world.tokens["marketer"]
    assert world.send("POST", _path(world, loc, "deactivate"), token=token).status_code == 200
    assert world.send("POST", _path(world, loc, "reactivate"), token=token).status_code == 200


def test_another_workspace_of_the_same_organization_answers_404(world: World):
    other = world.location("Other workspace store", ws=world.ws_other)
    before = world.row(BusinessLocation, other)
    for verb in ("deactivate", "reactivate"):
        assert _error(world.send("POST", _path(world, other, verb))) == NOT_FOUND
    assert world.row(BusinessLocation, other) == before
    assert _error(world.send("POST", _path(world, "0" * 32, "deactivate"))) == NOT_FOUND
