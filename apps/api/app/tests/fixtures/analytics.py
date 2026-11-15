"""The Phase 5E analytics seeded fixture (lane 5E-W1; Project Phase 5E plan §5).

The six-family, five-milestone seed behind exit criterion 4 ("analytics endpoints return
correct aggregates on seeded fixtures"): opportunities (Phase 4), recommendation briefs (5A),
approval decisions (5B), creative drafts and content reviews (5C), locations and
jurisdiction codes (5D) and exports (5E), written as rows with fixed timestamps so every
figure of ``GET /workspaces/{ws}/analytics`` is known in advance.

The root ``conftest.py`` star-imports this module (CF-3 payload), so a fixture is registered
by listing its name in ``__all__``. Application modules are imported inside the functions, not
at module level: the conftest imports this module before its offline guard is installed.

**The seed** (:func:`seed_six_families`; ``T0`` and ``T1 = T0 + 10 000 s`` are fixed instants).
Locations: ``alpha`` "Alpha Market" (active, ``US-TX``) and ``beta`` "Beta Market"
(deactivated, ``US-CA``).

======  ===========  ==========  =========================================================
row     location     state       notes
======  ===========  ==========  =========================================================
O1      alpha        new
O2      alpha        saved
O3      beta         actioned
O4      none         monitoring
O5      none         ignored
O6      alpha        new
B1v1    alpha (O1)   superseded  approved at T0+100 (A1), then B1v2 was written
B1v2    alpha (O1)   awaiting    no decision; created T0+50
B2      alpha (O2)   rejected    A2 at T0+200
B3      beta  (O3)   approved    A3 at T0+300
B4      none  (O4)   awaiting    approved at T0+1000 (A4), revoked at T0+1500 (A5); no code
B5      alpha (O6)   (deleted)   soft-deleted; approved at T0+5000 (A6)
D1      beta  (B3)   approved    review passed; A7 at T1+60; exported text/plain and json
D2      alpha (B1v1) superseded  review blocked; rejected at T1+120 (A8); not simulated
D3      alpha (B1v1) awaiting    revision of D2 (created T1+5); review passed; approved at
                                 T1+605 (A9), revoked at T1+905 (A10)
D4      none  (B4)   awaiting    a legacy draft with no code; review not passed (disclaimer)
D5      alpha (B2)   (deleted)   soft-deleted; review passed; A11 at T1+30; exported markdown
======  ===========  ==========  =========================================================

Briefs are created at ``T0`` (B1v2 at ``T0 + 50``), drafts at ``T1`` (D3 at ``T1 + 5``).
D1, D3, D4 and D5 are simulated. The expected figures are stated, field by field, in
``test_analytics_aggregates.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

#: The seed's fixed instants (UTC).
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
T1 = T0 + timedelta(seconds=10_000)
#: The two locations' names, in the order the comparison lists them (by name).
ALPHA_NAME = "Alpha Market"
BETA_NAME = "Beta Market"
ALPHA_CODE = "US-TX"
BETA_CODE = "US-CA"


def at(base: datetime, seconds: float) -> datetime:
    return base + timedelta(seconds=seconds)


@dataclass
class Seed:
    """The ids one :func:`seed_six_families` call wrote (keys as in the module table)."""

    organization_id: str
    workspace_id: str
    alpha: str = ""
    beta: str = ""
    ids: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Row builders (each adds and flushes one row; the caller owns the transaction)
# --------------------------------------------------------------------------- #
class Rows:
    """Builds the six families' rows in one workspace through the ORM (inserts only).

    Every row carries the workspace's organization and workspace ids. ``author`` and
    ``reviewer`` are user ids of the organization; their snapshots are taken from the users
    table, as the services do.
    """

    def __init__(self, s, *, organization_id: str, workspace_id: str, tag: str | None = None):
        from app.brands.service import get_primary_brand
        from app.scouting_requests.models import ScoutRequest

        self.s = s
        self.org = organization_id
        self.ws = workspace_id
        self.tag = tag or uuid.uuid4().hex[:10]
        self._n = 0
        self.brand_id = get_primary_brand(s, workspace_id).id
        self.scout_request_id = self._id("sr")
        s.add(ScoutRequest(id=self.scout_request_id, name="Analytics seed", brand_id=self.brand_id,
                           organization_id=self.org, workspace_id=self.ws))
        s.flush()

    def _id(self, kind: str) -> str:
        self._n += 1
        return f"{kind}{self.tag}{self._n:03d}"[:32]

    def snapshot(self, user_id: str) -> str:
        from app.core.actor_snapshot import actor_snapshot
        from app.organizations.models import User

        user = self.s.get(User, user_id)
        return actor_snapshot(user_id=user.id, full_name=user.full_name, email=user.email)

    def location(self, name: str, *, active: bool = True, code: str | None = None) -> str:
        from app.locations.models import BusinessLocation

        row = BusinessLocation(id=self._id("lo"), organization_id=self.org, workspace_id=self.ws,
                               brand_id=self.brand_id, name=name, is_active=active,
                               jurisdiction_code=code)
        self.s.add(row)
        self.s.flush()
        return row.id

    def opportunity(self, *, location_id: str | None, status: str = "new") -> str:
        from app.opportunities.models import Opportunity
        from app.signals.models import SignalCluster

        cluster = self._id("cl")
        self.s.add(SignalCluster(id=cluster, label="seed", workspace_id=self.ws,
                                 scout_request_id=self.scout_request_id))
        self.s.flush()
        row = Opportunity(id=self._id("op"), brand_id=self.brand_id, title="Seeded",
                          scout_request_id=self.scout_request_id, cluster_id=cluster,
                          classification="x", decision="act_now", location_id=location_id,
                          status=status, organization_id=self.org, workspace_id=self.ws)
        self.s.add(row)
        self.s.flush()
        return row.id

    def _record(self, opportunity_id: str) -> str:
        from app.intelligence.records import SignalIntelligenceRecord
        from app.signals.models import NormalizedSignal, RawSignal

        scoped = {"organization_id": self.org, "workspace_id": self.ws,
                  "scout_request_id": self.scout_request_id}
        raw, norm, record = self._id("rs"), self._id("ns"), self._id("in")
        self.s.add(RawSignal(id=raw, source_type="rss", content="seed", **scoped))
        self.s.flush()
        self.s.add(NormalizedSignal(id=norm, raw_signal_id=raw, source_type="rss",
                                    excerpt="seed", content_hash="0" * 64, **scoped))
        self.s.flush()
        self.s.add(SignalIntelligenceRecord(
            id=record, normalized_signal_id=norm, opportunity_id=opportunity_id,
            analysis_version="1", scoring_version="1", fingerprint="f" * 64, enricher="seed",
            accepted=True, classification="x", **scoped,
        ))
        self.s.flush()
        return record

    def brief(self, *, opportunity_id: str, author: str, version: int = 1,
              location_id: str | None, code: str | None, created_at: datetime,
              deleted: bool = False) -> str:
        from app.briefs.models import RecommendationBrief

        record = self._record(opportunity_id)
        row = RecommendationBrief(
            id=self._id("br"), organization_id=self.org, workspace_id=self.ws,
            opportunity_id=opportunity_id, intelligence_record_id=record, brief_version=version,
            location_id=location_id, analysis_version="1", scoring_version="1",
            prompt_name="explain_opportunity", prompt_version="1.2.0", model_identifier="mock-1",
            input_hash="0" * 64, is_simulated=True, created_by=author,
            created_by_snapshot=self.snapshot(author), summary="Seeded brief.",
            rationale="Inference: seeded.", recommended_actions=["Act."],
            suggested_angles=["Angle"], risk_notes=[], evidence_refs=[record],
            jurisdiction_code=code, created_at=created_at,
            deleted_at=created_at if deleted else None,
        )
        self.s.add(row)
        self.s.flush()
        return row.id

    def draft(self, *, brief_id: str, author: str, version: int = 1, parent: str | None = None,
              simulated: bool = True, created_at: datetime, deleted: bool = False,
              body: str = "Seeded draft body.") -> str:
        from app.briefs.models import RecommendationBrief
        from app.creative.models import CreativeDraft

        brief = self.s.get(RecommendationBrief, brief_id)
        row = CreativeDraft(
            id=self._id("dr"), organization_id=self.org, workspace_id=self.ws,
            brief_id=brief_id, opportunity_id=brief.opportunity_id,
            location_id=brief.location_id, draft_version=version, parent_draft_id=parent,
            channel="social_post", headline="Seeded", body=body, call_to_action="Visit",
            prompt_name="generate_creative", prompt_version="1.0.0", model_identifier="mock-1",
            input_hash="0" * 64, is_simulated=simulated, input_tokens=0, output_tokens=0,
            estimated_cost_usd=Decimal("0"), created_by=author,
            created_by_snapshot=self.snapshot(author),
            jurisdiction_code=brief.jurisdiction_code, created_at=created_at,
            deleted_at=created_at if deleted else None,
        )
        self.s.add(row)
        self.s.flush()
        return row.id

    def review(self, *, draft_id: str, outcome: str) -> str:
        """``outcome``: ``passed``, ``blocked`` or ``not_passed`` (a missing disclaimer)."""
        from app.creative.models import ContentReview, CreativeDraft

        draft = self.s.get(CreativeDraft, draft_id)
        blocked = outcome == "blocked"
        row = ContentReview(
            id=self._id("rv"), organization_id=self.org, workspace_id=self.ws,
            draft_id=draft_id, draft_version=draft.draft_version,
            risk_level="blocked" if blocked else "low", is_blocked=blocked, findings=[],
            warnings=[], blocked_claim_ids=[], required_disclaimers=[],
            disclaimers_resolved=outcome != "not_passed", passed=outcome == "passed",
            jurisdiction_code=draft.jurisdiction_code,
        )
        self.s.add(row)
        self.s.flush()
        return row.id

    def decision(self, *, subject: str, kind: str, decision: str, seq: int, reviewer: str,
                 decided_at: datetime) -> str:
        """One 5B decision on a brief (``kind="brief"``) or a draft (``kind="draft"``)."""
        from app.approvals.models import Approval
        from app.briefs.models import RecommendationBrief
        from app.creative.models import CreativeDraft

        if kind == "brief":
            version = self.s.get(RecommendationBrief, subject).brief_version
            links = {"subject_type": "recommendation_brief", "brief_id": subject}
        else:
            version = self.s.get(CreativeDraft, subject).draft_version
            links = {"subject_type": "creative_draft", "draft_id": subject}
        row = Approval(
            id=self._id("ap"), organization_id=self.org, workspace_id=self.ws,
            subject_id=subject, subject_version=version, decision=decision, decision_seq=seq,
            decided_by=reviewer, decided_by_snapshot=self.snapshot(reviewer),
            decided_role="reviewer", is_simulated=False, created_at=decided_at, **links,
        )
        self.s.add(row)
        self.s.flush()
        return row.id

    def export(self, *, draft_id: str, approval_id: str, fmt: str, exporter: str) -> str:
        from app.creative.models import CreativeDraft
        from app.exports.models import Export

        draft = self.s.get(CreativeDraft, draft_id)
        row = Export(
            id=self._id("ex"), organization_id=self.org, workspace_id=self.ws,
            draft_id=draft_id, draft_version=draft.draft_version, approval_id=approval_id,
            format=fmt, jurisdiction_code=draft.jurisdiction_code, exported_by=exporter,
            exported_by_snapshot=self.snapshot(exporter), is_simulated=draft.is_simulated,
        )
        self.s.add(row)
        self.s.flush()
        return row.id


def seed_six_families(
    s, *, organization_id: str, workspace_id: str, author: str, reviewer: str, exporter: str,
    tag: str | None = None,
) -> Seed:
    """Write the module table's rows into one workspace (flushes; the caller commits)."""
    r = Rows(s, organization_id=organization_id, workspace_id=workspace_id, tag=tag)
    seed = Seed(organization_id, workspace_id)
    seed.alpha = alpha = r.location(ALPHA_NAME, active=True, code=ALPHA_CODE)
    seed.beta = beta = r.location(BETA_NAME, active=False, code=BETA_CODE)
    ids = seed.ids

    ids["O1"] = r.opportunity(location_id=alpha, status="new")
    ids["O2"] = r.opportunity(location_id=alpha, status="saved")
    ids["O3"] = r.opportunity(location_id=beta, status="actioned")
    ids["O4"] = r.opportunity(location_id=None, status="monitoring")
    ids["O5"] = r.opportunity(location_id=None, status="ignored")
    ids["O6"] = r.opportunity(location_id=alpha, status="new")

    def brief(key, opp, loc, code, *, version=1, created=T0, deleted=False):
        ids[key] = r.brief(opportunity_id=ids[opp], author=author, version=version,
                           location_id=loc, code=code, created_at=created, deleted=deleted)

    brief("B1v1", "O1", alpha, ALPHA_CODE)
    brief("B1v2", "O1", alpha, ALPHA_CODE, version=2, created=at(T0, 50))
    brief("B2", "O2", alpha, ALPHA_CODE)
    brief("B3", "O3", beta, BETA_CODE)
    brief("B4", "O4", None, None)
    brief("B5", "O6", alpha, ALPHA_CODE, deleted=True)

    def decide(key, subject, kind, decision, seq, when):
        ids[key] = r.decision(subject=ids[subject], kind=kind, decision=decision, seq=seq,
                              reviewer=reviewer, decided_at=when)

    decide("A1", "B1v1", "brief", "approved", 1, at(T0, 100))
    decide("A2", "B2", "brief", "rejected", 1, at(T0, 200))
    decide("A3", "B3", "brief", "approved", 1, at(T0, 300))
    decide("A4", "B4", "brief", "approved", 1, at(T0, 1000))
    decide("A5", "B4", "brief", "revoked", 2, at(T0, 1500))
    decide("A6", "B5", "brief", "approved", 1, at(T0, 5000))

    ids["D1"] = r.draft(brief_id=ids["B3"], author=author, created_at=T1)
    ids["D2"] = r.draft(brief_id=ids["B1v1"], author=author, simulated=False, created_at=T1)
    ids["D3"] = r.draft(brief_id=ids["B1v1"], author=author, version=2, parent=ids["D2"],
                        created_at=at(T1, 5))
    ids["D4"] = r.draft(brief_id=ids["B4"], author=author, created_at=T1)
    ids["D5"] = r.draft(brief_id=ids["B2"], author=author, created_at=T1, deleted=True)
    for key, outcome in (("D1", "passed"), ("D2", "blocked"), ("D3", "passed"),
                         ("D4", "not_passed"), ("D5", "passed")):
        ids["R" + key[1:]] = r.review(draft_id=ids[key], outcome=outcome)

    decide("A7", "D1", "draft", "approved", 1, at(T1, 60))
    decide("A8", "D2", "draft", "rejected", 1, at(T1, 120))
    decide("A9", "D3", "draft", "approved", 1, at(T1, 605))
    decide("A10", "D3", "draft", "revoked", 2, at(T1, 905))
    decide("A11", "D5", "draft", "approved", 1, at(T1, 30))

    ids["E1"] = r.export(draft_id=ids["D1"], approval_id=ids["A7"], fmt="text/plain",
                         exporter=exporter)
    ids["E2"] = r.export(draft_id=ids["D1"], approval_id=ids["A7"], fmt="json",
                         exporter=exporter)
    ids["E3"] = r.export(draft_id=ids["D5"], approval_id=ids["A11"], fmt="text/markdown",
                         exporter=exporter)
    return seed


