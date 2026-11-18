"""I-5E integration fold: the checks no lane's module exercised (Project Phase 5E).

Two groups, each on SQLite and on PostgreSQL migrated to head.

**The notification emitter's caller contract and the preferences body** (lane 5E-W3's code).
The 5E mutant run at the integrated head found five recorded mutants that every committed
test survived, because each test lane listed them as the other's:

* the emitter refuses a workspace that does not belong to the organization (two mutants: the
  check always passing, and its query losing the organization predicate);
* it refuses a subject that is a row of the event's table in *another* workspace;
* it refuses a soft-deleted subject;
* a preferences update that names an event code twice is refused (422) and stores nothing.

The emitter's refusals are :class:`~app.notifications.service.NotificationContractError`: a
programming error, raised whatever the capability says, before anything is written.

**A negative approval latency** (lane 5E-W2's code; found by lane 5E-W4, its finding F-1).
``latency_stats`` documents that a negative value (a decision stamped before its subject:
a clock fault or a hand-written row) "is reported as measured, not hidden". The response
schema bounded a latency at zero or more, so one such row made the whole analytics read fail.
The bound is removed; these tests pin the documented behaviour in the function and through the
route.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.analytics.schemas import LatencyOut
from app.analytics.service import latency_stats
from app.core.enums import NotificationEventCode
from app.notifications.models import Notification, NotificationPreference
from app.notifications.service import NotificationContractError, emit_notifications
from app.tests.fixtures.analytics import T0, Rows, at, creative_world_on, set_capability

NOTIFICATIONS_ANALYTICS = "notifications_analytics"
BRIEF_CREATED = NotificationEventCode.BRIEF_CREATED


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def world(request, tmp_path_factory) -> Iterator:
    with creative_world_on(request.param, tmp_path_factory, "i5efold") as w:
        yield w


def _workspace(world, name: str) -> str:
    ws = world.workspace(name)
    set_capability(world, ws, NOTIFICATIONS_ANALYTICS)
    return ws


def _brief(world, ws: str, *, deleted: bool = False) -> str:
    with world.session() as s:
        rows = Rows(s, organization_id=world.org_of(ws), workspace_id=ws)
        opp = rows.opportunity(location_id=None)
        return rows.brief(opportunity_id=opp, author=world.owner_id(ws), location_id=None,
                          code="US-TX", created_at=T0, deleted=deleted)


def _emit(world, *, organization_id: str, workspace_id: str, subject_id: str) -> int:
    with world.session() as s:
        return emit_notifications(
            s, organization_id=organization_id, workspace_id=workspace_id,
            recipient_user_ids=[world.users["reviewer"]], event_code=BRIEF_CREATED,
            subject_id=subject_id,
        )


def _notifications(world) -> int:
    return len(world.rows(Notification))


# --------------------------------------------------------------------------- #
# The emitter's caller contract
# --------------------------------------------------------------------------- #
def test_the_control_emission_writes_one_row(world):
    """The arguments the refusals below vary are otherwise a valid emission."""
    ws = _workspace(world, "fold control")
    brief = _brief(world, ws)
    before = _notifications(world)
    assert _emit(world, organization_id=world.org, workspace_id=ws, subject_id=brief) == 1
    assert _notifications(world) == before + 1


def test_a_workspace_of_another_organization_is_refused_as_such(world):
    """Organization A with organization B's workspace: refused at the workspace check itself.

    The message is asserted because the subject check after it would refuse these arguments
    too; only the workspace check's own refusal shows that it ran."""
    set_capability(world, world.ws_b, NOTIFICATIONS_ANALYTICS)
    foreign_brief = _brief(world, world.ws_b)
    before = _notifications(world)
    with pytest.raises(NotificationContractError, match="does not belong to the organization"):
        _emit(world, organization_id=world.org, workspace_id=world.ws_b,
              subject_id=foreign_brief)
    assert _notifications(world) == before


def test_a_subject_of_another_workspace_is_refused(world):
    """A live brief of the sibling workspace is not a subject of this workspace's event."""
    ws = _workspace(world, "fold subject scope")
    sibling = _workspace(world, "fold subject sibling")
    sibling_brief = _brief(world, sibling)
    before = _notifications(world)
    with pytest.raises(NotificationContractError, match="not a live row"):
        _emit(world, organization_id=world.org, workspace_id=ws, subject_id=sibling_brief)
    assert _notifications(world) == before


