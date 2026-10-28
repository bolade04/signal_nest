"""FU-268-2: the per-source fetch limit and the per-run bound on #255's live multi-source path.

#255's :class:`~app.connectors.live.LiveRssConnector` reads every approved (here: replayed)
source of a market in one run and extends their signals into one list. #268's boundary
behaviour holds on that path, end to end through ``fetch_signals`` and ``pipeline._run``:

* **fetch limit first, per source** - each source contributes at most ``max_items`` (100)
  entries; the parser extracts at most one entry past it on every read path (fresh
  response, cache, cool-down, 304), so a source that held more is reported ``clipped``;
* **every source reports** - read (``fetched`` / ``eligible_seen`` / ``clipped``) or failed
  (``failed`` + a closed ``failure_reason``): a failed source is never an empty one; the
  run's stats carry ``fetch_limits`` and ``partial_fetch`` / ``partial_fetch_reasons`` next
  to #255's ``coverage`` (``sources`` / ``sources_failed``);
* **the takedown re-check second** - signals of a source taken down while the sources
  were read are dropped (P6-PRIV-4);
* **the bound last, per run** - N_max = 100 counts what would actually be indexed, after
  the takedown re-check; above it the run is refused (``signal_volume_above_bound``) with
  every source's facts, before anything is stored, embedded or sent to a model - so a
  refused run makes no provider call, no budget reservation and no usage row (#269).

Test doubles, named as such (nothing here reaches a third party):

* **loopback replay origin** - :class:`app.connectors.replay.ReplayServer` serving synthetic
  corpora written by this module on ``127.0.0.1``, the double for publishers that #255's
  replay smoke uses. ``APPROVED_SOURCES`` stays empty; live egress is enabled only inside a
  test, through ``CONNECTOR_RSS_REPLAY_ORIGIN`` (refused by settings outside
  development/test);
* **scripted wire** - ``httpx.MockTransport`` answers for the connector-level failure
  branches (no socket, no DNS; the resolver is a fixed function);
* **fake provider** - an ``AnthropicProvider`` over ``httpx.MockTransport`` with a fixed
  reply and a placeholder key, counting every call (no network, no credential);
* **takedown snapshot** - ``pipeline.active_takedowns`` replaced by the snapshot a run took
  before an operator's takedown committed.
"""

from __future__ import annotations

import functools
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.brands.models import Brand
from app.connectors import live as live_mod
from app.connectors import replay as replay_mod
from app.connectors.base import FetchScope
from app.connectors.coverage import CoverageReason
from app.connectors.egress import FetchLimits, SafeFeedFetcher
from app.connectors.feedcache import FeedCache, feed_cache
from app.connectors.fetch_limits import FETCH_LIMIT_REACHED, SOURCE_FAILED
from app.connectors.live import LiveRssConnector
from app.connectors.models import SourceTakedown
from app.connectors.replay import ReplayServer
from app.core.config import Settings, get_settings
from app.core.enums import ScoutRequestStatus
from app.db import seed as seed_mod
from app.db.models import Base
from app.infra.vector import SIGNAL_VOLUME_ABOVE_BOUND, SignalVolumeAboveBoundError
from app.jobs import handlers, pipeline
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.service import enqueue_scout_request
from app.jobs.status import JobErrorCode, JobStatus
from app.jobs.store import DurableJobStore
from app.jobs.worker import JobRunner
from app.llm import budget as ledger
from app.llm.metering import LLMMeter
from app.llm.models import LLMBudgetReservation, LLMUsageEvent
from app.llm.providers_real import AnthropicProvider
from app.llm.service import LLMService
from app.locations.models import BusinessLocation

# Importing the app registers every ORM model + job handler on the shared Base/registry.
from app.main import app  # noqa: F401
from app.organizations.models import Workspace
from app.scouting_requests.connectors import fetch_signals
from app.scouting_requests.models import ScoutRequest
from app.signals.models import RawSignal
from app.tests._llm_offline import offline_settings
from app.tests._seed_support import clear_seeded_runs
from app.tests.test_live_rss_connector import Clock, Wire, connector, feed_response, record, target

_LIMIT = 100
MARKET = "Toronto, ON"
SCOPE = FetchScope(market=MARKET)


