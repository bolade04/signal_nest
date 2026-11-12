"""W21 ADDR: existing Scout searches stay usable whatever the structured business address holds.

The operator's instruction: "Existing Scout searches must remain usable; the new completeness
requirement applies to jurisdiction-dependent actions." The Scout never reads the business
address (``jobs/pipeline.py`` ``_coverage_rule_for`` copies nine other fields of the rule), so:

* **S-1** ``_coverage_rule_for`` gives the identical ``CoverageRule`` for a location whose rule
  has empty, complete and agreeing, contradicting, or confirmed structured fields, and for one
  whose rule has none; ``CoverageRule`` has no field for any of them;
* **S-2** a Scout run (the fixture connector, on the seeded demo database) for a location with
  empty fields, and for the same location with a contradicting, incomplete address, or one
  complete but for the town the location's city requires (operator instruction of 2026-11-12,
  item 3), scans, scores and decides exactly the same;
* **S-3** the seed leaves the structured fields empty (nothing copies the location in);
* **S-4** creating and running a Scout needs nothing of the structured fields (S-2 runs one
  whose address could not support a jurisdiction-dependent action).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

from app.db import seed as seed_mod
from app.db.models import Base
from app.geography.engine import CoverageRule
from app.jobs.pipeline import _coverage_rule_for, _run
from app.jurisdiction.business_address import resolve_for_action
from app.locations.models import BusinessLocation, GeoCoverageRule
from app.opportunities.models import Opportunity
from app.scouting_requests.models import ScoutRequest

NEW = {
    "business_address_street", "business_address_locality", "business_address_region",
    "business_address_postcode", "business_address_country", "locality_confirmed_locality",
    "locality_confirmed_city", "locality_confirmed_jurisdiction", "locality_confirmed_at",
    "locality_confirmed_by_user_id",
}
STATES = {
    "empty": {},
    "complete": {"business_address_country": "United States", "business_address_region": "TX",
                 "business_address_locality": "Dallas"},
    "contradicting": {"business_address_country": "Canada",
                      "business_address_region": "Ontario",
                      "business_address_locality": "Toronto"},
    "confirmed": {"business_address_country": "US", "business_address_region": "Texas",
                  "business_address_locality": "Irving",
                  "locality_confirmed_locality": "Irving", "locality_confirmed_city": "Dallas",
                  "locality_confirmed_jurisdiction": "US-TX",
                  "locality_confirmed_at": datetime(2026, 11, 12, tzinfo=UTC)},
    "incomplete": {"business_address_locality": "Toronto"},
    # Operator instruction of 2026-11-12, item 3: the town is required before jurisdiction-
    # dependent actions when the location has a city; a Scout search is not such an action.
    "no-town": {"business_address_country": "United States", "business_address_region": "TX"},
}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("addr-scout")
    engine = create_engine(
        f"sqlite:///{tmp / 'scout.db'}", connect_args={"check_same_thread": False}, future=True
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    original = seed_mod.SessionLocal
    seed_mod.SessionLocal = factory
    try:
        seed_mod.seed(reset=True)
        yield factory
    finally:
        seed_mod.SessionLocal = original
        engine.dispose()


def _dallas(s) -> BusinessLocation:
    return s.scalar(select(BusinessLocation).where(BusinessLocation.city == "Dallas"))


def _set(s, rule_id: str, fields: dict) -> None:
    s.execute(
        update(GeoCoverageRule.__table__)
        .where(GeoCoverageRule.__table__.c.id == rule_id)
        .values({**dict.fromkeys(NEW), **fields})
    )
    s.flush()


def test_the_seed_leaves_the_structured_fields_empty(world) -> None:
    with world() as s:
        rules = s.scalars(select(GeoCoverageRule)).all()
        assert len(rules) == 4
        for rule in rules:
            assert {c: getattr(rule, c) for c in NEW} == dict.fromkeys(NEW)
            location = s.get(BusinessLocation, rule.location_id)
            # And therefore no seeded location could capture a jurisdiction yet.
            assert resolve_for_action(location, [rule]).reason in {
                "business_address_incomplete", "business_address_text_conflict",
                "address_suffix_ambiguous",
            }


def test_the_coverage_rule_the_scout_uses_ignores_the_structured_fields(world) -> None:
    fields = {f.name for f in dataclasses.fields(CoverageRule)}
    assert not fields & NEW and "business_address" not in fields
    with world() as s:
        location = _dallas(s)
        rule = s.scalar(select(GeoCoverageRule).where(GeoCoverageRule.location_id == location.id))
        request = ScoutRequest(
            id="addr-s1", organization_id=location.organization_id,
            workspace_id=location.workspace_id, brand_id=location.brand_id,
            location_id=location.id, name="S-1", status="queued", source_types=[],
            keywords=["coffee"], resolved_market=None,
        )
        s.add(request)
        s.flush()
        seen = {}
        for state, values in STATES.items():
            _set(s, rule.id, values)
            seen[state] = _coverage_rule_for(s, request)
        s.rollback()
    assert len({repr(v) for v in seen.values()}) == 1, seen


def _outcome(s, request_id: str) -> list[tuple]:
    return sorted(
        s.execute(
            select(
                Opportunity.title, Opportunity.resolved_market, Opportunity.inside_scout_area,
                Opportunity.decision, Opportunity.opportunity_score,
            ).where(Opportunity.scout_request_id == request_id)
        ).all()
    )


def test_a_scout_run_is_the_same_whatever_the_structured_fields_hold(world) -> None:
    results = {}
    for state in ("empty", "contradicting", "incomplete", "no-town"):
        with world() as s:
            location = _dallas(s)
            rule = s.scalar(
                select(GeoCoverageRule).where(GeoCoverageRule.location_id == location.id)
            )
            _set(s, rule.id, STATES[state])
            assert resolve_for_action(location, [rule]).is_resolved is False
            request = ScoutRequest(
                id=f"addr-s2-{state}", organization_id=location.organization_id,
                workspace_id=location.workspace_id, brand_id=location.brand_id,
                location_id=location.id, name=f"S-2 {state}", status="queued", source_types=[],
                keywords=["coffee", "cold brew", "loyalty", "oat milk", "latte"],
                resolved_market="Dallas, TX",
            )
            s.add(request)
            s.flush()
            stats = _run(s, request.id)
            s.commit()
            results[state] = (stats["scanned"], stats["opportunities"], _outcome(s, request.id))
    assert results["empty"][0] > 0 and results["empty"][1] > 0
    assert results["contradicting"] == results["empty"]
    assert results["incomplete"] == results["empty"]
    assert results["no-town"] == results["empty"]  # the town rule changes no Scout search
