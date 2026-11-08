"""Project Phase 5D-W4 adversarial battery: campaign hardening (exit criteria 2 and 3).

Lane 5D-W4 (W21) tests lane 5D-W1's campaigns router and campaign-scope checks against
``docs/project-phase-5d-multi-location-jurisdiction.md`` §2 (campaign hardening) and §4:

* **Exit criterion 2.** ``mode`` and ``status`` outside ``CampaignMode`` / ``CampaignStatus``
  are refused at the schema (422, nothing written) and by the database CHECKs
  ``ck_campaign_mode`` / ``ck_campaign_status`` (insert and update, schema bypassed); get and
  update work end to end; no answer of list, get, create or update carries
  ``organization_id`` or ``workspace_id`` (neither key nor value); a campaign outside the
  caller's workspace answers one uniform 404, byte for byte.
* **Exit criterion 3.** A cross-workspace ``campaign_id`` is rejected everywhere it is accepted.
  The places are enumerated from the served OpenAPI document (every operation's parameters and
  request-body fields, followed through ``$ref``), and the enumeration is pinned, so a new
  surface that accepts a campaign fails here until it is probed.
* **O9.** Every campaign write route commits before it answers: observed at the ASGI
  ``http.response.start`` message, the write is already visible to another connection.

Everything runs on SQLite and, when ``TEST_POSTGRES_URL`` is set, on PostgreSQL. The world is
:func:`app.tests._privacy_support.privacy_world` (organization A with ``WS_A`` / ``WS_A2``, B with
``WS_B``, C with ``WS_C``; ALICE owns A and C, CAROL is A's viewer, BOB owns B).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.briefs.models import RecommendationBrief
from app.campaign_context.models import Campaign, CampaignLocation
from app.core.config import get_settings
from app.core.enums import CampaignMode, CampaignStatus
from app.llm.mock import MockLLMProvider
from app.llm.models import LLMUsageEvent
from app.llm.service import LLMService, get_llm_service
from app.main import app
from app.scouting_requests.models import ScoutRequest
from app.tests._auth2_support import require_postgres
from app.tests._llm_offline import offline_llm, offline_settings
from app.tests._privacy_support import (
    ALICE,
    API,
    CAROL,
    ORG_A,
    ORG_B,
    ORG_C,
    WS_A,
    WS_A2,
    WS_B,
    WS_C,
    PrivacyEnv,
    privacy_world,
)

CAMPAIGNS = f"{API}/workspaces/{WS_A}/campaigns"
OWN, OWN_LOCATION = "a1-campaign", "a1-loc"
#: Campaigns that are not WS_A's: same organization, another organization ALICE also owns,
#: another organization ALICE is no member of.
FOREIGN = {"a2-campaign": WS_A2, "c1-campaign": WS_C, "b1-campaign": WS_B}
TENANT_VALUES = (ORG_A, ORG_B, ORG_C, WS_A, WS_A2, WS_B, WS_C)


@pytest.fixture(params=["sqlite", "postgresql"])
def env(request, tmp_path) -> Iterator[PrivacyEnv]:
    if request.param == "sqlite":
        with privacy_world(tmp_path / "campaign-hardening.db") as e:
            yield e
        return
    require_postgres()
    with privacy_world(None) as e:  # pragma: no cover - gated on PostgreSQL
        yield e


def _snapshot(env: PrivacyEnv) -> dict[str, tuple]:
    return env.snapshot(lambda table, _row: table.name != "auth_sessions")


def _send(env: PrivacyEnv, method: str, url: str, user: str = ALICE, body=None, client=None):
    kwargs: dict = {"headers": env.auth(user)}
    if body is not None:
        kwargs["json"] = body
    return (client or env.client).request(method, url, **kwargs)


def _stored(env: PrivacyEnv, campaign_id: str) -> dict | None:
    rows = env.rows(Campaign, Campaign.__table__.c.id == campaign_id)
    return rows[0] if rows else None


def _instant(value: str) -> datetime:
    """A timestamp of an answer as an instant: SQLite hands back naive UTC, PostgreSQL aware."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _same(answer: dict, other: dict) -> bool:
    stamps = ("created_at", "updated_at")
    return {k: v for k, v in answer.items() if k not in stamps} == {
        k: v for k, v in other.items() if k not in stamps
    } and all(_instant(answer[k]) == _instant(other[k]) for k in stamps)