# --------------------------------------------------------------------------- #
# The world: a real HTTP application on SQLite, and on PostgreSQL when configured
# --------------------------------------------------------------------------- #
def set_capability(world, workspace_id: str, capability: str, enabled: bool = True) -> None:
    """Store (or replace) a workspace override; the global flags stay off."""
    from sqlalchemy import delete

    from app.capabilities.models import WorkspaceCapabilityOverride

    with world.session() as s:
        s.execute(delete(WorkspaceCapabilityOverride).where(
            WorkspaceCapabilityOverride.workspace_id == workspace_id,
            WorkspaceCapabilityOverride.capability == capability,
        ))
        s.add(WorkspaceCapabilityOverride(
            organization_id=world.org_of(workspace_id), workspace_id=workspace_id,
            capability=capability, enabled=enabled, set_by_user_id=world.owner_id(workspace_id),
            reason="5E-W1 test",
        ))


@contextmanager
def creative_world_on(engine: str, tmp_path_factory, key: str) -> Iterator:
    """The 5C test world (``app.tests._creative_support``) on ``engine``.

    ``postgresql`` uses a database of its own, migrated to head; it is skipped where
    ``TEST_POSTGRES_URL`` is unset, and fails in CI (``require_postgres``)."""
    from app.tests._creative_support import creative_world, postgres_creative_world

    if engine == "sqlite":
        with creative_world(tmp_path_factory.mktemp(key) / "api.db", key) as world:
            yield world
    else:  # pragma: no cover - gated on PostgreSQL
        with postgres_creative_world(key) as world:
            yield world


