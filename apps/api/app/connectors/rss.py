"""RSS / news-feed connector — the first real Phase 3B scouting source.

RSS is publisher-syndicated content with explicit intent to distribute, which
makes it the lowest legal-risk of the candidate sources in
``docs/phase-3-plan.md`` Workstream A and the recommended first connector. This
module implements the whole connector *except live egress*: it parses a feed's
bytes into normalized :class:`ConnectorSignal` items with source attribution,
scoped to the request's market.

By default the feed bytes come from a **deterministic sandbox provider** (no
network). A live HTTP provider is intentionally not wired in this batch — turning
it on is gated on product-owner approval and connector legal sign-off (Phase 3
entry criteria). The parse/normalize path is identical either way, so enabling
live egress later needs no change here.

Safety: the parser rejects any feed declaring a DOCTYPE or entity (defusing
billion-laughs / XXE) and caps the input size before parsing.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from app.connectors.base import (
    ConnectorResult,
    ConnectorSignal,
    FailureKind,
    FetchScope,
    SourceConnector,
)
from app.connectors.ratelimit import TokenBucket
from app.connectors.retry import ConnectorFetchError, RetryPolicy, run_with_retry
from app.connectors.sample_feeds import sample_feed_for_market

#: Reject feeds larger than this before parsing (defensive bound).
MAX_FEED_BYTES = 2 * 1024 * 1024

FeedProvider = Callable[[str | None], bytes]


def _assert_safe_xml(data: bytes) -> None:
    """Reject XML that declares a DOCTYPE or entity, or exceeds the size cap."""
    if len(data) > MAX_FEED_BYTES:
        raise ConnectorFetchError(FailureKind.UNSAFE_CONTENT, "feed exceeds size limit")
    head = data[:4096].lstrip().lower()
    if b"<!doctype" in head or b"<!entity" in head or b"<!doctype" in data.lower():
        raise ConnectorFetchError(
            FailureKind.UNSAFE_CONTENT, "feed declares a DOCTYPE/entity"
        )


class RssNewsConnector(SourceConnector):
    """Parse RSS/news feeds into normalized signals, scoped to one market."""

    name = "rss_news"
    source_type = "rss_news"
    is_simulated = True  # sandbox provider; live egress is a gated follow-up.

    def __init__(
        self,
        *,
        feed_provider: FeedProvider | None = None,
        rate_limiter: TokenBucket | None = None,
        retry_policy: RetryPolicy | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = feed_provider or sample_feed_for_market
        self._limiter = rate_limiter
        self._retry = retry_policy or RetryPolicy()
        self._now = now or (lambda: datetime.now(UTC))

    def collect(self, scope: FetchScope) -> ConnectorResult:
        result = ConnectorResult(
            connector=self.name,
            source_type=self.source_type,
            retrieved_at=self._now(),
        )
        try:
            data = self._fetch_bytes(scope.market)
        except ConnectorFetchError as exc:
            result.failures.append(exc.failure)
            return result

        try:
            items = self._parse(data)
        except ConnectorFetchError as exc:
            result.failures.append(exc.failure)
            return result

        keywords = [k.lower() for k in scope.keywords]
        for item in items[: scope.max_items]:
            signal = self._normalize(item, scope.market, result.retrieved_at)
            if keywords:
                haystack = signal.content.lower()
                if not any(k in haystack for k in keywords):
                    continue
            result.signals.append(signal)
        return result

    # --- fetch + parse ---------------------------------------------------

    def _fetch_bytes(self, market: str | None) -> bytes:
        def _once() -> bytes:
            if self._limiter is not None and not self._limiter.try_acquire():
                raise ConnectorFetchError(FailureKind.RATE_LIMITED, "rate limit exceeded")
            return self._provider(market)

        return run_with_retry(_once, policy=self._retry)

    def _parse(self, data: bytes) -> list[dict]:
        _assert_safe_xml(data)
        try:
            root = ET.fromstring(data)  # noqa: S314 - DOCTYPE/entity rejected above
        except ET.ParseError as exc:
            raise ConnectorFetchError(FailureKind.PARSE_ERROR, "malformed feed XML") from exc

        channel = root.find("channel")
        if channel is None:
            raise ConnectorFetchError(FailureKind.PARSE_ERROR, "feed has no channel")
        source_title = (channel.findtext("title") or "").strip()

        items: list[dict] = []
        for el in channel.findall("item"):
            items.append(
                {
                    "title": (el.findtext("title") or "").strip(),
                    "link": (el.findtext("link") or "").strip() or None,
                    "description": (el.findtext("description") or "").strip(),
                    "author": (el.findtext("author") or "").strip() or None,
                    "pubdate": (el.findtext("pubDate") or "").strip() or None,
                    "source_title": source_title,
                }
            )
        return items

    def _normalize(
        self, item: dict, market: str | None, retrieved_at: datetime
    ) -> ConnectorSignal:
        content = " ".join(part for part in (item["title"], item["description"]) if part)
        age_days = self._age_days(item["pubdate"])
        geo_evidence: list[tuple[str, str]] = []
        if market:
            geo_evidence.append(("local_source", market))
            city = market.split(",")[0].strip()
            if city and city.lower() in content.lower():
                geo_evidence.append(("text_mention", market))

        return ConnectorSignal(
            source_type=self.source_type,
            content=content,
            market=market,
            source_url=item["link"],
            author=item["author"],
            language="en",
            topics=["news"],
            published_days_ago=age_days,
            news_coverage=True,
            geo_evidence=geo_evidence,
            is_simulated=self.is_simulated,
            attribution={
                "connector": self.name,
                "source_title": item["source_title"],
                "source_url": item["link"],
                "retrieved_at": retrieved_at.isoformat(),
                "license": "publisher-syndicated RSS",
            },
        )

    def _age_days(self, pubdate: str | None) -> float:
        if not pubdate:
            return 0.0
        try:
            published = parsedate_to_datetime(pubdate)
        except (TypeError, ValueError):
            return 0.0
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        delta = self._now() - published
        return max(0.0, delta.total_seconds() / 86400.0)
