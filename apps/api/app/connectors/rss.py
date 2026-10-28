"""RSS / news-feed parsing and the sandbox RSS connector.

RSS is publisher-syndicated content with explicit intent to distribute, which
makes it the lowest legal-risk of the candidate sources in
``docs/phase-3-plan.md`` Workstream A and the recommended first connector. This
module turns a feed's bytes into normalized :class:`ConnectorSignal` items with
source attribution, scoped to the request's market.

:func:`parse_feed` reads RSS 2.0 (and 0.9x), RSS 1.0 (RDF) and Atom 1.0, and is
shared by the sandbox :class:`RssNewsConnector` (deterministic bundled feed bytes, no
network) and the live connector in :mod:`app.connectors.live`, whose bytes come
through the egress guard in :mod:`app.connectors.egress`. Every text field is
reduced to bounded plain text and every entry link screened
(:mod:`app.connectors.content`) before it reaches a signal.

Safety: the parser caps the input size before parsing, screens the raw bytes for a
DOCTYPE/entity declaration, and then parses with ``defusedxml`` — which refuses DTDs,
entity declarations and external references structurally, whatever the document's
encoding — so an encoded or oddly placed declaration that the byte screen cannot see is
still rejected with the same structured ``UNSAFE_CONTENT`` failure (defence in depth,
P6-CI-6 SAST finding ``use-defused-xml``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from itertools import islice

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
from app.connectors.content import safe_entry_url, sanitize_text
from app.connectors.fetch_limits import (
    EGRESS_BLOCKED,
    FEED_TOO_LARGE,
    FETCH_ERROR,
    RATE_LIMITED,
    RESPONSE_REFUSED,
    RETRIES_EXHAUSTED,
    XML_MALFORMED,
    XML_UNSAFE,
    SourceFetchReport,
    lookahead_entries,
)
from app.connectors.ratelimit import TokenBucket
from app.connectors.retry import ConnectorFetchError, RetryPolicy, run_with_retry
from app.connectors.sample_feeds import sample_feed_for_market

#: Reject feeds larger than this before parsing (defensive bound).
MAX_FEED_BYTES = 2 * 1024 * 1024
#: Cap on a feed's display title.
MAX_TITLE_CHARS = 300

FeedProvider = Callable[[str | None], bytes]

_ATOM = "{http://www.w3.org/2005/Atom}"
_RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
_RSS1 = "{http://purl.org/rss/1.0/}"
_DC = "{http://purl.org/dc/elements/1.1/}"


@dataclass(frozen=True)
class FeedItem:
    """One feed entry, already sanitized."""

    title: str
    link: str | None
    description: str
    author: str | None
    published: datetime | None
    guid: str | None


@dataclass(frozen=True)
class ParsedFeed:
    """A parsed feed. ``items`` holds every entry, or - when :func:`parse_feed` was given
    ``max_entries`` (the #268 look-ahead) - at most that many, in document order."""

    format: str  # "rss" | "rdf" | "atom"
    title: str
    items: list[FeedItem]


_TOO_LARGE_DETAIL = "feed exceeds size limit"
#: The egress guard's static detail for a body above ``max_response_bytes`` (live path).
_RESPONSE_TOO_LARGE_DETAIL = "response exceeds size limit"
_DOCTYPE_DETAIL = "feed declares a DOCTYPE/entity"


def failure_reason(failure: FetchFailure) -> str:
    """The closed ``failure_reason`` code of a classified failure (no free text).

    ``failure.detail`` is the static detail the parser or the egress guard raised (never
    the source key prefix the live connector adds to ``ConnectorResult.failures``)."""
    kind = failure.kind
    if kind is FailureKind.UNSAFE_CONTENT:
        if failure.detail in (_TOO_LARGE_DETAIL, _RESPONSE_TOO_LARGE_DETAIL):
            return FEED_TOO_LARGE
        if failure.detail == _DOCTYPE_DETAIL:
            return XML_UNSAFE
        return RESPONSE_REFUSED  # an encoded body or a media type that is not a feed
    if kind is FailureKind.PARSE_ERROR:
        return XML_MALFORMED
    if kind is FailureKind.RATE_LIMITED:
        return RATE_LIMITED
    if kind is FailureKind.EGRESS_BLOCKED:
        return EGRESS_BLOCKED
    if kind.retryable:  # timeout / network / upstream error that outlasted the retries
        return RETRIES_EXHAUSTED
    return FETCH_ERROR


def _assert_safe_xml(data: bytes) -> None:
    """Reject XML that declares a DOCTYPE or entity, or exceeds the size cap."""
    if len(data) > MAX_FEED_BYTES:
        raise ConnectorFetchError(FailureKind.UNSAFE_CONTENT, _TOO_LARGE_DETAIL)
    head = data[:4096].lstrip().lower()
    if b"<!doctype" in head or b"<!entity" in head or b"<!doctype" in data.lower():
        raise ConnectorFetchError(FailureKind.UNSAFE_CONTENT, _DOCTYPE_DETAIL)


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    parsed: datetime | None = None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _text(el, path: str) -> str:
    return (el.findtext(path) or "").strip()


def _rss_item(el, ns: str = "") -> FeedItem:
    author = _text(el, f"{ns}author") or _text(el, f"{_DC}creator")
    return FeedItem(
        title=sanitize_text(_text(el, f"{ns}title")),
        link=safe_entry_url(_text(el, f"{ns}link")),
        description=sanitize_text(_text(el, f"{ns}description")),
        author=sanitize_text(author, max_chars=200) or None,
        published=_date(_text(el, f"{ns}pubDate") or _text(el, f"{_DC}date")),
        guid=sanitize_text(_text(el, f"{ns}guid"), max_chars=500) or None,
    )


def _atom_link(entry) -> str | None:
    for link in entry.findall(f"{_ATOM}link"):
        if link.get("rel") in (None, "alternate"):
            return safe_entry_url(link.get("href"))
    return None


def _atom_item(entry) -> FeedItem:
    summary = _text(entry, f"{_ATOM}summary") or _text(entry, f"{_ATOM}content")
    return FeedItem(
        title=sanitize_text(_text(entry, f"{_ATOM}title")),
        link=_atom_link(entry),
        description=sanitize_text(summary),
        author=sanitize_text(_text(entry, f"{_ATOM}author/{_ATOM}name"), max_chars=200)
        or None,
        published=_date(_text(entry, f"{_ATOM}published") or _text(entry, f"{_ATOM}updated")),
        guid=sanitize_text(_text(entry, f"{_ATOM}id"), max_chars=500) or None,
    )


def parse_feed(data: bytes, *, max_entries: int | None = None) -> ParsedFeed:
    """Parse untrusted feed bytes. Raises :class:`ConnectorFetchError` on any defect.

    ``max_entries`` bounds how many entries are **extracted** (sanitised, link-screened,
    dated) - the per-source look-ahead of #268: a connector asks for ``max_items + 1``
    (:func:`~app.connectors.fetch_limits.lookahead_entries`) so that one extra entry tells a
    clipped source from one that held exactly ``max_items``. ``None`` extracts every entry.
    The XML parser still builds the whole document tree, bounded only by
    :data:`MAX_FEED_BYTES` (checked before parsing), so a document that is malformed
    anywhere - even past the look-ahead - is rejected as a whole."""
    _assert_safe_xml(data)
    try:
        root = DET.fromstring(
            data, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
    except (DTDForbidden, EntitiesForbidden, ExternalReferenceForbidden) as exc:
        # The byte screen above misses declarations the parser still sees (another
        # encoding, a declaration past the screened head); defusedxml refuses them.
        raise ConnectorFetchError(FailureKind.UNSAFE_CONTENT, _DOCTYPE_DETAIL) from exc
    except DET.ParseError as exc:
        raise ConnectorFetchError(FailureKind.PARSE_ERROR, "malformed feed XML") from exc
    except (ValueError, LookupError) as exc:
        # expat refuses multi-byte declared encodings (Shift_JIS, EUC-JP, UTF-32) with a
        # bare ValueError and unknown encoding names with LookupError; both are defects of
        # this one feed, never of the run that reads it.
        raise ConnectorFetchError(
            FailureKind.PARSE_ERROR, "unsupported or unknown feed encoding"
        ) from exc

    if root.tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise ConnectorFetchError(FailureKind.PARSE_ERROR, "feed has no channel")
        return ParsedFeed(
            format="rss",
            title=sanitize_text(_text(channel, "title"), max_chars=MAX_TITLE_CHARS),
            items=[_rss_item(el) for el in islice(channel.iterfind("item"), max_entries)],
        )
    if root.tag == f"{_RDF}RDF":
        channel = root.find(f"{_RSS1}channel")
        title = _text(channel, f"{_RSS1}title") if channel is not None else ""
        return ParsedFeed(
            format="rdf",
            title=sanitize_text(title, max_chars=MAX_TITLE_CHARS),
            items=[
                _rss_item(el, _RSS1)
                for el in islice(root.iterfind(f"{_RSS1}item"), max_entries)
            ],
        )
    if root.tag == f"{_ATOM}feed":
        return ParsedFeed(
            format="atom",
            title=sanitize_text(_text(root, f"{_ATOM}title"), max_chars=MAX_TITLE_CHARS),
            items=[
                _atom_item(el) for el in islice(root.iterfind(f"{_ATOM}entry"), max_entries)
            ],
        )
    raise ConnectorFetchError(FailureKind.PARSE_ERROR, "unsupported feed format")


def item_signal(
    item: FeedItem,
    *,
    market: str | None,
    retrieved_at: datetime,
    now: datetime,
    is_simulated: bool,
    attribution: dict,
) -> ConnectorSignal:
    """Normalize one sanitized item into a market-scoped signal with provenance."""
    content = " ".join(part for part in (item.title, item.description) if part)
    age_days = 0.0
    if item.published is not None:
        age_days = max(0.0, (now - item.published).total_seconds() / 86400.0)
    geo_evidence: list[tuple[str, str]] = []
    if market:
        geo_evidence.append(("local_source", market))
        city = market.split(",")[0].strip()
        if city and city.lower() in content.lower():
            geo_evidence.append(("text_mention", market))
    return ConnectorSignal(
        source_type="rss_news",
        content=content,
        market=market,
        source_url=item.link,
        author=item.author,
        language="en",
        topics=["news"],
        published_days_ago=age_days,
        news_coverage=True,
        geo_evidence=geo_evidence,
        is_simulated=is_simulated,
        attribution={
            **attribution,
            "source_url": item.link,
            "item_guid": item.guid,
            "published_at": item.published.isoformat() if item.published else None,
            "retrieved_at": retrieved_at.isoformat(),
        },
    )


def keyword_match(signal: ConnectorSignal, keywords: tuple[str, ...]) -> bool:
    if not keywords:
        return True
    haystack = signal.content.lower()
    return any(k.lower() in haystack for k in keywords)


def source_report(
    source_key: str, scope: FetchScope, *, fetched: int, eligible: int, clipped: bool
) -> SourceFetchReport:
    """What one source delivered against its fetch limit (#268)."""
    return SourceFetchReport(
        source_key=source_key,
        max_items=scope.max_items,
        fetched=fetched,
        eligible_seen=eligible,
        clipped_by_fetch_limit=clipped,
    )


def failed_source_report(
    source_key: str, scope: FetchScope, failure: FetchFailure
) -> SourceFetchReport:
    """A source that contributed nothing because it failed (review r3 MINOR-1).

    ``fetched`` 0 is not "the source was empty": ``failed`` and ``failure_reason`` say it
    failed (a document rejected as a whole, e.g. malformed XML after the limit, is a
    failed source, never 100 good entries)."""
    return SourceFetchReport(
        source_key=source_key,
        max_items=scope.max_items,
        fetched=0,
        eligible_seen=0,
        clipped_by_fetch_limit=False,
        failed=True,
        failure_reason=failure_reason(failure),
    )


class RssNewsConnector(SourceConnector):
    """Sandbox RSS connector: bundled deterministic feeds, never the network."""

    name = "rss_news"
    source_type = "rss_news"
    is_simulated = True  # sandbox provider; live feeds go through app.connectors.live.

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
            # Look one entry past the fetch limit (never more): that single extra entry is
            # what tells a clipped source from one that held exactly ``max_items``.
            feed = parse_feed(data, max_entries=lookahead_entries(scope.max_items))
        except ConnectorFetchError as exc:
            result.failures.append(exc.failure)
            result.reports.append(failed_source_report(self.source_key, scope, exc.failure))
            return result

        clipped = len(feed.items) > scope.max_items
        window = feed.items[: scope.max_items]
        for item in window:
            signal = item_signal(
                item,
                market=scope.market,
                retrieved_at=result.retrieved_at,
                now=self._now(),
                is_simulated=self.is_simulated,
                attribution={
                    "connector": self.name,
                    "source_title": feed.title,
                    "license": "publisher-syndicated RSS",
                },
            )
            if keyword_match(signal, scope.keywords):
                result.signals.append(signal)
        result.reports.append(
            source_report(
                self.source_key,
                scope,
                fetched=len(window),
                eligible=len(result.signals),
                clipped=clipped,
            )
        )
        return result

    def _fetch_bytes(self, market: str | None) -> bytes:
        def _once() -> bytes:
            if self._limiter is not None and not self._limiter.try_acquire():
                raise ConnectorFetchError(FailureKind.RATE_LIMITED, "rate limit exceeded")
            return self._provider(market)

        return run_with_retry(_once, policy=self._retry)
