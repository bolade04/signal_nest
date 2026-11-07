"""Project Phase 5D, lane 5D-W1: the dedicated campaigns router (exit criteria 1-3).

The 5D plan gives this module to lane 5D-W2 as a cross-authored slice (§5). W2 finished
without it (no W1 code existed), so W1 writes it, disclosed as a deviation; independence comes
from the independent review and the 5D-W4 battery.

* **Projection and scoping** (exit criterion 2; live change 8(c)): every campaign answer has
  exactly the :class:`~app.campaign_context.schemas.CampaignOut` keys, so no tenant column;
  ``GET /campaigns/{item_id}`` answers a campaign of the caller's workspace, and a campaign of
  another workspace of the same organization, of another organization (the caller a member of
  both or of one) or no campaign at all answers one identical ``404``.
* **Membership** (exit criterion 1; integration item I-CF3-2): create and update write the
  ``campaign_locations`` join table in the request's transaction, reconciled both ways
  (missing pairs inserted, absent ones deleted); every location is validated against the
  campaign's workspace, fail closed, and a refused request writes nothing; answers read the join
  table, never the JSON array, which no request writes directly (it is overwritten with the
  reconciled membership, its mirror); a failed commit leaves nothing; on PostgreSQL a reconcile
  waits for another one in flight on the same campaign and then reconciles against what it
  committed.
* **Validation closure** (exit criterion 2): ``mode`` and ``status`` outside their closed
  vocabularies, or ``null``, are refused at the schema on create and update; get and update
  work end to end.
* **O9**: every campaign write route commits before it answers (``WriteDb``).
* **Scout-request campaign scope** (exit criterion 3; live change 8(a)): the definition audit's
  asymmetry -- the location of a new Scout was checked against its workspace, its campaign was
  stored as sent -- is reproduced and now fails closed with brief capture's rule and message.

The world is :func:`app.tests._privacy_support.privacy_world` (organization A with workspaces
``WS_A`` / ``WS_A2``, B with ``WS_B``, C with ``WS_C``; ALICE owns A and C, CAROL is A's viewer,
BOB owns B), on SQLite and, when ``TEST_POSTGRES_URL`` is set, on PostgreSQL (unset FAILS in CI
and skips only on a developer machine). Each seeded campaign holds its location in the JSON
array and no join-table row, as a row written before the CF-3 back-fill would.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.routing import iter_route_contexts
from sqlalchemy import event, select, text, update
from sqlalchemy.orm import Session

from app.campaign_context import campaigns as campaigns_module
from app.campaign_context.models import Campaign, CampaignLocation
from app.campaign_context.schemas import CampaignOut
from app.db.request_tx import commit_before_response
from app.locations.models import BusinessLocation
from app.main import app
from app.scouting_requests.models import ScoutRequest
from app.tests._auth2_support import require_postgres
from app.tests._privacy_support import (
    ALICE,
    API,
    BOB,
    CAROL,
    ORG_A,
    WS_A,
    WS_A2,
    WS_C,
    PrivacyEnv,
    privacy_world,
)

CAMPAIGNS = f"{API}/workspaces/{WS_A}/campaigns"
ITEM_404 = (404, "not_found", "Item not found in this workspace.")
LOCATION_404 = (404, "not_found", "Location not found in this workspace.")
DEACTIVATED_422 = (422, "validation_error", "A deactivated location cannot be added.")
CAMPAIGN_404 = (404, "not_found", "Campaign not found in this workspace.")
ANSWER_KEYS = set(CampaignOut.model_fields)
OWN_CAMPAIGN, OWN_LOCATION = "a1-campaign", "a1-loc"
#: Campaigns and a location the caller must never reach from WS_A.
SAME_ORG_CAMPAIGN, OTHER_ORG_MEMBER_CAMPAIGN, OTHER_ORG_CAMPAIGN = (
    "a2-campaign",  # WS_A2, organization A
    "c1-campaign",  # WS_C, organization C (ALICE owns it too)
    "b1-campaign",  # WS_B, organization B (ALICE is no member)
)


@pytest.fixture(params=["sqlite", "postgresql"])
def env(request, tmp_path) -> Iterator[PrivacyEnv]:
    if request.param == "sqlite":
        with privacy_world(tmp_path / "campaign-router.db") as e:
            yield e
        return
    require_postgres()
    with privacy_world(None) as e:  # pragma: no cover - gated on PostgreSQL
        yield e


@pytest.fixture
def pg_env() -> Iterator[PrivacyEnv]:
    """The same world on PostgreSQL only (a row-lock race has no SQLite counterpart: SQLite runs
    one writer at a time)."""
    require_postgres()
    with privacy_world(None) as e:  # pragma: no cover - gated on PostgreSQL
        yield e


def _instant(value: str) -> datetime:
    """A timestamp of an answer as an instant: SQLite hands back naive UTC, PostgreSQL aware."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _same(answer: dict, other: dict) -> bool:
    stamps = ("created_at", "updated_at")
    return {k: v for k, v in answer.items() if k not in stamps} == {
        k: v for k, v in other.items() if k not in stamps
    } and all(_instant(answer[k]) == _instant(other[k]) for k in stamps)


