"""Project Phase 5E (lane 5E-W4): in-app notification isolation and the O-12(B) gate (exit 2, 5).

The round's one new cross-USER IDOR class (5e exit 2(d)). Proven through the real routes:

* **Cross-user:** in one workspace, user B never sees user A's notification in a feed, and
  marking it read answers the 404 an unknown id answers (same code, same message) and leaves
  it unread. No request can name a user: the preferences body refuses a ``user_id`` (422) and
  a query ``user_id`` is ignored, so B's preference change writes B's rows only and never
  changes what A receives.
* **Cross-workspace:** A's notification id under another workspace of the same organization
  is the same 404; feeds and preferences are per workspace.
* **Membership stays 403** on every route, the reflection included, whether the capability
  is on or off (5e:59-64: "Do not 'fix' the 403 into a 404").
* **Mark-read** is idempotent and keeps the first ``read_at``, also when a request that loaded
  the row unread finishes after another request marked it.
* **O-12(B):** while ``notifications_analytics`` does not resolve enabled for the workspace,
  the feed, mark-read and both preference routes answer 503 for every role (before the body
  is validated) and write nothing, and the reflection answers 200 ``{"enabled": false}``. An
  enable override opens one workspace with the flag off; a disable override closes one with
  the flag on; the safety ceiling outranks both. The emitter writes no row while dark (and
  does not consume its recipient list): an export with O-12(B) off commits without a
  notification; with it on, the draft's author and the approver are told, never the exporter.

The world is the 5C harness on SQLite and, with ``TEST_POSTGRES_URL``, PostgreSQL at head (the
override CHECK admits ``notifications_analytics`` there since revision 53fdde95354a).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, update

import app.capabilities.resolver as resolver_mod
from app.approvals.models import Approval
from app.briefs.models import RecommendationBrief
from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import Capability
from app.core.actor_snapshot import actor_snapshot
from app.core.config import get_settings
from app.core.enums import NotificationEventCode
from app.creative.models import ContentReview, CreativeDraft
from app.exports.models import Export
from app.notifications.models import Notification, NotificationPreference
from app.notifications.service import emit_notifications, mark_read
from app.organizations.models import User
from app.tests._creative_support import (
    ALL_ROLES,
    CreativeWorld,
    creative_world,
    postgres_creative_world,
)

CAP = "notifications_analytics"
DARK = {"code": "capability_unavailable", "message": "Notifications are not available yet."}
NOT_FOUND = {"code": "not_found", "message": "Notification not found."}
MEMBERSHIP = {"code": "permission_denied", "message": "You are not a member of this organization."}
E = NotificationEventCode


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def world(request, tmp_path_factory) -> Iterator[CreativeWorld]:
    if request.param == "sqlite":
        with creative_world(tmp_path_factory.mktemp("w4ni") / "n.db", "w4ni") as w:
            yield w
        return
    with postgres_creative_world("w4ni") as w:  # skips unless TEST_POSTGRES_URL is set
        yield w


@pytest.fixture(autouse=True)
def flags_off(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "notifications_analytics_enabled", False)
    monkeypatch.setattr(settings, "content_export_enabled", False)


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
    """A fresh workspace of organization A with notifications ON by override."""
    ws = world.workspace(f"n {request.node.name[:40]}")
    set_cap(world, ws, True)
    return ws


def seed(world: CreativeWorld, ws: str, role: str, n: int = 1, **values) -> list[str]:
    """``n`` notifications for ``role``'s user in ``ws`` (direct rows: no emitter fires yet)."""
    ids = []
    with world.session() as s:
        for _ in range(n):
            note = Notification(
                id=uuid.uuid4().hex, organization_id=world.org_of(ws), workspace_id=ws,
                user_id=world.owner_id(ws) if ws == world.ws_b else world.users[role],
                event_code=values.get("event_code", "brief.created"),
                subject_id=values.get("subject_id", uuid.uuid4().hex),
            )
            s.add(note)
            ids.append(note.id)
    return ids


def base(ws: str) -> str:
    return f"/workspaces/{ws}"


def error(r) -> dict:
    return {k: v for k, v in r.json()["error"].items() if k != "request_id"}


def notes(world: CreativeWorld, **where) -> list[dict]:
    return world.rows(Notification, **where)


