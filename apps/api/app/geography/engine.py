"""Geographic targeting + geo-relevance engine (pure, framework-free).

Testable without FastAPI or a database. Handles:
  * haversine distance + radius matching (1..200 miles)
  * coverage evaluation for a resolved market against a coverage rule
  * geo-relevance resolution from public-content evidence with a confidence score
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

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


@dataclass
class GeoResolution:
    resolved_market: str | None
    confidence: float
    evidence: list[str] = field(default_factory=list)
    inside_scout_area: bool = False


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


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


def market_in_coverage(rule: CoverageRule, market: str | None) -> bool:
    """Decide whether a resolved market string falls inside a coverage rule."""
    if rule.online_global or rule.coverage_type == "online":
        return True
    m = _norm(market)
    if not m:
        return False
    # Explicit exclusions always win.
    for ex in rule.excluded_markets:
        if _norm(ex) and _norm(ex) in m:
            return False
    haystacks = [
        *rule.included_markets,
        rule.country or "",
        rule.state or "",
    ]
    for token in haystacks:
        t = _norm(token)
        if t and (t in m or m in t):
            return True
    # Radius rules with no market list are permissive on the market axis
    # (radius is enforced separately via coordinates).
    return rule.coverage_type == "radius" and not rule.included_markets


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

    per_market: dict[str, float] = {}
    notes: dict[str, list[str]] = {}
    for kind, market in signals:
        w = _EVIDENCE_WEIGHTS.get(kind, 0.25)
        key = market.strip()
        per_market[key] = per_market.get(key, 0.0) + w
        notes.setdefault(key, []).append(f"{kind}: {market}")

    best_market = max(per_market, key=per_market.get)
    # Saturating confidence: 1 - product(1 - normalized_weight).
    combined = 1.0
    for kind, market in signals:
        if market.strip() == best_market:
            combined *= 1 - min(0.95, _EVIDENCE_WEIGHTS.get(kind, 0.25))
    confidence = round(1 - combined, 2)

    inside = market_in_coverage(rule, best_market)
    return GeoResolution(
        resolved_market=best_market,
        confidence=confidence,
        evidence=notes[best_market],
        inside_scout_area=inside,
    )