def _snapshot(env: PrivacyEnv) -> dict[str, tuple]:
    """Every row of every table but ``auth_sessions``, which each request's fresh bearer
    (:meth:`PrivacyEnv.auth`) writes to; audit rows included."""
    return env.snapshot(lambda table, _row: table.name != "auth_sessions")


def _error(r) -> tuple[int, str, str]:
    body = r.json()["error"]
    return r.status_code, body["code"], body["message"]


def _send(env: PrivacyEnv, method: str, url: str, user: str = ALICE, body=None):
    kwargs: dict = {"headers": env.auth(user)}
    if body is not None:
        kwargs["json"] = body
    return env.client.request(method, url, **kwargs)


def _memberships(env: PrivacyEnv, campaign_id: str) -> list[dict]:
    return env.rows(CampaignLocation, CampaignLocation.__table__.c.campaign_id == campaign_id)


def _member_ids(env: PrivacyEnv, campaign_id: str) -> list[str]:
    return sorted(r["location_id"] for r in _memberships(env, campaign_id))


def _stored(env: PrivacyEnv, campaign_id: str) -> dict | None:
    rows = env.rows(Campaign, Campaign.__table__.c.id == campaign_id)
    return rows[0] if rows else None


def _add_locations(env: PrivacyEnv, ws: str, *names: str, active: bool = True) -> list[str]:
    """Locations of ``ws`` written directly (the location routes geocode; not under test)."""
    with Session(bind=env.witness) as s:
        anchor = s.get(BusinessLocation, {WS_A: "a1-loc", WS_A2: "a2-loc"}[ws])
        rows = [
            BusinessLocation(
                organization_id=anchor.organization_id,
                workspace_id=ws,
                brand_id=anchor.brand_id,
                name=name,
                is_active=active,
            )
            for name in names
        ]
        s.add_all(rows)
        s.commit()
        return [r.id for r in rows]


def _deactivate(env: PrivacyEnv, location_id: str) -> None:
    with env.witness.begin() as conn:
        conn.execute(
            update(BusinessLocation.__table__)
            .where(BusinessLocation.__table__.c.id == location_id)
            .values(is_active=False)
        )


def _create(env: PrivacyEnv, location_ids: list[str], **extra) -> dict:
    r = _send(
        env, "POST", CAMPAIGNS, body={"name": "Spring", "location_ids": location_ids, **extra}
    )
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# 1. The answer is projected; one campaign is read in its own workspace only
# --------------------------------------------------------------------------- #
def test_every_campaign_answer_is_the_projection(env) -> None:
    created = _create(env, [OWN_LOCATION])
    assert set(created) == ANSWER_KEYS
    assert not {"organization_id", "workspace_id"} & ANSWER_KEYS
    item = f"{CAMPAIGNS}/{created['id']}"
    answers = [
        _send(env, "GET", item),
        _send(env, "PATCH", item, body={"goal": "More visits"}),
        _send(env, "GET", item, user=CAROL),
    ]
    for r in answers:
        assert r.status_code == 200, r.text
        assert set(r.json()) == ANSWER_KEYS, r.json()
    listed = _send(env, "GET", CAMPAIGNS)
    assert listed.status_code == 200 and listed.json()
    assert all(set(row) == ANSWER_KEYS for row in listed.json())
    # The tenant columns are still attached server-side; they are only not echoed.
    stored = _stored(env, created["id"])
    assert (stored["organization_id"], stored["workspace_id"]) == (ORG_A, WS_A)


def test_get_one_answers_the_campaign_as_create_and_list_do(env) -> None:
    created = _create(env, [OWN_LOCATION], goal="Footfall", mode="grouped", status="draft")
    one = _send(env, "GET", f"{CAMPAIGNS}/{created['id']}")
    assert one.status_code == 200, one.text
    assert _same(one.json(), created)
    (listed,) = [row for row in _send(env, "GET", CAMPAIGNS).json() if row["id"] == created["id"]]
    assert _same(listed, created)