def preferences(world: CreativeWorld, ws: str) -> list[tuple]:
    return sorted(
        (r["user_id"], r["event_code"], r["enabled"])
        for r in world.rows(NotificationPreference, workspace_id=ws)
    )


# =========================================================================== #
# Cross-user
# =========================================================================== #
def test_marking_another_users_notification_is_the_unknown_id_404(world, ws):
    theirs = seed(world, ws, "marketer")[0]
    before = world.row(Notification, theirs)
    unknown = world.send("POST", f"{base(ws)}/notifications/{uuid.uuid4().hex}/read", "viewer")
    assert unknown.status_code == 404 and error(unknown) == NOT_FOUND
    for role in ALL_ROLES:
        if role == "marketer":
            continue
        r = world.send("POST", f"{base(ws)}/notifications/{theirs}/read", role)
        assert r.status_code == 404, (role, r.text)
        assert error(r) == error(unknown)
    assert world.row(Notification, theirs) == before
    own = world.send("POST", f"{base(ws)}/notifications/{theirs}/read", "marketer")
    assert own.status_code == 200 and own.json()["id"] == theirs  # the control


def test_each_feed_lists_only_the_session_users_own_rows(world, ws):
    mine = {role: set(seed(world, ws, role, n=i + 1)) for i, role in enumerate(ALL_ROLES)}
    seen: dict[str, set[str]] = {}
    for role in ALL_ROLES:
        # A query that tries to name another user is ignored: the user is the session's.
        for query in ("", f"?user_id={world.users['owner']}", f"?user={world.users['owner']}"):
            r = world.send("GET", f"{base(ws)}/notifications{query}", role)
            assert r.status_code == 200, (role, r.text)
            body = r.json()
            ids = {i["id"] for i in body["items"]}
            assert ids == mine[role], (role, query)
            assert body["total"] == len(mine[role]) and body["unread"] == len(mine[role])
            assert all(set(i) == {"id", "event_code", "subject_id", "created_at", "read_at"}
                       for i in body["items"])  # fmt: skip
            seen[role] = ids
    for a in ALL_ROLES:  # no two users' feeds share a row
        for b in ALL_ROLES:
            if a != b:
                assert not seen[a] & seen[b]


def test_another_users_read_does_not_change_my_unread_count(world, ws):
    seed(world, ws, "reviewer", n=2)
    others = seed(world, ws, "admin", n=1)
    world.send("POST", f"{base(ws)}/notifications/{others[0]}/read", "admin")
    feed = world.send("GET", f"{base(ws)}/notifications", "reviewer").json()
    assert (feed["total"], feed["unread"]) == (2, 2)


def test_preferences_are_the_session_users_and_no_request_can_name_another(world, ws):
    put = f"{base(ws)}/notification-preferences"
    r = world.send("PUT", put, "marketer",
                   {"items": [{"event_code": "brief.created", "enabled": False}]})  # fmt: skip
    assert r.status_code == 200, r.text
    marketer_rows = preferences(world, ws)
    assert marketer_rows == [(world.users["marketer"], "brief.created", False)]

    # Another user's read is their own defaults, whatever the marketer chose.
    for role in ("viewer", "owner"):
        got = world.send("GET", put, role)
        assert got.status_code == 200
        assert got.json()["items"] == [{"event_code": c.value, "enabled": True} for c in E]

    # A body that names a user is refused whole; nothing is written for anyone.
    victim = world.users["marketer"]
    for body in (
        {"user_id": victim, "items": [{"event_code": "brief.created", "enabled": True}]},
        {"items": [{"event_code": "brief.created", "enabled": True, "user_id": victim}]},
        {"items": [{"event_code": "brief.created", "enabled": True}], "user": victim},
    ):
        bad = world.send("PUT", put, "viewer", body)
        assert bad.status_code == 422, bad.text
    assert preferences(world, ws) == marketer_rows

    # The viewer's own change, with a query that tries to name the marketer, writes only the
    # viewer's row and leaves the marketer's choice as it was.
    change = {"items": [{"event_code": "brief.created", "enabled": True},
                        {"event_code": "export.completed", "enabled": False}]}  # fmt: skip
    mine = world.send("PUT", f"{put}?user_id={victim}", "viewer", change)
    assert mine.status_code == 200
    assert preferences(world, ws) == sorted(
        [*marketer_rows, (world.users["viewer"], "brief.created", True),
         (world.users["viewer"], "export.completed", False)]  # fmt: skip
    )
    marketer_view = world.send("GET", put, "marketer").json()["items"]
    assert {"event_code": "brief.created", "enabled": False} in marketer_view
    assert {"event_code": "export.completed", "enabled": True} in marketer_view


