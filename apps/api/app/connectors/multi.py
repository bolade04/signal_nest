"""Run several sources as one connector, keeping every source's own fetch limit.

Each member is a :class:`SourceConnector` that applies its own per-source fetch limit
(``FetchScope.max_items``) and reports against it. This aggregator concatenates the
members' signals and reports in order; it adds no limit of its own. The per-run
in-process bound is applied afterwards by the pipeline to the **sum** over sources
(order: fetch limits first, per source; then the bound, per run). It is a composition
only - it approves no source and opens no connection beyond what its members do.
"""

from __future__ import annotations

from app.connectors.base import ConnectorResult, FetchScope, SourceConnector


class MultiSourceConnector(SourceConnector):
    name = "multi_source"
    source_type = "multi"

    def __init__(self, members: list[SourceConnector]) -> None:
        self._members = list(members)
        self.is_simulated = all(m.is_simulated for m in self._members)

    def collect(self, scope: FetchScope) -> ConnectorResult:
        merged = ConnectorResult(connector=self.name, source_type=self.source_type)
        for member in self._members:
            part = member.collect(scope)
            merged.signals.extend(part.signals)
            merged.failures.extend(part.failures)
            merged.reports.extend(part.reports)
            merged.retrieved_at = max(merged.retrieved_at, part.retrieved_at)
        return merged