@pytest.mark.parametrize(
    "foreign",
    [SAME_ORG_CAMPAIGN, OTHER_ORG_MEMBER_CAMPAIGN, OTHER_ORG_CAMPAIGN, "no-such-campaign"],
)
def test_a_campaign_outside_the_workspace_is_not_found_uniformly(env, foreign) -> None:
    """GET, PATCH and DELETE of a campaign that is not WS_A's answer the same 404 as a campaign
    that does not exist, and the campaign is untouched."""
    before = _stored(env, foreign)
    for method, body in (("GET", None), ("PATCH", {"name": "Hijacked"}), ("DELETE", None)):
        r = _send(env, method, f"{CAMPAIGNS}/{foreign}", body=body)
        assert _error(r) == ITEM_404, (method, r.text)
    assert _stored(env, foreign) == before


def test_members_read_editors_write_outsiders_are_refused(env) -> None:
    item = f"{CAMPAIGNS}/{OWN_CAMPAIGN}"
    assert _send(env, "GET", item, user=CAROL).status_code == 200  # a viewer reads
    for method, body in (("POST", {"name": "x"}), ("PATCH", {"name": "x"}), ("DELETE", None)):
        url = CAMPAIGNS if method == "POST" else item
        assert _send(env, method, url, user=CAROL, body=body).status_code == 403, method
    outsider = _send(env, "GET", item, user=BOB)
    assert outsider.status_code == 403 and outsider.json()["error"]["code"] == "permission_denied"


# --------------------------------------------------------------------------- #
# 2. Membership is the campaign_locations join table (I-CF3-2)
# --------------------------------------------------------------------------- #
def test_create_writes_the_memberships_and_mirrors_them(env) -> None:
    second, third = _add_locations(env, WS_A, "Second", "Third")
    created = _create(env, [third, OWN_LOCATION, second])
    expected = sorted([third, OWN_LOCATION, second])
    assert created["location_ids"] == expected  # a set, answered in location-id order
    rows = _memberships(env, created["id"])
    assert sorted(r["location_id"] for r in rows) == expected
    assert {(r["organization_id"], r["workspace_id"]) for r in rows} == {(ORG_A, WS_A)}
    # The retained JSON array is the reconciled membership, not the request as sent.
    assert _stored(env, created["id"])["location_ids"] == expected


def test_update_reconciles_both_ways_in_place(env) -> None:
    l2, l3 = _add_locations(env, WS_A, "L2", "L3")
    created = _create(env, [OWN_LOCATION, l2])
    kept_row = next(r for r in _memberships(env, created["id"]) if r["location_id"] == l2)
    r = _send(env, "PATCH", f"{CAMPAIGNS}/{created['id']}", body={"location_ids": [l3, l2]})
    assert r.status_code == 200, r.text
    assert r.json()["location_ids"] == sorted([l2, l3])
    assert _member_ids(env, created["id"]) == sorted([l2, l3])  # the stale pair is deleted
    # The membership that stays is the same row (not deleted and re-inserted).
    assert next(r for r in _memberships(env, created["id"]) if r["location_id"] == l2) == kept_row
    assert _stored(env, created["id"])["location_ids"] == sorted([l2, l3])


def test_update_to_an_empty_list_removes_every_membership(env) -> None:
    created = _create(env, [OWN_LOCATION])
    r = _send(env, "PATCH", f"{CAMPAIGNS}/{created['id']}", body={"location_ids": []})
    assert r.status_code == 200 and r.json()["location_ids"] == []
    assert _memberships(env, created["id"]) == []
    assert _stored(env, created["id"])["location_ids"] == []


def test_an_update_without_location_ids_leaves_the_memberships_alone(env) -> None:
    created = _create(env, [OWN_LOCATION])
    before = (_memberships(env, created["id"]), _stored(env, created["id"])["location_ids"])
    r = _send(env, "PATCH", f"{CAMPAIGNS}/{created['id']}", body={"name": "Renamed"})
    assert r.status_code == 200 and r.json()["location_ids"] == [OWN_LOCATION]
    assert (_memberships(env, created["id"]), _stored(env, created["id"])["location_ids"]) == before