def _no_tenant_columns(answer) -> None:
    """Neither the tenant keys nor any tenant id value, anywhere in the answer."""
    rows = answer if isinstance(answer, list) else [answer]
    for row in rows:
        assert not {"organization_id", "workspace_id"} & set(row), row
    text = repr(answer)
    for value in TENANT_VALUES:
        assert value not in text, value


# --------------------------------------------------------------------------- #
# 1. mode / status: refused at the schema (422) ...
# --------------------------------------------------------------------------- #
BAD_VALUES = [
    " grouped",
    "grouped ",
    "GROUPED",
    "ｇｒｏｕｐｅｄ",  # full-width grouped
    "per-location",
    "per location",
    "",
    "active\n",
    "Active",
    "ａｃｔｉｖｅ",  # full-width active
    "inactive",
    "live",
    0,
    1,
    True,
    ["grouped"],
    {"value": "grouped"},
]


@pytest.mark.parametrize("field", ["mode", "status"])
@pytest.mark.parametrize("bad", BAD_VALUES, ids=repr)
def test_an_invalid_mode_or_status_is_refused_at_the_schema(env, field, bad) -> None:
    snapshot = _snapshot(env)
    created = _send(env, "POST", CAMPAIGNS, body={"name": "x", field: bad})
    assert created.status_code == 422, created.text
    assert created.json()["error"]["code"] == "validation_error"
    patched = _send(env, "PATCH", f"{CAMPAIGNS}/{OWN}", body={field: bad})
    assert patched.status_code == 422, patched.text
    assert _snapshot(env) == snapshot


@pytest.mark.parametrize("field,members", [("mode", CampaignMode), ("status", CampaignStatus)])
def test_every_member_is_accepted_and_stored_verbatim(env, field, members) -> None:
    for member in members:
        r = _send(env, "PATCH", f"{CAMPAIGNS}/{OWN}", body={field: member.value})
        assert r.status_code == 200, r.text
        assert r.json()[field] == member.value
        assert _stored(env, OWN)[field] == member.value


# --------------------------------------------------------------------------- #
# ... and by the database CHECKs, on insert and on update (the schema bypassed)
# --------------------------------------------------------------------------- #
DB_BAD = [
    " grouped",
    "grouped ",
    "GROUPED",
    "ｇｒｏｕｐｅｄ",
    "per-location",
    "",
    "active\n",
    "Active",
    "inactive",
    "unknown",
]


def _campaign_row(**values) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "organization_id": ORG_A,
        "workspace_id": WS_A,
        "brand_id": "a1-brand",
        "name": "Direct",
        "mode": CampaignMode.GROUPED.value,
        "status": CampaignStatus.ACTIVE.value,
        "location_ids": [],
        **values,
    }


@pytest.mark.parametrize("field", ["mode", "status"])
@pytest.mark.parametrize("bad", DB_BAD, ids=repr)
def test_the_database_check_refuses_an_invalid_value(env, field, bad) -> None:
    table = Campaign.__table__
    before = _snapshot(env)
    with pytest.raises(IntegrityError):
        with env.witness.begin() as conn:
            conn.execute(insert(table).values(**_campaign_row(**{field: bad})))
    with pytest.raises(IntegrityError):
        with env.witness.begin() as conn:
            conn.execute(update(table).where(table.c.id == OWN).values(**{field: bad}))
    assert _snapshot(env) == before


@pytest.mark.parametrize("field", ["mode", "status"])
def test_the_database_refuses_a_null_mode_or_status(env, field) -> None:
    table = Campaign.__table__
    with pytest.raises(IntegrityError):
        with env.witness.begin() as conn:
            conn.execute(update(table).where(table.c.id == OWN).values(**{field: None}))
    assert _stored(env, OWN)[field] is not None