def _feed(count: int, tag: str, *, broken_after: int | None = None, word: str = "coffee") -> bytes:
    """A synthetic RSS document of ``count`` distinct entries mentioning ``word``."""
    items = "".join(
        f"<item><title>Specialty {word} {tag} story {i}</title>"
        f"<link>https://feeds.example.org/{tag}/{i}</link>"
        f"<description>{word} {tag}{i}a {tag}{i}b {tag}{i}c unique{tag}{i}</description>"
        f"<guid>https://feeds.example.org/{tag}/{i}</guid>"
        "<pubDate>Mon, 07 Jul 2025 09:00:00 +0000</pubDate></item>"
        for i in range(count)
    )
    tail = "</channel></rss>"
    if broken_after is not None:  # malformed beyond the entries: the document is rejected whole
        tail = "<item><title>broken</channel></rss>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>Synthetic {tag}</title><link>https://feeds.example.org/{tag}</link>"
        f"<description>synthetic</description>{items}{tail}"
    ).encode()


# --------------------------------------------------------------------------- #
# The live connector (scripted wire: no socket, no DNS)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("count", "clipped"), [(0, False), (99, False), (100, False), (101, True), (150, True)]
)
def test_a_live_source_reports_a_clip_exactly_when_it_held_more_than_the_limit(count, clipped):
    src = record("clip-src")
    wire = Wire({src.host: [feed_response(_feed(count, "c"))]})
    result = connector([target(src)], wire, Clock()).collect(SCOPE)
    [report] = result.reports
    assert (report.source_key, report.max_items, report.clipped_by_fetch_limit) == (
        "clip-src",
        _LIMIT,
        clipped,
    )
    assert report.fetched == min(count, _LIMIT) == report.eligible_seen == len(result.signals)
    assert (report.failed, report.failure_reason) == (False, None)


def test_the_live_look_ahead_is_bounded_on_every_read_path(monkeypatch):
    """Fresh response, politeness-interval cache, 304 revalidation and the cool-down cache:
    each read extracts at most ``max_items + 1`` entries of a 500-entry feed."""
    seen: list[tuple[int | None, int]] = []
    real = live_mod.parse_feed

    def spy(data, **kw):
        out = real(data, **kw)
        seen.append((kw.get("max_entries"), len(out.items)))
        return out

    monkeypatch.setattr(live_mod, "parse_feed", spy)
    src, clock = record("ahead-src"), Clock()
    wire = Wire(
        {
            src.host: [
                feed_response(_feed(500, "h"), etag='"e1"'),
                httpx.Response(304, headers={"etag": '"e1"'}),
                httpx.Response(429, headers={"retry-after": "600"}),
            ]
        }
    )
    conn = connector([target(src)], wire, clock)
    assert conn.collect(SCOPE).reports[0].clipped_by_fetch_limit  # fresh response
    clock.t += 10
    assert conn.collect(SCOPE).reports[0].clipped_by_fetch_limit  # served from the cache
    clock.t += 1000
    assert conn.collect(SCOPE).reports[0].clipped_by_fetch_limit  # 304: the held body
    clock.t += 1000
    limited = conn.collect(SCOPE)  # a 429 starts a cool-down: this run's read fails
    assert limited.reports[0].failure_reason == "rate_limited"
    clock.t += 10
    assert conn.collect(SCOPE).reports[0].clipped_by_fetch_limit  # cooling: the held body
    assert len(wire.requests) == 3
    assert seen == [(_LIMIT + 1, _LIMIT + 1)] * 4
    seen.clear()
    clock.t += 10
    conn.collect(FetchScope(market=MARKET, max_items=7))
    assert seen == [(8, 8)]


def _fetcher(wire: Wire, *, resolver=None, limits: FetchLimits | None = None) -> SafeFeedFetcher:
    return SafeFeedFetcher(
        user_agent="SignalNestBot/test (+https://operator.test/bot)",
        resolver=resolver or (lambda host, port: ["93.184.216.34"]),
        transport=httpx.MockTransport(wire),
        limits=limits or FetchLimits(),
    )


def _live(targets, wire: Wire, **fetcher_kw) -> LiveRssConnector:
    return LiveRssConnector(
        targets,
        fetcher=_fetcher(wire, **fetcher_kw),
        cache=FeedCache(clock=Clock()),
        sleep=lambda s: None,
        max_attempts=2,
    )


_DOCTYPE = b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "b">]><rss><channel></channel></rss>'


