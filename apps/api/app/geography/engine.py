"""Geographic targeting + geo-relevance engine (pure, framework-free).

Testable without FastAPI or a database. Handles:
  * haversine distance + radius matching (1..200 miles)
  * coverage evaluation for a resolved market against a coverage rule
  * geo-relevance resolution from public-content evidence with a confidence score

Markets are compared by structured identity (``app.geography.markets``), never by
substring, and coverage fails closed (P6-DATA-6): a market that cannot be placed,
or a rule that names no place it could be compared with, is outside the scout area.
Only an ``online`` / ``online_global`` rule admits every market.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.geography.markets import (
    MarketIdentity,
    may_overlap,
    normalize_country,
    normalize_region,
    parse_market,
    parse_observed,
    text_within,
)

EARTH_RADIUS_MILES = 3958.7613


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two coordinates."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True)
class CoverageRule:
    coverage_type: str = "radius"
    center_latitude: float | None = None
    center_longitude: float | None = None
    radius_miles: int | None = None
    country: str | None = None
    state: str | None = None
    included_markets: tuple[str, ...] = ()
    excluded_markets: tuple[str, ...] = ()
    online_global: bool = False
    #: The scoped location's own market (from its city / state / country fields).
    #: Its country qualifies bare market names in this rule ("Ikeja" for a Nigerian
    #: location), and it is the scope of a rule that lists no market.
    home_market: MarketIdentity | None = None


@dataclass
class GeoResolution:
    resolved_market: str | None
    confidence: float
    evidence: list[str] = field(default_factory=list)
    inside_scout_area: bool = False


def is_within_radius(
    rule: CoverageRule, point_lat: float | None, point_lon: float | None
) -> bool | None:
    """True/False when a radius check applies; None when it cannot be evaluated."""
    if rule.radius_miles is None or rule.center_latitude is None or rule.center_longitude is None:
        return None
    if point_lat is None or point_lon is None:
        return None
    distance = haversine_miles(
        rule.center_latitude, rule.center_longitude, point_lat, point_lon
    )
    return distance <= rule.radius_miles


#: Coverage types whose scope is a whole country / a whole state or province.
_COUNTRY_TYPES = frozenset({"country"})
_STATE_TYPES = frozenset({"state", "multi_state"})


def _context_country(rule: CoverageRule) -> str | None:
    """The rule's country, else its location's: qualifies bare names in the rule."""
    return normalize_country(rule.country) or (
        rule.home_market.country if rule.home_market else None
    )


def coverage_scopes(rule: CoverageRule) -> list[MarketIdentity]:
    """The places a non-online rule covers, as structured identities.

    A rule names its places with listed markets, plus its state for a
    ``state``/``multi_state`` rule and its country for a ``country`` rule. A rule
    that names no place covers its own location's market at the rule's granularity
    (country, state, or the city itself). A named place that cannot be read, or
    names no country, contributes no scope - it never falls back to the location.
    """
    country = _context_country(rule)
    names_state = rule.coverage_type in _STATE_TYPES and bool(rule.state)
    names_country = rule.coverage_type in _COUNTRY_TYPES and bool(rule.country)
    scopes: list[MarketIdentity] = []
    if rule.included_markets or names_state or names_country:
        for text in rule.included_markets:
            scope = parse_market(text, default_country=country)
            if scope is not None:
                scopes.append(scope)
        if names_state:
            region, region_country = normalize_region(rule.state, country)
            if region and region_country:
                scopes.append(
                    MarketIdentity(region=region, country=region_country, label=rule.state)
                )
        if names_country:
            own_country = normalize_country(rule.country)
            if own_country:
                scopes.append(MarketIdentity(country=own_country, label=rule.country))
    elif rule.home_market is not None:
        home = rule.home_market
        if rule.coverage_type in _COUNTRY_TYPES:
            scopes.append(MarketIdentity(country=home.country, label=home.label))
        elif rule.coverage_type in _STATE_TYPES:
            if home.region:
                scopes.append(
                    MarketIdentity(region=home.region, country=home.country, label=home.label)
                )
        elif home.city:
            scopes.append(home)
    return [scope for scope in scopes if scope.is_qualified]


def market_in_coverage(rule: CoverageRule, market: str | None) -> bool:
    """Decide whether an observed market falls inside a coverage rule (fail closed)."""
    if rule.online_global or rule.coverage_type == "online":
        return True
    if parse_observed(market) is None:
        return False
    # Explicit exclusions always win, including over a market they cannot rule out.
    country = _context_country(rule)
    observed = parse_observed(market)
    for text in rule.excluded_markets:
        excluded = parse_market(text, default_country=country)
        if excluded is not None and may_overlap(observed, excluded):
            return False
    # Radius distance is not evaluated here: an observed market carries no
    # coordinates (see ``is_within_radius``), so the market scope stands in for it.
    return any(text_within(market, scope) for scope in coverage_scopes(rule))


# Evidence weights for geo-relevance resolution.
_EVIDENCE_WEIGHTS = {
    "platform_geotag": 0.9,
    "text_mention": 0.55,
    "hashtag": 0.35,
    "profile_location": 0.5,
    "source_community": 0.6,
    "local_source": 0.65,
    "search_query_context": 0.4,
    "comments_context": 0.3,
    "business_address": 0.7,
}


def resolve_geo(
    rule: CoverageRule,
    signals: list[tuple[str, str]],
    target_market: str | None = None,
) -> GeoResolution:
    """Resolve a market from weighted evidence tuples ``(evidence_kind, market)``.

    Confidence is a saturating combination of independent evidence weights.
    """
    if not signals:
        return GeoResolution(resolved_market=None, confidence=0.0)

    # Evidence naming the same place in different words ("London, UK" and
    # "London, United Kingdom") is pooled; the first wording seen is reported.
    keys: dict[str, tuple] = {}
    display: dict[tuple, str] = {}
    per_market: dict[tuple, float] = {}
    notes: dict[tuple, list[str]] = {}
    for kind, market in signals:
        text = market.strip()
        if text not in keys:
            identity = parse_observed(text)
            keys[text] = (
                ("place", identity.city, identity.region, identity.country)
                if identity is not None and identity.is_qualified
                else ("text", text)
            )
        key = keys[text]
        display.setdefault(key, text)
        per_market[key] = per_market.get(key, 0.0) + _EVIDENCE_WEIGHTS.get(kind, 0.25)
        notes.setdefault(key, []).append(f"{kind}: {market}")

    best_key = max(per_market, key=per_market.get)
    best_market = display[best_key]
    # Saturating confidence: 1 - product(1 - normalized_weight).
    combined = 1.0
    for kind, market in signals:
        if keys[market.strip()] == best_key:
            combined *= 1 - min(0.95, _EVIDENCE_WEIGHTS.get(kind, 0.25))
    confidence = round(1 - combined, 2)

    inside = market_in_coverage(rule, best_market)
    return GeoResolution(
        resolved_market=best_market,
        confidence=confidence,
        evidence=notes[best_key],
        inside_scout_area=inside,
    )