@pytest.mark.parametrize("foreign", ["a2-loc", "c1-loc", "b1-loc", "not-a-location"])
def test_a_location_outside_the_workspace_is_refused_and_nothing_is_written(env, foreign) -> None:
    """Another workspace's location (same organization, an organization the caller also owns,
    a foreign organization) or no location at all: one 404, no campaign, no membership."""
    snapshot = _snapshot(env)
    r = _send(env, "POST", CAMPAIGNS, body={"name": "Bad", "location_ids": [OWN_LOCATION, foreign]})
    assert _error(r) == LOCATION_404, r.text
    assert _snapshot(env) == snapshot
    created = _create(env, [OWN_LOCATION])
    before = (_stored(env, created["id"]), _memberships(env, created["id"]))
    r = _send(
        env,
        "PATCH",
        f"{CAMPAIGNS}/{created['id']}",
        body={"name": "Renamed", "location_ids": [foreign]},
    )
    assert _error(r) == LOCATION_404, r.text
    assert (_stored(env, created["id"]), _memberships(env, created["id"])) == before


def test_a_deactivated_location_may_stay_a_member_but_is_never_added(env) -> None:
    closing, closed = _add_locations(env, WS_A, "Closing", "Closed")
    _deactivate(env, closed)
    created = _create(env, [closing])
    _deactivate(env, closing)
    item = f"{CAMPAIGNS}/{created['id']}"
    kept = _send(env, "PATCH", item, body={"location_ids": [closing, OWN_LOCATION]})
    assert kept.status_code == 200, kept.text
    before = _memberships(env, created["id"])
    added = _send(env, "PATCH", item, body={"location_ids": [closing, OWN_LOCATION, closed]})
    assert _error(added) == DEACTIVATED_422
    assert _memberships(env, created["id"]) == before
    assert _error(_send(env, "POST", CAMPAIGNS, body={"name": "x", "location_ids": [closed]})) == (
        DEACTIVATED_422
    )


def test_answers_read_the_join_table_never_the_json_array(env) -> None:
    """The seeded campaign holds its location only in the JSON array; the answer reads the join
    table. Then the two disagree the other way round: the answer still follows the join table."""
    item = f"{CAMPAIGNS}/{OWN_CAMPAIGN}"
    assert _stored(env, OWN_CAMPAIGN)["location_ids"] == [OWN_LOCATION]
    assert _send(env, "GET", item).json()["location_ids"] == []
    (other,) = _add_locations(env, WS_A, "Joined")
    with Session(bind=env.witness) as s:
        s.add(
            CampaignLocation(
                organization_id=ORG_A,
                workspace_id=WS_A,
                campaign_id=OWN_CAMPAIGN,
                location_id=other,
            )
        )
        s.commit()
    assert _send(env, "GET", item).json()["location_ids"] == [other]
    (listed,) = [r for r in _send(env, "GET", CAMPAIGNS).json() if r["id"] == OWN_CAMPAIGN]
    assert listed["location_ids"] == [other]
    renamed = _send(env, "PATCH", item, body={"name": "Renamed"})
    assert renamed.json()["location_ids"] == [other]
    assert _stored(env, OWN_CAMPAIGN)["location_ids"] == [OWN_LOCATION]  # untouched by a rename


def test_delete_removes_the_campaign_and_its_memberships(env) -> None:
    created = _create(env, [OWN_LOCATION])
    with env.witness.begin() as conn:  # a Scout of the campaign keeps existing, unlinked
        conn.execute(
            update(ScoutRequest.__table__)
            .where(ScoutRequest.__table__.c.id == "a1-scout")
            .values(campaign_id=created["id"])
        )
    r = _send(env, "DELETE", f"{CAMPAIGNS}/{created['id']}")
    assert r.status_code == 204
    assert _stored(env, created["id"]) is None
    assert _memberships(env, created["id"]) == []
    (scout,) = env.rows(ScoutRequest, ScoutRequest.__table__.c.id == "a1-scout")
    assert scout["campaign_id"] is None


def test_a_failed_commit_leaves_no_campaign_change_and_no_membership(env) -> None:
    created = _create(env, [OWN_LOCATION])
    (l2,) = _add_locations(env, WS_A, "L2")
    headers = env.auth(ALICE)
    snapshot = _snapshot(env)
    flushed: list[str] = []

    def record(session, _context) -> None:
        flushed.extend(type(o).__name__ for o in (*session.new, *session.dirty, *session.deleted))

    def refuse(session) -> None:
        raise RuntimeError("injected commit failure")

    event.listen(Session, "after_flush", record)
    event.listen(Session, "before_commit", refuse)
    try:
        with pytest.raises(RuntimeError, match="injected commit failure"):
            env.client.patch(
                f"{CAMPAIGNS}/{created['id']}",
                headers=headers,
                json={"name": "Renamed", "location_ids": [l2]},
            )
        assert {"Campaign", "CampaignLocation"} <= set(flushed)  # the route ran to its commit
        flushed.clear()
        with pytest.raises(RuntimeError, match="injected commit failure"):
            env.client.post(CAMPAIGNS, headers=headers, json={"name": "New", "location_ids": [l2]})
        assert {"Campaign", "CampaignLocation"} <= set(flushed)
    finally:
        event.remove(Session, "before_commit", refuse)
        event.remove(Session, "after_flush", record)
    assert _snapshot(env) == snapshot