@pytest.mark.parametrize(
    ("answers", "fetcher_kw", "reason"),
    [
        ([httpx.Response(404)], {}, "fetch_error"),
        ([httpx.Response(429, headers={"retry-after": "60"})], {}, "rate_limited"),
        ([httpx.Response(503), httpx.Response(503)], {}, "retries_exhausted"),
        ([feed_response(_DOCTYPE)], {}, "xml_unsafe"),
        ([feed_response(_feed(120, "m", broken_after=120))], {}, "xml_malformed"),
        ([feed_response(b'<rss version="2.0"></rss>')], {}, "xml_malformed"),
        (
            [httpx.Response(200, content=_feed(5, "t"), headers={"content-type": "text/html"})],
            {},
            "response_refused",
        ),
        (
            [
                httpx.Response(
                    200,
                    content=iter([b"\x1f\x8b not really gzip"]),
                    headers={"content-type": "application/rss+xml", "content-encoding": "gzip"},
                )
            ],
            {},
            "response_refused",
        ),
        (
            [feed_response(_feed(200, "big"))],
            {"limits": FetchLimits(max_response_bytes=4096)},
            "feed_too_large",
        ),
        ([], {"resolver": lambda host, port: ["10.0.0.7"]}, "egress_blocked"),
    ],
    ids=[
        "http-404",
        "http-429",
        "upstream-503-twice",
        "doctype",
        "malformed-after-120-entries",
        "no-channel",
        "media-type",
        "content-encoding",
        "egress-byte-cap",
        "private-address",
    ],
)
def test_every_live_failure_branch_reports_a_failed_source_with_its_reason(
    answers, fetcher_kw, reason
):
    src = record("fail-src")
    conn = _live([target(src)], Wire({src.host: list(answers)}), **fetcher_kw)
    result = conn.collect(SCOPE)
    assert result.signals == []
    [report] = result.reports  # a failure always reports
    assert report.source_key == "fail-src"
    assert (report.failed, report.failure_reason) == (True, reason)
    # A rejected document is a failed source - never "an empty source", never 100 good
    # entries read before a malformation past the limit.
    assert (report.fetched, report.eligible_seen, report.clipped_by_fetch_limit) == (0, 0, False)
    assert conn.failed_sources == ["fail-src"] and len(result.failures) == 1


def test_a_failed_and_a_clipped_live_source_both_report_in_target_order():
    bad, big, small = record("src-bad"), record("src-big"), record("src-small")
    wire = Wire(
        {
            bad.host: [httpx.Response(404)],
            big.host: [feed_response(_feed(150, "g"))],
            small.host: [feed_response(_feed(10, "s"))],
        }
    )
    result = _live([target(bad), target(big), target(small)], wire).collect(SCOPE)
    assert [
        (r.source_key, r.fetched, r.clipped_by_fetch_limit, r.failed) for r in result.reports
    ] == [
        ("src-bad", 0, False, True),
        ("src-big", 100, True, False),
        ("src-small", 10, False, False),
    ]
    assert len(result.signals) == 110


# --------------------------------------------------------------------------- #
# The real path: replay origin -> live connector -> fetch_signals -> pipeline._run
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("live_fetch_boundary")
    engine = create_engine(
        f"sqlite:///{tmp / 'live.db'}", connect_args={"check_same_thread": False}, future=True
    )
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    original = seed_mod.SessionLocal
    seed_mod.SessionLocal = make
    try:
        seed_mod.seed(reset=True)
        clear_seeded_runs(make)
        yield make
    finally:
        seed_mod.SessionLocal = original
        engine.dispose()


def _toronto_request(make, *, keywords=("coffee",), workspace_id: str | None = None) -> str:
    """A Toronto scout request (own location) in the demo workspace or ``workspace_id``."""
    with make() as s:
        demo = s.get(ScoutRequest, seed_mod.sid("scout", "dallas"))
        org_id = demo.organization_id
        if workspace_id is None:
            ws_id, brand_id = demo.workspace_id, demo.brand_id
        else:
            ws_id = workspace_id
            brand_id = s.scalar(select(Brand.id).where(Brand.workspace_id == ws_id))
        loc = BusinessLocation(
            organization_id=org_id,
            workspace_id=ws_id,
            brand_id=brand_id,
            name="Replay - Toronto",
            city="Toronto",
            state_province="ON",
            is_active=True,
        )
        s.add(loc)
        s.flush()
        req = ScoutRequest(
            organization_id=org_id,
            workspace_id=ws_id,
            brand_id=brand_id,
            location_id=loc.id,
            name="Toronto live boundary",
            status=ScoutRequestStatus.QUEUED.value,
            source_types=[],
            keywords=list(keywords),
            resolved_market=MARKET,
        )
        s.add(req)
        s.commit()
        return req.id


