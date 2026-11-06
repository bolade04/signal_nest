"""Phase 5D exit criterion 5 (connector half): ``ConnectorPolicy.permits`` with a non-empty
``allowed_markets`` and an unresolved market **denies**.

The plan records that the policy once skipped its market check when the market was unknown
(fail open). P6-DATA-6 replaced that check (``app.connectors.policy``), so at the W21 CF-3
base the cure is already in the code (CF-3 reconciliation C5). These tests pin it as its own
criterion: ``None``, blank, placeholder, unqualified and unplaceable markets are refused by
a market-restricted policy and by the registry that builds the RSS connector from settings,
while a resolved market inside the clearance is still served. The path is dark: only the RSS
connector carries ``allowed_markets`` (``connector_rss_markets``, empty by default).
"""

from __future__ import annotations

import pytest

from app.connectors.policy import ConnectorPolicy
from app.connectors.registry import resolve_connector
from app.connectors.rss import RssNewsConnector
from app.core.config import get_settings

FOUR_MARKETS = ("Dallas, TX", "London, UK", "Lagos, NG", "Nairobi, KE")

#: Markets a request can carry that place it nowhere.
UNRESOLVED_MARKETS = [
    None,
    "",
    "   ",
    "None",
    "null",
    "undefined",
    "London",
    "Lagos",
    "Atlantis",
    ",",
    "London, None",
]


def _policy(*markets: str) -> ConnectorPolicy:
    return ConnectorPolicy(source_type="rss_news", enabled=True, allowed_markets=frozenset(markets))


@pytest.mark.parametrize("market", UNRESOLVED_MARKETS)
@pytest.mark.parametrize(
    "allowed",
    [FOUR_MARKETS, ("London, UK",), ("Texas",), ("Kenya",), ("Lagos, Lagos, NG",)],
    ids=["four-markets", "london", "a-state", "a-country", "lagos-qualified"],
)
def test_a_market_restricted_policy_denies_an_unresolved_market(allowed, market):
    policy = _policy(*allowed)
    assert policy.permits(source_types=("rss_news",), market=market) is False
    # With no source-type filter as well: the market check is not skipped.
    assert policy.permits(source_types=(), market=market) is False


@pytest.mark.parametrize("market", FOUR_MARKETS)
def test_the_cure_does_not_over_deny_a_resolved_market(market):
    assert _policy(*FOUR_MARKETS).permits(source_types=("rss_news",), market=market) is True


@pytest.mark.parametrize(
    ("allowed", "market"),
    [
        ("York, UK", "New York, NY"),
        ("York, PA", "New York, NY"),
        ("New York, NY", "York, PA"),
        ("Lagos, NG", "Lagos, Portugal"),
        ("London, UK", "New London, CT"),
        ("Kenya", "Kentucky, US"),
    ],
)
def test_no_substring_false_positive(allowed, market):
    assert _policy(allowed).permits(source_types=("rss_news",), market=market) is False


def test_an_unrestricted_policy_is_not_a_market_restriction():
    """Empty ``allowed_markets`` is "no jurisdiction restriction" by design; the criterion
    concerns a non-empty one. Unchanged."""
    policy = ConnectorPolicy(source_type="rss_news", enabled=True)
    assert policy.permits(source_types=("rss_news",), market=None) is True


@pytest.mark.parametrize("market", UNRESOLVED_MARKETS)
def test_the_registry_never_builds_the_rss_connector_for_an_unresolved_market(market):
    """End to end through settings, as the pipeline resolves it (rss enabled, markets set)."""
    settings = get_settings().model_copy(update={"connector_rss_markets": list(FOUR_MARKETS)})
    assert (
        resolve_connector(
            source_types=("rss_news",), market=market, rss_enabled=True, settings=settings
        )
        is None
    )
    served = resolve_connector(
        source_types=("rss_news",), market="London, UK", rss_enabled=True, settings=settings
    )
    assert isinstance(served, RssNewsConnector)
