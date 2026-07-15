"""Phase 3B scouting-connector foundation + RSS connector tests.

Nothing here performs network I/O: the RSS connector is driven by its bundled
deterministic sandbox feed (or an injected in-memory provider). Coverage:

* **Rate limiter** — token consumption, refill over an injected clock, wait math.
* **Retry** — classified backoff, retry-only-transient, give-up after the cap.
* **Policy** — enablement + jurisdiction gating.
* **RSS connector** — parse → normalize → attribute, market isolation, keyword
  filter, and defensive rejection of unsafe / malformed feeds with classified,
  secret-free failures.
* **Registry + seam** — live connector resolves only when enabled and permitted;
  otherwise the sandbox fixture connector stays authoritative (unchanged default).
* **Contract** — a ``ConnectorSignal`` exposes every attribute the scout pipeline
  reads, so a connector is a drop-in for the fixture path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.connectors.base import (
    ConnectorSignal,
    FailureKind,
    FetchScope,
)
from app.connectors.policy import ConnectorPolicy
from app.connectors.ratelimit import TokenBucket
from app.connectors.retry import (
    ConnectorFetchError,
    RetryPolicy,
    run_with_retry,
)
from app.connectors.rss import RssNewsConnector
from app.core.config import Settings


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# --------------------------------------------------------------------------- #
# Rate limiter
# --------------------------------------------------------------------------- #


def test_token_bucket_consumes_then_blocks_until_refill() -> None:
    clock = _FakeClock()
    bucket = TokenBucket(capacity=2, refill_per_second=1.0, clock=clock)

    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is True
    # Bucket empty now.
    assert bucket.try_acquire() is False
    assert bucket.retry_after_seconds() == pytest.approx(1.0)

    clock.advance(1.0)
    assert bucket.try_acquire() is True


def test_token_bucket_rejects_invalid_construction() -> None:
    clock = _FakeClock()
    with pytest.raises(ValueError):
        TokenBucket(capacity=0, refill_per_second=1.0, clock=clock)
    with pytest.raises(ValueError):
        TokenBucket(capacity=1, refill_per_second=0.0, clock=clock)


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #


def test_retry_policy_backoff_is_bounded_and_first_attempt_never_waits() -> None:
    policy = RetryPolicy(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=2.0)
    assert policy.delay_for(1) == 0.0
    assert policy.delay_for(2) == pytest.approx(0.5)
    assert policy.delay_for(3) == pytest.approx(1.0)
    assert policy.delay_for(4) == pytest.approx(2.0)  # capped
    assert policy.delay_for(5) == pytest.approx(2.0)


def test_retry_policy_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay_seconds=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay_seconds=2.0, max_delay_seconds=1.0)


def test_run_with_retry_recovers_from_transient_then_succeeds() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectorFetchError(FailureKind.TIMEOUT, "timed out")
        return "ok"

    out = run_with_retry(flaky, policy=RetryPolicy(max_attempts=3))
    assert out == "ok"
    assert calls["n"] == 3


def test_run_with_retry_does_not_retry_permanent_failure() -> None:
    calls = {"n": 0}

    def broken() -> str:
        calls["n"] += 1
        raise ConnectorFetchError(FailureKind.PARSE_ERROR, "bad xml")

    with pytest.raises(ConnectorFetchError) as exc:
        run_with_retry(broken, policy=RetryPolicy(max_attempts=5))
    assert exc.value.failure.kind is FailureKind.PARSE_ERROR
    assert calls["n"] == 1  # permanent ⇒ no retry


def test_run_with_retry_gives_up_after_max_attempts() -> None:
    calls = {"n": 0}

    def always_timeout() -> str:
        calls["n"] += 1
        raise ConnectorFetchError(FailureKind.TIMEOUT, "timed out")

    with pytest.raises(ConnectorFetchError):
        run_with_retry(always_timeout, policy=RetryPolicy(max_attempts=3))
    assert calls["n"] == 3


def test_failure_kind_retryability() -> None:
    assert FailureKind.TIMEOUT.retryable is True
    assert FailureKind.RATE_LIMITED.retryable is True
    assert FailureKind.PARSE_ERROR.retryable is False
    assert FailureKind.UNSAFE_CONTENT.retryable is False
    assert FailureKind.NOT_CONFIGURED.retryable is False


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


def test_policy_disabled_never_permits() -> None:
    policy = ConnectorPolicy(source_type="rss_news", enabled=False)
    assert policy.permits(source_types=("rss_news",), market="Dallas, TX") is False


def test_policy_requires_matching_source_type() -> None:
    policy = ConnectorPolicy(source_type="rss_news", enabled=True)
    assert policy.permits(source_types=("reddit",), market=None) is False
    assert policy.permits(source_types=("rss_news",), market=None) is True
    # Empty source_types ⇒ no source-type restriction.
    assert policy.permits(source_types=(), market=None) is True


def test_policy_enforces_jurisdiction() -> None:
    policy = ConnectorPolicy(
        source_type="rss_news",
        enabled=True,
        allowed_markets=frozenset({"Dallas, TX"}),
    )
    assert policy.permits(source_types=("rss_news",), market="Dallas, TX") is True
    assert policy.permits(source_types=("rss_news",), market="London, UK") is False


# --------------------------------------------------------------------------- #
# RSS connector
# --------------------------------------------------------------------------- #


def _fixed_now() -> datetime:
    # Exactly 7 days after the sample feed's static pubDate (Mon 07 Jul 2025 09:00Z).
    return datetime(2025, 7, 14, 9, 0, tzinfo=UTC)


def test_rss_connector_parses_and_attributes_sandbox_feed() -> None:
    conn = RssNewsConnector(now=_fixed_now)
    result = conn.collect(FetchScope(market="Dallas, TX"))

    assert result.degraded is False
    assert len(result.signals) == 2
    for s in result.signals:
        assert s.source_type == "rss_news"
        assert s.market == "Dallas, TX"
        assert s.is_simulated is True
        assert s.news_coverage is True
        assert s.attribution["connector"] == "rss_news"
        assert s.attribution["license"] == "publisher-syndicated RSS"
        assert s.attribution["source_title"] == "Dallas Local Business News"
        # Age is derived from the feed's static pubDate (2025-07-07) vs fixed now.
        assert s.published_days_ago == pytest.approx(7.0, abs=0.01)
        assert ("local_source", "Dallas, TX") in s.geo_evidence


def test_rss_connector_isolates_markets() -> None:
    conn = RssNewsConnector(now=_fixed_now)
    dallas = conn.collect(FetchScope(market="Dallas, TX")).signals
    london = conn.collect(FetchScope(market="London, UK")).signals

    assert all("Dallas" in s.content for s in dallas)
    assert all("London" in s.content for s in london)
    assert all("London" not in s.content for s in dallas)


def test_rss_connector_unknown_market_returns_no_signals() -> None:
    conn = RssNewsConnector(now=_fixed_now)
    result = conn.collect(FetchScope(market="Atlantis"))
    assert result.signals == []
    assert result.degraded is False


def test_rss_connector_applies_keyword_filter() -> None:
    conn = RssNewsConnector(now=_fixed_now)
    # Only the second sample item mentions "pickup".
    result = conn.collect(FetchScope(market="Dallas, TX", keywords=("pickup",)))
    assert len(result.signals) == 1
    assert "pickup" in result.signals[0].content.lower()


def test_rss_connector_rejects_doctype_feed_as_unsafe() -> None:
    unsafe = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x "y">]>'
        b"<rss version=\"2.0\"><channel><title>x</title></channel></rss>"
    )
    conn = RssNewsConnector(feed_provider=lambda _m: unsafe, now=_fixed_now)
    result = conn.collect(FetchScope(market="Dallas, TX"))
    assert result.signals == []
    assert result.degraded is True
    assert result.failures[0].kind is FailureKind.UNSAFE_CONTENT


def test_rss_connector_classifies_malformed_xml() -> None:
    conn = RssNewsConnector(feed_provider=lambda _m: b"<rss><broken", now=_fixed_now)
    result = conn.collect(FetchScope(market="Dallas, TX"))
    assert result.signals == []
    assert result.failures[0].kind is FailureKind.PARSE_ERROR


def test_rss_connector_classifies_and_gives_up_on_persistent_network_fault() -> None:
    def failing(_market: str | None) -> bytes:
        raise ConnectorFetchError(FailureKind.NETWORK, "connection reset")

    conn = RssNewsConnector(
        feed_provider=failing,
        retry_policy=RetryPolicy(max_attempts=2),
        now=_fixed_now,
    )
    result = conn.collect(FetchScope(market="Dallas, TX"))
    assert result.signals == []
    assert result.degraded is True
    assert result.failures[0].kind is FailureKind.NETWORK


def test_rss_connector_records_rate_limit_when_bucket_empty() -> None:
    clock = _FakeClock()
    bucket = TokenBucket(capacity=1, refill_per_second=1.0, clock=clock)
    assert bucket.try_acquire() is True  # drain the only token

    conn = RssNewsConnector(
        rate_limiter=bucket,
        retry_policy=RetryPolicy(max_attempts=1),
        now=_fixed_now,
    )
    result = conn.collect(FetchScope(market="Dallas, TX"))
    assert result.signals == []
    assert result.failures[0].kind is FailureKind.RATE_LIMITED


# --------------------------------------------------------------------------- #
# Registry + seam
# --------------------------------------------------------------------------- #


def test_registry_returns_none_when_disabled() -> None:
    from app.connectors.registry import resolve_connector

    resolved = resolve_connector(
        source_types=("rss_news",),
        market="Dallas, TX",
        settings=_settings(connector_rss_enabled=False),
    )
    assert resolved is None


def test_registry_builds_rss_when_enabled_and_permitted() -> None:
    from app.connectors.registry import resolve_connector

    resolved = resolve_connector(
        source_types=("rss_news",),
        market="Dallas, TX",
        settings=_settings(connector_rss_enabled=True),
    )
    assert isinstance(resolved, RssNewsConnector)


def test_registry_respects_source_type_and_jurisdiction() -> None:
    from app.connectors.registry import resolve_connector

    # Enabled but the request does not include rss_news.
    assert (
        resolve_connector(
            source_types=("reddit",),
            market="Dallas, TX",
            settings=_settings(connector_rss_enabled=True),
        )
        is None
    )
    # Enabled but market outside the connector's cleared jurisdiction.
    assert (
        resolve_connector(
            source_types=("rss_news",),
            market="London, UK",
            settings=_settings(
                connector_rss_enabled=True,
                connector_rss_markets=["Dallas, TX"],
            ),
        )
        is None
    )


def test_get_connector_defaults_to_fixture_connector() -> None:
    from app.scouting_requests.connectors import FixtureConnector, get_connector

    conn = get_connector(source_types=["rss_news"], market="Dallas, TX")
    assert isinstance(conn, FixtureConnector)


def test_get_connector_uses_live_connector_when_enabled(monkeypatch) -> None:
    import app.connectors.registry as registry

    monkeypatch.setattr(
        registry, "get_settings", lambda: _settings(connector_rss_enabled=True)
    )
    from app.scouting_requests.connectors import get_connector

    conn = get_connector(source_types=["rss_news"], market="Dallas, TX")
    assert isinstance(conn, RssNewsConnector)


# --------------------------------------------------------------------------- #
# Contract: ConnectorSignal is a drop-in for the fixture path
# --------------------------------------------------------------------------- #


def test_connector_signal_exposes_every_pipeline_attribute() -> None:
    # These are exactly the attributes app/jobs/pipeline.py reads off a connector
    # item. If a rename ever drops one, the pipeline would silently break — this
    # locks the contract.
    required = [
        "source_type",
        "source_url",
        "author",
        "language",
        "content",
        "topics",
        "market",
        "engagement",
        "published_days_ago",
        "duplicate_count",
        "distinct_source_types",
        "has_buying_intent",
        "has_active_ads",
        "news_coverage",
        "search_trend_up",
        "geo_evidence",
    ]
    signal = ConnectorSignal(source_type="rss_news", content="hi", market=None)
    for attr in required:
        assert hasattr(signal, attr), f"ConnectorSignal missing pipeline attribute {attr!r}"
