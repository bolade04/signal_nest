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

Safety: the parser caps the input size before parsing, screens the raw bytes for a
DOCTYPE/entity declaration, and then parses with ``defusedxml`` — which refuses DTDs,
entity declarations and external references structurally, whatever the document's
encoding — so an encoded or oddly placed declaration that the byte screen cannot see is
still rejected with the same structured ``UNSAFE_CONTENT`` failure (defence in depth,
P6-CI-6 SAST finding ``use-defused-xml``).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from defusedxml import (
    DTDForbidden,
    EntitiesForbidden,
    ExternalReferenceForbidden,
)
from defusedxml import ElementTree as DET

from app.connectors.base import (
    ConnectorResult,
    ConnectorSignal,
    FailureKind,
    FetchFailure,
    FetchScope,
    SourceConnector,
)
from app.connectors.fetch_limits import (
    FEED_TOO_LARGE,
    FETCH_ERROR,
    RATE_LIMITED,
    RETRIES_EXHAUSTED,
    XML_MALFORMED,
    XML_UNSAFE,
    SourceFetchReport,
)
from app.connectors.ratelimit import TokenBucket
from app.connectors.retry import ConnectorFetchError, RetryPolicy, run_with_retry
from app.connectors.sample_feeds import sample_feed_for_market

#: Reject feeds larger than this before parsing (defensive bound).
MAX_FEED_BYTES = 2 * 1024 * 1024

FeedProvider = Callable[[str | None], bytes]


_TOO_LARGE_DETAIL = "feed exceeds size limit"


def failure_reason(failure: FetchFailure) -> str:
    """The closed ``failure_reason`` code of a classified failure (no free text)."""
    kind = failure.kind
    if kind is FailureKind.UNSAFE_CONTENT:
        return FEED_TOO_LARGE if failure.detail == _TOO_LARGE_DETAIL else XML_UNSAFE
    if kind is FailureKind.PARSE_ERROR:
        return XML_MALFORMED
    if kind is FailureKind.RATE_LIMITED:
        return RATE_LIMITED
    if kind.retryable:  # timeout / network / upstream error that outlasted the retries
        return RETRIES_EXHAUSTED
    return FETCH_ERROR


def _assert_safe_xml(data: bytes) -> None:
    """Reject XML that declares a DOCTYPE or entity, or exceeds the size cap."""
    if len(data) > MAX_FEED_BYTES:
        raise ConnectorFetchError(FailureKind.UNSAFE_CONTENT, _TOO_LARGE_DETAIL)
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
        source_key: str | None = None,
    ) -> None:
        #: Stable, non-secret key this source is reported under in ``fetch_limits``.
        self.source_key = source_key or self.name
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
            result.reports.append(self._failed_report(scope, exc.failure))
            return result

        # Look one entry past the fetch limit (never more): that single extra entry is
        # what tells a clipped source from one that held exactly ``max_items``.
        try:
            items = self._parse(data, max_entries=max(0, scope.max_items) + 1)
        except ConnectorFetchError as exc:
            result.failures.append(exc.failure)
            result.reports.append(self._failed_report(scope, exc.failure))
            return result

        clipped = len(items) > scope.max_items
        window = items[: scope.max_items]
        keywords = [k.lower() for k in scope.keywords]
        for item in window:
            signal = self._normalize(item, scope.market, result.retrieved_at)
            if keywords:
                haystack = signal.content.lower()
                if not any(k in haystack for k in keywords):
                    continue
            result.signals.append(signal)
        result.reports.append(
            self._report(scope, fetched=len(window), eligible=len(result.signals), clipped=clipped)
        )
        return result

    def _failed_report(self, scope: FetchScope, failure: FetchFailure) -> SourceFetchReport:
        # The source contributed nothing. ``fetched`` 0 is not "the source was empty":
        # ``failed`` and ``failure_reason`` say it failed (a document rejected as a whole,
        # e.g. malformed XML after the limit, is a failed source, never 100 good entries).
        return SourceFetchReport(
            source_key=self.source_key,
            max_items=scope.max_items,
            fetched=0,
            eligible_seen=0,
            clipped_by_fetch_limit=False,
            failed=True,
            failure_reason=failure_reason(failure),
        )

    def _report(
        self, scope: FetchScope, *, fetched: int, eligible: int, clipped: bool
    ) -> SourceFetchReport:
        return SourceFetchReport(
            source_key=self.source_key,
            max_items=scope.max_items,
            fetched=fetched,
            eligible_seen=eligible,
            clipped_by_fetch_limit=clipped,
        )

    # --- fetch + parse ---------------------------------------------------

    def _fetch_bytes(self, market: str | None) -> bytes:
        def _once() -> bytes:
            if self._limiter is not None and not self._limiter.try_acquire():
                raise ConnectorFetchError(FailureKind.RATE_LIMITED, "rate limit exceeded")
            return self._provider(market)

        return run_with_retry(_once, policy=self._retry)

    def _parse(self, data: bytes, *, max_entries: int | None = None) -> list[dict]:
        """Parse ``data`` into at most ``max_entries`` item dicts (all when ``None``).

        The XML parser still builds the whole document tree; that is bounded only by
        :data:`MAX_FEED_BYTES`, checked before parsing. ``max_entries`` bounds the number of
        item dicts extracted (and so the normalisation work) on top of that."""
        _assert_safe_xml(data)
        try:
            root = DET.fromstring(
                data, forbid_dtd=True, forbid_entities=True, forbid_external=True
            )
        except (DTDForbidden, EntitiesForbidden, ExternalReferenceForbidden) as exc:
            # The byte screen above misses declarations the parser still sees (another
            # encoding, a declaration past the screened head); defusedxml refuses them.
            raise ConnectorFetchError(
                FailureKind.UNSAFE_CONTENT, "feed declares a DOCTYPE/entity"
            ) from exc
        except DET.ParseError as exc:
            raise ConnectorFetchError(FailureKind.PARSE_ERROR, "malformed feed XML") from exc

        channel = root.find("channel")
        if channel is None:
            raise ConnectorFetchError(FailureKind.PARSE_ERROR, "feed has no channel")
        source_title = (channel.findtext("title") or "").strip()

        items: list[dict] = []
        for el in channel.iterfind("item"):
            if max_entries is not None and len(items) >= max_entries:
                break
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
