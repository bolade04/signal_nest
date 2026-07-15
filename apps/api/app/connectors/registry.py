"""Resolve which connector serves a scout request.

The registry maps a request scope (enabled source types + market) to a concrete
:class:`SourceConnector` when policy permits, or ``None`` when no live connector
applies (the caller then falls back to the sandbox fixture connector). Live
connectors are **off by default**: a source runs only when its config flag is on
and its jurisdiction policy admits the request, so default behaviour is
byte-identical to the pre-Phase-3B fixture path.
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
