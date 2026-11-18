"""Status analytics: null, never a fabricated zero, and the zero-row case (Project Phase 5E
exit criterion 4, "incomplete states are null"; lane 5E-W1 testing lane 5E-W2's code).

On SQLite and PostgreSQL, through ``GET /workspaces/{ws}/analytics``:

* **a workspace with no rows**: every count is 0 (an empty scope measures 0); both approval
  latencies have ``decided`` 0 and a null mean and median; both review rates are null; the
  comparison is empty and ``unassigned`` is a row of zeros (the workspace view always has it);
* **a location with no rows** (a workspace's only location): the same nulls, the location's
  row of zeros, and ``unassigned`` null;
* **families present but incomplete**: briefs and a draft with no decision leave the latencies
  null while their counts are real; a draft with no content review leaves both rates null
  while ``drafts_generated`` counts it; one decided version gives a real latency and one
  review a real rate, ``0.0`` included where it was measured.

Positive paths only (lane 5E-W4 owns the refusals).
"""

from __future__ import annotations

from app.tests.fixtures.analytics import T0, T1, AnalyticsWorld, Rows, at, set_capability

ZERO_LATENCY = {"decided": 0, "mean_seconds": None, "median_seconds": None}
ZERO_TOTALS = {"opportunities": 0, "briefs": 0, "drafts": 0, "approvals": 0, "exports": 0}
EMPTY_FIGURES = {
    "opportunities": {"total": 0, "new": 0, "saved": 0, "monitoring": 0, "ignored": 0,
                      "actioned": 0},
    "briefs": {"total": 0, "awaiting_decision": 0, "approved": 0, "rejected": 0,
               "superseded": 0},
    "approvals": {"total": 0, "approved": 0, "rejected": 0, "revoked": 0, "on_briefs": 0,
                  "on_drafts": 0},
    "drafts": {"total": 0, "awaiting_decision": 0, "approved": 0, "rejected": 0,
               "superseded": 0},
    "reviews": {"total": 0, "passed": 0, "blocked": 0, "not_passed": 0},
    "exports": {"total": 0, "plain_text": 0, "markdown": 0, "json_document": 0},
    "approval_latency": {"briefs": ZERO_LATENCY, "drafts": ZERO_LATENCY},
    "outcomes": {"drafts_generated": 0, "revisions": 0, "simulated": 0,
                 "review_pass_rate": None, "review_block_rate": None},
}


def _figures(body: dict) -> dict:
    return {key: body[key] for key in EMPTY_FIGURES}


def _ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def _workspace(analytics_world: AnalyticsWorld, name: str) -> str:
    world = analytics_world.world
    ws = world.workspace(name)
    set_capability(world, ws, "notifications_analytics")
    return ws


def test_a_workspace_with_no_rows_measures_zero_and_reports_null(
    analytics_world: AnalyticsWorld,
):
    ws = _workspace(analytics_world, "analytics empty")
    body = _ok(analytics_world.read(ws))
    assert body["workspace_id"] == ws and body["location_id"] is None
    assert _figures(body) == EMPTY_FIGURES
    assert body["locations"] == []
    assert body["unassigned"] == ZERO_TOTALS


def test_a_location_with_no_rows_reports_null_and_no_remainder(analytics_world: AnalyticsWorld):
    world = analytics_world.world
    ws = _workspace(analytics_world, "analytics empty location")
    with world.session() as s:
        location_id = Rows(s, organization_id=world.org, workspace_id=ws).location(
            "Gamma Market", code="GB"
        )
    workspace = _ok(analytics_world.read(ws))
    assert _figures(workspace) == EMPTY_FIGURES
    assert workspace["locations"] == [
        {"location_id": location_id, "name": "Gamma Market", "is_active": True, **ZERO_TOTALS}
    ]
    assert workspace["unassigned"] == ZERO_TOTALS

    view = _ok(analytics_world.read(ws, location_id=location_id))
    assert view["location_id"] == location_id
    assert _figures(view) == EMPTY_FIGURES
    assert view["locations"] == workspace["locations"]
    assert view["unassigned"] is None


def test_undecided_and_unreviewed_rows_leave_their_figures_null(analytics_world: AnalyticsWorld):
    """Counts are real; a latency without a decided version and a rate without a review are
    null, not 0."""
    world = analytics_world.world
    ws = _workspace(analytics_world, "analytics incomplete")
    with world.session() as s:
        rows = Rows(s, organization_id=world.org, workspace_id=ws)
        opp = rows.opportunity(location_id=None, status="saved")
        brief = rows.brief(opportunity_id=opp, author=world.users["marketer"],
                           location_id=None, code="GB", created_at=T0)
        rows.draft(brief_id=brief, author=world.users["marketer"], created_at=T1)
    body = _ok(analytics_world.read(ws))
    assert body["opportunities"]["total"] == 1 and body["opportunities"]["saved"] == 1
    assert body["briefs"] == {"total": 1, "awaiting_decision": 1, "approved": 0, "rejected": 0,
                              "superseded": 0}
    assert body["drafts"]["total"] == 1 and body["drafts"]["awaiting_decision"] == 1
    assert body["approval_latency"] == {"briefs": ZERO_LATENCY, "drafts": ZERO_LATENCY}
    assert body["reviews"] == {"total": 0, "passed": 0, "blocked": 0, "not_passed": 0}
    assert body["outcomes"] == {"drafts_generated": 1, "revisions": 0, "simulated": 1,
                                "review_pass_rate": None, "review_block_rate": None}
    assert body["unassigned"] == {"opportunities": 1, "briefs": 1, "drafts": 1, "approvals": 0,
                                  "exports": 0}


def test_one_measurement_gives_a_real_figure_including_a_measured_zero(
    analytics_world: AnalyticsWorld,
):
    """One decided brief gives its latency; one passed review gives a pass rate of 1.0 and a
    block rate of 0.0 (measured, so not null)."""
    world = analytics_world.world
    ws = _workspace(analytics_world, "analytics one each")
    with world.session() as s:
        rows = Rows(s, organization_id=world.org, workspace_id=ws)
        opp = rows.opportunity(location_id=None)
        brief = rows.brief(opportunity_id=opp, author=world.users["marketer"],
                           location_id=None, code="GB", created_at=T0)
        rows.decision(subject=brief, kind="brief", decision="approved", seq=1,
                      reviewer=world.users["reviewer"], decided_at=at(T0, 42))
        draft = rows.draft(brief_id=brief, author=world.users["marketer"], created_at=T1)
        rows.review(draft_id=draft, outcome="passed")
    body = _ok(analytics_world.read(ws))
    assert body["approval_latency"] == {
        "briefs": {"decided": 1, "mean_seconds": 42.0, "median_seconds": 42.0},
        "drafts": ZERO_LATENCY,
    }
    assert body["outcomes"]["review_pass_rate"] == 1.0
    assert body["outcomes"]["review_block_rate"] == 0.0