def test_one_users_opt_out_never_suppresses_another_users_delivery(world, ws):
    """An opt-out is the session user's, for that event code, in that workspace only."""
    brief = world.seed_brief(ws, approved=False)
    world.send("PUT", f"{base(ws)}/notification-preferences", "reviewer",
               {"items": [{"event_code": "brief.created", "enabled": False}]})  # fmt: skip
    world.send("PUT", f"{base(ws)}/notification-preferences", "viewer",
               {"items": [{"event_code": "export.completed", "enabled": False}]})  # fmt: skip
    with world.session() as s:
        written = emit_notifications(
            s, organization_id=world.org, workspace_id=ws,
            recipient_user_ids=[world.users["reviewer"], world.users["admin"],
                                world.users["viewer"]],  # fmt: skip
            event_code=E.BRIEF_CREATED, subject_id=brief,
        )
    assert written == 2
    assert sorted(r["user_id"] for r in notes(world, workspace_id=ws)) == sorted(
        [world.users["admin"], world.users["viewer"]]  # the viewer's opt-out is another code's
    )


def test_a_soft_deleted_notification_is_gone_from_the_feed_and_the_unknown_404(world, ws):
    kept, gone = seed(world, ws, "marketer", n=2)
    with world.session() as s:
        s.execute(update(Notification).where(Notification.id == gone)
                  .values(deleted_at=datetime.now(UTC)))  # fmt: skip
    feed = world.send("GET", f"{base(ws)}/notifications", "marketer").json()
    assert [i["id"] for i in feed["items"]] == [kept] and feed["total"] == 1
    r = world.send("POST", f"{base(ws)}/notifications/{gone}/read", "marketer")
    assert r.status_code == 404 and error(r) == NOT_FOUND
    assert world.row(Notification, gone)["read_at"] is None


# =========================================================================== #
# Cross-workspace
# =========================================================================== #
def test_a_notification_of_another_workspace_is_the_unknown_id_404(world, ws):
    set_cap(world, world.ws2, True)
    mine = seed(world, ws, "marketer")[0]
    elsewhere = seed(world, world.ws2, "marketer")[0]
    r = world.send("POST", f"{base(world.ws2)}/notifications/{mine}/read", "marketer")
    assert r.status_code == 404 and error(r) == NOT_FOUND
    r = world.send("POST", f"{base(ws)}/notifications/{elsewhere}/read", "marketer")
    assert r.status_code == 404 and error(r) == NOT_FOUND
    assert world.row(Notification, mine)["read_at"] is None
    assert world.row(Notification, elsewhere)["read_at"] is None
    here = {i["id"] for i in world.send("GET", f"{base(ws)}/notifications", "marketer")
            .json()["items"]}  # fmt: skip
    there = {i["id"] for i in world.send("GET", f"{base(world.ws2)}/notifications", "marketer")
             .json()["items"]}  # fmt: skip
    assert mine in here and elsewhere not in here
    assert elsewhere in there and mine not in there


def test_preferences_are_per_workspace(world, ws):
    set_cap(world, world.ws2, True)
    world.send("PUT", f"{base(ws)}/notification-preferences", "admin",
               {"items": [{"event_code": "draft.generated", "enabled": False}]})  # fmt: skip
    there = world.send("GET", f"{base(world.ws2)}/notification-preferences", "admin").json()
    assert {"event_code": "draft.generated", "enabled": True} in there["items"]


