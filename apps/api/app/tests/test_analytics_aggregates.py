"""Status analytics returns correct aggregates on the seeded fixture (Project Phase 5E exit
criterion 4, positive half; lane 5E-W1 testing lane 5E-W2's code).

``GET /workspaces/{ws}/analytics`` on the six-family seed of
:mod:`app.tests.fixtures.analytics`, on SQLite and on PostgreSQL. Every figure is stated
here and derived from the seed's table:

* **the workspace view**: counts by state for the six families (a newer live version
  supersedes; the controlling decision decides; a revoked approval returns the subject to
  the queue; soft-deleted briefs, drafts and reviews count nowhere, while approvals and
  exports of a deleted subject still count for the workspace as compliance records);
  approval latency (creation to first decision: mean and median); generation and review
  outcomes; the per-location comparison (every location, active or not, by name) and the
  ``unassigned`` remainder, which together add up to each family's total;
* **a location view**: the same figures narrowed to the location, the inactive one included;
  only that location in the comparison; ``unassigned`` null;
* each location row equals that location view's totals;
* the readers (owner, admin, marketer, reviewer) all see the same figures.

Positive paths only; refusals, isolation and the role negatives are lane 5E-W4's.
"""

from __future__ import annotations

import pytest

from app.tests.fixtures.analytics import ALPHA_NAME, BETA_NAME, AnalyticsWorld

#: The workspace view of the seed (module docstring of the fixture).
WORKSPACE_FIGURES = {
    "opportunities": {"total": 6, "new": 2, "saved": 1, "monitoring": 1, "ignored": 1,
                      "actioned": 1},
    "briefs": {"total": 5, "awaiting_decision": 2, "approved": 1, "rejected": 1,
               "superseded": 1},
    "approvals": {"total": 11, "approved": 7, "rejected": 2, "revoked": 2, "on_briefs": 6,
                  "on_drafts": 5},
    "drafts": {"total": 4, "awaiting_decision": 2, "approved": 1, "rejected": 0,
               "superseded": 1},
    "reviews": {"total": 4, "passed": 2, "blocked": 1, "not_passed": 1},
    "exports": {"total": 3, "plain_text": 1, "markdown": 1, "json_document": 1},
    "approval_latency": {
        "briefs": {"decided": 4, "mean_seconds": 400.0, "median_seconds": 250.0},
        "drafts": {"decided": 3, "mean_seconds": 260.0, "median_seconds": 120.0},
    },
    "outcomes": {"drafts_generated": 4, "revisions": 1, "simulated": 3,
                 "review_pass_rate": 0.5, "review_block_rate": 0.25},
}
ALPHA_TOTALS = {"opportunities": 3, "briefs": 3, "drafts": 2, "approvals": 5, "exports": 0}
BETA_TOTALS = {"opportunities": 1, "briefs": 1, "drafts": 1, "approvals": 2, "exports": 2}
UNASSIGNED = {"opportunities": 2, "briefs": 1, "drafts": 1, "approvals": 4, "exports": 1}

ALPHA_FIGURES = {
    "opportunities": {"total": 3, "new": 2, "saved": 1, "monitoring": 0, "ignored": 0,
                      "actioned": 0},
    "briefs": {"total": 3, "awaiting_decision": 1, "approved": 0, "rejected": 1,
               "superseded": 1},
    "approvals": {"total": 5, "approved": 2, "rejected": 2, "revoked": 1, "on_briefs": 2,
                  "on_drafts": 3},
    "drafts": {"total": 2, "awaiting_decision": 1, "approved": 0, "rejected": 0,
               "superseded": 1},
    "reviews": {"total": 2, "passed": 1, "blocked": 1, "not_passed": 0},
    "exports": {"total": 0, "plain_text": 0, "markdown": 0, "json_document": 0},
    "approval_latency": {
        "briefs": {"decided": 2, "mean_seconds": 150.0, "median_seconds": 150.0},
        "drafts": {"decided": 2, "mean_seconds": 360.0, "median_seconds": 360.0},
    },
    "outcomes": {"drafts_generated": 2, "revisions": 1, "simulated": 1,
                 "review_pass_rate": 0.5, "review_block_rate": 0.5},
}
BETA_FIGURES = {
    "opportunities": {"total": 1, "new": 0, "saved": 0, "monitoring": 0, "ignored": 0,
                      "actioned": 1},
    "briefs": {"total": 1, "awaiting_decision": 0, "approved": 1, "rejected": 0,
               "superseded": 0},
    "approvals": {"total": 2, "approved": 2, "rejected": 0, "revoked": 0, "on_briefs": 1,
                  "on_drafts": 1},
    "drafts": {"total": 1, "awaiting_decision": 0, "approved": 1, "rejected": 0,
               "superseded": 0},
    "reviews": {"total": 1, "passed": 1, "blocked": 0, "not_passed": 0},
    "exports": {"total": 2, "plain_text": 1, "markdown": 0, "json_document": 1},
    "approval_latency": {
        "briefs": {"decided": 1, "mean_seconds": 300.0, "median_seconds": 300.0},
        "drafts": {"decided": 1, "mean_seconds": 60.0, "median_seconds": 60.0},
    },
    "outcomes": {"drafts_generated": 1, "revisions": 0, "simulated": 1,
                 "review_pass_rate": 1.0, "review_block_rate": 0.0},
}
FAMILIES = ("opportunities", "briefs", "drafts", "approvals", "exports")
READERS = ("owner", "admin", "marketer", "reviewer")


def _figures(body: dict) -> dict:
    return {key: body[key] for key in WORKSPACE_FIGURES}


