"""In-app notification preferences: read, update and suppression (Project Phase 5E exit
criterion 5, "preferences suppress"; lane 5E-W1 testing lane 5E-W3's code).

On SQLite and PostgreSQL, with the O-12(B) capability opened by a workspace override:

* **No choice means delivered**: ``GET …/notification-preferences`` answers every event code,
  in the vocabulary's order, enabled, for every member.
* **An update is partial and the session user's own**: ``PUT`` sets the codes it names, leaves
  the others, answers every code, can be changed again, and touches neither another member's
  preferences nor the same user's in another workspace.
* **Preferences suppress**: a member who turned an event off gets no notification of that
  event in that workspace, and still gets every other event, and that event elsewhere;
  turning it back on restores delivery; an export does not notify an approver who turned
  ``export.completed`` off, and still notifies the draft's author.

Positive paths and behaviour only: request validation and every refusal are lane 5E-W4's.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.core.enums import NotificationEventCode
from app.notifications.models import Notification, NotificationPreference
from app.notifications.service import emit_notifications
from app.tests.fixtures.analytics import T0, T1, Rows, at, creative_world_on, set_capability

ALL_ON = [{"event_code": code.value, "enabled": True} for code in NotificationEventCode]
ROLES = ("owner", "admin", "marketer", "reviewer", "compliance_reviewer", "viewer")


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def world(request, tmp_path_factory) -> Iterator:
    with creative_world_on(request.param, tmp_path_factory, "w1prefs") as w:
        yield w


def _workspace(world, name: str, *, export: bool = False) -> str:
    ws = world.workspace(name)
    set_capability(world, ws, "notifications_analytics")
    if export:
        set_capability(world, ws, "content_export")
    return ws


def _path(ws: str) -> str:
    return f"/workspaces/{ws}/notification-preferences"


def _get(world, ws: str, role: str) -> list[dict]:
    r = world.send("GET", _path(ws), role)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _put(world, ws: str, role: str, changes: dict[str, bool]) -> list[dict]:
    body = {"items": [{"event_code": c, "enabled": on} for c, on in changes.items()]}
    r = world.send("PUT", _path(ws), role, body)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _with(changes: dict[str, bool]) -> list[dict]:
    return [{"event_code": i["event_code"], "enabled": changes.get(i["event_code"], True)}
            for i in ALL_ON]


def test_with_no_choice_every_event_is_delivered(world):
    ws = _workspace(world, "prefs defaults")
    for role in ROLES:
        assert _get(world, ws, role) == ALL_ON, role


def test_an_update_is_partial_and_the_session_users_own(world):
    users = world.users
    ws = _workspace(world, "prefs update")
    other = _workspace(world, "prefs update other")
    answered = _put(world, ws, "viewer", {"brief.created": False})
    assert answered == _with({"brief.created": False})
    assert _get(world, ws, "viewer") == _with({"brief.created": False})
    # One stored row, the viewer's, in this workspace.
    [row] = world.rows(NotificationPreference, workspace_id=ws)
    assert (row["user_id"], row["event_code"], row["enabled"]) == (
        users["viewer"], "brief.created", False)
    # Another member here, and the same member in another workspace, are untouched.
    assert _get(world, ws, "reviewer") == ALL_ON
    assert _get(world, other, "viewer") == ALL_ON

    # Changed again: the stored choice is updated, the other codes stay as they were.
    answered = _put(world, ws, "viewer", {"brief.created": True, "export.completed": False})
    assert answered == _with({"export.completed": False})
    assert _get(world, ws, "viewer") == _with({"export.completed": False})
    assert {(r["event_code"], r["enabled"])
            for r in world.rows(NotificationPreference, workspace_id=ws)} == {
        ("brief.created", True), ("export.completed", False)}


def _brief(world, ws: str) -> str:
    with world.session() as s:
        rows = Rows(s, organization_id=world.org, workspace_id=ws)
        opp = rows.opportunity(location_id=None)
        return rows.brief(opportunity_id=opp, author=world.users["owner"], location_id=None,
                          code="US-TX", created_at=T0)


def _emit(world, ws: str, brief_id: str, recipients: list[str], code: str) -> int:
    with world.session() as s:
        return emit_notifications(s, organization_id=world.org, workspace_id=ws,
                                  recipient_user_ids=recipients, event_code=code,
                                  subject_id=brief_id)


def _who(world, ws: str, code: str) -> set[str]:
    return {r["user_id"] for r in world.rows(Notification, workspace_id=ws, event_code=code)}


def test_a_member_who_turned_an_event_off_is_not_notified_of_that_event(world):
    users = world.users
    ws = _workspace(world, "prefs suppress")
    other = _workspace(world, "prefs suppress other")
    _put(world, ws, "marketer", {"brief.created": False})
    _put(world, ws, "reviewer", {"draft.generated": False})  # another event only
    recipients = [users["marketer"], users["reviewer"], users["viewer"]]

    assert _emit(world, ws, _brief(world, ws), recipients, "brief.created") == 2
    assert _who(world, ws, "brief.created") == {users["reviewer"], users["viewer"]}
    # The marketer's choice is for this workspace only.
    assert _emit(world, other, _brief(world, other), recipients, "brief.created") == 3
    assert _who(world, other, "brief.created") == set(recipients)


def test_turning_an_event_back_on_restores_delivery(world):
    users = world.users
    ws = _workspace(world, "prefs restore")
    _put(world, ws, "viewer", {"brief.created": False})
    first = _brief(world, ws)
    assert _emit(world, ws, first, [users["viewer"]], "brief.created") == 0
    _put(world, ws, "viewer", {"brief.created": True})
    second = _brief(world, ws)
    assert _emit(world, ws, second, [users["viewer"]], "brief.created") == 1
    assert [(r["user_id"], r["subject_id"])
            for r in world.rows(Notification, workspace_id=ws)] == [(users["viewer"], second)]


def test_an_export_skips_an_approver_who_turned_export_notifications_off(world):
    users = world.users
    ws = _workspace(world, "prefs export", export=True)
    with world.session() as s:
        rows = Rows(s, organization_id=world.org, workspace_id=ws)
        opp = rows.opportunity(location_id=None)
        brief = rows.brief(opportunity_id=opp, author=users["marketer"], location_id=None,
                           code="US-TX", created_at=T0)
        # D-N4 item 38: a draft is exportable only while its brief version is approved.
        rows.decision(subject=brief, kind="brief", decision="approved", seq=1,
                      reviewer=users["reviewer"], decided_at=at(T0, 60))
        draft = rows.draft(brief_id=brief, author=users["marketer"], created_at=T1)
        rows.review(draft_id=draft, outcome="passed")
        rows.decision(subject=draft, kind="draft", decision="approved", seq=1,
                      reviewer=users["reviewer"], decided_at=at(T1, 60))
    _put(world, ws, "reviewer", {"export.completed": False})
    r = world.send("POST", f"/workspaces/{ws}/drafts/{draft}/exports", "owner",
                   {"format": "json"})
    assert r.status_code == 201, r.text
    export_id = r.json()["id"]
    assert [(n["user_id"], n["event_code"], n["subject_id"])
            for n in world.rows(Notification, workspace_id=ws)] == [
        (users["marketer"], "export.completed", export_id)]
    feed = world.send("GET", f"/workspaces/{ws}/notifications", "reviewer").json()
    assert feed["total"] == 0