# =========================================================================== #
# Membership stays 403
# =========================================================================== #
@pytest.mark.parametrize("capability", [True, None])
def test_a_non_member_gets_the_membership_403_on_every_route(world, ws, capability):
    """Organization B's owner on organization A's workspace (on, then dark): never a 404 and
    never a 503; nothing changes."""
    set_cap(world, ws, capability)
    theirs = seed(world, ws, "owner")[0]
    before_note, before_prefs = world.row(Notification, theirs), preferences(world, ws)
    calls = [
        ("GET", f"{base(ws)}/notifications", None),
        ("POST", f"{base(ws)}/notifications/{theirs}/read", None),
        ("POST", f"{base(ws)}/notifications/{uuid.uuid4().hex}/read", None),
        ("GET", f"{base(ws)}/notification-preferences", None),
        ("PUT", f"{base(ws)}/notification-preferences",
         {"items": [{"event_code": "brief.created", "enabled": False}]}),
        ("GET", f"{base(ws)}/notifications-analytics-capability", None),
    ]
    for method, url, body in calls:
        r = world.send(method, url, body=body, token=world.owner_b)
        assert r.status_code == 403, (method, url, r.text)
        assert error(r) == MEMBERSHIP
    assert world.row(Notification, theirs) == before_note
    assert preferences(world, ws) == before_prefs
    # And organization A's members on organization B's workspace.
    set_cap(world, world.ws_b, True)
    for role in ALL_ROLES:
        r = world.send("GET", f"{base(world.ws_b)}/notifications", role)
        assert r.status_code == 403 and error(r) == MEMBERSHIP


# =========================================================================== #
# Mark-read: idempotent, first read_at kept, also under a race
# =========================================================================== #
def test_mark_read_is_idempotent_and_answers_the_first_read_at(world, ws):
    nid = seed(world, ws, "viewer")[0]
    url = f"{base(ws)}/notifications/{nid}/read"
    first = world.send("POST", url, "viewer")
    assert first.status_code == 200 and first.json()["read_at"] is not None
    stored = world.row(Notification, nid)["read_at"]
    for _ in range(3):
        again = world.send("POST", url, "viewer")
        assert again.status_code == 200
        assert again.json()["read_at"] == first.json()["read_at"]
    assert world.row(Notification, nid)["read_at"] == stored
    feed = world.send("GET", f"{base(ws)}/notifications", "viewer").json()
    assert (feed["total"], feed["unread"]) == (1, 0)


def test_a_mark_read_that_loaded_the_row_unread_keeps_the_first_read_at(world, ws):
    """Two requests race: this one loaded the row while unread; another marks it read and
    commits first. The conditional update leaves the first ``read_at`` and this request
    answers it."""
    nid = seed(world, ws, "viewer")[0]
    viewer = world.users["viewer"]
    s = world.factory()
    try:
        stale = s.get(Notification, nid)
        assert stale is not None and stale.read_at is None
        winner = world.send("POST", f"{base(ws)}/notifications/{nid}/read", "viewer")
        assert winner.status_code == 200
        first = world.row(Notification, nid)["read_at"]
        assert first is not None
        late = mark_read(s, workspace_id=ws, user_id=viewer, notification_id=nid)
        s.commit()
        assert late.read_at is not None
        assert late.read_at.replace(tzinfo=None) == first.replace(tzinfo=None)
    finally:
        s.close()
    assert world.row(Notification, nid)["read_at"] == first


# =========================================================================== #
# O-12(B): the gate on every route, the override both ways, the ceiling
# =========================================================================== #
def gated_calls(ws: str, nid: str) -> list[tuple[str, str, object]]:
    return [
        ("GET", f"{base(ws)}/notifications", None),
        ("POST", f"{base(ws)}/notifications/{nid}/read", None),
        ("POST", f"{base(ws)}/notifications/{uuid.uuid4().hex}/read", None),
        ("GET", f"{base(ws)}/notification-preferences", None),
        ("PUT", f"{base(ws)}/notification-preferences",
         {"items": [{"event_code": "brief.created", "enabled": False}]}),
        # Dark comes before body validation: a malformed body is still the dark answer.
        ("PUT", f"{base(ws)}/notification-preferences", {"items": [], "user_id": "x"}),
    ]