def _second_workspace(make, slug: str) -> str:
    with make() as s:
        demo = s.get(ScoutRequest, seed_mod.sid("scout", "dallas"))
        ws = Workspace(organization_id=demo.organization_id, name=slug, slug=slug)
        s.add(ws)
        s.flush()
        s.add(Brand(organization_id=demo.organization_id, workspace_id=ws.id, name=f"B {slug}"))
        s.commit()
        return ws.id


def _write_corpus(directory: Path, feeds: dict[str, bytes]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    captures = []
    for key, body in feeds.items():
        (directory / f"{key}.xml").write_bytes(body)
        captures.append(
            {
                "capture_id": key,
                "source_key": key,
                "display_name": f"{key} (synthetic)",
                "publisher": "Synthetic test publisher (not a real publisher)",
                "feed_url": f"https://feeds.example.org/{key}.rss",
                "markets": [MARKET],
                "tos_url": "https://feeds.example.org/terms",
                "file": f"{key}.xml",
                "content_type": "application/rss+xml; charset=utf-8",
                "etag": None,
                "last_modified": None,
                "sha256": hashlib.sha256(body).hexdigest(),
                "synthetic": True,
                "captured_at": None,
                "keywords": ["coffee"],
            }
        )
    (directory / "manifest.json").write_text(json.dumps({"captures": captures}))


@contextmanager
def _replayed(monkeypatch, tmp_path: Path, feeds: dict[str, bytes]) -> Iterator[ReplayServer]:
    """Serve ``feeds`` from the loopback replay origin (the publisher double) and turn live
    egress on for this test only, exactly as #255's replay smoke does."""
    corpus = tmp_path / "corpus"
    _write_corpus(corpus, feeds)
    monkeypatch.setattr(replay_mod, "CORPUS_DIR", corpus)
    feed_cache.clear()
    with ReplayServer(corpus) as server:
        monkeypatch.setenv("CONNECTOR_RSS_REPLAY_ORIGIN", server.origin)
        monkeypatch.setenv("CONNECTOR_RSS_LIVE_ENABLED", "true")
        monkeypatch.setenv("CONNECTOR_RSS_ENABLED", "true")  # the workspace's CAP-1 decision
        get_settings.cache_clear()
        try:
            yield server
        finally:
            get_settings.cache_clear()
            feed_cache.clear()


def _rows(s, request_id: str) -> int:
    return s.scalar(
        select(func.count()).select_from(RawSignal).where(RawSignal.scout_request_id == request_id)
    )


def _run(make, request_id: str) -> tuple[dict, dict, int]:
    """``pipeline._run`` in a rolled-back session: (result, stats, rows written)."""
    with make() as s:
        before = _rows(s, request_id)
        result = pipeline._run(s, request_id)
        stats = dict(s.get(ScoutRequest, request_id).stats)
        rows = _rows(s, request_id) - before
        s.rollback()
    return result, stats, rows


def _refuse(make, request_id: str, monkeypatch) -> tuple[SignalVolumeAboveBoundError, dict]:
    """Expect the refusal before any dedupe work; return it and the stats the durable
    handler's after-rollback writer records for it."""

    def _never(*_a, **_k):  # pragma: no cover - the assertion is that it is not reached
        raise AssertionError("the dedupe stage ran for a refused run")

    monkeypatch.setattr(pipeline, "build_index", _never)
    monkeypatch.setattr(pipeline, "embed_text", _never)
    with make() as s:
        before = _rows(s, request_id)
        with pytest.raises(SignalVolumeAboveBoundError) as exc:
            pipeline._run(s, request_id)
        s.rollback()
        assert _rows(s, request_id) == before  # nothing stored: no partial run
    with make() as s:
        request = s.get(ScoutRequest, request_id)
        context = ExecutionContext.for_scout_request(
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
            request_id=request.id,
        )
        handlers._record_refused_run(request.id, context, exc.value)(s)
        s.expire_all()
        request = s.get(ScoutRequest, request_id)
        assert request.status == ScoutRequestStatus.FAILED.value
        stats = dict(request.stats)
        s.rollback()
    return exc.value, stats


def _limits(stats: dict) -> list[tuple]:
    return [
        (
            f["source_key"],
            f["max_items"],
            f["fetched"],
            f["eligible_seen"],
            f["clipped"],
            f["failed"],
            f["failure_reason"],
        )
        for f in stats["fetch_limits"]
    ]


def test_one_live_source_of_150_is_clipped_scans_100_and_says_so(world, monkeypatch, tmp_path):
    rid = _toronto_request(world)
    with _replayed(monkeypatch, tmp_path, {"lfb-solo": _feed(150, "solo")}) as server:
        result, stats, rows = _run(world, rid)
        assert len(server.hits) == 1  # one source, read once, over loopback
    assert result["scanned"] == stats["scanned"] == rows == _LIMIT
    assert "refused" not in stats  # a clip is not a refusal: the run completed
    assert stats["partial_fetch"] is True
    assert stats["partial_fetch_reasons"] == [FETCH_LIMIT_REACHED]
    assert _limits(stats) == [("lfb-solo", _LIMIT, 100, 100, True, False, None)]
    assert stats["coverage"] == {
        "status": "covered",
        "reason": "live_sources",
        "is_simulated": True,
        "sources": 1,
        "sources_failed": 0,
    }
    assert result["fetch_limits"] == stats["fetch_limits"]  # the job's summary carries it


def test_two_live_sources_60_plus_60_are_refused_with_both_sources_facts(
    world, monkeypatch, tmp_path
):
    rid = _toronto_request(world)
    feeds = {"lfb-a60": _feed(60, "a60"), "lfb-b60": _feed(60, "b60")}
    with _replayed(monkeypatch, tmp_path, feeds):
        exc, stats = _refuse(world, rid, monkeypatch)
    assert (exc.scanned, exc.bound) == (120, _LIMIT)  # neither source clipped; the sum is
    assert stats == pipeline.refused_run_stats(
        scanned=120, bound=_LIMIT, fetch_limits=exc.fetch_limits
    )
    assert stats["refused"] is True and stats["refusal_reason"] == SIGNAL_VOLUME_ABOVE_BOUND
    assert stats["partial_fetch"] is False and "partial_fetch_reasons" not in stats
    assert _limits(stats) == [
        ("lfb-a60", _LIMIT, 60, 60, False, False, None),
        ("lfb-b60", _LIMIT, 60, 60, False, False, None),
    ]
    assert "coverage" not in stats  # coverage describes a completed run (P6-DATA-2)


def test_two_live_sources_50_plus_50_complete_at_the_bound_without_clip_or_refusal(
    world, monkeypatch, tmp_path
):
    rid = _toronto_request(world)
    feeds = {"lfb-a50": _feed(50, "a50"), "lfb-b50": _feed(50, "b50")}
    with _replayed(monkeypatch, tmp_path, feeds):
        result, stats, rows = _run(world, rid)
    assert result["scanned"] == rows == _LIMIT and "refused" not in stats
    assert stats["partial_fetch"] is False and "partial_fetch_reasons" not in stats
    assert [(f["source_key"], f["clipped"], f["failed"]) for f in stats["fetch_limits"]] == [
        ("lfb-a50", False, False),
        ("lfb-b50", False, False),
    ]
    assert (stats["coverage"]["sources"], stats["coverage"]["sources_failed"]) == (2, 0)


def test_a_failed_live_source_is_reported_failed_not_empty(world, monkeypatch, tmp_path):
    rid = _toronto_request(world)
    feeds = {
        "lfb-broken": _feed(120, "brk", broken_after=120),  # malformed past the limit
        "lfb-good": _feed(50, "good"),
    }
    with _replayed(monkeypatch, tmp_path, feeds):
        result, stats, rows = _run(world, rid)
    assert result["scanned"] == rows == 50  # only the healthy source's entries
    assert stats["partial_fetch"] is True and stats["partial_fetch_reasons"] == [SOURCE_FAILED]
    assert _limits(stats) == [
        ("lfb-broken", _LIMIT, 0, 0, False, True, "xml_malformed"),
        ("lfb-good", _LIMIT, 50, 50, False, False, None),
    ]
    assert stats["coverage"]["reason"] == "live_sources"  # one source was read
    assert (stats["coverage"]["sources"], stats["coverage"]["sources_failed"]) == (2, 1)


def test_an_empty_live_source_and_a_failed_one_are_told_apart(world, monkeypatch, tmp_path):
    rid = _toronto_request(world)
    with _replayed(monkeypatch, tmp_path, {"lfb-empty": _feed(0, "empty")}):
        _result, empty, _rows_ = _run(world, rid)
    with _replayed(monkeypatch, tmp_path / "x", {"lfb-fail": _feed(3, "f", broken_after=3)}):
        _result, failed, _rows_ = _run(world, rid)
    assert empty["scanned"] == failed["scanned"] == 0
    assert empty["partial_fetch"] is False and empty["coverage"]["reason"] == "live_sources"
    assert _limits(empty) == [("lfb-empty", _LIMIT, 0, 0, False, False, None)]
    assert failed["partial_fetch"] is True and failed["partial_fetch_reasons"] == [SOURCE_FAILED]
    assert failed["coverage"]["reason"] == CoverageReason.SOURCES_UNAVAILABLE.value
    assert _limits(failed) == [("lfb-fail", _LIMIT, 0, 0, False, True, "xml_malformed")]


def test_a_refusal_carries_a_failed_and_a_clipped_source(world, monkeypatch, tmp_path):
    rid = _toronto_request(world)
    feeds = {
        "lfb-r-bad": _feed(5, "rbad", broken_after=5),
        "lfb-r-one": _feed(1, "rone"),
        "lfb-r-big": _feed(150, "rbig"),
    }
    with _replayed(monkeypatch, tmp_path, feeds):
        exc, stats = _refuse(world, rid, monkeypatch)
    assert exc.scanned == 101  # 0 (failed) + 1 + 100 (clipped): fetch limits first
    assert stats["refusal_reason"] == SIGNAL_VOLUME_ABOVE_BOUND
    assert stats["partial_fetch"] is True
    assert stats["partial_fetch_reasons"] == [SOURCE_FAILED, FETCH_LIMIT_REACHED]
    assert _limits(stats) == [
        ("lfb-r-bad", _LIMIT, 0, 0, False, True, "xml_malformed"),
        ("lfb-r-one", _LIMIT, 1, 1, False, False, None),
        ("lfb-r-big", _LIMIT, 100, 100, True, False, None),
    ]


def _taken_down_while_reading(monkeypatch, make, key: str) -> None:
    """An operator's takedown of ``key`` committed after the run read its takedown snapshot
    (the double: ``active_takedowns`` returns the earlier, empty snapshot)."""
    with make() as s:
        s.add(SourceTakedown(source_key=key, purge_summary={}, repeat_purges=[]))
        s.commit()
    monkeypatch.setattr(pipeline, "active_takedowns", lambda _db: frozenset())


@pytest.mark.parametrize(
    ("per_source", "scanned", "reasons"),
    [(60, 60, None), (150, 100, [FETCH_LIMIT_REACHED])],
    ids=["120-eligible-to-60", "200-eligible-to-100"],
)
def test_the_bound_counts_after_the_takedown_recheck(
    world, monkeypatch, tmp_path, per_source, scanned, reasons
):
    rid = _toronto_request(world)
    kept, gone = f"lfb-kept-{per_source}", f"lfb-gone-{per_source}"
    feeds = {kept: _feed(per_source, f"k{per_source}"), gone: _feed(per_source, f"g{per_source}")}
    _taken_down_while_reading(monkeypatch, world, gone)
    with _replayed(monkeypatch, tmp_path, feeds) as server:
        result, stats, rows = _run(world, rid)
        assert len(server.hits) == 2  # both sources were read: the takedown came later
    # Counting before the re-check would see 120 (or 200) and refuse; the bound counts
    # what would actually be indexed.
    assert "refused" not in stats
    assert result["scanned"] == stats["scanned"] == rows == scanned
    with world() as s:
        assert (
            s.scalar(
                select(func.count()).select_from(RawSignal).where(RawSignal.source_key == gone)
            )
            == 0
        )
    assert stats.get("partial_fetch_reasons") == reasons
    assert [f["source_key"] for f in stats["fetch_limits"]] == [kept, gone]


def test_fetch_signals_carries_every_live_sources_report(world, monkeypatch, tmp_path):
    feeds = {"lfb-fs-big": _feed(150, "fsb"), "lfb-fs-bad": _feed(4, "fsx", broken_after=4)}
    with _replayed(monkeypatch, tmp_path, feeds):
        batch = fetch_signals(market=MARKET, keywords=["coffee"], source_types=[], rss_enabled=True)
    assert len(batch.signals) == 100
    assert (batch.coverage.sources, batch.coverage.sources_failed) == (2, 1)
    assert [(r.source_key, r.clipped_by_fetch_limit, r.failed) for r in batch.reports] == [
        ("lfb-fs-big", True, False),
        ("lfb-fs-bad", False, True),
    ]


# --------------------------------------------------------------------------- #
# Through the durable worker: refusal, tenant isolation and the #269 meter
# --------------------------------------------------------------------------- #
class _FakeProvider:
    """The fake provider double: Anthropic's adapter over ``httpx.MockTransport`` with a
    fixed classification reply and a placeholder key; every call is recorded."""

    MODEL = "claude-sonnet-4-6"

    def __init__(self) -> None:
        self.calls: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.content)
        body = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"signal_type": "complaint", "pain_point_dna": "speed_complaint"}
                    ),
                }
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1000, "output_tokens": 500},
        }
        return httpx.Response(200, json=body)

    def service(self) -> LLMService:
        settings = offline_settings(
            llm_provider="anthropic", llm_api_key="fixture-key", llm_model=self.MODEL
        )
        provider = AnthropicProvider(
            "fixture-key", self.MODEL, 5, client=httpx.Client(transport=httpx.MockTransport(self))
        )
        return LLMService(provider, sleep=lambda _s: None, settings=settings)

    def calls_mentioning(self, word: str) -> int:
        return sum(word.encode() in body for body in self.calls)