def test_the_database_admits_exactly_the_members(env) -> None:
    table = Campaign.__table__
    for mode, status in zip(
        list(CampaignMode), [*CampaignStatus][: len(CampaignMode)], strict=True
    ):
        row = _campaign_row(mode=mode.value, status=status.value)
        with env.witness.begin() as conn:
            conn.execute(insert(table).values(**row))
        assert (_stored(env, row["id"])["mode"], _stored(env, row["id"])["status"]) == (
            mode.value,
            status.value,
        )
    for status in CampaignStatus:
        with env.witness.begin() as conn:
            conn.execute(update(table).where(table.c.id == OWN).values(status=status.value))


# --------------------------------------------------------------------------- #
# 2. Get and update end to end; the tenant columns are never answered or assigned
# --------------------------------------------------------------------------- #
def test_create_get_update_list_end_to_end_without_tenant_columns(env) -> None:
    created = _send(
        env,
        "POST",
        CAMPAIGNS,
        body={
            "name": "Spring",
            "goal": "Footfall",
            "mode": "grouped",
            "status": "draft",
            "location_ids": [OWN_LOCATION],
        },
    )
    assert created.status_code == 201, created.text
    _no_tenant_columns(created.json())
    item = f"{CAMPAIGNS}/{created.json()['id']}"
    got = _send(env, "GET", item)
    assert got.status_code == 200
    _no_tenant_columns(got.json())
    assert _same(got.json(), created.json())
    for change in (
        {"name": "Summer"},
        {"goal": None},
        {"mode": "recommend"},
        {"status": "paused"},
        {"location_ids": []},
        {},
    ):
        patched = _send(env, "PATCH", item, body=change)
        assert patched.status_code == 200, (change, patched.text)
        _no_tenant_columns(patched.json())
        for key, value in change.items():
            assert patched.json()[key] == value
        assert _same(_send(env, "GET", item).json(), patched.json())
    final = _send(env, "GET", item).json()
    assert (
        final["name"],
        final["goal"],
        final["mode"],
        final["status"],
        final["location_ids"],
    ) == ("Summer", None, "recommend", "paused", [])
    listed = _send(env, "GET", CAMPAIGNS)
    assert listed.status_code == 200
    _no_tenant_columns(listed.json())
    paged = _send(env, "GET", f"{CAMPAIGNS}?limit=1")
    assert paged.status_code == 200 and len(paged.json()) == 1
    _no_tenant_columns(paged.json())
    viewer = _send(env, "GET", item, user=CAROL)
    assert viewer.status_code == 200
    _no_tenant_columns(viewer.json())


def test_tenant_columns_in_a_body_are_never_assigned(env) -> None:
    """Mass assignment: a body naming another tenant (or an id, or a timestamp) neither moves
    the campaign nor creates one elsewhere."""
    forged = {
        "organization_id": ORG_B,
        "workspace_id": WS_B,
        "brand_id": "b1-brand",
        "id": "forged-campaign-id",
        "created_at": "2000-01-01T00:00:00Z",
    }
    # Ignoring the unknown fields (201 / 200) and refusing them (422) both conform: no contract
    # requires either. What is required is that the tenant columns are never assigned.
    created = _send(env, "POST", CAMPAIGNS, body={"name": "Forged", **forged})
    assert created.status_code in (201, 422), created.text
    if created.status_code == 201:
        stored = _stored(env, created.json()["id"])
        assert created.json()["id"] != "forged-campaign-id"
        assert (stored["organization_id"], stored["workspace_id"], stored["brand_id"]) == (
            ORG_A,
            WS_A,
            "a1-brand",
        )
    patched = _send(env, "PATCH", f"{CAMPAIGNS}/{OWN}", body={"name": "Still mine", **forged})
    assert patched.status_code in (200, 422), patched.text
    stored = _stored(env, OWN)
    assert (stored["organization_id"], stored["workspace_id"], stored["brand_id"]) == (
        ORG_A,
        WS_A,
        "a1-brand",
    )
    assert _stored(env, "forged-campaign-id") is None
    assert env.count(Campaign, Campaign.__table__.c.workspace_id == WS_B) == 1


