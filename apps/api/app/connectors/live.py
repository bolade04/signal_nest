"""Live RSS connector and its fail-closed gate (P6-DATA-1 / P6-DATA-4).

:func:`live_connector_for` is the single decision point for "may this scout run read
live feeds?". In order, any ``no`` ends the decision and the run falls back to the
simulated paths (or reports ``not_covered``):

1. ``connector_rss_kill_switch`` is off;
2. ``connector_rss_live_enabled`` is on;
3. the request's source types admit ``rss_news`` (an empty list admits every type);
4. at least one record in the source registry is activatable (enabled, terms
   approved, dated approval reference), serves the request's market **exactly**, and
   has not been taken down by an operator.

Under default settings — and with :data:`~app.connectors.sources.APPROVED_SOURCES`
empty — the answer is always ``no``.

:class:`LiveRssConnector` reads each permitted source through
:class:`~app.connectors.egress.SafeFeedFetcher`, honouring the per-source politeness
interval, conditional requests and cool-downs held by
:class:`~app.connectors.feedcache.FeedCache`. One failing source never sinks the
others; each failure is recorded per source. Every signal carries its provenance:
connector, delivery mode, source key, publisher, feed URL, terms URL, approval
reference, the feed bytes' SHA-256, the item link/guid and the retrieval time.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.connectors.base import (
    ConnectorResult,
    FailureKind,
    FetchFailure,
    FetchScope,
    SourceConnector,
)
from app.connectors.egress import (
    FetchLimits,
    SafeFeedFetcher,
    build_user_agent,
    parse_replay_origin,
)
from app.connectors.feedcache import CachedFeed, FeedCache, feed_cache
from app.connectors.retry import ConnectorFetchError
from app.connectors.rss import ParsedFeed, item_signal, keyword_match, parse_feed
from app.connectors.sources import APPROVED_SOURCES, SourceRecord
from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger("signalnest.connectors.live")

#: Transient kinds a live fetch retries (a 429 never is: it starts a cool-down).
_RETRYABLE = frozenset({FailureKind.TIMEOUT, FailureKind.NETWORK, FailureKind.UPSTREAM_ERROR})


@dataclass(frozen=True)
class SourceTarget:
    """Where to fetch a source from, and what to stamp on its signals."""

    source: SourceRecord
    #: The URL actually requested (the record's feed URL, or a replay URL).
    fetch_url: str
    #: "live" or "replay".
    delivery: str
    #: Simulated content (a synthetic replay capture) — never true for live delivery.
    is_simulated: bool = False
    #: Extra provenance, e.g. the capture id of a replayed recording.
    provenance: tuple[tuple[str, object], ...] = ()

    @property
    def allowed_hosts(self) -> frozenset[str]:
        redirect_hosts = (h.lower() for h in self.source.allowed_redirect_hosts)
        return frozenset({self.source.host, *redirect_hosts})


def _targets(settings: Settings) -> list[SourceTarget]:
    if settings.connector_rss_replay_origin:
        from app.connectors.replay import replay_targets

        return replay_targets(settings.connector_rss_replay_origin)
    return [
        SourceTarget(source=s, fetch_url=s.feed_url, delivery="live") for s in APPROVED_SOURCES
    ]


def live_targets_for(
    *,
    market: str | None,
    source_types: tuple[str, ...],
    takedowns: frozenset[str],
    settings: Settings,
    targets: list[SourceTarget] | None = None,
) -> list[SourceTarget]:
    """The sources a run in ``market`` may read live; ``[]`` means no live egress."""
    if settings.connector_rss_kill_switch or not settings.connector_rss_live_enabled:
        return []
    if source_types and "rss_news" not in source_types:
        return []
    candidates = _targets(settings) if targets is None else targets
    return [
        t
        for t in candidates
        if t.source.is_activatable()
        and t.source.serves_market(market)
        and t.source.source_key not in takedowns
    ]


def build_fetcher(settings: Settings) -> SafeFeedFetcher:
    return SafeFeedFetcher(
        user_agent=build_user_agent(
            settings.application_version, settings.connector_egress_contact_url
        ),
        limits=FetchLimits(
            connect_timeout_seconds=settings.connector_egress_connect_timeout_seconds,
            read_timeout_seconds=settings.connector_egress_read_timeout_seconds,
            total_timeout_seconds=settings.connector_egress_total_timeout_seconds,
            max_response_bytes=settings.connector_egress_max_response_bytes,
            max_redirects=settings.connector_egress_max_redirects,
        ),
        replay_origin=parse_replay_origin(settings.connector_rss_replay_origin),
    )


class LiveRssConnector(SourceConnector):
    """Read approved feeds through the egress guard; one result per scout run.

    Each source is read in isolation: any failure while fetching, parsing or turning one
    source's items into signals is recorded for that source and the run continues with
    the next one.
    """

    name = "rss_news_live"
    source_type = "rss_news"
    is_simulated = False

    def __init__(
        self,
        targets: list[SourceTarget],
        *,
        fetcher: SafeFeedFetcher,
        cache: FeedCache | None = None,
        min_fetch_interval_seconds: float = 900.0,
        max_attempts: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.targets = targets
        self._fetcher = fetcher
        self._cache = cache if cache is not None else feed_cache
        self._interval = min_fetch_interval_seconds
        self._attempts = max(1, max_attempts)
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(UTC))
        #: Source keys that failed in the last :meth:`collect`.
        self.failed_sources: list[str] = []

    def collect(self, scope: FetchScope) -> ConnectorResult:
        result = ConnectorResult(
            connector=self.name, source_type=self.source_type, retrieved_at=self._now()
        )
        self.failed_sources = []
        for target in self.targets:
            key = target.source.source_key
            started = time.monotonic()
            try:
                feed, parsed = self._feed(target)
                signals, items = self._signals(target, feed, parsed, scope)
            except ConnectorFetchError as exc:
                self._record_failure(result, key, exc.failure.kind, exc.failure.detail)
                continue
            except Exception as exc:  # noqa: BLE001 - one source's defect never sinks the run
                detail = f"unreadable source ({type(exc).__name__})"
                self._record_failure(result, key, FailureKind.PARSE_ERROR, detail)
                continue
            result.signals.extend(signals)
            logger.info(
                "connector.source_read",
                extra={
                    "extra_fields": {
                        "source_key": key,
                        "delivery": target.delivery,
                        "items": items,
                        "kept": len(signals),
                        "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    }
                },
            )
        return result

    def _record_failure(
        self, result: ConnectorResult, key: str, kind: FailureKind, detail: str
    ) -> None:
        self.failed_sources.append(key)
        result.failures.append(FetchFailure(kind=kind, detail=f"{key}: {detail}"))
        logger.warning(
            "connector.source_failed",
            extra={"extra_fields": {"source_key": key, "kind": kind.value}},
        )

    def _signals(
        self, target: SourceTarget, feed: CachedFeed, parsed: ParsedFeed, scope: FetchScope
    ) -> tuple[list, int]:
        """This source's keyword-matched signals, and how many items its feed held."""
        attribution = {
            "connector": "rss_news",
            "delivery": target.delivery,
            "source_key": target.source.source_key,
            "publisher": target.source.publisher,
            "source_title": parsed.title or target.source.display_name,
            "feed_url": target.source.feed_url,
            "tos_url": target.source.tos_url,
            "approval_reference": target.source.approval_reference,
            "attribution": target.source.attribution,
            "feed_sha256": feed.sha256,
            "synthetic": target.is_simulated,
            **dict(target.provenance),
        }
        signals = []
        for item in parsed.items[: scope.max_items]:
            signal = item_signal(
                item,
                market=scope.market,
                retrieved_at=feed.retrieved_at,
                now=self._now(),
                is_simulated=target.is_simulated,
                attribution=attribution,
            )
            if keyword_match(signal, scope.keywords):
                signals.append(signal)
        return signals, len(parsed.items)

    def _feed(self, target: SourceTarget) -> tuple[CachedFeed, ParsedFeed]:
        """The source's feed and its parse. A body is cached only after it parsed, so a
        feed this connector cannot read is never served again from the cache."""
        key = target.source.source_key
        interval = max(self._interval, target.source.min_fetch_interval_seconds or 0.0)
        fresh = self._cache.fresh(key, interval)
        if fresh is not None:
            return fresh, parse_feed(fresh.body)
        if self._cache.cooling(key):
            held = self._cache.cached(key)
            if held is not None:
                return held, parse_feed(held.body)
            raise ConnectorFetchError(*self._cache.cooling_reason(key))
        validators = self._cache.validators(key)
        last: ConnectorFetchError | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                response = self._fetcher.fetch(
                    target.fetch_url, allowed_hosts=target.allowed_hosts, validators=validators
                )
            except ConnectorFetchError as exc:
                last = exc
                if exc.retry_after_seconds is not None:
                    self._cache.cool_down(key, exc.retry_after_seconds)
                if exc.failure.kind not in _RETRYABLE or exc.retry_after_seconds is not None:
                    raise
                if attempt < self._attempts:
                    self._sleep(0.5 * attempt)
                continue
            if response.not_modified:
                held = self._cache.revalidated(key, response)
                if held is None:  # validators are only sent while a body is held
                    raise ConnectorFetchError(FailureKind.UPSTREAM_ERROR, "304 without a body")
                return held, parse_feed(held.body)
            try:
                parsed = parse_feed(response.body)
            except ConnectorFetchError as exc:
                # Not cached, and not asked for again before the politeness interval
                # passes: an unreadable feed costs its publisher one request per
                # interval. A previously cached good body stays available meanwhile.
                self._cache.cool_down(
                    key, interval, kind=exc.failure.kind, detail=exc.failure.detail
                )
                raise
            except Exception:
                self._cache.cool_down(
                    key, interval, kind=FailureKind.PARSE_ERROR, detail="unreadable feed"
                )
                raise
            return self._cache.store(key, response), parsed
        assert last is not None
        raise last


def live_connector_for(
    *,
    market: str | None,
    source_types: tuple[str, ...],
    takedowns: frozenset[str],
    rss_enabled: bool,
    settings: Settings | None = None,
) -> LiveRssConnector | None:
    """The live connector for this scope, or ``None``.

    ``rss_enabled`` is the capability resolver's decision for the request's workspace
    (P6-CAP-1): live feeds are RSS, so a workspace whose RSS is off (override, safety
    ceiling or the global flag) gets no live connector, whatever the live gate says.
    It has no default so no caller can bypass the per-workspace decision by omission.
    """
    settings = settings or get_settings()
    # The takedown ran in another process (the API); this process drops any bytes it
    # still holds for a taken-down source before it reads anything (P6-PRIV-4).
    for key in takedowns:
        feed_cache.forget(key)
    if not rss_enabled:
        return None
    targets = live_targets_for(
        market=market, source_types=source_types, takedowns=takedowns, settings=settings
    )
    if not targets:
        return None
    return LiveRssConnector(
        targets,
        fetcher=build_fetcher(settings),
        min_fetch_interval_seconds=settings.connector_rss_min_fetch_interval_seconds,
        max_attempts=settings.connector_rss_max_attempts,
    )
