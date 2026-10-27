"""#268: the per-source fetch limit versus the per-run in-process bound (P6-PLAT-1 B).

Two independent limits sit on the path from a source to the dedupe stage:

* the **fetch limit** - one connector reads at most ``FetchScope.max_items`` (100) entries
  from one source per run - applied first, per source;
* the **bound** - ``vector_inprocess_max_signals_per_run`` (N_max = 100) - applied second,
  per run, to the total over all sources; a run above it is refused.

Before this change the fetch limit clipped silently: a 150-entry feed produced 100 signals
and a run that looked complete. These tests pin that the clip is detected (one entry of
look-ahead, never more than ``max_items + 1`` entries parsed), reported per source in the
run's stats as ``fetch_limits``, and marked ``partial_fetch`` / ``fetch_limit_reached`` -
and that the bound still refuses the total over several sources, showing the same facts.
Replay feeds only: no network, no approved source.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.connectors import rss as rss_mod
from app.connectors.base import FailureKind, FetchScope
from app.connectors.fetch_limits import (
    FETCH_LIMIT_REACHED,
    SOURCE_FAILED,
    SourceFetchReport,
    fetch_stats,
    partial_fetch_facts,
)
from app.connectors.multi import MultiSourceConnector
from app.connectors.ratelimit import TokenBucket
from app.connectors.registry import resolve_connector
from app.connectors.retry import ConnectorFetchError, RetryPolicy
from app.connectors.rss import RssNewsConnector
from app.core.config import get_settings
from app.core.enums import ScoutRequestStatus
from app.db import seed as seed_mod
from app.db.models import Base
from app.infra.vector import SIGNAL_VOLUME_ABOVE_BOUND, SignalVolumeAboveBoundError
from app.jobs import handlers, pipeline
from app.jobs.context import ExecutionContext

# Importing the app registers every ORM model + job handler on the shared Base/registry.
from app.main import app  # noqa: F401
from app.scouting_requests.connectors import FixtureConnector, fetch_with_reports
from app.scouting_requests.models import ScoutRequest
from app.tests._seed_support import clear_seeded_runs

_LIMIT = 100


def _feed(count: int, tag: str) -> bytes:
    """A replay RSS feed of ``count`` distinct entries that all match the keyword."""
    items = "".join(
        f"<item><title>Specialty coffee cafe {tag} story {i}</title>"
        f"<link>https://replay.invalid/{tag}/{i}</link>"
        f"<description>coffee cafe {tag}{i}a {tag}{i}b {tag}{i}c unique{tag}{i}</description>"
        f"<pubDate>Mon, 07 Jul 2025 09:00:00 +0000</pubDate></item>"
        for i in range(count)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>Replay {tag}</title><link>https://replay.invalid/{tag}</link>"
        f"<description>replay</description>{items}</channel></rss>"
    ).encode()


def _source(count: int, tag: str) -> RssNewsConnector:
    return RssNewsConnector(feed_provider=lambda _m: _feed(count, tag), source_key=f"replay:{tag}")


def _collect(count: int, **scope) -> tuple[list, SourceFetchReport]:
    result = _source(count, "solo").collect(FetchScope(market="Dallas, TX", **scope))
    assert len(result.reports) == 1
    return result.signals, result.reports[0]


# --------------------------------------------------------------------------- #
# The connector: look-ahead of one entry, bounded
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("count", "clipped"),
    [(0, False), (99, False), (100, False), (101, True), (150, True)],
)
def test_connector_reports_a_clip_exactly_when_the_source_held_more_than_the_limit(count, clipped):
    signals, report = _collect(count)
    assert report.clipped_by_fetch_limit is clipped
    assert report.max_items == _LIMIT
    assert report.fetched == min(count, _LIMIT) == len(signals)
    assert report.eligible_seen == len(signals)
    assert report.source_key == "replay:solo"


def test_connector_never_parses_more_than_one_entry_past_the_limit(monkeypatch):
    seen: list[int] = []
    real = RssNewsConnector._parse

    def spy(self, data, **kw):
        out = real(self, data, **kw)
        seen.append(len(out))
        return out

    monkeypatch.setattr(RssNewsConnector, "_parse", spy)
    _collect(500)  # a 500-entry feed: only max_items + 1 entries are ever turned into items
    assert seen == [_LIMIT + 1]
    seen.clear()
    _collect(500, max_items=7)
    assert seen == [8]


def test_the_byte_cap_still_applies_before_any_parsing():
    oversize = b"<rss>" + b"x" * (rss_mod.MAX_FEED_BYTES + 1)
    result = RssNewsConnector(feed_provider=lambda _m: oversize).collect(
        FetchScope(market="Dallas, TX")
    )
    assert [f.kind.value for f in result.failures] == ["unsafe_content"]
    assert result.signals == []
    assert result.reports[0].fetched == 0 and not result.reports[0].clipped_by_fetch_limit


def test_clip_is_judged_on_the_source_not_on_the_keywords():
    # Only the first 100 entries are read; the 101st exists, so the source is clipped
    # even though a keyword filter would have discarded some of what was read.
    signals, report = _collect(150, keywords=("story 1",))
    assert report.clipped_by_fetch_limit is True
    assert report.fetched == _LIMIT
    assert report.eligible_seen == len(signals) < _LIMIT


def test_a_source_without_a_declared_limit_reports_none_and_is_never_clipped():
    outcome = FixtureConnector().fetch_reported(market="Dallas, TX", keywords=[], source_types=[])
    [report] = outcome.reports
    assert (report.source_key, report.max_items, report.clipped_by_fetch_limit) == (
        "fixture",
        None,
        False,
    )
    assert report.fetched == len(outcome.signals)


def test_a_connector_double_without_reports_is_reported_not_left_silent():
    class Double:
        name = "double"

        def fetch(self, *, market, keywords, source_types):
            return [object(), object()]

    outcome = fetch_with_reports(Double(), market=None, keywords=[], source_types=[])
    assert [r.as_stats() for r in outcome.reports] == [
        {
            "source_key": "double",
            "max_items": None,
            "clipped": False,
            "fetched": 2,
            "eligible_seen": 2,
            "failed": False,
            "failure_reason": None,
        }
    ]


def test_multi_source_keeps_each_sources_own_limit_and_adds_none():
    multi = MultiSourceConnector([_source(150, "a"), _source(60, "b")])
    result = multi.collect(FetchScope(market="Dallas, TX"))
    assert len(result.signals) == 160  # 100 (clipped) + 60: the sum is the bound's business
    assert [(r.source_key, r.fetched, r.clipped_by_fetch_limit) for r in result.reports] == [
        ("replay:a", 100, True),
        ("replay:b", 60, False),
    ]


def test_fetch_stats_shape_is_pinned():
    clipped = SourceFetchReport("a", 100, 100, 100, True)
    whole = SourceFetchReport("b", 100, 60, 60, False)
    assert fetch_stats([clipped, whole]) == {
        "fetch_limits": [
            {
                "source_key": "a",
                "max_items": 100,
                "clipped": True,
                "fetched": 100,
                "eligible_seen": 100,
                "failed": False,
                "failure_reason": None,
            },
            {
                "source_key": "b",
                "max_items": 100,
                "clipped": False,
                "fetched": 60,
                "eligible_seen": 60,
                "failed": False,
                "failure_reason": None,
            },
        ],
        "partial_fetch": True,
        "partial_fetch_reasons": [FETCH_LIMIT_REACHED],
    }
    assert fetch_stats([whole]) == {
        "fetch_limits": [
            {
                "source_key": "b",
                "max_items": 100,
                "clipped": False,
                "fetched": 60,
                "eligible_seen": 60,
                "failed": False,
                "failure_reason": None,
            }
        ],
        "partial_fetch": False,
    }
    assert FETCH_LIMIT_REACHED == "fetch_limit_reached"


# --------------------------------------------------------------------------- #
# The pipeline: connector -> _run, offline
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def factory(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("fetch_limit_boundary")
    engine = create_engine(
        f"sqlite:///{tmp / 'fetch.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    original = seed_mod.SessionLocal
    seed_mod.SessionLocal = make
    try:
        seed_mod.seed(reset=True)
        clear_seeded_runs(make)
        with make() as s:
            request = s.get(ScoutRequest, seed_mod.sid("scout", "dallas"))
            request.source_types = ["rss_news"]
            request.keywords = ["coffee"]
            s.commit()
        yield make
    finally:
        seed_mod.SessionLocal = original
        engine.dispose()


@pytest.fixture
def bound(monkeypatch):
    monkeypatch.setattr(get_settings(), "vector_inprocess_max_signals_per_run", _LIMIT)


def _use(monkeypatch, connector) -> None:
    monkeypatch.setattr(pipeline, "get_connector", lambda **_kw: connector)


def _request_id() -> str:
    return seed_mod.sid("scout", "dallas")


def test_150_entry_source_through_the_real_registry_is_clipped_and_says_so(
    factory, bound, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "connector_rss_enabled", True)
    monkeypatch.setattr(settings, "connector_rss_markets", [])
    monkeypatch.setattr(rss_mod, "sample_feed_for_market", lambda _m: _feed(150, "solo"))
    # The registry hands the pipeline exactly one connector for this scope.
    assert isinstance(
        resolve_connector(source_types=("rss_news",), market="Dallas, TX", rss_enabled=True),
        RssNewsConnector,
    )
    with factory() as s:
        result = pipeline._run(s, _request_id())
        stats = dict(s.get(ScoutRequest, _request_id()).stats)
        s.rollback()
    assert result["scanned"] == stats["scanned"] == _LIMIT  # scanned = actually processed
    assert stats["partial_fetch"] is True
    assert stats["partial_fetch_reasons"] == [FETCH_LIMIT_REACHED]
    assert stats["fetch_limits"] == [
        {
            "source_key": "rss_news",
            "max_items": _LIMIT,
            "clipped": True,
            "fetched": 100,
            "eligible_seen": 100,
            "failed": False,
            "failure_reason": None,
        }
    ]
    assert "refused" not in stats  # the run succeeded: a clip is not a refusal
    assert result["partial_fetch"] is True  # the job's result_summary carries it too


def test_exactly_100_entries_is_complete_not_partial(factory, bound, monkeypatch):
    _use(monkeypatch, _source(100, "solo"))
    with factory() as s:
        pipeline._run(s, _request_id())
        stats = dict(s.get(ScoutRequest, _request_id()).stats)
        s.rollback()
    assert stats["scanned"] == _LIMIT
    assert stats["partial_fetch"] is False and "partial_fetch_reasons" not in stats
    assert stats["fetch_limits"][0]["clipped"] is False


def _refuse(factory, connector, monkeypatch) -> tuple[SignalVolumeAboveBoundError, dict]:
    """Run ``_run`` expecting the bound refusal; return it and the stats the handler writes."""
    _use(monkeypatch, connector)
    with factory() as s:
        with pytest.raises(SignalVolumeAboveBoundError) as exc:
            pipeline._run(s, _request_id())
        s.rollback()
    with factory() as s:
        request = s.get(ScoutRequest, _request_id())
        context = ExecutionContext.for_scout_request(
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
            request_id=request.id,
        )
        handlers._record_refused_run(request.id, context, exc.value)(s)
        s.expire_all()
        request = s.get(ScoutRequest, _request_id())
        assert request.status == ScoutRequestStatus.FAILED.value
        stats = dict(request.stats)
        s.rollback()
    return exc.value, stats


def test_two_sources_60_plus_60_exceed_the_bound_and_the_refusal_shows_both_sources(
    factory, bound, monkeypatch
):
    exc, stats = _refuse(
        factory, MultiSourceConnector([_source(60, "a"), _source(60, "b")]), monkeypatch
    )
    assert (exc.scanned, exc.bound) == (120, _LIMIT)  # neither source was clipped; the sum is
    assert stats["refused"] is True and stats["refusal_reason"] == SIGNAL_VOLUME_ABOVE_BOUND
    assert stats["scanned"] == 120 and stats["bound"] == _LIMIT
    assert stats["partial_fetch"] is False
    assert [(f["source_key"], f["fetched"], f["clipped"]) for f in stats["fetch_limits"]] == [
        ("replay:a", 60, False),
        ("replay:b", 60, False),
    ]


def test_two_clipped_sources_are_refused_on_the_clipped_total_with_both_facts(
    factory, bound, monkeypatch
):
    exc, stats = _refuse(
        factory, MultiSourceConnector([_source(150, "a"), _source(150, "b")]), monkeypatch
    )
    assert exc.scanned == 200  # fetch limits first (100 + 100), then the bound on the sum
    assert stats["refusal_reason"] == SIGNAL_VOLUME_ABOVE_BOUND
    assert stats["partial_fetch"] is True
    assert stats["partial_fetch_reasons"] == [FETCH_LIMIT_REACHED]
    assert [f["clipped"] for f in stats["fetch_limits"]] == [True, True]


def test_two_sources_50_plus_50_fit_exactly_without_clip_or_refusal(factory, bound, monkeypatch):
    _use(monkeypatch, MultiSourceConnector([_source(50, "a"), _source(50, "b")]))
    with factory() as s:
        result = pipeline._run(s, _request_id())
        stats = dict(s.get(ScoutRequest, _request_id()).stats)
        s.rollback()
    assert result["scanned"] == _LIMIT
    assert stats["partial_fetch"] is False
    assert "refused" not in stats
    assert [(f["source_key"], f["clipped"]) for f in stats["fetch_limits"]] == [
        ("replay:a", False),
        ("replay:b", False),
    ]


# --------------------------------------------------------------------------- #
# A failed source never looks like an empty one (review r3 MINOR-1)
# --------------------------------------------------------------------------- #
def _bytes_source(data: bytes, tag: str = "bad", **kw) -> RssNewsConnector:
    return RssNewsConnector(feed_provider=lambda _m: data, source_key=f"replay:{tag}", **kw)


def _raising_source(kind: FailureKind, tag: str, **kw) -> RssNewsConnector:
    def provider(_m):
        raise ConnectorFetchError(kind, "static detail")

    return RssNewsConnector(feed_provider=provider, source_key=f"replay:{tag}", **kw)


class _NoTokens(TokenBucket):
    def __init__(self) -> None:  # no clock, no capacity: always refuses
        pass

    def try_acquire(self, *_a, **_k) -> bool:
        return False


def _broken_after_120() -> bytes:
    good = _feed(120, "brk").decode()
    return good.replace("</channel></rss>", "<item><title>oops</channel></rss>").encode()


def _doctype() -> bytes:
    return b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "b">]><rss><channel></channel></rss>'


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (_bytes_source(b"<rss>" + b"x" * (rss_mod.MAX_FEED_BYTES + 1)), "feed_too_large"),
        (_bytes_source(_doctype()), "xml_unsafe"),
        (_bytes_source(_broken_after_120()), "xml_malformed"),
        (_bytes_source(b'<rss version="2.0"></rss>'), "xml_malformed"),
        (
            RssNewsConnector(
                feed_provider=lambda _m: _feed(5, "x"), rate_limiter=_NoTokens(), source_key="r"
            ),
            "rate_limited",
        ),
        (
            _raising_source(FailureKind.TIMEOUT, "slow", retry_policy=RetryPolicy(max_attempts=2)),
            "retries_exhausted",
        ),
        (_raising_source(FailureKind.NOT_CONFIGURED, "nc"), "fetch_error"),
    ],
    ids=[
        "feed-too-large",
        "doctype",
        "malformed-after-the-limit-120",
        "no-channel",
        "rate-limited",
        "retries-exhausted",
        "fetch-error",
    ],
)
def test_every_failure_branch_reports_a_failed_source_with_its_reason(source, reason):
    result = source.collect(FetchScope(market="Dallas, TX"))
    assert result.signals == []
    [report] = result.reports  # R6: the failure branch always reports
    assert report.failed is True and report.failure_reason == reason
    # A rejected document is a failed source: it is never "100 good entries processed".
    assert (report.fetched, report.eligible_seen, report.clipped_by_fetch_limit) == (0, 0, False)
    assert len(result.failures) == 1


def test_a_run_with_a_failed_source_is_partial_and_keeps_the_clip_fact(factory, bound, monkeypatch):
    _use(
        monkeypatch,
        MultiSourceConnector([_bytes_source(_broken_after_120(), "bad"), _source(150, "big")]),
    )
    with factory() as s:
        result = pipeline._run(s, _request_id())
        stats = dict(s.get(ScoutRequest, _request_id()).stats)
        s.rollback()
    assert result["scanned"] == stats["scanned"] == _LIMIT  # only the healthy source's 100
    assert stats["partial_fetch"] is True
    # Both facts are kept; neither collapses into the other.
    assert stats["partial_fetch_reasons"] == [SOURCE_FAILED, FETCH_LIMIT_REACHED]
    bad, big = stats["fetch_limits"]
    assert (bad["failed"], bad["failure_reason"], bad["clipped"]) == (True, "xml_malformed", False)
    assert (big["failed"], big["clipped"]) == (False, True)


def test_one_failed_source_alone_is_not_complete_coverage(factory, bound, monkeypatch):
    _use(monkeypatch, _bytes_source(_doctype()))
    with factory() as s:
        pipeline._run(s, _request_id())
        stats = dict(s.get(ScoutRequest, _request_id()).stats)
        s.rollback()
    assert stats["scanned"] == 0
    assert stats["partial_fetch"] is True and stats["partial_fetch_reasons"] == [SOURCE_FAILED]
    assert stats["fetch_limits"][0]["failure_reason"] == "xml_unsafe"


def test_a_refusal_also_shows_a_failed_source(factory, bound, monkeypatch):
    members = [_bytes_source(_doctype(), "bad"), _source(60, "a"), _source(60, "b")]
    _exc, stats = _refuse(factory, MultiSourceConnector(members), monkeypatch)
    assert stats["refusal_reason"] == SIGNAL_VOLUME_ABOVE_BOUND
    assert stats["partial_fetch"] is True and stats["partial_fetch_reasons"] == [SOURCE_FAILED]
    assert [f["failed"] for f in stats["fetch_limits"]] == [True, False, False]


def test_partial_fetch_facts_never_collapse_the_two_reasons():
    assert partial_fetch_facts([{"clipped": False, "failed": False}]) == {"partial_fetch": False}
    assert partial_fetch_facts([{"failed": True}]) == {
        "partial_fetch": True,
        "partial_fetch_reasons": [SOURCE_FAILED],
    }
    assert partial_fetch_facts([{"clipped": True}, {"failed": True}])["partial_fetch_reasons"] == [
        SOURCE_FAILED,
        FETCH_LIMIT_REACHED,
    ]