# --------------------------------------------------------------------------- #
# 3. One uniform 404 across workspaces and organizations
# --------------------------------------------------------------------------- #
def _normalised(r) -> tuple[int, bytes]:
    """Status and raw body with this response's own request id replaced."""
    request_id = r.json().get("error", {}).get("request_id")
    body = r.content.replace(request_id.encode(), b"<RID>") if request_id else r.content
    return r.status_code, body


#: (workspace in the path, campaign id): every way ALICE can name a campaign that is not the
#: path workspace's, including her own WS_A campaign under her other workspaces.
NOT_HERE = [
    (WS_A, "a2-campaign"),
    (WS_A, "c1-campaign"),
    (WS_A, "b1-campaign"),
    (WS_A, "no-such-campaign"),
    (WS_A, "A1-CAMPAIGN"),
    (WS_A, " a1-campaign"),
    (WS_A, "x" * 300),
    (WS_A2, OWN),
    (WS_C, OWN),
    (WS_A2, "c1-campaign"),
]


def test_a_campaign_outside_the_workspace_is_one_byte_identical_404(env) -> None:
    before = _snapshot(env)
    answers = {}
    for method, body in (
        ("GET", None),
        ("PATCH", {"name": "Hijacked"}),
        ("PATCH", {"location_ids": [OWN_LOCATION]}),
        ("DELETE", None),
    ):
        seen = set()
        for workspace, campaign in NOT_HERE:
            url = f"{API}/workspaces/{workspace}/campaigns/{campaign}"
            r = _send(env, method, url, body=body)
            seen.add(_normalised(r))
            assert r.status_code == 404, (method, workspace, campaign, r.text)
        assert len(seen) == 1, (method, seen)
        answers[method] = seen.pop()
    assert answers["GET"] == answers["PATCH"] == answers["DELETE"]
    # A viewer gets the same answer an owner does.
    viewer = _send(env, "GET", f"{CAMPAIGNS}/a2-campaign", user=CAROL)
    assert _normalised(viewer) == answers["GET"]
    assert _snapshot(env) == before


# --------------------------------------------------------------------------- #
# 4. A cross-workspace campaign_id is rejected everywhere it is accepted
# --------------------------------------------------------------------------- #
#: How the places were enumerated: every operation of the served OpenAPI document; each
#: parameter (path, query, header) and each request-body property, followed through ``$ref``,
#: ``anyOf`` / ``oneOf`` / ``allOf`` and array items, whose name contains "campaign"; plus the
#: campaigns router's own operations (its id is the path's ``item_id``).
ACCEPTS_A_CAMPAIGN = {
    ("POST", "/workspaces/{workspace_id}/scout-requests", "body", "campaign_id"): "write",
    (
        "POST",
        "/workspaces/{workspace_id}/opportunities/{opportunity_id}/briefs",
        "body",
        "campaign_id",
    ): "write",
    ("GET", "/workspaces/{workspace_id}/opportunities", "query", "campaign_id"): "filter",
    # Free-text goals, not a campaign reference.
    ("POST", "/workspaces/{workspace_id}/onboarding", "body", "profile.campaign_goals"): "text",
    ("PUT", "/workspaces/{workspace_id}/business-profile", "body", "campaign_goals"): "text",
}


def _inputs_naming_a_campaign() -> dict[tuple[str, str, str, str], None]:
    spec = app.openapi()
    components = spec.get("components", {}).get("schemas", {})

    def properties(schema: dict, seen: set[str], prefix: str = "") -> Iterator[str]:
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[-1]
            if name not in seen:
                seen.add(name)
                yield from properties(components[name], seen, prefix)
            return
        for key in ("anyOf", "oneOf", "allOf"):
            for sub in schema.get(key, []):
                yield from properties(sub, seen, prefix)
        if "items" in schema:
            yield from properties(schema["items"], seen, prefix)
        for name, sub in schema.get("properties", {}).items():
            yield f"{prefix}{name}"
            yield from properties(sub, seen, f"{prefix}{name}.")

    found = {}
    for path, item in spec["paths"].items():
        short = path.removeprefix(API)
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for parameter in operation.get("parameters", []):
                if "campaign" in parameter["name"].lower():
                    found[(method.upper(), short, parameter["in"], parameter["name"])] = None
            for content in operation.get("requestBody", {}).get("content", {}).values():
                for name in properties(content["schema"], set()):
                    if "campaign" in name.lower():
                        found[(method.upper(), short, "body", name)] = None
    return found


