"""Signal connectors.

A single ``Connector`` interface is what live connectors (Phase 3B onward)
implement. The default ``FixtureConnector`` reads from the simulated fixture
library, filtered by the request's market + keywords + enabled source types, so
results stay isolated per location.

Since Phase 3B, :func:`get_connector` consults the connector registry
(``app.connectors``): when a live connector is enabled *and* its policy permits
the request scope, that connector is used; otherwise this falls back to the
sandbox ``FixtureConnector``, so default behaviour is unchanged.

Since P6-CAP-1, "enabled" is the capability resolver's per-workspace decision
(:func:`resolve_rss_capability`), never the raw ``connector_rss_enabled`` flag.
"""

from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy.orm import Session

from app.capabilities.registry import Capability
from app.capabilities.resolver import CapabilityResolution, resolve_capability
from app.core.config import get_settings
from app.core.logging import get_logger, log_event
from app.scouting_requests.fixtures import FixtureSignal, fixtures_for_market

logger = get_logger("signalnest.scouting.connectors")


class Connector(Protocol):
    name: str

    def fetch(
        self, *, market: str | None, keywords: list[str], source_types: list[str]
    ) -> list[FixtureSignal]:
        ...


class FixtureConnector:
    name = "fixture"
    is_simulated = True

    def fetch(
        self, *, market: str | None, keywords: list[str], source_types: list[str]
    ) -> list[FixtureSignal]:
        candidates = fixtures_for_market(market)
        allowed = set(source_types) if source_types else None
        kw = [k.lower() for k in keywords]

        results: list[FixtureSignal] = []
        for signal in candidates:
            if allowed is not None and signal.source_type not in allowed:
                continue
            if kw:
                haystack = (signal.content + " " + " ".join(signal.topics)).lower()
                if not any(k in haystack for k in kw):
                    # Keep spam/noise fixtures so the noise filter is exercised.
                    if "spam" not in signal.topics:
                        continue
            results.append(signal)
        return results


def resolve_rss_capability(
    db: Session, *, organization_id: str, workspace_id: str
) -> CapabilityResolution:
    """The one place RSS availability is decided for a workspace (P6-CAP-1).

    Goes through the deny-biased resolver, so the safety ceiling and a workspace
    *disable* override apply; RSS is not ``workspace_enableable``, so an enable
    override resolves to the secure default and never turns it on. The operator
    console's effective view resolves the same ``(capability, workspace)`` pair with
    the same function, so what it reports is what the pipeline selects.

    A dependency failure is logged and re-raised — the run fails and is retried —
    never coerced into a decision either way.
    """
    try:
        return resolve_capability(
            session=db,
            settings=get_settings(),
            capability=Capability.CONNECTOR_RSS,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
    except Exception:
        log_event(
            logger,
            "connector_rss_gate_failed",
            level=logging.ERROR,
            outcome="error",
            capability=Capability.CONNECTOR_RSS.value,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        raise


def get_connector(
    *,
    db: Session | None = None,
    organization_id: str | None = None,
    workspace_id: str | None = None,
    source_types: list[str] | None = None,
    market: str | None = None,
) -> Connector:
    """Resolve the connector for a request scope.

    Returns a live connector when its capability resolves enabled for the request's
    workspace and its policy permits the scope; otherwise the sandbox
    :class:`FixtureConnector`. Without a session *and* a full tenant scope there is
    no workspace to resolve a capability for, so the answer is the fixture
    connector — deny-biased, never the raw global flag. Called with no arguments
    (the legacy signature) it therefore always returns the fixture connector.
    """
    if db is None or organization_id is None or workspace_id is None:
        return FixtureConnector()

    from app.connectors.registry import resolve_connector

    rss = resolve_rss_capability(
        db, organization_id=organization_id, workspace_id=workspace_id
    )
    live = resolve_connector(
        source_types=tuple(source_types or ()),
        market=market,
        rss=rss,
    )
    if rss.effective_enabled:
        # Only an enable is logged per run: the dark default would otherwise log on
        # every scout run without saying anything new.
        log_event(
            logger,
            "connector_rss_gate_decided",
            outcome="allowed",
            capability=Capability.CONNECTOR_RSS.value,
            organization_id=organization_id,
            workspace_id=workspace_id,
            decided_by=rss.decided_by.value,
            selected=live is not None,
        )
    return live or FixtureConnector()
