"""Live RSS connector and its fail-closed gate (P6-DATA-1 / P6-DATA-4).

:func:`live_connector_for` is the single decision point for "may this scout run read
live feeds?". In order, any ``no`` ends the decision and the run falls back to the
simulated paths (or reports ``not_covered``):

1. the capability resolver admits RSS for the request's workspace (P6-CAP-1:
   ``connector_rss_enabled`` on globally and no workspace disable override; passed in
   as ``rss_enabled``, which has no default, and checked before any target is computed);
2. ``connector_rss_kill_switch`` is off;
3. ``connector_rss_live_enabled`` is on;
4. the request's source types admit ``rss_news`` (an empty list admits every type);
5. at least one record in the source registry is activatable (enabled, terms
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

Per-source fetch limit (#268, FU-268-2): each source contributes at most
``FetchScope.max_items`` entries. The parser extracts at most one entry past that limit
(:func:`~app.connectors.fetch_limits.lookahead_entries`), so a source that held more is
reported ``clipped`` instead of passing silently for complete; the byte caps (the egress
guard's ``max_response_bytes`` and the parser's ``MAX_FEED_BYTES``) bound everything read.
Every source - read, clipped or failed - yields one
:class:`~app.connectors.fetch_limits.SourceFetchReport` in ``ConnectorResult.reports``; a
failed source is reported ``failed`` with a closed ``failure_reason``, never as an empty
one. The per-run in-process bound applies afterwards, in the pipeline, to the total over
all sources.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

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
from app.connectors.fetch_limits import lookahead_entries
from app.connectors.retry import ConnectorFetchError
from app.connectors.rss import (
    ParsedFeed,
    failed_source_report,
    item_signal,
    keyword_match,
    parse_feed,
    source_report,
)
from app.connectors.sources import APPROVED_SOURCES, SourceRecord
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.jobs.admission import (
    AdmissionUnresolved,
    RunStopped,
    admit_external_operation,
    record_external_end,
)

logger = get_logger("signalnest.connectors.live")

#: D-25 execution controls (PROPOSED): fetch failures whose outcome is KNOWN - the egress
#: guard refused before connecting, or a response was received. Everything else (any timeout,
#: a transport error after connecting, anything doubtful) is UNKNOWN (contract §8.2.2).
_KNOWN_KINDS = frozenset(
    {
        FailureKind.EGRESS_BLOCKED,
        FailureKind.UPSTREAM_ERROR,
        FailureKind.RATE_LIMITED,
        FailureKind.HTTP_CLIENT_ERROR,
        FailureKind.UNSAFE_CONTENT,
    }
)


def fetch_end_known(exc: ConnectorFetchError) -> bool:
    """Whether a failed fetch attempt's outcome is known, from the egress exception chain."""
    cause = exc.__cause__
    if exc.failure.kind in _KNOWN_KINDS or isinstance(cause, httpx.ConnectError):
        return True
    # DNS failures happen before any connection; they carry no httpx cause.
    return exc.failure.kind == FailureKind.NETWORK and not isinstance(cause, httpx.HTTPError)