def _enqueue(make, request_id: str) -> str:
    with make() as s:
        request = s.get(ScoutRequest, request_id)
        job = enqueue_scout_request(
            s,
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
            scout_request_id=request.id,
            location_id=request.location_id,
            request_id=request.id,
        )
        s.commit()
        return job.id


def _drain(make) -> int:
    runner = JobRunner(
        settings=Settings(_env_file=None), session_factory=make, store=DurableJobStore()
    )
    ran = 0
    for _ in range(10):
        if not runner.poll_once(worker_id="w-live-bound"):
            return ran
        ran += 1
    raise AssertionError("worker did not reach quiescence")  # pragma: no cover


def _count(s, model, **where) -> int:
    q = select(func.count()).select_from(model)
    for name, value in where.items():
        q = q.where(getattr(model, name) == value)
    return s.scalar(q)


@pytest.fixture
def meters(monkeypatch):
    """Record every meter the durable handler builds (the real class, observed)."""
    built: list[LLMMeter] = []
    real = handlers.LLMMeter

    def build(*a, **k):
        meter = real(*a, **k)
        built.append(meter)
        return meter

    monkeypatch.setattr(handlers, "LLMMeter", build)
    return built


@pytest.fixture
def releases(monkeypatch):
    """Record every ``release_for_job`` the durable handler and its hook make."""
    seen: list[tuple[str, str]] = []
    real = handlers.release_for_job

    def spy(db, *, job_id, now, outcome):
        seen.append((job_id, outcome))
        return real(db, job_id=job_id, now=now, outcome=outcome)

    monkeypatch.setattr(handlers, "release_for_job", spy)
    return seen


