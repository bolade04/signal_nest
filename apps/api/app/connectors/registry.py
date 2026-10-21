"""Resolve which connector serves a scout request.

The registry maps a request scope (enabled source types + market) to a concrete
:class:`SourceConnector` when policy permits, or ``None`` when no live connector
applies (the caller then falls back to the sandbox fixture connector). Live
connectors are **off by default**: a source runs only when its capability resolves
enabled for the request's workspace and its jurisdiction policy admits the request,
so default behaviour is byte-identical to the pre-Phase-3B fixture path.

Whether RSS is enabled is **not** read from the raw ``connector_rss_enabled`` flag
here (P6-CAP-1). The caller passes the resolver's per-workspace decision for
:attr:`~app.capabilities.registry.Capability.CONNECTOR_RSS`, so the safety ceiling and
a workspace *disable* override apply. (RSS is not ``workspace_enableable``: an enable
override can never turn it on — only the global flag can.)
"""

from __future__ import annotations

import time

from app.capabilities.registry import Capability
from app.capabilities.resolver import CapabilityResolution
from app.connectors.base import SourceConnector
from app.connectors.policy import ConnectorPolicy
from app.connectors.ratelimit import TokenBucket
from app.connectors.retry import RetryPolicy
from app.connectors.rss import RssNewsConnector
from app.core.config import Settings, get_settings


def _rss_policy(settings: Settings, *, enabled: bool) -> ConnectorPolicy:
    return ConnectorPolicy(
        source_type="rss_news",
        enabled=enabled,
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
    rss: CapabilityResolution,
    settings: Settings | None = None,
) -> SourceConnector | None:
    """Return the permitted live connector for this scope, or ``None``.

    ``rss`` is the capability resolver's decision for ``connector_rss`` in the
    request's workspace; only its ``effective_enabled`` switches the connector on.
    It is required (no default) so no caller can fall back to the raw global flag.
    A resolution for any other capability is a programming error and is refused
    loudly rather than read as an answer about RSS.
    """
    if rss.capability is not Capability.CONNECTOR_RSS:
        raise ValueError(
            f"resolve_connector needs a {Capability.CONNECTOR_RSS.value} resolution, "
            f"got {rss.capability.value}"
        )
    settings = settings or get_settings()

    policy = _rss_policy(settings, enabled=rss.effective_enabled)
    if policy.permits(source_types=source_types, market=market):
        return _build_rss(settings)

    return None
