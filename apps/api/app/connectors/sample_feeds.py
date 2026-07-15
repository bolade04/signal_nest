"""Deterministic RSS sample feeds for the sandbox connector.

These stand in for live publisher feeds so the RSS connector's full path
(fetch → parse → normalize → attribute) runs offline and deterministically, with
**zero network egress**. Items are tagged per market so a request scoped to one
location only ever ingests that market's news — preserving the four-market
isolation guarantee exactly as the fixture connector does.

The XML is well-formed RSS 2.0 with no DOCTYPE/entity declarations (the parser
rejects those defensively). Content mirrors the demo specialty-coffee brand so
topics overlap across markets while geography differs.
"""

from __future__ import annotations

_FEED_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{city} Local Business News</title>
    <link>https://news.example.com/{slug}</link>
    <description>Simulated local news feed for {city}.</description>
    <item>
      <title>Specialty coffee demand rising in {city}</title>
      <link>https://news.example.com/{slug}/specialty-coffee-demand</link>
      <description>Roasters in {city} report growing demand for dairy-free and
        single-origin options as consumers seek specialty coffee.</description>
      <author>news@example.com (Local Desk)</author>
      <pubDate>{pubdate}</pubDate>
      <guid>https://news.example.com/{slug}/specialty-coffee-demand</guid>
    </item>
    <item>
      <title>Cafes in {city} struggle with mobile-order pickup times</title>
      <link>https://news.example.com/{slug}/mobile-order-delays</link>
      <description>Customers across {city} complain that mobile order pickup is
        slow and oat milk is frequently out of stock at busy cafes.</description>
      <author>news@example.com (Local Desk)</author>
      <pubDate>{pubdate}</pubDate>
      <guid>https://news.example.com/{slug}/mobile-order-delays</guid>
    </item>
  </channel>
</rss>
"""

# Static, deterministic publish date (never "now") so runs are reproducible.
_PUBDATE = "Mon, 07 Jul 2025 09:00:00 +0000"

_MARKET_FEEDS: dict[str, tuple[str, str]] = {
    "Dallas, TX": ("Dallas", "dallas"),
    "London, UK": ("London", "london"),
    "Lagos, NG": ("Lagos", "lagos"),
    "Nairobi, KE": ("Nairobi", "nairobi"),
}


def sample_feed_for_market(market: str | None) -> bytes:
    """Return the deterministic RSS bytes for ``market``.

    An unknown or absent market yields an empty (but well-formed) channel so the
    connector never blends another market's news into an unscoped request.
    """
    for key, (city, slug) in _MARKET_FEEDS.items():
        if market and (key.lower() in market.lower() or market.lower() in key.lower()):
            return _FEED_TEMPLATE.format(city=city, slug=slug, pubdate=_PUBDATE).encode()
    empty = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<rss version=\"2.0\"><channel><title>No feed</title>"
        "<link>https://news.example.com/</link>"
        "<description>No market feed.</description></channel></rss>"
    )
    return empty.encode()


SAMPLE_MARKETS = tuple(_MARKET_FEEDS)