def test_a_refused_live_run_is_isolated_and_spends_nothing_while_its_neighbour_runs(
    world, monkeypatch, tmp_path, meters, releases
):
    """Two workspaces, one worker loop, one replayed market: the request whose keywords
    match 60 + 60 entries is refused; the other workspace's request (15 + 15 matches)
    completes. The refused run made no provider call and left no reservation and no usage
    row; its neighbour's calls each left exactly one usage row (the counters are live)."""
    provider = _FakeProvider()
    monkeypatch.setattr(pipeline, "llm_service", provider.service())
    refused_id = _toronto_request(world, keywords=("coffee",))
    other_ws = _second_workspace(world, "live-bound-ws2")
    other_id = _toronto_request(world, keywords=("tea",), workspace_id=other_ws)
    feeds = {
        "lfb-w-a": _feed(60, "wa"),
        "lfb-w-b": _feed(60, "wb"),
        "lfb-w-tea": _feed(15, "wt", word="tea"),
        "lfb-w-tea2": _feed(15, "wt2", word="tea"),
    }
    with _replayed(monkeypatch, tmp_path, feeds):
        refused_job, other_job = _enqueue(world, refused_id), _enqueue(world, other_id)
        assert _drain(world) == 2

    with world() as s:
        job, other = s.get(Job, refused_job), s.get(Job, other_job)
        refused, neighbour = s.get(ScoutRequest, refused_id), s.get(ScoutRequest, other_id)
        # --- the refused run: non-retryable, visible, nothing written, nothing spent
        assert (job.status, job.attempt_count) == (JobStatus.FAILED.value, 1)
        assert job.last_error_code == JobErrorCode.NON_RETRYABLE.value
        assert job.last_error_summary == SIGNAL_VOLUME_ABOVE_BOUND
        assert job.result_summary is None
        assert refused.status == ScoutRequestStatus.FAILED.value
        assert refused.stats["refusal_reason"] == SIGNAL_VOLUME_ABOVE_BOUND
        assert refused.stats["scanned"] == 120
        assert [f["source_key"] for f in refused.stats["fetch_limits"]] == [
            "lfb-w-a",
            "lfb-w-b",
            "lfb-w-tea",
            "lfb-w-tea2",
        ]
        assert _rows(s, refused_id) == 0
        assert provider.calls_mentioning("coffee") == 0
        assert _count(s, LLMUsageEvent, job_id=refused_job) == 0
        assert _count(s, LLMBudgetReservation, job_id=refused_job) == 0
        # --- the neighbour: completed in the same loop, its own facts only
        assert other.status == JobStatus.SUCCEEDED.value
        assert neighbour.status == ScoutRequestStatus.COMPLETED.value
        assert neighbour.stats["scanned"] == 30 and "refused" not in neighbour.stats
        assert other.result_summary["scanned"] == 30
        # Every provider call left exactly one usage row, all of them the neighbour's: had
        # the refused run made a call, its usage would be recorded under its own job.
        assert provider.calls_mentioning("tea") > 0
        assert len(provider.calls) == _count(s, LLMUsageEvent, job_id=other_job)
    # --- #269 intact on the refusal path: the handler's meter was the run's meter (bound
    # to the refused request through metered_run) and attempted nothing; the terminal hook
    # still released the job's reservations; the neighbour released at attempt end.
    by_ws = {m.workspace_id: m for m in meters}
    with world() as s:
        refused_ws = s.get(ScoutRequest, refused_id).workspace_id
    assert by_ws[refused_ws].scout_request_id == refused_id
    assert by_ws[refused_ws].attempts == 0 and by_ws[refused_ws].spent == 0
    assert (refused_job, "terminal") in releases
    assert (refused_job, "attempt_end") not in releases
    assert (other_job, "attempt_end") in releases