def _ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def test_the_workspace_view_counts_every_family_by_state(analytics_world: AnalyticsWorld):
    body = _ok(analytics_world.read())
    seed = analytics_world.seed
    assert body["workspace_id"] == seed.workspace_id
    assert body["location_id"] is None
    assert body["generated_at"]
    assert _figures(body) == WORKSPACE_FIGURES


def test_the_workspace_view_compares_every_location_by_name(analytics_world: AnalyticsWorld):
    body = _ok(analytics_world.read())
    seed = analytics_world.seed
    assert body["locations"] == [
        {"location_id": seed.alpha, "name": ALPHA_NAME, "is_active": True, **ALPHA_TOTALS},
        {"location_id": seed.beta, "name": BETA_NAME, "is_active": False, **BETA_TOTALS},
    ]
    assert body["unassigned"] == UNASSIGNED


def test_location_rows_and_the_remainder_add_up_to_each_total(analytics_world: AnalyticsWorld):
    body = _ok(analytics_world.read())
    totals = {
        "opportunities": body["opportunities"]["total"],
        "briefs": body["briefs"]["total"],
        "drafts": body["drafts"]["total"],
        "approvals": body["approvals"]["total"],
        "exports": body["exports"]["total"],
    }
    for family in FAMILIES:
        located = sum(row[family] for row in body["locations"])
        assert located + body["unassigned"][family] == totals[family], family


@pytest.mark.parametrize(
    ("which", "figures", "totals", "name", "active"),
    [
        ("alpha", ALPHA_FIGURES, ALPHA_TOTALS, ALPHA_NAME, True),
        ("beta", BETA_FIGURES, BETA_TOTALS, BETA_NAME, False),
    ],
)
def test_a_location_view_narrows_every_figure_to_that_location(
    analytics_world: AnalyticsWorld, which, figures, totals, name, active
):
    location_id = getattr(analytics_world.seed, which)
    body = _ok(analytics_world.read(location_id=location_id))
    assert body["location_id"] == location_id
    assert _figures(body) == figures
    assert body["locations"] == [
        {"location_id": location_id, "name": name, "is_active": active, **totals}
    ]
    # The remainder has no meaning inside one location: null, not a row of zeros.
    assert body["unassigned"] is None


def test_each_location_row_equals_that_location_views_totals(analytics_world: AnalyticsWorld):
    rows = {row["location_id"]: row for row in _ok(analytics_world.read())["locations"]}
    for location_id, row in rows.items():
        view = _ok(analytics_world.read(location_id=location_id))
        assert row["opportunities"] == view["opportunities"]["total"]
        assert row["briefs"] == view["briefs"]["total"]
        assert row["drafts"] == view["drafts"]["total"]
        assert row["approvals"] == view["approvals"]["total"]
        assert row["exports"] == view["exports"]["total"]


@pytest.mark.parametrize("role", READERS)
def test_every_reader_sees_the_same_figures(analytics_world: AnalyticsWorld, role):
    body = _ok(analytics_world.read(role=role))
    assert _figures(body) == WORKSPACE_FIGURES
    assert body["unassigned"] == UNASSIGNED


def test_a_soft_deleted_brief_counts_nowhere_but_its_decision_does(
    analytics_world: AnalyticsWorld,
):
    """P2: B5 (soft-deleted, at alpha) is in no brief figure and no latency, at the workspace
    or at alpha; its decision A6 is still one of the workspace's approvals (a compliance
    record), attributed to no location."""
    workspace = _ok(analytics_world.read())
    alpha = _ok(analytics_world.read(location_id=analytics_world.seed.alpha))
    assert workspace["briefs"]["total"] == 5 and alpha["briefs"]["total"] == 3
    assert workspace["approval_latency"]["briefs"]["decided"] == 4
    assert alpha["approval_latency"]["briefs"]["decided"] == 2
    assert workspace["approvals"]["on_briefs"] == 6
    assert alpha["approvals"]["on_briefs"] == 2


def test_exports_are_counted_where_their_live_draft_is(analytics_world: AnalyticsWorld):
    """E1 and E2 (D1, at beta) count at beta; E3's draft D5 is soft-deleted, so it counts for
    the workspace only and is unassigned; alpha has none."""
    seed = analytics_world.seed
    assert _ok(analytics_world.read())["exports"]["total"] == 3
    assert _ok(analytics_world.read(location_id=seed.beta))["exports"]["total"] == 2
    assert _ok(analytics_world.read(location_id=seed.alpha))["exports"]["total"] == 0


def test_a_new_row_moves_the_figures_at_the_next_read(analytics_world: AnalyticsWorld):
    """Nothing is cached: an opportunity saved at alpha is in the next read, at the workspace
    and at alpha (on a workspace of its own, seeded the same way)."""
    from app.tests.fixtures.analytics import Rows, seed_six_families, set_capability

    world = analytics_world.world
    ws = world.workspace("analytics fresh read")
    with world.session() as s:
        seed = seed_six_families(s, organization_id=world.org, workspace_id=ws,
                                 author=world.users["marketer"],
                                 reviewer=world.users["reviewer"], exporter=world.users["owner"])
    set_capability(world, ws, "notifications_analytics")
    before = _ok(analytics_world.read(ws))
    assert _figures(before) == WORKSPACE_FIGURES
    with world.session() as s:
        Rows(s, organization_id=seed.organization_id, workspace_id=ws).opportunity(
            location_id=seed.alpha, status="saved"
        )
    after = _ok(analytics_world.read(ws))
    assert after["opportunities"]["total"] == 7 and after["opportunities"]["saved"] == 2
    assert after["locations"][0]["opportunities"] == ALPHA_TOTALS["opportunities"] + 1
    assert after["unassigned"] == UNASSIGNED
