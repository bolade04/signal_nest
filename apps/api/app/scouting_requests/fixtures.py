"""Simulated public-signal fixtures.

These stand in for live connectors during Phase 1/2. Every fixture is clearly marked
``is_simulated=True`` downstream. Signals are tagged by ``market`` so that a scout
request scoped to one location only ever pulls that market's signals — this is what
keeps Dallas / London / Lagos / Nairobi results independent by default.

The demo brand is a specialty-coffee chain ("Brew & Bean"), so topics overlap across
markets while geography differs.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FixtureSignal:
    source_type: str
    content: str
    market: str | None
    topics: list[str]
    author: str = "public_user"
    source_url: str = "https://example.com/simulated"
    published_days_ago: float = 5.0
    language: str = "en"
    engagement: int = 10
    distinct_source_types: int = 1
    duplicate_count: int = 1
    has_buying_intent: bool = False
    has_active_ads: bool = False
    news_coverage: bool = False
    search_trend_up: bool = False
    geo_evidence: list[tuple[str, str]] = field(default_factory=list)


def _cafe_signals(market: str, hashtag: str, community: str) -> list[FixtureSignal]:
    """A reusable bundle of cafe signals for a given market."""
    ev = lambda kinds: [(k, market) for k in kinds]  # noqa: E731
    return [
        FixtureSignal(
            source_type="reddit",
            content=(
                f"Anyone know a good oat milk latte in {market.split(',')[0]}? "
                "Every place near me is always out of oat milk and the wait is brutal."
            ),
            market=market, topics=["oat milk", "latte", "wait", "coffee"],
            engagement=64, distinct_source_types=3, duplicate_count=5,
            has_buying_intent=True, search_trend_up=True,
            geo_evidence=ev(["source_community", "text_mention", "hashtag"]),
            source_url=f"https://reddit.com/r/{community}/simulated1",
        ),
        FixtureSignal(
            source_type="reviews",
            content=(
                "Love the coffee but the mobile order pickup is so slow, waited 15 minutes "
                "past my pickup time. Great beans though."
            ),
            market=market, topics=["mobile order", "slow", "pickup", "coffee"],
            engagement=22, distinct_source_types=2, duplicate_count=3,
            geo_evidence=ev(["local_source", "profile_location"]),
            published_days_ago=9,
        ),
        FixtureSignal(
            source_type="rss_news",
            content=(
                f"Specialty coffee demand rising in {market.split(',')[0]} as new roasters "
                "open; consumers seeking dairy-free and single-origin options."
            ),
            market=market, topics=["specialty coffee", "dairy-free", "single-origin"],
            engagement=40, distinct_source_types=2, duplicate_count=2,
            news_coverage=True, search_trend_up=True,
            geo_evidence=ev(["local_source", "text_mention"]),
            published_days_ago=4,
        ),
        FixtureSignal(
            source_type="competitor_scan",
            content=(
                "Competitor cafe running promos on cold brew subscriptions; customers "
                "complain their loyalty app keeps logging them out."
            ),
            market=market, topics=["cold brew", "subscription", "loyalty app", "competitor"],
            engagement=18, distinct_source_types=2, duplicate_count=2,
            has_active_ads=True,
            geo_evidence=ev(["source_community"]),
            published_days_ago=6,
        ),
        # A near-duplicate of the first signal (re-post) — must be caught by dedupe.
        FixtureSignal(
            source_type="reddit",
            content=(
                f"Anyone know a good oat milk latte in {market.split(',')[0]}? "
                "Every place near me is always out of oat milk and the wait is brutal."
            ),
            market=market, topics=["oat milk", "latte", "wait", "coffee"],
            author="reposter_42", engagement=8, distinct_source_types=1, duplicate_count=1,
            geo_evidence=ev(["text_mention"]),
            source_url=f"https://reddit.com/r/{community}/simulated1-repost",
            published_days_ago=1,
        ),
        # A signal whose wording contains risky comparative / superlative claims —
        # exercises the claim-safety guard (unsupported comparison + competitor attack).
        FixtureSignal(
            source_type="reviews",
            content=(
                f"Honestly the oat milk latte here is the best in {market.split(',')[0]} "
                "and way better than Starbucks — those competitor chains just scam you "
                "with hidden fees on every order."
            ),
            market=market, topics=["oat milk", "latte", "coffee", "comparison"],
            engagement=31, distinct_source_types=2, duplicate_count=2,
            geo_evidence=ev(["local_source", "text_mention"]),
            published_days_ago=3,
        ),
        # A weak-geo signal: a single low-weight hashtag hint gives low-confidence
        # geographic evidence (demonstrates the confidence scoring on geo).
        FixtureSignal(
            source_type="reddit",
            content=(
                "Weekend coffee run — anyone else obsessed with single-origin pour-overs "
                "lately? The dairy-free options are finally getting good."
            ),
            market=market, topics=["single-origin", "dairy-free", "coffee"],
            engagement=12, distinct_source_types=1, duplicate_count=1,
            geo_evidence=[("hashtag", market)],
            published_days_ago=7,
        ),
        # A low-relevance / off-topic signal that should be filtered or scored low.
        FixtureSignal(
            source_type="reddit",
            content="Traffic on the highway was insane today, took me an hour to get home.",
            market=market, topics=["traffic", "coffee"],
            engagement=5, distinct_source_types=1, duplicate_count=1,
            geo_evidence=ev(["text_mention"]),
            published_days_ago=2,
        ),
        # A noise / spam signal that must be filtered out.
        FixtureSignal(
            source_type="reddit",
            content="FREE MONEY!!! Click here to buy followers and DM me crypto now!!!",
            market=market, topics=["spam"],
            author="bot_promo99", engagement=0,
            geo_evidence=[],
        ),
    ]


_MARKET_FIXTURES: dict[str, list[FixtureSignal]] = {
    "Dallas, TX": _cafe_signals("Dallas, TX", "#DFW", "Dallas"),
    "London, UK": _cafe_signals("London, UK", "#London", "london"),
    "Lagos, NG": _cafe_signals("Lagos, NG", "#Lagos", "Nigeria"),
    "Nairobi, KE": _cafe_signals("Nairobi, KE", "#Nairobi", "Kenya"),
}


def fixtures_for_market(market: str | None) -> list[FixtureSignal]:
    if not market:
        # Global/online mode: return a flattened copy across markets.
        out: list[FixtureSignal] = []
        for signals in _MARKET_FIXTURES.values():
            out.extend(signals)
        return out
    for key, signals in _MARKET_FIXTURES.items():
        if key.lower() in market.lower() or market.lower() in key.lower():
            return signals
    return []


ALL_MARKETS = list(_MARKET_FIXTURES.keys())