def assert_dark(world: CreativeWorld, ws: str) -> None:
    nid = seed(world, ws, "marketer")[0]
    before_prefs = preferences(world, ws)
    for role in ALL_ROLES:
        for method, url, body in gated_calls(ws, nid):
            r = world.send(method, url, role, body)
            assert r.status_code == 503, (role, method, url, r.text)
            assert error(r) == DARK
        reflected = world.send("GET", f"{base(ws)}/notifications-analytics-capability", role)
        assert reflected.status_code == 200 and reflected.json() == {"enabled": False}
    assert world.row(Notification, nid)["read_at"] is None
    assert preferences(world, ws) == before_prefs


def assert_open(world: CreativeWorld, ws: str) -> None:
    nid = seed(world, ws, "marketer")[0]
    reflected = world.send("GET", f"{base(ws)}/notifications-analytics-capability", "viewer")
    assert reflected.json() == {"enabled": True}
    assert world.send("GET", f"{base(ws)}/notifications", "marketer").status_code == 200
    assert world.send("POST", f"{base(ws)}/notifications/{nid}/read", "marketer").status_code == 200
    assert world.send("GET", f"{base(ws)}/notification-preferences", "viewer").status_code == 200


def test_dark_every_gated_route_answers_503_and_writes_nothing(world, ws):
    set_cap(world, ws, None)
    assert_dark(world, ws)


def test_an_enable_override_opens_one_workspace_while_the_flag_is_off(world, ws):
    assert_open(world, ws)
    neighbour = world.workspace("n neighbour")
    assert_dark(world, neighbour)


def test_a_disable_override_closes_one_workspace_while_the_flag_is_on(world, ws, monkeypatch):
    monkeypatch.setattr(get_settings(), "notifications_analytics_enabled", True)
    neighbour = world.workspace("n neighbour on")
    set_cap(world, ws, False)
    assert_dark(world, ws)
    assert_open(world, neighbour)  # the flag alone opens a workspace without an override


def test_the_safety_ceiling_outranks_an_enable_override_and_the_flag(world, ws, monkeypatch):
    monkeypatch.setattr(get_settings(), "notifications_analytics_enabled", True)
    assert_open(world, ws)
    real = resolver_mod._ceiling_blocks
    monkeypatch.setattr(
        resolver_mod, "_ceiling_blocks",
        lambda c, **kw: c is Capability.NOTIFICATIONS_ANALYTICS or real(c, **kw),
    )
    assert_dark(world, ws)


def test_other_capabilities_never_open_notifications(world, ws):
    set_cap(world, ws, None)
    for other in ("content_export", "creative_generation", "recommendation_briefs"):
        set_cap(world, ws, True, capability=other)
    assert_dark(world, ws)


# =========================================================================== #
# The emitter writes nothing while dark
# =========================================================================== #
def test_the_emitter_writes_nothing_and_reads_no_recipient_while_dark(world, ws, monkeypatch):
    brief = world.seed_brief(ws, approved=False)
    consumed: list[bool] = []

    def lazy():
        consumed.append(True)
        yield world.users["reviewer"]

    for state in (None, False):
        set_cap(world, ws, state)
        with world.session() as s:
            assert emit_notifications(
                s, organization_id=world.org, workspace_id=ws, recipient_user_ids=lazy(),
                event_code=E.BRIEF_CREATED, subject_id=brief,
            ) == 0
    monkeypatch.setattr(get_settings(), "notifications_analytics_enabled", True)
    set_cap(world, ws, False)  # the per-workspace kill switch, with the flag on
    with world.session() as s:
        assert emit_notifications(
            s, organization_id=world.org, workspace_id=ws, recipient_user_ids=lazy(),
            event_code=E.BRIEF_CREATED, subject_id=brief,
        ) == 0
    assert notes(world, workspace_id=ws) == [] and consumed == []
    set_cap(world, ws, True)  # the control: on, the same call writes the row
    with world.session() as s:
        assert emit_notifications(
            s, organization_id=world.org, workspace_id=ws, recipient_user_ids=lazy(),
            event_code=E.BRIEF_CREATED, subject_id=brief,
        ) == 1
    assert [r["user_id"] for r in notes(world, workspace_id=ws)] == [world.users["reviewer"]]


