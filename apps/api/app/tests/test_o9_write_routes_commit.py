"""O9 (FU-267-F7): the Tier 0 and Tier 1 write routes commit before they answer.

``P6-W21-O9-01/AUDIT.md`` §2.1 lists the routes that wrote through ``get_db`` and relied on its
post-yield commit, which FastAPI runs after the response has been sent. The 19 routes here
(inventory numbers 1-19) now declare ``db: Session = WriteDb``
(:mod:`app.db.request_tx`). For each, on the real application and its real routes, with only
``get_db`` overridden and committed state read through a separate engine (the witness):

* **T1, failure injection** (one test per route class of AUDIT §4.3): the commit fails, the
  client gets a 500 (not a 2xx), and the witness sees neither the change nor its audit row;
* **T2, durability**: ``get_db``'s teardown never commits, yet the 2xx write and its audit
  row are durable, because they were committed before the response;
* **T3/T6, ordering and the read-after-write window**: a commit is logged before
  ``http.response.start``, and a read made when the final body is sent sees the change and
  its audit row. The dedicated **G6** test is the Scout edit case of review r2 (#278
  Limitation 14);
* **T4, refusals**: a route that refuses (some after a flush) keeps its status, and nothing
  of the refused write or its audit is committed.

Harness: the four-market demo seed in a SQLite file built once per module, copied for each
test, so every test starts from the same committed state. The scheduling and feedback
capabilities are switched on for the test (both are dark by default).

**SQLite and O9-X1 (matrix O14, out of scope here).** On SQLite the override-set service's
SAVEPOINT (``capabilities/service.py``, ``begin_nested``) runs before any other statement of
the request's transaction, so pysqlite commits the override row when the SAVEPOINT is
released. A failed request commit then loses only the audit row. T1 for route #1 therefore
asserts the 500 and the absent audit row here; the override row's absence is asserted on
PostgreSQL in ``test_o9_write_routes_commit_postgres.py``.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.audit.models import AuditLog
from app.brands.models import Brand, BusinessProfile
from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import Capability
from app.capabilities.service import set_capability_override
from app.core.config import get_settings
from app.core.enums import ScoutRequestStatus
from app.db import seed as seed_mod
from app.db.models import Base
from app.db.request_tx import commit_before_response
from app.db.session import get_db
from app.feedback.models import OpportunityFeedback
from app.intelligence.records import SignalIntelligenceRecord
from app.locations.models import BusinessLocation, GeoCoverageRule
from app.main import app
from app.opportunities.models import Opportunity
from app.organizations.models import User, Workspace
from app.scouting_requests import schedules as schedule_service
from app.scouting_requests.models import ScoutRequest, ScoutSchedule
from app.tests._auth2_support import active_rate_limiter, live_bearer, sqlite_file_engine
from app.tests._seed_support import clear_seeded_runs
from app.tests._write_commit_support import (
    CommitSwitch,
    SendRecorder,
    commit_precedes_response,
    failing_commit_factory,
    mirror_get_db,
    never_commits,
    record_commits,
)

API = get_settings().api_prefix
ORG, WS, OWNER = seed_mod.sid("org"), seed_mod.sid("ws"), seed_mod.sid("user")
OPERATOR = "o9-operator"
FRESH_WS = "o9-fresh-workspace"
FEEDBACK = Capability.OPPORTUNITY_FEEDBACK.value


def loc(market: str) -> str:
    return seed_mod.sid("loc", market)


def scout(market: str) -> str:
    return seed_mod.sid("scout", market)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    """The seeded database every test copies: demo seed, no runs, an operator, and a
    workspace of the demo organization that has not been onboarded."""
    path = tmp_path_factory.mktemp("o9_routes") / "template.db"
    engine = sqlite_file_engine(path)
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    original = seed_mod.SessionLocal
    seed_mod.SessionLocal = make
    try:
        seed_mod.seed(reset=True)
    finally:
        seed_mod.SessionLocal = original
    clear_seeded_runs(make)
    with make() as s:
        s.add(
            User(
                id=OPERATOR,
                email="o9-operator@example.com",
                full_name="O9 operator",
                hashed_password="x",
                is_active=True,
                is_operator=True,
            )
        )
        s.add(Workspace(id=FRESH_WS, organization_id=ORG, name="O9 fresh", slug="o9-fresh"))
        s.commit()
    engine.dispose()
    return path


@dataclass
class World:
    witness: Engine
    factory: sessionmaker
    switch: CommitSwitch
    owner: dict
    operator: dict
    opportunity_id: str
    record_id: str
    events: list[str] = field(default_factory=list)

    def read(self, fn: Callable[[Session], object]) -> object:
        """Committed state, through an engine of its own."""
        with Session(self.witness) as s:
            return fn(s)

    def audits(self, action: str | None = None) -> int:
        stmt = select(func.count()).select_from(AuditLog)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        return self.read(lambda s: s.scalar(stmt))

    def write(self, fn: Callable[[Session], None]) -> None:
        """Committed setup, through the witness engine (not through the app)."""
        with Session(self.witness, expire_on_commit=False) as s:
            fn(s)
            s.commit()


@pytest.fixture
def world(template, tmp_path, monkeypatch) -> Iterator[World]:
    path = tmp_path / "o9_routes.db"
    shutil.copyfile(template, path)
    request_engine, witness = sqlite_file_engine(path), sqlite_file_engine(path)
    switch = CommitSwitch()
    factory = failing_commit_factory(request_engine, switch)
    settings = get_settings()
    monkeypatch.setattr(settings, "scout_scheduling_enabled", True)
    # Live scheduling needs the environment ceiling lifted as well (FU-267-C1, W21
    # integration): the schedule routes #7-#10 answer 503 capability_unavailable under it.
    monkeypatch.setattr(settings, "scout_scheduling_ceiling_lifted", True)
    monkeypatch.setattr(settings, "opportunity_feedback_enabled", True)
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = mirror_get_db(factory)
    active_rate_limiter()._hits.clear()
    try:
        with Session(witness) as s:
            opportunity_id, record_id = s.execute(
                select(Opportunity.id, SignalIntelligenceRecord.id)
                .join(
                    SignalIntelligenceRecord,
                    SignalIntelligenceRecord.opportunity_id == Opportunity.id,
                )
                .where(Opportunity.workspace_id == WS)
                .order_by(Opportunity.id, SignalIntelligenceRecord.id)
                .limit(1)
            ).one()
        yield World(
            witness=witness,
            factory=factory,
            switch=switch,
            owner={"Authorization": f"Bearer {live_bearer(OWNER)}"},
            operator={"Authorization": f"Bearer {live_bearer(OPERATOR)}"},
            opportunity_id=opportunity_id,
            record_id=record_id,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        if previous is not None:
            app.dependency_overrides[get_db] = previous
        active_rate_limiter()._hits.clear()
        request_engine.dispose()
        witness.dispose()


# --------------------------------------------------------------------------- #
# The 19 routes (AUDIT §2.1 numbers; class per AUDIT §4.3)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Case:
    no: int
    klass: str
    method: str
    #: The route's served path template.
    route: str
    url: Callable[[World], str]
    status: int
    #: Committed state the write changes, read through the witness.
    observe: Callable[[Session, World], object]
    #: What ``observe`` reads once the write is durable.
    after: object
    #: The audit action written with the change (``None``: the route writes no audit).
    audit: str | None
    body: Callable[[World], dict] | dict | None = None
    params: dict | None = None
    prepare: Callable[[World], None] | None = None
    operator: bool = False

    @property
    def id(self) -> str:
        return f"#{self.no} {self.method} {self.route.removeprefix(API)}"

    def json(self, w: World) -> dict | None:
        return self.body(w) if callable(self.body) else self.body


def _col(column, *where):
    """``observe``: one column of one row."""
    return lambda s, w: s.scalar(select(column).where(*where))


def _count(model, *where):
    """``observe``: how many rows match."""
    return lambda s, w: s.scalar(select(func.count()).select_from(model).where(*where))


def _schedule_of(market: str):
    def make(w: World) -> None:
        def create(s: Session) -> None:
            request = s.get(ScoutRequest, scout(market))
            schedule_service.create_schedule(
                s, request=request, interval="daily", actor_user_id=OWNER
            )

        w.write(create)

    return make


def _disabled_schedule_of(market: str):
    def make(w: World) -> None:
        _schedule_of(market)(w)
        w.write(
            lambda s: s.execute(
                update(ScoutSchedule)
                .where(ScoutSchedule.scout_request_id == scout(market))
                .values(enabled=False, next_run_at=None)
            )
        )

    return make


def _override_present(w: World) -> None:
    w.write(
        lambda s: set_capability_override(
            s,
            organization_id=ORG,
            workspace_id=WS,
            capability=Capability.OPPORTUNITY_FEEDBACK,
            enabled=False,
            actor_user_id=OPERATOR,
        )
    )


def _set(model, row_id: str, **values):
    return lambda w: w.write(
        lambda s: s.execute(update(model).where(model.id == row_id).values(**values))
    )


_W = f"{API}/workspaces/{{workspace_id}}"
_SR = f"{_W}/scout-requests/{{request_id}}"
_LOC = f"{_W}/locations/{{location_id}}"
_OPP = f"{_W}/opportunities/{{opportunity_id}}"
_OVR = f"{API}/internal/system/capabilities/overrides"
_WS_URL = f"{API}/workspaces/{WS}"
_DALLAS_SCOUT = f"{_WS_URL}/scout-requests/{scout('dallas')}"
_LONDON_SCOUT = f"{_WS_URL}/scout-requests/{scout('london')}"
_FEEDBACK_OVERRIDES = (
    WorkspaceCapabilityOverride.workspace_id == WS,
    WorkspaceCapabilityOverride.capability == FEEDBACK,
)
_LONDON_SCHEDULE = (ScoutSchedule.scout_request_id == scout("london"),)

CASES: tuple[Case, ...] = (
    Case(
        no=1,
        klass="V",
        method="PUT",
        route=_OVR,
        url=lambda w: _OVR,
        status=200,
        observe=_count(WorkspaceCapabilityOverride, *_FEEDBACK_OVERRIDES),
        after=1,
        audit="workspace_capability_override.created",
        body={"organization_id": ORG, "workspace_id": WS, "capability": FEEDBACK, "enabled": False},
        operator=True,
    ),
    Case(
        no=2,
        klass="V",
        method="DELETE",
        route=_OVR,
        url=lambda w: _OVR,
        status=200,
        observe=_count(WorkspaceCapabilityOverride, *_FEEDBACK_OVERRIDES),
        after=0,
        audit="workspace_capability_override.cleared",
        params={"organization_id": ORG, "workspace_id": WS, "capability": FEEDBACK},
        prepare=_override_present,
        operator=True,
    ),
    Case(
        no=3,
        klass="III",
        method="PUT",
        route=_SR,
        url=lambda w: _DALLAS_SCOUT,
        status=200,
        observe=_col(ScoutRequest.keywords, ScoutRequest.id == scout("dallas")),
        after=["o9-after"],
        audit="scout_request.updated",
        body={"keywords": ["o9-after"]},
    ),
    Case(
        no=4,
        klass="II",
        method="POST",
        route=f"{_SR}/resume",
        url=lambda w: f"{_DALLAS_SCOUT}/resume",
        status=200,
        observe=_col(ScoutRequest.status, ScoutRequest.id == scout("dallas")),
        after=ScoutRequestStatus.DRAFT.value,
        audit="scout_request.resumed",
        prepare=_set(ScoutRequest, scout("dallas"), status=ScoutRequestStatus.PAUSED.value),
    ),
    Case(
        no=5,
        klass="II",
        method="POST",
        route=f"{_LOC}/deactivate",
        url=lambda w: f"{_WS_URL}/locations/{loc('nairobi')}/deactivate",
        status=200,
        observe=_col(BusinessLocation.is_active, BusinessLocation.id == loc("nairobi")),
        after=False,
        audit="location.deactivated",
    ),
    Case(
        no=6,
        klass="II",
        method="POST",
        route=f"{_LOC}/reactivate",
        url=lambda w: f"{_WS_URL}/locations/{loc('nairobi')}/reactivate",
        status=200,
        observe=_col(BusinessLocation.is_active, BusinessLocation.id == loc("nairobi")),
        after=True,
        audit="location.reactivated",
        prepare=_set(BusinessLocation, loc("nairobi"), is_active=False),
    ),
    Case(
        no=7,
        klass="I",
        method="POST",
        route=f"{_SR}/schedule",
        url=lambda w: f"{_LONDON_SCOUT}/schedule",
        status=201,
        observe=_count(ScoutSchedule, *_LONDON_SCHEDULE),
        after=1,
        audit="scout_schedule.created",
        body={"interval": "daily"},
    ),
    Case(
        no=8,
        klass="II",
        method="POST",
        route=f"{_SR}/schedule/pause",
        url=lambda w: f"{_LONDON_SCOUT}/schedule/pause",
        status=200,
        observe=_col(ScoutSchedule.enabled, *_LONDON_SCHEDULE),
        after=False,
        audit="scout_schedule.paused",
        prepare=_schedule_of("london"),
    ),
    Case(
        no=9,
        klass="II",
        method="POST",
        route=f"{_SR}/schedule/resume",
        url=lambda w: f"{_LONDON_SCOUT}/schedule/resume",
        status=200,
        observe=_col(ScoutSchedule.enabled, *_LONDON_SCHEDULE),
        after=True,
        audit="scout_schedule.resumed",
        prepare=_disabled_schedule_of("london"),
    ),
    Case(
        no=10,
        klass="IV",
        method="DELETE",
        route=f"{_SR}/schedule",
        url=lambda w: f"{_LONDON_SCOUT}/schedule",
        status=204,
        observe=_count(ScoutSchedule, *_LONDON_SCHEDULE),
        after=0,
        audit="scout_schedule.deleted",
        prepare=_schedule_of("london"),
    ),
    Case(
        no=11,
        klass="I",
        method="POST",
        route=f"{_W}/onboarding",
        url=lambda w: f"{API}/workspaces/{FRESH_WS}/onboarding",
        status=201,
        observe=_count(Brand, Brand.workspace_id == FRESH_WS),
        after=1,
        audit="onboarding.completed",
        body={"brand_name": "O9 brand", "profile": {"company_name": "O9 company"}},
    ),
    Case(
        no=12,
        klass="II",
        method="PUT",
        route=f"{_W}/business-profile",
        url=lambda w: f"{_WS_URL}/business-profile",
        status=200,
        observe=_col(BusinessProfile.company_name, BusinessProfile.workspace_id == WS),
        after="O9 company after",
        audit="business_profile.updated",
        body={"company_name": "O9 company after"},
    ),
    Case(
        no=13,
        klass="I",
        method="POST",
        route=f"{_W}/locations",
        url=lambda w: f"{_WS_URL}/locations",
        status=201,
        observe=_count(BusinessLocation, BusinessLocation.name == "O9 location"),
        after=1,
        audit="location.created",
        body={"name": "O9 location", "city": "Austin", "country": "US"},
    ),
    Case(
        no=14,
        klass="II",
        method="PUT",
        route=f"{_LOC}/geo-coverage",
        url=lambda w: f"{_WS_URL}/locations/{loc('dallas')}/geo-coverage",
        status=200,
        observe=_col(GeoCoverageRule.radius_miles, GeoCoverageRule.location_id == loc("dallas")),
        after=37,
        audit="geo_coverage.updated",
        body={"coverage_type": "radius", "radius_miles": 37},
    ),
    Case(
        no=15,
        klass="II",
        method="PUT",
        route=f"{_OPP}/status",
        url=lambda w: f"{_WS_URL}/opportunities/{w.opportunity_id}/status",
        status=200,
        observe=lambda s, w: s.scalar(
            select(Opportunity.status).where(Opportunity.id == w.opportunity_id)
        ),
        after="saved",
        audit="opportunity.saved",
        body={"status": "saved"},
    ),
    Case(
        no=16,
        klass="I",
        method="POST",
        route=f"{_OPP}/feedback",
        url=lambda w: f"{_WS_URL}/opportunities/{w.opportunity_id}/feedback",
        status=201,
        observe=lambda s, w: s.scalar(
            select(func.count())
            .select_from(OpportunityFeedback)
            .where(OpportunityFeedback.opportunity_id == w.opportunity_id)
        ),
        after=1,
        audit="opportunity_feedback.created",
        body=lambda w: {"intelligence_record_id": w.record_id, "is_useful": True},
    ),
    Case(
        no=17,
        klass="I",
        method="POST",
        route=f"{_W}/scout-requests",
        url=lambda w: f"{_WS_URL}/scout-requests",
        status=201,
        observe=_count(ScoutRequest, ScoutRequest.name == "O9 scout"),
        after=1,
        audit="scout_request.created",
        body={"name": "O9 scout", "keywords": ["o9"]},
    ),
    Case(
        no=18,
        klass="I",
        method="POST",
        route=f"{API}/organizations/{{organization_id}}/workspaces",
        url=lambda w: f"{API}/organizations/{ORG}/workspaces",
        status=201,
        observe=_count(Workspace, Workspace.name == "O9 workspace"),
        after=1,
        audit=None,
        body={"name": "O9 workspace"},
    ),
    Case(
        no=19,
        klass="II",
        method="PUT",
        route=_LOC,
        url=lambda w: f"{_WS_URL}/locations/{loc('dallas')}",
        status=200,
        observe=_col(BusinessLocation.name, BusinessLocation.id == loc("dallas")),
        after="O9 renamed",
        audit=None,
        body={"name": "O9 renamed", "city": "Dallas", "country": "US"},
    ),
)
BY_NO = {c.no: c for c in CASES}


def _send(case: Case, w: World, client: TestClient):
    active_rate_limiter()._hits.clear()
    return client.request(
        case.method,
        case.url(w),
        json=case.json(w),
        params=case.params,
        headers=w.operator if case.operator else w.owner,
    )


def _of(*classes: str) -> list:
    return [pytest.param(c, id=c.id) for c in CASES if c.klass in classes]


def _all() -> list:
    return [pytest.param(c, id=c.id) for c in CASES]


def test_the_cases_are_the_nineteen_tier_0_and_1_routes_and_all_use_write_db():
    """AUDIT §6 steps 1-2: inventory numbers 1-19, each a served route on ``WriteDb``."""
    from fastapi.routing import iter_route_contexts

    assert sorted(BY_NO) == list(range(1, 20))
    served = {}
    for rc in iter_route_contexts(app.routes):
        if getattr(rc, "dependant", None) is None:
            continue
        stack, function_scoped = [rc.dependant], False
        while stack:
            d = stack.pop()
            if d.call is commit_before_response and d.scope == "function":
                function_scoped = True
            stack.extend(d.dependencies)
        for method in set(rc.methods) - {"HEAD", "OPTIONS"}:
            served[(method, rc.path)] = function_scoped
    for case in CASES:
        assert served.get((case.method, case.route)) is True, case.id


# --------------------------------------------------------------------------- #
# T1: a failed commit answers 500 and persists nothing (one test per route class)
# --------------------------------------------------------------------------- #
#: Committed state that SQLite cannot keep atomic for this route (O9-X1 / matrix O14; see
#: the module docstring). Asserted on PostgreSQL instead.
_SQLITE_SAVEPOINT_COMMITS_ROW = {1}


def _assert_failed_commit_answers_500_and_persists_nothing(case: Case, w: World) -> None:
    if case.prepare:
        case.prepare(w)
    before = w.read(lambda s: case.observe(s, w))
    audits_before = w.audits()
    assert before != case.after, f"{case.id}: the case would not observe its write"

    w.switch.armed = True
    reply = _send(case, w, TestClient(app, raise_server_exceptions=False))
    w.switch.armed = False

    assert reply.status_code == 500, (case.id, reply.status_code, reply.text)
    assert reply.json()["error"]["code"] == "internal_error"
    assert w.switch.failures == 1
    assert w.audits() == audits_before, f"{case.id}: an audit row survived the failed commit"
    if case.no not in _SQLITE_SAVEPOINT_COMMITS_ROW:
        assert w.read(lambda s: case.observe(s, w)) == before, f"{case.id}: the write survived"


@pytest.mark.parametrize("case", _of("I"))
def test_t1_class_i_insert_and_flush_failed_commit_answers_500_and_persists_nothing(world, case):
    _assert_failed_commit_answers_500_and_persists_nothing(case, world)


@pytest.mark.parametrize("case", _of("II"))
def test_t1_class_ii_attribute_update_failed_commit_answers_500_and_persists_nothing(world, case):
    _assert_failed_commit_answers_500_and_persists_nothing(case, world)


@pytest.mark.parametrize("case", _of("III"))
def test_t1_class_iii_compare_and_set_failed_commit_answers_500_and_persists_nothing(world, case):
    _assert_failed_commit_answers_500_and_persists_nothing(case, world)


@pytest.mark.parametrize("case", _of("IV"))
def test_t1_class_iv_delete_failed_commit_answers_500_and_persists_nothing(world, case):
    _assert_failed_commit_answers_500_and_persists_nothing(case, world)


@pytest.mark.parametrize("case", _of("V"))
def test_t1_class_v_service_owned_failed_commit_answers_500_and_persists_nothing(world, case):
    _assert_failed_commit_answers_500_and_persists_nothing(case, world)


# --------------------------------------------------------------------------- #
# T2: durable although get_db's teardown never commits
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", _all())
def test_t2_the_write_and_its_audit_are_durable_when_teardown_never_commits(world, case):
    if case.prepare:
        case.prepare(world)
    action = case.audit
    audits_before = world.audits(action) if action else None
    app.dependency_overrides[get_db] = never_commits(world.factory)

    reply = _send(case, world, TestClient(app))

    assert reply.status_code == case.status, (case.id, reply.status_code, reply.text)
    assert world.read(lambda s: case.observe(s, world)) == case.after, case.id
    if action:
        assert world.audits(action) == audits_before + 1, case.id


# --------------------------------------------------------------------------- #
# T3/T6: the commit precedes the response; a read at the final body sees the write
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", _all())
def test_t3_t6_commit_precedes_the_response_and_a_read_at_the_final_body_sees_it(world, case):
    if case.prepare:
        case.prepare(world)
    action = case.audit
    audits_before = world.audits(action) if action else 0

    def second_client():
        return (
            world.read(lambda s: case.observe(s, world)),
            world.audits(action) - audits_before if action else None,
        )

    recorder = SendRecorder(app, world.events, at_final_body=second_client)
    stop = record_commits(world.switch, world.events)
    try:
        reply = _send(case, world, TestClient(recorder))
    finally:
        stop()

    assert reply.status_code == case.status, (case.id, reply.status_code, reply.text)
    assert commit_precedes_response(world.events), (case.id, world.events)
    assert recorder.seen_at_final_body == [(case.after, 1 if action else None)], case.id


def test_g6_a_read_sent_after_the_scout_edit_200_sees_the_new_keywords(world):
    """Review r2 G6 (#278 Limitation 14), deterministic: at the instant the client has the
    complete 200 for ``PUT .../scout-requests/{id}``, a second client reads the new keywords.
    Before O9 it read the old ones (AUDIT proof D1)."""
    case = BY_NO[3]
    before = world.read(lambda s: case.observe(s, world))
    recorder = SendRecorder(
        app, world.events, at_final_body=lambda: world.read(lambda s: case.observe(s, world))
    )

    reply = _send(case, world, TestClient(recorder))

    assert reply.status_code == 200 and reply.json()["keywords"] == ["o9-after"]
    assert before != ["o9-after"]
    assert recorder.seen_at_final_body == [["o9-after"]]


# --------------------------------------------------------------------------- #
# T4: a refusal keeps its status and commits nothing
# --------------------------------------------------------------------------- #
def _fifth_scout(w: World) -> None:
    """Four enabled schedules (the workspace cap) and a fifth Scout to schedule."""
    for market in ("dallas", "london", "lagos", "nairobi"):
        _schedule_of(market)(w)

    def add(s: Session) -> None:
        seeded = s.get(ScoutRequest, scout("dallas"))
        s.add(
            ScoutRequest(
                id="o9-fifth-scout",
                organization_id=seeded.organization_id,
                workspace_id=seeded.workspace_id,
                brand_id=seeded.brand_id,
                location_id=seeded.location_id,
                name="O9 fifth",
                status=ScoutRequestStatus.DRAFT.value,
                source_types=seeded.source_types,
                keywords=["o9"],
                stats={},
            )
        )

    w.write(add)


@dataclass(frozen=True)
class Refusal:
    name: str
    case: Case
    prepare: Callable[[World], None] | None
    status: int
    code: str
    observe: Callable[[Session, World], object]
    url: Callable[[World], str] | None = None
    body: dict | None = None


REFUSALS = (
    Refusal(
        name="#1 RSS enable, refused after its .rejected audit is flushed",
        case=BY_NO[1],
        prepare=None,
        status=422,
        code="capability_override_not_permitted",
        observe=_count(WorkspaceCapabilityOverride, WorkspaceCapabilityOverride.workspace_id == WS),
        body={
            "organization_id": ORG,
            "workspace_id": WS,
            "capability": Capability.CONNECTOR_RSS.value,
            "enabled": True,
        },
    ),
    Refusal(
        name="#3 Scout edit while queued",
        case=BY_NO[3],
        prepare=_set(ScoutRequest, scout("dallas"), status=ScoutRequestStatus.QUEUED.value),
        status=422,
        code="scout_in_progress",
        observe=BY_NO[3].observe,
    ),
    Refusal(
        name="#5 deactivate with an enabled schedule",
        case=BY_NO[5],
        prepare=_schedule_of("nairobi"),
        status=409,
        code="conflict",
        observe=BY_NO[5].observe,
    ),
    Refusal(
        name="#7 schedule over the workspace cap",
        case=BY_NO[7],
        prepare=_fifth_scout,
        status=409,
        code="conflict",
        observe=_count(ScoutSchedule, ScoutSchedule.scout_request_id == "o9-fifth-scout"),
        url=lambda w: f"{_WS_URL}/scout-requests/o9-fifth-scout/schedule",
    ),
)


@pytest.mark.parametrize("refusal", [pytest.param(r, id=r.name) for r in REFUSALS])
def test_t4_a_refusal_keeps_its_status_and_commits_nothing(world, refusal):
    if refusal.prepare:
        refusal.prepare(world)
    before = world.read(lambda s: refusal.observe(s, world))
    audits_before = world.audits()
    events: list[str] = []
    stop = record_commits(world.switch, events)
    try:
        active_rate_limiter()._hits.clear()
        case = refusal.case
        reply = TestClient(app).request(
            case.method,
            (refusal.url or case.url)(world),
            json=refusal.body if refusal.body is not None else case.json(world),
            params=case.params,
            headers=world.operator if case.operator else world.owner,
        )
    finally:
        stop()

    assert reply.status_code == refusal.status, (refusal.name, reply.text)
    assert reply.json()["error"]["code"] == refusal.code
    assert events == [], f"{refusal.name}: a refused request committed"
    assert world.read(lambda s: refusal.observe(s, world)) == before
    assert world.audits() == audits_before