def test_a_reconcile_waits_for_one_in_flight_and_reconciles_what_it_committed(pg_env) -> None:
    """PostgreSQL: a request's reconcile takes the campaign's row lock first. While another
    transaction holds it and adds a membership, the request waits; then it reads the committed
    membership and adds nothing twice (without the lock it would insert the same pair and fail
    on ``uq_campaign_location``)."""
    env = pg_env
    created = _create(env, [])
    (l2,) = _add_locations(env, WS_A, "L2")
    headers = env.auth(ALICE)
    outcome: dict = {}

    def patch() -> None:
        try:
            outcome["response"] = env.client.patch(
                f"{CAMPAIGNS}/{created['id']}", headers=headers, json={"location_ids": [l2]}
            )
        except Exception as exc:  # noqa: BLE001 - reported below
            outcome["error"] = exc

    with env.witness.connect() as holder:
        holder.execute(
            text("SELECT id FROM campaigns WHERE id = :id FOR NO KEY UPDATE"), {"id": created["id"]}
        )
        holder.execute(
            text(
                "INSERT INTO campaign_locations "
                "(id, organization_id, workspace_id, campaign_id, location_id, created_at) "
                "VALUES (:id, :org, :ws, :campaign, :loc, now())"
            ),
            {
                "id": "held-membership",
                "org": ORG_A,
                "ws": WS_A,
                "campaign": created["id"],
                "loc": l2,
            },
        )
        worker = threading.Thread(target=patch)
        worker.start()
        deadline = time.monotonic() + 30
        waiting = 0
        while time.monotonic() < deadline and not waiting:
            with env.engine.connect() as probe:
                waiting = probe.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
            time.sleep(0.05)
        assert waiting, "the request never waited for the in-flight reconcile"
        holder.commit()
    worker.join(30)
    assert not worker.is_alive()
    assert "error" not in outcome, outcome.get("error")
    r = outcome["response"]
    assert r.status_code == 200, r.text
    assert r.json()["location_ids"] == [l2]
    assert [m["id"] for m in _memberships(env, created["id"])] == ["held-membership"]


# --------------------------------------------------------------------------- #
# 3. Validation closure: closed vocabularies; get and update end to end
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body",
    [
        {"mode": "everywhere"},
        {"mode": "Grouped"},
        {"status": "running"},
        {"status": "ACTIVE"},
        {"mode": None},
        {"status": None},
        {"name": None},
        {"location_ids": None},
    ],
)
def test_mode_and_status_outside_their_vocabularies_are_refused(env, body) -> None:
    snapshot = _snapshot(env)
    created = _send(env, "POST", CAMPAIGNS, body={"name": "x", **body})
    assert created.status_code == 422, created.text
    patched = _send(env, "PATCH", f"{CAMPAIGNS}/{OWN_CAMPAIGN}", body=body)
    assert patched.status_code == 422, patched.text
    assert patched.json()["error"]["code"] == "validation_error"
    assert _snapshot(env) == snapshot


def test_get_and_update_work_end_to_end(env) -> None:
    created = _create(env, [OWN_LOCATION])
    assert (created["mode"], created["status"]) == ("per_location", "active")
    item = f"{CAMPAIGNS}/{created['id']}"
    changes = {"name": "Summer", "goal": None, "mode": "recommend", "status": "paused"}
    patched = _send(env, "PATCH", item, body=changes)
    assert patched.status_code == 200, patched.text
    read = _send(env, "GET", item).json()
    assert _same(read, patched.json())
    assert {k: read[k] for k in changes} == changes
    assert (read["id"], read["location_ids"]) == (created["id"], [OWN_LOCATION])
    stored = _stored(env, created["id"])
    assert {k: stored[k] for k in changes} == changes