def test_a_soft_deleted_subject_is_refused(world):
    ws = _workspace(world, "fold deleted subject")
    deleted_brief = _brief(world, ws, deleted=True)
    before = _notifications(world)
    with pytest.raises(NotificationContractError, match="not a live row"):
        _emit(world, organization_id=world.org, workspace_id=ws, subject_id=deleted_brief)
    assert _notifications(world) == before


def test_the_contract_is_checked_while_the_capability_is_off(world):
    """A broken contract raises in the shipped dark state too, where a hook's tests see it."""
    ws = world.workspace("fold dark contract")
    deleted_brief = _brief(world, ws, deleted=True)
    with pytest.raises(NotificationContractError, match="not a live row"):
        _emit(world, organization_id=world.org, workspace_id=ws, subject_id=deleted_brief)


# --------------------------------------------------------------------------- #
# The preferences body
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("second", [False, True], ids=["same-choice", "opposite-choice"])
def test_a_preferences_update_naming_a_code_twice_is_refused_and_stores_nothing(world, second):
    ws = _workspace(world, f"fold prefs {second}")
    code = NotificationEventCode.EXPORT_COMPLETED.value
    body = {"items": [{"event_code": code, "enabled": False},
                      {"event_code": code, "enabled": second}]}
    r = world.send("PUT", f"/workspaces/{ws}/notification-preferences", "marketer", body)
    assert r.status_code == 422, r.text
    assert world.rows(NotificationPreference, workspace_id=ws) == []
    # The same code once is accepted: the refusal is of the repetition.
    ok = world.send("PUT", f"/workspaces/{ws}/notification-preferences", "marketer",
                    {"items": body["items"][:1]})
    assert ok.status_code == 200, ok.text
    assert len(world.rows(NotificationPreference, workspace_id=ws)) == 1


# --------------------------------------------------------------------------- #
# A negative approval latency is reported as measured
# --------------------------------------------------------------------------- #
def test_latency_stats_reports_a_negative_value_as_measured():
    assert latency_stats([-5.0]) == LatencyOut(decided=1, mean_seconds=-5.0, median_seconds=-5.0)
    # Not clamped to zero and not dropped: it weighs in the mean and orders in the median.
    assert latency_stats([-30.0, 10.0, 50.0]) == LatencyOut(
        decided=3, mean_seconds=10.0, median_seconds=10.0
    )
    assert latency_stats([-30.0, 10.0]) == LatencyOut(
        decided=2, mean_seconds=-10.0, median_seconds=-10.0
    )


def test_the_schema_admits_a_negative_latency_and_keeps_its_other_rules():
    assert LatencyOut(decided=1, mean_seconds=-0.001, median_seconds=-86400.0).mean_seconds < 0
    with pytest.raises(ValueError):
        LatencyOut(decided=-1, mean_seconds=None, median_seconds=None)  # a count is never < 0
    with pytest.raises(ValueError):
        LatencyOut(decided=1, mean_seconds=1.0, median_seconds=1.0, extra=1)  # closed model


def test_the_analytics_read_answers_with_a_decision_stamped_before_its_subject(world):
    """One brief decided 30 s *before* it was created, one draft decided 12 s after: the read
    answers 200 and reports both as measured (it answered 500 while the schema had a floor)."""
    ws = _workspace(world, "fold negative latency")
    users = world.users
    with world.session() as s:
        rows = Rows(s, organization_id=world.org, workspace_id=ws)
        opp = rows.opportunity(location_id=None)
        brief = rows.brief(opportunity_id=opp, author=users["marketer"], location_id=None,
                           code="GB", created_at=T0)
        rows.decision(subject=brief, kind="brief", decision="approved", seq=1,
                      reviewer=users["reviewer"], decided_at=at(T0, -30))
        draft = rows.draft(brief_id=brief, author=users["marketer"], created_at=T0)
        rows.decision(subject=draft, kind="draft", decision="approved", seq=1,
                      reviewer=users["reviewer"], decided_at=at(T0, 12))
    r = world.send("GET", f"/workspaces/{ws}/analytics", "owner")
    assert r.status_code == 200, r.text
    assert r.json()["approval_latency"] == {
        "briefs": {"decided": 1, "mean_seconds": -30.0, "median_seconds": -30.0},
        "drafts": {"decided": 1, "mean_seconds": 12.0, "median_seconds": 12.0},
    }