def test_the_places_that_accept_a_campaign_are_the_enumerated_ones() -> None:
    assert set(_inputs_naming_a_campaign()) == set(ACCEPTS_A_CAMPAIGN)
    # No Phase 5C surface (creative drafts, generation, review, approval) accepts a campaign:
    # a draft's campaign is its brief's (``creative/service.py``), never a request's.
    assert not [k for k in ACCEPTS_A_CAMPAIGN if "/creative" in k[1] or "/drafts" in k[1]]


SCOUTS = f"{API}/workspaces/{WS_A}/scout-requests"
#: Ids that name no campaign of WS_A: other tenants' campaigns and near-misses of WS_A's own.
NOT_OURS = [
    *FOREIGN,
    "no-such-campaign",
    "",
    " a1-campaign",
    "a1-campaign ",
    "A1-CAMPAIGN",
    "a1-campaign' OR '1'='1",
    "a1-opp",
]


def test_scout_request_create_rejects_every_campaign_not_of_its_workspace(env) -> None:
    before = _snapshot(env)
    for campaign in NOT_OURS:
        for body in (
            {"name": "S", "campaign_id": campaign},
            {"name": "S", "location_id": OWN_LOCATION, "campaign_id": campaign},
        ):
            r = _send(env, "POST", SCOUTS, body=body)
            assert r.status_code == 404, (campaign, r.text)
            assert r.json()["error"]["message"] == "Campaign not found in this workspace."
    assert _snapshot(env) == before
    ok = _send(env, "POST", SCOUTS, body={"name": "S", "campaign_id": OWN})
    assert ok.status_code == 201 and ok.json()["campaign_id"] == OWN


def test_a_deleted_campaign_is_not_found_for_a_new_scout(env) -> None:
    created = _send(env, "POST", CAMPAIGNS, body={"name": "Gone"}).json()
    assert _send(env, "DELETE", f"{CAMPAIGNS}/{created['id']}").status_code == 204
    r = _send(env, "POST", SCOUTS, body={"name": "S", "campaign_id": created["id"]})
    assert r.status_code == 404


def test_a_scout_request_edit_cannot_set_a_campaign_at_all(env) -> None:
    before = _snapshot(env)
    for campaign in (*FOREIGN, OWN, None):
        r = _send(env, "PUT", f"{SCOUTS}/a1-scout", body={"name": "S2", "campaign_id": campaign})
        assert r.status_code == 422, (campaign, r.text)
    assert _snapshot(env) == before


class _SpyMock(MockLLMProvider):
    calls = 0

    def generate(self, request, prompt_name, prompt_version):
        _SpyMock.calls += 1
        return super().generate(request, prompt_name, prompt_version)


def test_brief_create_rejects_every_campaign_not_of_its_workspace(env, monkeypatch) -> None:
    """Refused before the model is called: no call, no usage row, no brief."""
    monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", True)
    _SpyMock.calls = 0
    app.dependency_overrides[get_llm_service] = lambda: LLMService(
        _SpyMock(seed="w4"), sleep=lambda _s: None, settings=offline_settings()
    )
    url = f"{API}/workspaces/{WS_A}/opportunities/a1-opp/briefs"
    try:
        with offline_llm():
            before = _snapshot(env)
            for campaign in NOT_OURS:
                body = {"intelligence_record_id": "a1-intel", "campaign_id": campaign}
                r = _send(env, "POST", url, body=body)
                assert r.status_code in (404, 422), (campaign, r.text)
                if r.status_code == 422:  # the body's id grammar refuses it first
                    assert r.json()["error"]["code"] == "validation_error", campaign
                else:
                    assert r.json()["error"]["message"] == "Campaign not found in this workspace."
            assert _SpyMock.calls == 0
            assert env.count(LLMUsageEvent) == 0 and env.count(RecommendationBrief) == 0
            assert _snapshot(env) == before
    finally:
        app.dependency_overrides.pop(get_llm_service, None)


