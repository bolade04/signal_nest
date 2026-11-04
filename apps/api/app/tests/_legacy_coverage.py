"""The CF-3 ``market_in_coverage`` (base ``daa898cd``, ``app/geography/engine.py``), kept
verbatim as the reference the Phase 5D exclusion-order fix is compared with. Not a test
module. Used by ``test_jurisdiction_scoring_snapshot`` (exit criterion 8(b)) and
``test_coverage_exclusion_order`` (exit criterion 5).

At that base the function returned ``True`` for an ``online_global`` or ``online`` rule
before reading the rule's exclusions: the bypass the fix cures.
"""

from __future__ import annotations

from app.geography import engine as engine_mod
from app.geography.engine import CoverageRule
from app.geography.markets import may_overlap, parse_market, parse_observed, text_within


def legacy_market_in_coverage(rule: CoverageRule, market: str | None) -> bool:
    """Decide whether an observed market falls inside a coverage rule (fail closed)."""
    if rule.online_global or rule.coverage_type == "online":
        return True
    if parse_observed(market) is None:
        return False
    # Explicit exclusions always win, including over a market they cannot rule out.
    country = engine_mod._context_country(rule)
    observed = parse_observed(market)
    for text in rule.excluded_markets:
        excluded = parse_market(text, default_country=country)
        if excluded is not None and may_overlap(observed, excluded):
            return False
    # Radius distance is not evaluated here: an observed market carries no
    # coordinates (see ``is_within_radius``), so the market scope stands in for it.
    return any(text_within(market, scope) for scope in engine_mod.coverage_scopes(rule))
