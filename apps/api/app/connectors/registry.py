"""Resolve the sandbox RSS connector for a scout request.

The registry maps a request scope (enabled source types + market) to the sandbox
:class:`RssNewsConnector` when ``connector_rss_enabled`` and its policy permit it,
or ``None`` (the caller then falls back to the fixture library). The sandbox reads
bundled sample feeds and never the network; live feeds are selected by
:mod:`app.connectors.live` through
:func:`app.scouting_requests.connectors.fetch_signals`.
"""

from __future__ import annotations

import time

from app.connectors.base import SourceConnector
from app.connectors.policy import ConnectorPolicy
from app.connectors.ratelimit import TokenBucket
from app.connectors.retry import RetryPolicy
from app.connectors.rss import RssNewsConnector
from app.core.config import Settings, get_settings


def _rss_policy(settings: Settings) -> ConnectorPolicy:
    return ConnectorPolicy(
        source_type="rss_news",
        enabled=settings.connector_rss_enabled,
        allowed_markets=frozenset(settings.connector_rss_markets),
    )


def _build_rss(settings: Settings) -> RssNewsConnector:
    limiter = TokenBucket(
        capacity=settings.connector_rss_rate_capacity,
        refill_per_second=settings.connector_rss_rate_refill_per_second,
        clock=time.monotonic,
    )
    retry = RetryPolicy(max_attempts=settings.connector_rss_max_attempts)
    return RssNewsConnector(rate_limiter=limiter, retry_policy=retry)


def resolve_connector(
    *,
    source_types: tuple[str, ...],
    market: str | None,
    settings: Settings | None = None,
) -> SourceConnector | None:
    """Return the permitted live connector for this scope, or ``None``."""
    settings = settings or get_settings()

    policy = _rss_policy(settings)
    if policy.permits(source_types=source_types, market=market):
        return _build_rss(settings)

    return None