def test_the_opportunity_feed_filter_cannot_reach_another_workspace(env) -> None:
    """The one read that takes a campaign is a filter over the caller's own workspace: another
    workspace's campaign id matches nothing there, so it reveals nothing (and writes nothing)."""
    feed = f"{API}/workspaces/{WS_A}/opportunities"
    own = _send(env, "GET", f"{feed}?campaign_id={OWN}")
    assert own.status_code == 200 and [o["id"] for o in own.json()] == ["a1-opp"]
    for campaign in FOREIGN:
        r = _send(env, "GET", f"{feed}?campaign_id={campaign}")
        assert r.status_code == 200 and r.json() == [], campaign


def test_every_seeded_foreign_campaign_exists_in_its_own_workspace(env) -> None:
    """The probes above are scope refusals, not misses: each foreign id is a real campaign."""
    with Session(bind=env.witness) as s:
        homes = dict(s.execute(select(Campaign.id, Campaign.workspace_id)).all())
    for campaign, workspace in FOREIGN.items():
        assert homes[campaign] == workspace


# --------------------------------------------------------------------------- #
# 5. O9: every campaign write commits before the response starts
# --------------------------------------------------------------------------- #
class _ObserveResponseStart:
    """An ASGI wrapper that runs ``probe`` when the app sends ``http.response.start``: the
    moment the client could first see the answer."""

    def __init__(self, inner, probe) -> None:
        self.inner, self.probe, self.seen = inner, probe, []

    async def __call__(self, scope, receive, send):
        async def observed(message):
            if message["type"] == "http.response.start" and scope["type"] == "http":
                self.seen.append((message["status"], self.probe()))
            await send(message)

        await self.inner(scope, receive, observed)


def test_every_campaign_write_is_durable_when_the_response_starts(env) -> None:
    name = f"o9-{uuid.uuid4().hex[:8]}"

    def committed_by_name() -> list[str]:
        with env.witness.connect() as conn:
            return list(conn.execute(select(Campaign.id).where(Campaign.name == name)).scalars())

    observer = _ObserveResponseStart(app, committed_by_name)
    client = TestClient(observer)

    created = _send(
        env, "POST", CAMPAIGNS, body={"name": name, "location_ids": [OWN_LOCATION]}, client=client
    )
    assert created.status_code == 201, created.text
    campaign_id = created.json()["id"]
    assert observer.seen[-1] == (201, [campaign_id])

    def members() -> list[str]:
        with env.witness.connect() as conn:
            return list(
                conn.execute(
                    select(CampaignLocation.location_id).where(
                        CampaignLocation.campaign_id == campaign_id
                    )
                ).scalars()
            )

    observer.probe = members
    patched = _send(
        env, "PATCH", f"{CAMPAIGNS}/{campaign_id}", body={"location_ids": []}, client=client
    )
    assert patched.status_code == 200
    assert observer.seen[-1] == (200, [])

    observer.probe = lambda: _stored(env, campaign_id)
    deleted = _send(env, "DELETE", f"{CAMPAIGNS}/{campaign_id}", client=client)
    assert deleted.status_code == 204
    assert observer.seen[-1] == (204, None)


def test_a_failed_commit_of_a_delete_never_answers_success(env) -> None:
    headers = env.auth(ALICE)
    before = _snapshot(env)

    def refuse(_session) -> None:
        raise RuntimeError("injected commit failure")

    event.listen(Session, "before_commit", refuse)
    try:
        with pytest.raises(RuntimeError, match="injected commit failure"):
            env.client.delete(f"{CAMPAIGNS}/{OWN}", headers=headers)
    finally:
        event.remove(Session, "before_commit", refuse)
    assert _snapshot(env) == before
    assert _stored(env, OWN) is not None
    # And a refused cross-workspace write never reached a commit at all.
    assert _stored(env, "a2-campaign") is not None
    assert env.count(ScoutRequest) >= 1