#: Transient kinds a live fetch retries within one fetch (a 429 never is). Every failure,
#: retried or not, ends in a cool-down once the fetch gives up (r2-MINOR-1).
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
        # Look one entry past the per-source fetch limit (never more): that single extra
        # entry is what tells a clipped source from one that held exactly ``max_items``.
        max_entries = lookahead_entries(scope.max_items)
        for target in self.targets:
            key = target.source.source_key
            started = time.monotonic()
            try:
                feed, parsed = self._feed(target, max_entries)
                signals, fetched, clipped = self._signals(target, feed, parsed, scope)
            except (RunStopped, AdmissionUnresolved):
                # D-25 (PROPOSED): a refused admission stops the whole run; it is not a
                # failure of this source and must not be recorded as one.
                raise
            except ConnectorFetchError as exc:
                self._record_failure(result, scope, key, exc.failure.kind, exc.failure.detail)
                continue
            except Exception as exc:  # noqa: BLE001 - one source's defect never sinks the run
                detail = f"unreadable source ({type(exc).__name__})"
                self._record_failure(result, scope, key, FailureKind.PARSE_ERROR, detail)
                continue
            result.signals.extend(signals)
            result.reports.append(
                source_report(key, scope, fetched=fetched, eligible=len(signals), clipped=clipped)
            )
            logger.info(
                "connector.source_read",
                extra={
                    "extra_fields": {
                        "source_key": key,
                        "delivery": target.delivery,
                        "items": len(parsed.items),
                        "fetched": fetched,
                        "clipped": clipped,
                        "kept": len(signals),
                        "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    }
                },
            )
        return result

    def _record_failure(
        self,
        result: ConnectorResult,
        scope: FetchScope,
        key: str,
        kind: FailureKind,
        detail: str,
    ) -> None:
        self.failed_sources.append(key)
        failure = FetchFailure(kind=kind, detail=f"{key}: {detail}")
        result.failures.append(failure)
        # A failed source contributed nothing; it is reported failed, never as an empty
        # one (the closed reason is derived from the kind and the static detail).
        result.reports.append(
            failed_source_report(key, scope, FetchFailure(kind=kind, detail=detail))
        )
        logger.warning(
            "connector.source_failed",
            extra={"extra_fields": {"source_key": key, "kind": kind.value}},
        )

    def _signals(
        self, target: SourceTarget, feed: CachedFeed, parsed: ParsedFeed, scope: FetchScope
    ) -> tuple[list, int, bool]:
        """This source's keyword-matched signals from the entries within its fetch limit,
        how many entries were read within it, and whether the source held more (clipped)."""
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
        window = parsed.items[: scope.max_items]
        for item in window:
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
        return signals, len(window), len(parsed.items) > scope.max_items

    def _feed(self, target: SourceTarget, max_entries: int) -> tuple[CachedFeed, ParsedFeed]:
        """The source's feed and its parse (at most ``max_entries`` entries extracted). A
        body is cached only after it parsed, so a feed this connector cannot read is never
        served again from the cache.

        Politeness floor (#255 review r2-MINOR-1): a source is contacted at most once per
        interval, failing or not, when runs do not overlap (see
        ``docs/operations/live-data-sources.md`` §6). Every failed fetch - once its bounded
        retries are spent, whatever the failure - starts a cool-down
        (:meth:`FeedCache.fetch_failed`: one interval, doubling per consecutive failure up
        to a bound; a ``Retry-After`` can only lengthen it). Meanwhile the last good body,
        if any, is served without a request. A D-25 stop (``RunStopped`` /
        ``AdmissionUnresolved``) is not a source failure and starts no cool-down."""
        key = target.source.source_key
        interval = max(self._interval, target.source.min_fetch_interval_seconds or 0.0)
        fresh = self._cache.fresh(key, interval)
        if fresh is not None:
            return fresh, parse_feed(fresh.body, max_entries=max_entries)
        if self._cache.cooling(key):
            held = self._cache.cached(key)
            if held is not None:
                return held, parse_feed(held.body, max_entries=max_entries)
            raise ConnectorFetchError(*self._cache.cooling_reason(key))
        try:
            return self._fetch(target, max_entries)
        except (RunStopped, AdmissionUnresolved):
            # D-25 (PROPOSED): the run stops; the source did not fail.
            raise
        except ConnectorFetchError as exc:
            if exc.retry_after_seconds is not None:
                # The publisher asked for a pause: while it lasts the source reports
                # rate limiting (it is never contacted before it ends).
                self._cache.fetch_failed(
                    key, interval, retry_after_seconds=exc.retry_after_seconds
                )
            else:
                self._cache.fetch_failed(
                    key, interval, kind=exc.failure.kind, detail=exc.failure.detail
                )
            raise
        except Exception:
            self._cache.fetch_failed(
                key, interval, kind=FailureKind.PARSE_ERROR, detail="unreadable feed"
            )
            raise

    def _fetch(self, target: SourceTarget, max_entries: int) -> tuple[CachedFeed, ParsedFeed]:
        """One fetch of the source (bounded retries with backoff for transient failures)
        and its parse. Raises on failure; :meth:`_feed` starts the cool-down."""
        key = target.source.source_key
        validators = self._cache.validators(key)
        last: ConnectorFetchError | None = None
        for attempt in range(1, self._attempts + 1):
            # D-25 (PROPOSED): every fetch attempt (first try and each retry) is admitted
            # immediately before its request is dispatched; a no-op off the durable path.
            # The redirect hops of one attempt belong to that admitted attempt.
            # Execution controls (PROPOSED): the admission commits a ledger row (K = 1); its
            # end is recorded as known or unknown once the client call returns or raises.
            admission_id = admit_external_operation("fetch")
            try:
                response = self._fetcher.fetch(
                    target.fetch_url, allowed_hosts=target.allowed_hosts, validators=validators
                )
            except ConnectorFetchError as exc:
                record_external_end(admission_id, known=fetch_end_known(exc))
                last = exc
                if exc.failure.kind not in _RETRYABLE or exc.retry_after_seconds is not None:
                    raise
                if attempt < self._attempts:
                    self._sleep(0.5 * attempt)
                continue
            except BaseException:
                record_external_end(admission_id, known=False)
                raise
            record_external_end(admission_id, known=True)
            if response.not_modified:
                held = self._cache.revalidated(key, response)
                if held is None:  # validators are only sent while a body is held
                    raise ConnectorFetchError(FailureKind.UPSTREAM_ERROR, "304 without a body")
                return held, parse_feed(held.body, max_entries=max_entries)
            # An unreadable body raises here: it is not cached, and the cool-down
            # :meth:`_feed` starts keeps it from being asked for again before the
            # politeness interval passes. A previously cached good body stays available.
            parsed = parse_feed(response.body, max_entries=max_entries)
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
