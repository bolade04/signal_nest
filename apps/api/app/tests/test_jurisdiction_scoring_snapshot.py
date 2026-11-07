"""Phase 5D exit criterion 8(b): the scoring-snapshot comparison that bounds the live
exclusions-before-short-circuit fix in ``market_in_coverage``.

The fix runs in the live Scout pipeline (``resolve_geo`` -> ``inside_scout_area`` ->
``decide``). The plan bounds it with a scoring-snapshot comparison over the seeded
four-market fixtures that proves no unrelated scoring output moves. Two throwaway worlds
are built the same way: each is seeded (``app.db.seed``, which runs the four seeded Scouts
through the worker path) and then runs the same scenarios. In the ``legacy`` world the
CF-3 ``market_in_coverage`` (kept verbatim in ``app.tests._legacy_coverage``, base
``daa898cd``) is patched in for the whole build; the ``current`` world runs the code under
test. Every scoring output of every run is projected (all columns of the scoring tables
except identity and time) and the two snapshots are compared field by field.

* Scenarios the fix does not target (the seeded radius rules, ``online`` and
  ``online_global`` rules without exclusions or with exclusions of other markets and of
  confusable places, a radius rule with exclusions, the unscoped global Scout) must be
  **identical**.
* Scenarios it targets (an ``online`` / ``online_global`` rule that excludes the
  location's own market) must move, and only in the fields downstream of
  ``inside_scout_area``: the evidence flag, the card flag, ``decision`` and the two fields
  derived from it (``recommended_action``, ``urgency``). No score moves.
* A control perturbs each scoring column in the ``current`` world (rolled back) and
  checks that the comparison sees it, so a snapshot that stops comparing a field fails.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import seed as seed_mod
from app.db.base import Base
from app.geography import engine as engine_mod
from app.geography.engine import CoverageRule
from app.geography.geocoder import fixture_geocode
from app.jobs.pipeline import _run
from app.locations.models import BusinessLocation, GeoCoverageRule
from app.opportunities.models import Opportunity, OpportunityScore, ValidationEvidence
from app.scouting_requests.models import ScoutRequest
from app.signals.models import NormalizedSignal, SignalLocationEvidence
from app.tests._legacy_coverage import legacy_market_in_coverage

# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

CITIES = seed_mod.CITIES
MARKETS = [c["market"] for c in CITIES]

#: Places that share a name with a seeded market, or contain one, but are other places.
CONFUSABLES = ["York", "London, Ontario", "Lagos, Portugal", "Dallas, GA", "Paris, France"]


def _others(market: str) -> list[str]:
    return [m for m in MARKETS if m != market]


#: variant -> (coverage rule fields given the city's market, does the fix target it?)
VARIANTS: dict[str, tuple[Any, bool]] = {
    "online": (lambda m: {"coverage_type": "online"}, False),
    "online-global": (
        lambda m: {"coverage_type": "radius", "online_global": True, "included_markets": [m]},
        False,
    ),
    "online-excludes-others": (
        lambda m: {"coverage_type": "online", "excluded_markets": _others(m)},
        False,
    ),
    "online-global-excludes-others": (
        lambda m: {
            "coverage_type": "radius",
            "online_global": True,
            "included_markets": [m],
            "excluded_markets": _others(m),
        },
        False,
    ),
    "online-global-excludes-confusables": (
        lambda m: {
            "coverage_type": "radius",
            "online_global": True,
            "excluded_markets": CONFUSABLES,
        },
        False,
    ),
    "radius-excludes-others": (
        lambda m: {
            "coverage_type": "radius",
            "radius_miles": 25,
            "included_markets": [m],
            "excluded_markets": _others(m),
        },
        False,
    ),
    "online-excludes-own": (
        lambda m: {"coverage_type": "online", "excluded_markets": [m]},
        True,
    ),
    "online-global-excludes-own": (
        lambda m: {
            "coverage_type": "radius",
            "online_global": True,
            "included_markets": [m],
            "excluded_markets": [m],
        },
        True,
    ),
}

TARGETED = frozenset(
    f"{variant}:{c['key']}" for variant, (_, hit) in VARIANTS.items() if hit for c in CITIES
)

# --------------------------------------------------------------------------- #
# The projection: every column of the scoring tables except identity and time.
# --------------------------------------------------------------------------- #

#: Identity, tenancy, foreign-key and time columns: not scoring output. Anything else in
#: these tables is compared (``test_the_snapshot_covers_every_scoring_column``).
NOT_COMPARED: dict[str, frozenset[str]] = {
    "normalized_signals": frozenset(
        {
            "id",
            "created_at",
            "updated_at",
            "organization_id",
            "workspace_id",
            "scout_request_id",
            "raw_signal_id",
            "cluster_id",
            "published_at",
        }
    ),
    "signal_location_evidence": frozenset(
        {"id", "created_at", "updated_at", "workspace_id", "normalized_signal_id"}
    ),
    "opportunities": frozenset(
        {
            "id",
            "created_at",
            "updated_at",
            "organization_id",
            "workspace_id",
            "brand_id",
            "scout_request_id",
            "location_id",
            "campaign_id",
            "cluster_id",
        }
    ),
    "opportunity_scores": frozenset(
        {"id", "created_at", "updated_at", "workspace_id", "opportunity_id"}
    ),
    "validation_evidence": frozenset(
        {"id", "created_at", "updated_at", "workspace_id", "opportunity_id"}
    ),
}

_MODELS = {
    "normalized_signals": NormalizedSignal,
    "signal_location_evidence": SignalLocationEvidence,
    "opportunities": Opportunity,
    "opportunity_scores": OpportunityScore,
    "validation_evidence": ValidationEvidence,
}


def _fields(table: str) -> list[str]:
    return [c.name for c in _MODELS[table].__table__.columns if c.name not in NOT_COMPARED[table]]


#: Fields the fix may move, and only in the scenarios it targets: the geo flag and what
#: ``decide`` derives from it (``pipeline._build_opportunities``).
DOWNSTREAM_OF_GEO = frozenset(
    {
        ("signal_location_evidence", "inside_scout_area"),
        ("opportunities", "inside_scout_area"),
        ("opportunities", "decision"),
        ("opportunities", "recommended_action"),
        ("opportunities", "urgency"),
    }
)

#: Run statistics compared for every Scout.
STATS_KEYS = (
    "scanned",
    "noise_filtered",
    "signals_analyzed",
    "opportunities",
    "opportunities_new",
    "opportunities_seen_again",
)


def _row(obj: Any, table: str) -> dict[str, Any]:
    return {name: getattr(obj, name) for name in _fields(table)}


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _project(session, requests: dict[str, str]) -> dict[tuple, Any]:
    """Flat snapshot: (scenario, table, key, field) -> value."""
    flat: dict[tuple, Any] = {}
    for scenario, request_id in sorted(requests.items()):
        request = session.get(ScoutRequest, request_id)
        for key in STATS_KEYS:
            flat[(scenario, "stats", "-", key)] = (request.stats or {}).get(key)
        signals = session.scalars(
            select(NormalizedSignal).where(NormalizedSignal.scout_request_id == request_id)
        ).all()
        by_hash: dict[str, list[dict]] = {}
        for signal in signals:
            row = _row(signal, "normalized_signals")
            evidence = session.scalars(
                select(SignalLocationEvidence).where(
                    SignalLocationEvidence.normalized_signal_id == signal.id
                )
            ).all()
            row["geo"] = sorted((_row(e, "signal_location_evidence") for e in evidence), key=_canon)
            by_hash.setdefault(signal.content_hash, []).append(row)
        for content_hash, rows in by_hash.items():
            for index, row in enumerate(sorted(rows, key=_canon)):
                key = f"{content_hash}#{index}"
                for name, value in row.items():
                    if name == "geo":
                        for g_index, geo in enumerate(value):
                            for g_name, g_value in geo.items():
                                flat[
                                    (
                                        scenario,
                                        "signal_location_evidence",
                                        f"{key}/{g_index}",
                                        g_name,
                                    )
                                ] = g_value
                        flat[(scenario, "signal_location_evidence", key, "count")] = len(value)
                    else:
                        flat[(scenario, "normalized_signals", key, name)] = value
        cards = session.scalars(
            select(Opportunity).where(Opportunity.scout_request_id == request_id)
        ).all()
        flat[(scenario, "opportunities", "-", "count")] = len(cards)
        for card in cards:
            key = card.finding_key
            for name, value in _row(card, "opportunities").items():
                flat[(scenario, "opportunities", key, name)] = value
            for table, model in (
                ("opportunity_scores", OpportunityScore),
                ("validation_evidence", ValidationEvidence),
            ):
                rows = sorted(
                    (
                        _row(r, table)
                        for r in session.scalars(
                            select(model).where(model.opportunity_id == card.id)
                        ).all()
                    ),
                    key=_canon,
                )
                flat[(scenario, table, key, "count")] = len(rows)
                for index, row in enumerate(rows):
                    for name, value in row.items():
                        flat[(scenario, table, f"{key}#{index}", name)] = value
    return flat


def _moved(before: dict[tuple, Any], after: dict[tuple, Any]) -> set[tuple]:
    """Every path whose value differs, or that exists on one side only."""
    return {
        path
        for path in before.keys() | after.keys()
        if path not in before or path not in after or _canon(before[path]) != _canon(after[path])
    }


# --------------------------------------------------------------------------- #
# The two worlds
# --------------------------------------------------------------------------- #


def _add_scenario(s, ids: dict, *, scenario: str, city: dict, coverage: dict | None) -> str:
    geo = fixture_geocode(city["geocode"])
    location_id = None
    if city is not None and coverage is not None:
        location = BusinessLocation(
            id=f"loc-{scenario}",
            organization_id=ids["organization"],
            workspace_id=ids["workspace"],
            brand_id=ids["brand"],
            name=f"Location {scenario}",
            city=geo.city,
            state_province=geo.state_province,
            country=geo.country,
            latitude=geo.latitude,
            longitude=geo.longitude,
            is_active=True,
        )
        s.add(location)
        s.add(
            GeoCoverageRule(
                id=f"geo-{scenario}",
                organization_id=ids["organization"],
                workspace_id=ids["workspace"],
                location_id=location.id,
                business_address=f"{geo.city}, {geo.country}",
                center_latitude=geo.latitude,
                center_longitude=geo.longitude,
                **coverage,
            )
        )
        location_id = location.id
    request = ScoutRequest(
        id=f"scout-{scenario}",
        organization_id=ids["organization"],
        workspace_id=ids["workspace"],
        brand_id=ids["brand"],
        location_id=location_id,
        name=f"Scout {scenario}",
        status="queued",
        source_types=[],
        keywords=["coffee", "cold brew", "loyalty", *city["audience"]["keywords"]],
        resolved_market=city["market"] if location_id else None,
    )
    s.add(request)
    s.flush()
    return request.id


def _build_world(tmp_path_factory, *, legacy: bool) -> tuple[Any, dict[str, str]]:
    tmp = tmp_path_factory.mktemp("legacy" if legacy else "current")
    db_engine = create_engine(
        f"sqlite:///{tmp / 'snapshot.db'}", connect_args={"check_same_thread": False}, future=True
    )
    Base.metadata.create_all(db_engine)
    factory = sessionmaker(bind=db_engine, expire_on_commit=False, future=True)
    original = seed_mod.SessionLocal
    seed_mod.SessionLocal = factory
    try:
        with pytest.MonkeyPatch.context() as mp:
            if legacy:
                mp.setattr(engine_mod, "market_in_coverage", legacy_market_in_coverage)
            seed_mod.seed(reset=True)
            requests = {f"seeded:{c['key']}": seed_mod.sid("scout", c["key"]) for c in CITIES}
            with factory() as s:
                seeded = s.get(BusinessLocation, seed_mod.sid("loc", CITIES[0]["key"]))
                ids = {
                    "organization": seeded.organization_id,
                    "workspace": seeded.workspace_id,
                    "brand": seeded.brand_id,
                }
                for variant, (coverage_for, _) in VARIANTS.items():
                    for city in CITIES:
                        scenario = f"{variant}:{city['key']}"
                        requests[scenario] = _add_scenario(
                            s,
                            ids,
                            scenario=scenario,
                            city=city,
                            coverage=coverage_for(city["market"]),
                        )
                        _run(s, requests[scenario])
                # The unscoped Scout: no location, no market (the default global rule).
                requests["global"] = _add_scenario(
                    s, ids, scenario="global", city=CITIES[0], coverage=None
                )
                _run(s, requests["global"])
                s.commit()
    finally:
        seed_mod.SessionLocal = original
    return factory, requests


@pytest.fixture(scope="module")
def worlds(tmp_path_factory) -> Iterator[dict[str, Any]]:
    legacy_factory, legacy_requests = _build_world(tmp_path_factory, legacy=True)
    current_factory, current_requests = _build_world(tmp_path_factory, legacy=False)
    assert legacy_requests == current_requests
    with legacy_factory() as s:
        legacy = _project(s, legacy_requests)
    with current_factory() as s:
        current = _project(s, current_requests)
    yield {
        "legacy": legacy,
        "current": current,
        "current_factory": current_factory,
        "requests": current_requests,
    }


def _scenario_moves(worlds, scenario: str) -> set[tuple]:
    return {p for p in _moved(worlds["legacy"], worlds["current"]) if p[0] == scenario}


# --------------------------------------------------------------------------- #
# The comparison
# --------------------------------------------------------------------------- #


def test_every_scenario_produced_scored_findings(worlds):
    """Non-vacuous: every Scout scanned signals and wrote scored cards in both worlds."""
    current = worlds["current"]
    assert set(worlds["requests"]) == {f"seeded:{c['key']}" for c in CITIES} | {
        f"{v}:{c['key']}" for v in VARIANTS for c in CITIES
    } | {"global"}
    for scenario in worlds["requests"]:
        assert current[(scenario, "stats", "-", "scanned")] > 0, scenario
        assert current[(scenario, "opportunities", "-", "count")] > 0, scenario
        assert worlds["legacy"][(scenario, "opportunities", "-", "count")] > 0, scenario


@pytest.mark.parametrize(
    "scenario",
    sorted(
        {f"seeded:{c['key']}" for c in CITIES}
        | {f"{v}:{c['key']}" for v, (_, hit) in VARIANTS.items() if not hit for c in CITIES}
        | {"global"}
    ),
)
def test_no_scoring_output_moves_where_the_fix_does_not_apply(worlds, scenario):
    """The seeded four-market fixtures and every rule the fix does not target: the whole
    snapshot is identical, field for field."""
    assert _scenario_moves(worlds, scenario) == set()


@pytest.mark.parametrize("scenario", sorted(TARGETED))
def test_where_the_fix_applies_only_the_geo_flag_and_the_decision_move(worlds, scenario):
    """An online rule excluding the location's own market: the legacy engine admitted the
    excluded market (the bypass), the fix refuses it. Only the fields downstream of
    ``inside_scout_area`` move; every score, signal and count is unchanged."""
    moves = _scenario_moves(worlds, scenario)
    assert moves, "the fix did not change the targeted scenario"
    assert {(table, field) for _, table, _, field in moves} <= DOWNSTREAM_OF_GEO
    legacy, current = worlds["legacy"], worlds["current"]
    flags = [
        path
        for path in current
        if path[0] == scenario
        and path[3] == "inside_scout_area"
        and (path[1] == "opportunities" or current[path[:3] + ("resolved_market",)] is not None)
    ]
    assert flags
    for path in flags:
        assert legacy[path] is True and current[path] is False, path


def test_the_unrelated_and_targeted_sets_are_the_whole_matrix():
    assert len(TARGETED) == 2 * len(CITIES)
    assert {c["market"] for c in CITIES} == {"Dallas, TX", "London, UK", "Lagos, NG", "Nairobi, KE"}


def test_no_score_is_allowed_to_move_even_where_the_fix_applies():
    """The fields the targeted scenarios may move are the geo flag and what ``decide``
    derives from it; no score, factor, weight, confidence or signal field is among them."""
    scores = (
        {
            ("opportunities", f)
            for f in (
                "opportunity_score",
                "confidence_score",
                "confidence_level",
                "priority_score",
                "relevance_score",
                "risk_level",
                "classification",
                "resolved_market",
                "title",
                "explanation_source",
                "why_it_matters",
                "ai_inference",
                "suggested_angles",
            )
        }
        | {
            ("opportunity_scores", "total"),
            ("opportunity_scores", "breakdown"),
            ("validation_evidence", "weight"),
            ("signal_location_evidence", "confidence"),
            ("signal_location_evidence", "resolved_market"),
        }
        | {("normalized_signals", f) for f in _fields("normalized_signals")}
    )
    assert not DOWNSTREAM_OF_GEO & scores
    assert {table for table, _ in DOWNSTREAM_OF_GEO} == {
        "signal_location_evidence",
        "opportunities",
    }


# --------------------------------------------------------------------------- #
# Controls: the comparison can see every scoring column move
# --------------------------------------------------------------------------- #


def test_the_snapshot_covers_every_scoring_column():
    """Every column of the scoring tables is compared unless it is identity, tenancy, a
    foreign key or a timestamp (a pinned list; a snapshot that drops a column fails here)."""
    allowed_skips = {
        "id",
        "created_at",
        "updated_at",
        "organization_id",
        "workspace_id",
        "brand_id",
        "scout_request_id",
        "location_id",
        "campaign_id",
        "cluster_id",
        "raw_signal_id",
        "normalized_signal_id",
        "opportunity_id",
        "published_at",
    }
    for table, skipped in NOT_COMPARED.items():
        assert skipped <= allowed_skips, table
        assert set(_fields(table)) | skipped == {
            c.name for c in _MODELS[table].__table__.columns
        }, table
    for table, field in DOWNSTREAM_OF_GEO:
        assert field in _fields(table)


#: Scoring outputs that must never move outside the targeted scenarios, perturbed one at a
#: time in the current world to show the comparison detects each.
_SCORING = [
    ("opportunities", Opportunity, f)
    for f in (
        "opportunity_score",
        "confidence_score",
        "confidence_level",
        "priority_score",
        "relevance_score",
        "risk_level",
        "classification",
        "decision",
        "inside_scout_area",
        "resolved_market",
        "title",
        "recommended_action",
        "urgency",
        "explanation_source",
    )
] + [
    ("opportunity_scores", OpportunityScore, "total"),
    ("opportunity_scores", OpportunityScore, "breakdown"),
    ("validation_evidence", ValidationEvidence, "weight"),
    ("signal_location_evidence", SignalLocationEvidence, "inside_scout_area"),
    ("signal_location_evidence", SignalLocationEvidence, "confidence"),
    ("signal_location_evidence", SignalLocationEvidence, "resolved_market"),
    ("normalized_signals", NormalizedSignal, "pre_analysis_score"),
    ("normalized_signals", NormalizedSignal, "is_noise"),
    ("normalized_signals", NormalizedSignal, "signal_type"),
]


def _perturbed(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float):
        return value + 1
    if isinstance(value, dict):
        return {**value, "perturbed": 1}
    if isinstance(value, list):
        return [*value, "perturbed"]
    return f"{value}-perturbed"


@pytest.mark.parametrize(("table", "model", "field"), _SCORING, ids=lambda x: str(x))
def test_the_comparison_detects_a_moved_scoring_field(worlds, table, model, field):
    scenario = "seeded:dallas"
    request_id = worlds["requests"][scenario]
    with worlds["current_factory"]() as s:
        row = s.scalars(select(model).limit(1).where(_scoped(model, request_id))).first()
        assert row is not None
        setattr(row, field, _perturbed(getattr(row, field)))
        s.flush()
        moved = _moved(
            worlds["current"],
            _project(s, {scenario: request_id})
            | {path: value for path, value in worlds["current"].items() if path[0] != scenario},
        )
        s.rollback()
    assert moved and {(t, f) for _, t, _, f in moved} == {(table, field)}


def _scoped(model, request_id: str):
    if model is Opportunity:
        return Opportunity.scout_request_id == request_id
    if model is NormalizedSignal:
        return NormalizedSignal.scout_request_id == request_id
    if model is SignalLocationEvidence:
        return SignalLocationEvidence.normalized_signal_id.in_(
            select(NormalizedSignal.id).where(NormalizedSignal.scout_request_id == request_id)
        )
    return model.opportunity_id.in_(
        select(Opportunity.id).where(Opportunity.scout_request_id == request_id)
    )


def test_the_legacy_reference_is_the_bypass_the_fix_cures():
    """The patched-in reference really is the CF-3 behaviour (so the targeted scenarios
    compare the fix with the bypass, not with itself)."""
    for rule in (
        CoverageRule(coverage_type="online", excluded_markets=("London, UK",)),
        CoverageRule(coverage_type="radius", online_global=True, excluded_markets=("London, UK",)),
    ):
        assert legacy_market_in_coverage(rule, "London, UK") is True
        assert engine_mod.market_in_coverage(rule, "London, UK") is False
