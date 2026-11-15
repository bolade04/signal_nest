"""Project Phase 5E (lane 5E-W4): status analytics tenant isolation (5E plan §2; exit 4).

``GET /workspaces/{ws}/analytics`` aggregates the six families (opportunities, briefs,
approval decisions, drafts, content reviews, exports) of the path workspace only:

* **Cross-workspace:** rows of a sibling workspace of the same organization, and of another
  organization, never count, in any family, in the workspace view, a location view or the
  per-location comparison; adding them changes no figure. The comparison lists only this
  workspace's locations.
* **Cross-location:** a ``location_id`` that is not one of this workspace's locations (a
  sibling workspace's, another organization's, or unknown) answers one uniform 404, the
  same body for all three; this workspace's own location answers 200 with that location's
  figures only.
* **Membership** stays 403 for a non-member naming the workspace, with or without a location.

Analytics is opened per workspace by a ``notifications_analytics`` override (O-12(B); the flag
stays ``False``). Rows are written directly on real evidence chains (the 5C harness world), on
SQLite and, with ``TEST_POSTGRES_URL``, on PostgreSQL at head.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import delete

from app.approvals.models import Approval
from app.brands.service import get_primary_brand
from app.briefs.models import RecommendationBrief
from app.capabilities.models import WorkspaceCapabilityOverride
from app.core.actor_snapshot import actor_snapshot
from app.core.config import get_settings
from app.creative.models import ContentReview, CreativeDraft
from app.exports.models import Export
from app.intelligence.records import SignalIntelligenceRecord
from app.locations.models import BusinessLocation
from app.opportunities.models import Opportunity
from app.organizations.models import User
from app.scouting_requests.models import ScoutRequest
from app.signals.models import NormalizedSignal, RawSignal, SignalCluster
from app.tests._creative_support import CreativeWorld, creative_world, postgres_creative_world

CAP = "notifications_analytics"
LOCATION_404 = {"code": "not_found", "message": "Location not found in this workspace."}
MEMBERSHIP = {"code": "permission_denied", "message": "You are not a member of this organization."}
FAMILIES = ("opportunities", "briefs", "approvals", "drafts", "reviews", "exports")
COMPARED = ("opportunities", "briefs", "drafts", "approvals", "exports")


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def world(request, tmp_path_factory) -> Iterator[CreativeWorld]:
    if request.param == "sqlite":
        with creative_world(tmp_path_factory.mktemp("w4ai") / "a.db", "w4ai") as w:
            yield w
        return
    with postgres_creative_world("w4ai") as w:  # skips unless TEST_POSTGRES_URL is set
        yield w


@pytest.fixture(autouse=True)
def flag_off(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "notifications_analytics_enabled", False)


def set_cap(world: CreativeWorld, ws: str, enabled: bool | None) -> None:
    with world.session() as s:
        s.execute(
            delete(WorkspaceCapabilityOverride).where(
                WorkspaceCapabilityOverride.workspace_id == ws,
                WorkspaceCapabilityOverride.capability == CAP,
            )
        )
        if enabled is not None:
            s.add(WorkspaceCapabilityOverride(
                organization_id=world.org_of(ws), workspace_id=ws, capability=CAP,
                enabled=enabled, set_by_user_id=world.owner_id(ws), reason="5E-W4 test",
            ))


def location(world: CreativeWorld, ws: str, name: str) -> str:
    with world.session() as s:
        row = BusinessLocation(
            organization_id=world.org_of(ws), workspace_id=ws,
            brand_id=get_primary_brand(s, ws).id, name=name, is_active=True,
        )
        s.add(row)
        s.flush()
        return row.id


def family(world: CreativeWorld, ws: str, location_id: str | None = None) -> None:
    """One row of each family on one evidence chain: an opportunity, an approved brief, an
    approved draft with a passed review, and one export of it, all at ``location_id``."""
    org, tag = world.org_of(ws), uuid.uuid4().hex[:12]
    actor = world.owner_id(ws) if ws == world.ws_b else world.users["reviewer"]
    with world.session() as s:
        user = s.get(User, actor)
        snap = actor_snapshot(user_id=user.id, full_name=user.full_name, email=user.email)
        brand = get_primary_brand(s, ws)
        scoped = {"organization_id": org, "workspace_id": ws}
        s.add(ScoutRequest(id=f"sr{tag}", name="Scout", brand_id=brand.id, **scoped))
        s.flush()
        req = {**scoped, "scout_request_id": f"sr{tag}"}
        s.add(RawSignal(id=f"rs{tag}", source_type="rss", content="parking", **req))
        s.add(SignalCluster(id=f"cl{tag}", label="parking", workspace_id=ws,
                            scout_request_id=f"sr{tag}"))  # fmt: skip
        s.flush()
        s.add(NormalizedSignal(id=f"ns{tag}", raw_signal_id=f"rs{tag}", cluster_id=f"cl{tag}",
                               source_type="rss", excerpt="parking", content_hash="0" * 64,
                               **req))  # fmt: skip
        s.flush()
        s.add(Opportunity(id=f"op{tag}", brand_id=brand.id, title="Parking",
                          scout_request_id=f"sr{tag}", cluster_id=f"cl{tag}",
                          classification="x", decision="act_now", location_id=location_id,
                          **scoped))  # fmt: skip
        s.flush()
        s.add(SignalIntelligenceRecord(
            id=f"in{tag}", normalized_signal_id=f"ns{tag}", opportunity_id=f"op{tag}",
            analysis_version="1", scoring_version="1", fingerprint="f" * 64, enricher="test",
            accepted=True, classification="x", **req,
        ))
        s.flush()
        s.add(RecommendationBrief(
            id=f"br{tag}", opportunity_id=f"op{tag}", intelligence_record_id=f"in{tag}",
            brief_version=1, analysis_version="1", scoring_version="1",
            prompt_name="explain_opportunity", prompt_version="1.2.0",
            model_identifier="mock-1", input_hash="0" * 64, is_simulated=True,
            created_by=actor, created_by_snapshot=snap, summary="s", rationale="r",
            recommended_actions=[], suggested_angles=[], risk_notes=[], evidence_refs=[],
            jurisdiction_code="US-TX", location_id=location_id, **scoped,
        ))
        s.flush()
        s.add(CreativeDraft(
            id=f"dr{tag}", brief_id=f"br{tag}", opportunity_id=f"op{tag}", draft_version=1,
            channel="social_post", body="Weekend parking is easy.",
            prompt_name="generate_creative", prompt_version="1.0.0", model_identifier="mock-1",
            input_hash="0" * 64, is_simulated=True, input_tokens=1, output_tokens=1,
            estimated_cost_usd=0, jurisdiction_code="US-TX", created_by=actor,
            created_by_snapshot=snap, location_id=location_id, **scoped,
        ))
        s.flush()
        s.add(ContentReview(
            draft_id=f"dr{tag}", draft_version=1, risk_level="low", is_blocked=False,
            findings=[], warnings=[], blocked_claim_ids=[], required_disclaimers=[],
            disclaimers_resolved=True, passed=True, jurisdiction_code="US-TX", **scoped,
        ))
        for subject, kind, link in ((f"br{tag}", "recommendation_brief", "brief_id"),
                                    (f"dr{tag}", "creative_draft", "draft_id")):  # fmt: skip
            s.add(Approval(
                id=f"ap{kind[0]}{tag}", subject_type=kind, subject_id=subject, subject_version=1,
                decision="approved", decision_seq=1, decided_by=actor, decided_by_snapshot=snap,
                decided_role="reviewer", is_simulated=False, **{link: subject}, **scoped,
            ))
        s.flush()
        s.add(Export(
            draft_id=f"dr{tag}", draft_version=1, approval_id=f"apc{tag}", format="text/plain",
            jurisdiction_code="US-TX", exported_by=actor, exported_by_snapshot=snap,
            is_simulated=True, **scoped,
        ))


def read(world: CreativeWorld, ws: str, role: str = "owner", *, location_id: str | None = None,
         token: str | None = None):  # fmt: skip
    query = f"?location_id={location_id}" if location_id is not None else ""
    return world.send("GET", f"/workspaces/{ws}/analytics{query}", role, token=token)


def totals(body: dict) -> dict[str, int]:
    return {f: body[f]["total"] for f in FAMILIES}


def error(r) -> dict:
    return {k: v for k, v in r.json()["error"].items() if k != "request_id"}


@pytest.fixture
def ws(world: CreativeWorld, request) -> str:
    ws = world.workspace(f"a {request.node.name[:40]}", enabled=False)
    set_cap(world, ws, True)
    return ws


# --------------------------------------------------------------------------- #
# Cross-workspace
# --------------------------------------------------------------------------- #
def test_other_workspaces_rows_never_count(world, ws):
    here = location(world, ws, "Here")
    family(world, ws, here)
    family(world, ws, None)
    expected = {"opportunities": 2, "briefs": 2, "approvals": 4, "drafts": 2, "reviews": 2,
                "exports": 2}  # fmt: skip
    first = read(world, ws)
    assert first.status_code == 200, first.text
    assert totals(first.json()) == expected

    # A sibling workspace of the same organization, and another organization, fill up.
    sibling_location = location(world, world.ws2, "Sibling")
    foreign_location = location(world, world.ws_b, "Foreign")
    for _ in range(2):
        family(world, world.ws2, sibling_location)
        family(world, world.ws2, None)
        family(world, world.ws_b, foreign_location)
        family(world, world.ws_b, None)

    for role in ("owner", "admin", "marketer", "reviewer"):
        body = read(world, ws, role).json()
        assert body["workspace_id"] == ws
        assert totals(body) == expected, role
        assert [row["location_id"] for row in body["locations"]] == [here]
        assert {c: body["locations"][0][c] for c in COMPARED} == {
            "opportunities": 1, "briefs": 1, "drafts": 1, "approvals": 2, "exports": 1
        }
        assert body["unassigned"] == {
            "opportunities": 1, "briefs": 1, "drafts": 1, "approvals": 2, "exports": 1
        }
        assert body["approvals"]["on_briefs"] == 2 and body["approvals"]["on_drafts"] == 2
        assert body["exports"]["plain_text"] == 2
        assert body["approval_latency"]["briefs"]["decided"] == 2
        assert body["approval_latency"]["drafts"]["decided"] == 2
        assert body["outcomes"]["drafts_generated"] == 2

    located = read(world, ws, location_id=here).json()
    assert totals(located) == {"opportunities": 1, "briefs": 1, "approvals": 2, "drafts": 1,
                               "reviews": 1, "exports": 1}  # fmt: skip


def test_a_sibling_workspace_sees_its_own_rows_not_these(world, ws):
    """The other direction: the sibling's analytics never count this workspace's rows."""
    sibling = world.workspace("a sibling", enabled=False)
    set_cap(world, sibling, True)
    family(world, ws, location(world, ws, "Mine"))
    assert totals(read(world, sibling).json()) == dict.fromkeys(FAMILIES, 0)
    family(world, sibling, None)
    assert totals(read(world, sibling).json()) == dict.fromkeys(FAMILIES, 1) | {"approvals": 2}
    assert read(world, sibling).json()["locations"] == []


# --------------------------------------------------------------------------- #
# Cross-location
# --------------------------------------------------------------------------- #
def test_a_location_outside_the_workspace_is_one_uniform_404(world, ws):
    own = location(world, ws, "Own")
    family(world, ws, own)
    sibling = location(world, world.ws2, "Sibling shop")
    foreign = location(world, world.ws_b, "Foreign shop")
    family(world, world.ws2, sibling)
    family(world, world.ws_b, foreign)
    unknown = read(world, ws, location_id=uuid.uuid4().hex)
    assert unknown.status_code == 404 and error(unknown) == LOCATION_404
    for role in ("owner", "admin", "marketer", "reviewer"):
        for location_id in (sibling, foreign):
            r = read(world, ws, role, location_id=location_id)
            assert r.status_code == 404, (role, location_id, r.text)
            assert error(r) == error(unknown)
            assert "Sibling" not in r.text and "Foreign" not in r.text
    mine = read(world, ws, location_id=own)
    assert mine.status_code == 200
    assert mine.json()["location_id"] == own
    assert [row["location_id"] for row in mine.json()["locations"]] == [own]
    assert mine.json()["unassigned"] is None


def test_an_inactive_location_of_the_workspace_is_still_its_own(world, ws):
    own = location(world, ws, "Closed")
    family(world, ws, own)
    with world.session() as s:
        s.get(BusinessLocation, own).is_active = False
    r = read(world, ws, location_id=own)
    assert r.status_code == 200 and totals(r.json())["opportunities"] == 1
    assert r.json()["locations"][0]["is_active"] is False


def test_each_location_view_counts_only_that_locations_rows(world, ws):
    first, second = location(world, ws, "First"), location(world, ws, "Second")
    family(world, ws, first)
    family(world, ws, second)
    family(world, ws, second)
    family(world, ws, None)
    one = totals(read(world, ws, location_id=first).json())
    two = totals(read(world, ws, location_id=second).json())
    assert one == {"opportunities": 1, "briefs": 1, "approvals": 2, "drafts": 1, "reviews": 1,
                   "exports": 1}  # fmt: skip
    assert two == {"opportunities": 2, "briefs": 2, "approvals": 4, "drafts": 2, "reviews": 2,
                   "exports": 2}  # fmt: skip
    whole = read(world, ws).json()
    by_id = {row["location_id"]: row for row in whole["locations"]}
    for column in COMPARED:  # the comparison adds up to the workspace's totals exactly
        assert by_id[first][column] + by_id[second][column] + whole["unassigned"][column] == (
            whole[column]["total"]
        )


# --------------------------------------------------------------------------- #
# Membership
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("capability", [True, None])
def test_a_non_member_gets_the_membership_403(world, ws, capability):
    set_cap(world, ws, capability)
    own = location(world, ws, "Member only")
    for location_id in (None, own, uuid.uuid4().hex):
        r = read(world, ws, location_id=location_id, token=world.owner_b)
        assert r.status_code == 403 and error(r) == MEMBERSHIP
    set_cap(world, world.ws_b, True)
    for role in ("owner", "admin", "marketer", "reviewer", "compliance_reviewer", "viewer"):
        r = read(world, world.ws_b, role)
        assert r.status_code == 403 and error(r) == MEMBERSHIP