@dataclass
class AnalyticsWorld:
    """A world whose workspace ``seed.workspace_id`` holds the seed, with O-12(B) on there.

    The sibling workspace (``world.ws2``) and organization B's workspace hold the same seed as
    noise, with O-12(B) off: nothing of theirs may reach the seeded workspace's figures."""

    world: object
    seed: Seed

    def read(self, workspace_id: str | None = None, *, role: str = "owner",
             location_id: str | None = None):
        path = f"/workspaces/{workspace_id or self.seed.workspace_id}/analytics"
        if location_id is not None:
            path += f"?location_id={location_id}"
        return self.world.send("GET", path, role)


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def analytics_world(request, tmp_path_factory) -> Iterator[AnalyticsWorld]:
    """The seeded analytics world, once per test module and engine."""
    with creative_world_on(request.param, tmp_path_factory, "w1analytics") as world:
        ws = world.workspace("analytics seeded")
        users = world.users
        with world.session() as s:
            seed = seed_six_families(s, organization_id=world.org, workspace_id=ws,
                                     author=users["marketer"], reviewer=users["reviewer"],
                                     exporter=users["owner"])
            seed_six_families(s, organization_id=world.org, workspace_id=world.ws2,
                              author=users["marketer"], reviewer=users["reviewer"],
                              exporter=users["owner"])
            seed_six_families(s, organization_id=world.org_b, workspace_id=world.ws_b,
                              author=world.owner_b_id, reviewer=world.owner_b_id,
                              exporter=world.owner_b_id)
        set_capability(world, ws, "notifications_analytics")
        yield AnalyticsWorld(world, seed)


__all__ = ["analytics_world"]