# --------------------------------------------------------------------------- #
# 4. O9: every campaign write route commits before it answers
# --------------------------------------------------------------------------- #
def test_every_campaign_route_is_the_dedicated_routers_and_writes_commit_first() -> None:
    seen = {}
    for rc in iter_route_contexts(app.routes):
        if not rc.path.startswith(f"{API}/workspaces/{{workspace_id}}/campaigns"):
            continue
        deps, stack = [], [rc.dependant]
        while stack:
            d = stack.pop()
            deps.append(d)
            stack.extend(d.dependencies)
        helpers = [d for d in deps if d.call is commit_before_response]
        for method in set(rc.methods) - {"HEAD", "OPTIONS"}:
            seen[(method, rc.path.removeprefix(API))] = (
                rc.dependant.call.__module__,
                [d.scope for d in helpers],
            )
    assert seen == {
        ("GET", "/workspaces/{workspace_id}/campaigns"): (campaigns_module.__name__, []),
        ("POST", "/workspaces/{workspace_id}/campaigns"): (campaigns_module.__name__, ["function"]),
        ("GET", "/workspaces/{workspace_id}/campaigns/{item_id}"): (campaigns_module.__name__, []),
        ("PATCH", "/workspaces/{workspace_id}/campaigns/{item_id}"): (
            campaigns_module.__name__,
            ["function"],
        ),
        ("DELETE", "/workspaces/{workspace_id}/campaigns/{item_id}"): (
            campaigns_module.__name__,
            ["function"],
        ),
    }


# --------------------------------------------------------------------------- #
# 5. Scout requests: the campaign is checked like the location (exit criterion 3, 8(a))
# --------------------------------------------------------------------------- #
SCOUTS = f"{API}/workspaces/{WS_A}/scout-requests"


@pytest.mark.parametrize(
    "foreign",
    [SAME_ORG_CAMPAIGN, OTHER_ORG_MEMBER_CAMPAIGN, OTHER_ORG_CAMPAIGN, "no-such-campaign"],
)
def test_a_scout_request_refuses_a_campaign_outside_its_workspace(env, foreign) -> None:
    """The definition audit's asymmetry: creating a Scout validated its location against the
    workspace and stored its campaign as sent. Both are refused alike now, and the campaign
    refusal is brief capture's (``briefs/service.py``): one 404 whatever the campaign is."""
    snapshot = _snapshot(env)
    location_refused = _send(env, "POST", SCOUTS, body={"name": "S", "location_id": "a2-loc"})
    assert _error(location_refused) == LOCATION_404
    r = _send(env, "POST", SCOUTS, body={"name": "S", "campaign_id": foreign})
    assert _error(r) == CAMPAIGN_404, r.text
    r = _send(
        env, "POST", SCOUTS, body={"name": "S", "location_id": OWN_LOCATION, "campaign_id": foreign}
    )
    assert _error(r) == CAMPAIGN_404, r.text
    assert _snapshot(env) == snapshot


def test_a_scout_request_keeps_its_own_workspaces_campaign_or_none(env) -> None:
    for campaign_id in (OWN_CAMPAIGN, None):
        r = _send(
            env,
            "POST",
            SCOUTS,
            body={"name": "S", "location_id": OWN_LOCATION, "campaign_id": campaign_id},
        )
        assert r.status_code == 201, r.text
        assert r.json()["campaign_id"] == campaign_id
        (row,) = env.rows(ScoutRequest, ScoutRequest.__table__.c.id == r.json()["id"])
        assert (row["workspace_id"], row["campaign_id"]) == (WS_A, campaign_id)


def test_the_world_is_what_the_module_assumes(env) -> None:
    """Guards the fixtures the probes above lean on."""
    with Session(bind=env.witness) as s:
        homes = dict(
            s.execute(
                select(Campaign.id, Campaign.workspace_id).where(
                    Campaign.id.in_([OWN_CAMPAIGN, SAME_ORG_CAMPAIGN, OTHER_ORG_MEMBER_CAMPAIGN])
                )
            ).all()
        )
        assert homes == {
            OWN_CAMPAIGN: WS_A,
            SAME_ORG_CAMPAIGN: WS_A2,
            OTHER_ORG_MEMBER_CAMPAIGN: WS_C,
        }
        assert s.scalar(select(CampaignLocation.id).limit(1)) is None  # no back-fill ran here
    r = _send(env, "GET", f"{API}/workspaces/{WS_C}/campaigns/{OTHER_ORG_MEMBER_CAMPAIGN}")
    assert r.status_code == 200  # ALICE can read it in its own workspace, so 404 above is scope