def _exportable(world: CreativeWorld, ws: str) -> str:
    brief_id = world.seed_brief(ws)
    tag = uuid.uuid4().hex[:12]
    with world.session() as s:
        brief = s.get(RecommendationBrief, brief_id)
        author = s.get(User, world.users["marketer"])
        reviewer = s.get(User, world.users["reviewer"])
        scoped = {"organization_id": world.org, "workspace_id": ws}
        s.add(CreativeDraft(
            id=f"dr{tag}", brief_id=brief_id, opportunity_id=brief.opportunity_id,
            draft_version=1, channel="social_post", body="Weekend parking is easy.",
            prompt_name="generate_creative", prompt_version="1.0.0", model_identifier="mock-1",
            input_hash="0" * 64, is_simulated=True, input_tokens=1, output_tokens=1,
            estimated_cost_usd=0, jurisdiction_code="US-TX", created_by=author.id,
            created_by_snapshot=actor_snapshot(
                user_id=author.id, full_name=author.full_name, email=author.email
            ),
            **scoped,
        ))
        s.flush()
        s.add(ContentReview(
            draft_id=f"dr{tag}", draft_version=1, risk_level="low", is_blocked=False,
            findings=[], warnings=[], blocked_claim_ids=[], required_disclaimers=[],
            disclaimers_resolved=True, passed=True, jurisdiction_code="US-TX", **scoped,
        ))
        s.add(Approval(
            subject_type="creative_draft", subject_id=f"dr{tag}", subject_version=1,
            draft_id=f"dr{tag}", decision="approved", decision_seq=1, decided_by=reviewer.id,
            decided_by_snapshot=actor_snapshot(
                user_id=reviewer.id, full_name=reviewer.full_name, email=reviewer.email
            ),
            decided_role="reviewer", is_simulated=False, **scoped,
        ))
    return f"dr{tag}"


def test_an_export_while_notifications_are_dark_commits_without_a_notification(world, ws):
    set_cap(world, ws, True, capability="content_export")
    set_cap(world, ws, None)  # O-12(B) dark for this workspace
    draft = _exportable(world, ws)
    r = world.send("POST", f"{base(ws)}/drafts/{draft}/exports", "admin", {"format": "json"})
    assert r.status_code == 201, r.text
    assert world.row(Export, r.json()["id"]) is not None
    assert notes(world, workspace_id=ws) == []

    set_cap(world, ws, True)  # the control: on, the author and the approver are told
    r = world.send("POST", f"{base(ws)}/drafts/{draft}/exports", "admin", {"format": "json"})
    assert r.status_code == 201, r.text
    told = notes(world, workspace_id=ws)
    assert sorted(n["user_id"] for n in told) == sorted(
        [world.users["marketer"], world.users["reviewer"]]
    )
    assert {(n["event_code"], n["subject_id"]) for n in told} == {
        ("export.completed", r.json()["id"])
    }
    assert world.users["admin"] not in {n["user_id"] for n in told}  # never the exporter
    # The marketer's feed shows it; the exporter's does not.
    feed = world.send("GET", f"{base(ws)}/notifications", "marketer").json()
    assert [i["subject_id"] for i in feed["items"]] == [r.json()["id"]]
    assert world.send("GET", f"{base(ws)}/notifications", "admin").json()["items"] == []

    # The author exporting their own draft is the actor: only the approver is told.
    mine = world.send("POST", f"{base(ws)}/drafts/{draft}/exports", "marketer",
                      {"format": "text/plain"})  # fmt: skip
    assert mine.status_code == 201, mine.text
    told = [n for n in notes(world, workspace_id=ws) if n["subject_id"] == mine.json()["id"]]
    assert [n["user_id"] for n in told] == [world.users["reviewer"]]


def test_the_dark_check_is_per_workspace(world, ws):
    """A workspace that is on does not open emission in its dark sibling."""
    dark = world.workspace("n dark sibling")
    brief_dark = world.seed_brief(dark, approved=False)
    brief_on = world.seed_brief(ws, approved=False)
    with world.session() as s:
        for space, brief in ((dark, brief_dark), (ws, brief_on)):
            emit_notifications(
                s, organization_id=world.org, workspace_id=space,
                recipient_user_ids=[world.users["viewer"]], event_code=E.BRIEF_CREATED,
                subject_id=brief,
            )
    assert notes(world, workspace_id=dark) == []
    assert len(notes(world, workspace_id=ws)) == 1
    assert world.count(Notification, workspace_id=dark) == 0