def test_a_refused_live_run_reserves_nothing_on_the_shared_ledger(
    world, monkeypatch, tmp_path, meters, releases
):
    """The shared-ledger path (PostgreSQL's, forced here on SQLite as #269's tests do):
    every call would be reserved in its own transaction before it is made. A refused run
    reaches no call, so ``budget.reserve`` is never invoked and no reservation, usage or
    period row appears."""
    provider = _FakeProvider()
    monkeypatch.setattr(pipeline, "llm_service", provider.service())
    shared = functools.partial(handlers.LLMMeter, shared_ledger=True)
    monkeypatch.setattr(handlers, "LLMMeter", shared)
    reserved: list = []
    real_reserve = ledger.reserve
    monkeypatch.setattr(
        ledger, "reserve", lambda *a, **k: reserved.append(k) or real_reserve(*a, **k)
    )
    rid = _toronto_request(world)
    feeds = {"lfb-sh-a": _feed(60, "sha"), "lfb-sh-b": _feed(60, "shb")}
    with world() as s:
        reservations_before = _count(s, LLMBudgetReservation)
        usage_before = _count(s, LLMUsageEvent)
    with _replayed(monkeypatch, tmp_path, feeds):
        job_id = _enqueue(world, rid)
        assert _drain(world) == 1
    with world() as s:
        job = s.get(Job, job_id)
        assert (job.status, job.last_error_summary) == (
            JobStatus.FAILED.value,
            SIGNAL_VOLUME_ABOVE_BOUND,
        )
        assert _count(s, LLMBudgetReservation) == reservations_before
        assert _count(s, LLMUsageEvent) == usage_before
    assert reserved == [] and provider.calls == []
    [meter] = [m for m in meters if m.job_id == job_id]
    assert meter.shared_ledger is True and meter.attempts == 0
    assert (job_id, "terminal") in releases
