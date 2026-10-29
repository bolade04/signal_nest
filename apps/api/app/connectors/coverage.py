"""Source-coverage outcome of one scout run (P6-DATA-2).

A run never ends in an unexplained empty result. Every run records *why* it holds
the signals it holds, as a closed reason code in the run's stats
(``ScoutRequest.stats["coverage"]``, the job's ``result_summary`` and the API's
``coverage`` field), so the product can tell apart:

* ``covered`` — a source covers the market. Zero opportunities then means the
  sources were read and nothing relevant was found.
* ``not_covered`` — no configured source (live, sandbox or demo library) covers the
  market. Running again will not help; the market needs a source.
* ``unavailable`` — live sources cover the market but every one of them failed this
  run (timeout, upstream error, rate limit). Trying again later may help.

``is_simulated`` says whether anything the run consumed was simulated (the demo
library or the sandbox feed). ``sources`` / ``sources_failed`` count the live sources
consulted; they are 0 for the simulated paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CoverageStatus(StrEnum):
    COVERED = "covered"
    NOT_COVERED = "not_covered"
    UNAVAILABLE = "unavailable"


class CoverageReason(StrEnum):
    #: Approved live sources serve the market and at least one was read.
    LIVE_SOURCES = "live_sources"
    #: The bundled sandbox RSS feed (simulated) serves the market.
    SANDBOX_FEED = "sandbox_feed"
    #: The simulated demo fixture library serves the market.
    FIXTURE_LIBRARY = "fixture_library"
    #: No configured source serves the market.
    NO_SOURCE_FOR_MARKET = "no_source_for_market"
    #: Live sources serve the market but every one failed this run.
    SOURCES_UNAVAILABLE = "sources_unavailable"


_STATUS_FOR = {
    CoverageReason.LIVE_SOURCES: CoverageStatus.COVERED,
    CoverageReason.SANDBOX_FEED: CoverageStatus.COVERED,
    CoverageReason.FIXTURE_LIBRARY: CoverageStatus.COVERED,
    CoverageReason.NO_SOURCE_FOR_MARKET: CoverageStatus.NOT_COVERED,
    CoverageReason.SOURCES_UNAVAILABLE: CoverageStatus.UNAVAILABLE,
}


@dataclass(frozen=True)
class Coverage:
    reason: CoverageReason
    is_simulated: bool
    sources: int = 0
    sources_failed: int = 0

    @property
    def status(self) -> CoverageStatus:
        return _STATUS_FOR[self.reason]

    def as_stats(self) -> dict:
        """The JSON object stored under ``stats["coverage"]``."""
        return {
            "status": self.status.value,
            "reason": self.reason.value,
            "is_simulated": self.is_simulated,
            "sources": self.sources,
            "sources_failed": self.sources_failed,
        }
