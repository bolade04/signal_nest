"""W21 FU-HELD-BODY-1 (PROPOSED): a copy held during a failure cool-down is never a complete read.

During a live source's cool-down after a failed fetch (#284: every failure class; on main before
it, a ``Retry-After`` hold-off and an unreadable body), the connector serves the copy it holds of
the source's last good read without contacting the source. Before this change such a run recorded
nothing about it: ``run_outcome`` read ``complete`` with no next step. Now:

* **recorded** - the source's report says ``read_mode: held``, when that copy was last read
  successfully (``last_read_at``), the failure that started the cool-down
  (``last_failure_reason``) and when the source may be contacted again (``next_retry_at``);
* **lifted** - such a run is ``partial`` (``source_held``), never ``complete``, and its next step
  is ``wait_for_source_retry`` with the retry time, not "run again later" - as it is for a failed
  source that is cooling down;
* **unchanged** - which body is served and when a source is contacted (#284's back-off tests in
  ``test_live_rss_connector`` pass unchanged); a fresh ``200``, a ``304`` and a read inside the
  politeness interval stay complete; a row recorded before these facts lifts as before, with the
  facts ``null`` (not recorded), never inferred.

Test doubles (nothing here reaches a third party): ``httpx.MockTransport`` answers with a fixed
resolver (no socket, no DNS), a fake monotonic clock in the feed cache, and - in the last test -
a scripted HTTP origin on ``127.0.0.1`` read through the real egress guard. The worker-level tests
run the real gate, worker, pipeline and route with ``APPROVED_SOURCES`` replaced by one test
record for the seeded Dallas Scout's market and the live switches turned on for the test only.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from app.connectors import live as live_mod
from app.connectors import replay as replay_mod
from app.connectors.base import FetchScope
from app.connectors.egress import SafeFeedFetcher
from app.connectors.feedcache import FeedCache, feed_cache
from app.connectors.fetch_limits import (
    SOURCE_FAILED,
    SOURCE_HELD,
    SourceFetchReport,
    SourceReadMode,
    fetch_stats,
)
from app.core.config import get_settings
from app.llm import service as service_module
from app.llm.mock import MockLLMProvider
from app.llm.service import LLMService
from app.scouting_requests.models import ScoutRequest
from app.scouting_requests.schemas import (
    PartialFetchReason,
    ScoutRunNextAction,
    ScoutRunOutcomeKind,
    run_outcome_from_stats,
)
from app.tests._llm_offline import offline_settings
from app.tests.test_live_fetch_boundary import _write_corpus
from app.tests.test_live_rss_connector import (
    NOW,
    RSS,
    SHIFT_JIS,
    Clock,
    Wire,
    connector,
    feed_response,
    record,
    target,
)
from app.tests.test_run_outcome_guidance_w21 import (
    _assert_is_the_web_fixture,
    _owner,
    _poll,
    _rid,
    _scout,
    _source,
    _ws,
    client,  # noqa: F401 - fixture
    committed,  # noqa: F401 - fixture
    factory,  # noqa: F401 - fixture
    flag_on,  # noqa: F401 - fixture
    replay,  # noqa: F401 - fixture
)
from app.tests.test_scout_pause_promise import _NOW, API, _queue, _runner

SCOPE = FetchScope(market="Toronto, ON", keywords=("coffee", "espresso"))
INTERVAL = 900.0
#: The demo-workspace Scout these worker tests run, and the market its live test source serves.
DALLAS = "Dallas, TX"


# --------------------------------------------------------------------------- #
# Every failure class that starts a cool-down (#284), and main's two before it
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FailureClass:
    #: The answers of one failing fetch (its in-fetch retries included, at ``max_attempts=3``).
    answers: tuple
    #: The failing run's closed ``failure_reason``.
    reason: str
    #: The closed reason a held copy reports for the cool-down (``last_failure_reason``).
    held_reason: str
    #: The cool-down's length after this first failure, seconds.
    hold: float


def _times(n: int, make) -> tuple:
    return tuple(make() for _ in range(n))


_REDIRECT_AWAY = {"location": "https://elsewhere.publisher.test/rss.xml"}
CLASSES: dict[str, FailureClass] = {
    # #284: every failure class now cools down (one interval for the first failure).
    "http-500": FailureClass(
        _times(3, lambda: httpx.Response(500)), "retries_exhausted", "retries_exhausted", 900
    ),
    "http-503": FailureClass(
        _times(3, lambda: httpx.Response(503)), "retries_exhausted", "retries_exhausted", 900
    ),
    "timeout": FailureClass(
        _times(3, lambda: httpx.ReadTimeout("slow source")),
        "retries_exhausted",
        "retries_exhausted",
        900,
    ),
    "connection-failed": FailureClass(
        _times(3, lambda: httpx.ConnectError("refused")),
        "retries_exhausted",
        "retries_exhausted",
        900,
    ),
    "http-404": FailureClass((httpx.Response(404),), "fetch_error", "fetch_error", 900),
    "http-403": FailureClass((httpx.Response(403),), "fetch_error", "fetch_error", 900),
    "http-429": FailureClass((httpx.Response(429),), "rate_limited", "rate_limited", 900),
    "not-a-feed": FailureClass(
        (httpx.Response(200, content=b"<html/>", headers={"content-type": "text/html"}),),
        "response_refused",
        "response_refused",
        900,
    ),
    "redirect-off-allow-list": FailureClass(
        (httpx.Response(302, headers=_REDIRECT_AWAY),), "egress_blocked", "egress_blocked", 900
    ),
    # On main before #284: a Retry-After hold-off and an unreadable body.
    "http-429-retry-after": FailureClass(
        (httpx.Response(429, headers={"retry-after": "1200"}),),
        "rate_limited",
        "rate_limited",
        1200,
    ),
    "http-503-retry-after": FailureClass(
        (httpx.Response(503, headers={"retry-after": "1200"}),),
        "retries_exhausted",
        "rate_limited",  # a Retry-After hold-off reports rate limiting (#284, unchanged)
        1200,
    ),
    "unreadable-body": FailureClass(
        (feed_response(SHIFT_JIS),), "xml_malformed", "xml_malformed", 900
    ),
}


def _counts(**stats) -> dict:
    """A completed run's stats around its fetch facts, as the pipeline writes them."""
    return {"scanned": 1, "noise_filtered": 0, "signals_analyzed": 1, "opportunities": 1, **stats}


# --------------------------------------------------------------------------- #
# 1. The connector records how each source was read (mocked transport)
# --------------------------------------------------------------------------- #
def test_a_fresh_read_a_304_and_a_reuse_are_recorded_and_stay_complete() -> None:
    src, clock = record(), Clock()
    cache = FeedCache(clock=clock)
    wire = Wire(
        {src.host: [feed_response(etag='"e1"'), httpx.Response(304, headers={"etag": '"e1"'})]}
    )
    conn = connector([target(src)], wire, clock, cache=cache)
    seen = []
    for step in (0, 60, 841):  # a 200; inside the interval, no request; after it, a 304
        clock.t += step
        [report] = conn.collect(SCOPE).reports
        retrieved = cache.cached(src.source_key).retrieved_at
        seen.append((report.read_mode, report.last_read_at == retrieved, report.next_retry_at))
        stats = _counts(**fetch_stats([report]))
        assert stats["partial_fetch"] is False
        got = run_outcome_from_stats(stats)
        assert (got.kind, got.next_action, got.next_retry_at) == ("complete", None, None)
    assert seen == [("fetched", True, None), ("reused", True, None), ("revalidated", True, None)]
    assert len(wire.requests) == 2


@pytest.mark.parametrize("name", list(CLASSES))
def test_a_held_copy_after_each_failure_class_is_recorded_held_and_lifts_partial(name) -> None:
    """good read -> the failure (recorded failed, cooling) -> a run inside the cool-down: the held
    copy is served with no request (#284, unchanged), and now recorded as such."""
    case = CLASSES[name]
    src, clock = record(), Clock()
    cache = FeedCache(clock=clock)
    wire = Wire({src.host: [feed_response(etag='"e1"'), *case.answers, feed_response()]})
    conn = connector([target(src)], wire, clock, cache=cache, max_attempts=3)
    good = conn.collect(SCOPE)
    read_at = cache.cached(src.source_key).retrieved_at
    assert good.reports[0].read_mode == "fetched"

    clock.t += INTERVAL + 1
    [failed] = conn.collect(SCOPE).reports
    assert (failed.failed, failed.failure_reason, failed.read_mode) == (True, case.reason, None)
    assert failed.next_retry_at == NOW + timedelta(seconds=case.hold)
    asked = len(wire.requests)
    assert asked == 1 + len(case.answers)

    clock.t += 60
    held = conn.collect(SCOPE)
    # The connector serves exactly what it served before this change: no request, no failure.
    assert len(wire.requests) == asked and held.failures == [] and conn.failed_sources == []
    assert [s.content for s in held.signals] == [s.content for s in good.signals]
    [report] = held.reports
    assert (report.failed, report.read_mode, report.last_failure_reason) == (
        False,
        "held",
        case.held_reason,
    )
    assert report.last_read_at == read_at  # the copy's own last good read, not this run
    assert report.next_retry_at == NOW + timedelta(seconds=case.hold - 60)
    assert report.fetched == good.reports[0].fetched

    stats = _counts(**fetch_stats([report]))
    assert (stats["partial_fetch"], stats["partial_fetch_reasons"]) == (True, [SOURCE_HELD])
    [entry] = stats["fetch_limits"]
    assert entry["read_mode"] == "held" and entry["last_failure_reason"] == case.held_reason
    assert entry["last_read_at"] == read_at.isoformat()
    assert entry["next_retry_at"] == (NOW + timedelta(seconds=case.hold - 60)).isoformat()
    got = run_outcome_from_stats(stats)
    assert got.kind is ScoutRunOutcomeKind.PARTIAL
    assert got.partial_reasons == [PartialFetchReason.SOURCE_HELD]
    assert got.next_action is ScoutRunNextAction.WAIT_FOR_SOURCE_RETRY
    assert got.next_retry_at == NOW + timedelta(seconds=case.hold - 60)
    assert got.sources[0].last_read_at == read_at

    # Once the cool-down has passed: one request, a fresh read, complete again.
    clock.t += case.hold - 60
    [after] = conn.collect(SCOPE).reports
    assert len(wire.requests) == asked + 1
    assert (after.read_mode, after.next_retry_at, after.last_failure_reason) == (
        "fetched",
        None,
        None,
    )
    assert run_outcome_from_stats(_counts(**fetch_stats([after]))).kind == "complete"


def test_a_failed_source_cooling_down_without_a_copy_says_when_it_is_retried() -> None:
    """No good read before the failure: nothing is held, the source is reported failed both in
    the run that met the failure and during the cool-down, with its retry time; the next step is
    to wait for it, not to run again at once."""
    src, clock = record(), Clock()
    wire = Wire({src.host: [httpx.Response(500)] * 3})
    conn = connector([target(src)], wire, clock, cache=FeedCache(clock=clock), max_attempts=3)
    for step, left in ((0, 900), (300, 600)):
        clock.t += step
        [report] = conn.collect(SCOPE).reports
        assert (report.failed, report.failure_reason) == (True, "retries_exhausted")
        assert report.next_retry_at == NOW + timedelta(seconds=left)
        got = run_outcome_from_stats(_counts(**fetch_stats([report])))
        assert (got.kind, got.partial_reasons) == ("partial", [PartialFetchReason.SOURCE_FAILED])
        assert got.next_action is ScoutRunNextAction.WAIT_FOR_SOURCE_RETRY
        assert got.next_retry_at == NOW + timedelta(seconds=left)
    assert len(wire.requests) == 3


def test_one_held_and_one_good_source_is_partial_and_waits_for_the_held_one() -> None:
    a, b = record("source-a"), record("source-b")
    clock = Clock()
    wire = Wire(
        {
            a.host: [feed_response(), httpx.Response(404)],
            b.host: [feed_response(), feed_response()],
        }
    )
    conn = connector([target(a), target(b)], wire, clock, cache=FeedCache(clock=clock))
    conn.collect(SCOPE)
    clock.t += INTERVAL + 1
    conn.collect(SCOPE)  # a: 404 (cooling), b: a fresh read
    clock.t += 60
    result = conn.collect(SCOPE)
    assert [(r.source_key, r.read_mode) for r in result.reports] == [
        ("source-a", "held"),
        ("source-b", "reused"),
    ]
    got = run_outcome_from_stats(_counts(**fetch_stats(result.reports)))
    assert (got.kind, got.partial_reasons) == ("partial", [PartialFetchReason.SOURCE_HELD])
    assert got.next_action is ScoutRunNextAction.WAIT_FOR_SOURCE_RETRY
    assert got.next_retry_at == NOW + timedelta(seconds=INTERVAL - 60)


def test_a_failure_recorded_inside_the_interval_marks_the_copy_held_not_reused() -> None:
    """Defensive: should a failure be recorded while the copy is still within its interval, the
    served copy is reported held (the last contact failed), never reused."""
    src, clock = record(), Clock()
    cache = FeedCache(clock=clock)
    wire = Wire({src.host: [feed_response()]})
    conn = connector([target(src)], wire, clock, cache=cache)
    conn.collect(SCOPE)
    cache.fetch_failed(src.source_key, INTERVAL)
    clock.t += 10
    [report] = conn.collect(SCOPE).reports
    assert (report.read_mode, report.next_retry_at) == ("held", NOW + timedelta(seconds=890))
    assert len(wire.requests) == 1


def test_retry_in_reads_the_cool_down_without_changing_it() -> None:
    clock = Clock()
    cache = FeedCache(clock=clock)
    assert cache.retry_in("k") is None
    cache.fetch_failed("k", INTERVAL)
    clock.t += 100
    assert cache.retry_in("k") == 800 and cache.retry_in("k") == 800
    assert cache.consecutive_failures("k") == 1 and cache.cooling("k")
    clock.t += 800
    assert cache.retry_in("k") is None and not cache.cooling("k")


# --------------------------------------------------------------------------- #
# 2. The lift: never complete for a held copy; older rows lift as before, facts unknown
# --------------------------------------------------------------------------- #
def _row(key="a", **facts) -> dict:
    return SourceFetchReport(key, 100, 5, 5, False, **facts).as_stats()


_T1 = datetime(2026, 11, 5, 9, 0, tzinfo=NOW.tzinfo)
_T2 = datetime(2026, 11, 5, 10, 0, tzinfo=NOW.tzinfo)


def test_an_older_row_without_read_facts_lifts_as_before_with_them_unknown() -> None:
    """A row written before this change (the shape #268 pinned): the same kind and next step as
    before, and every new fact ``null`` - not recorded - never a guessed ``fetched``."""
    old = {
        "source_key": "a",
        "max_items": 100,
        "clipped": False,
        "fetched": 5,
        "eligible_seen": 5,
        "failed": False,
        "failure_reason": None,
    }
    assert _row() == old  # a report without the facts still writes exactly that shape
    for partial, reasons, kind, action in (
        (False, None, "complete", None),
        (True, ["fetch_limit_reached"], "partial", "no_action"),
    ):
        stats = _counts(fetch_limits=[{**old, "clipped": partial}], partial_fetch=partial)
        if reasons:
            stats["partial_fetch_reasons"] = reasons
        got = run_outcome_from_stats(stats)
        assert (got.kind, got.next_action, got.next_retry_at) == (kind, action, None)
        [source] = got.sources
        assert (source.read_mode, source.last_read_at, source.next_retry_at) == (None, None, None)
        assert source.last_failure_reason is None
    failed = {**old, "fetched": 0, "eligible_seen": 0, "failed": True}
    failed["failure_reason"] = "fetch_error"
    got = run_outcome_from_stats(
        _counts(fetch_limits=[failed], partial_fetch=True, partial_fetch_reasons=[SOURCE_FAILED])
    )
    assert (got.next_action, got.next_retry_at) == ("run_again_later", None)  # as before


def test_a_held_copy_is_never_complete_even_without_its_times() -> None:
    """``held`` alone decides the kind; a missing retry time is stated as unknown (``null``), and
    the step is still to wait, never to run again at once."""
    stats = _counts(**fetch_stats([SourceFetchReport("a", 100, 5, 5, False, read_mode="held")]))
    got = run_outcome_from_stats(stats)
    assert (got.kind, got.partial_reasons) == ("partial", [PartialFetchReason.SOURCE_HELD])
    assert (got.next_action, got.next_retry_at) == ("wait_for_source_retry", None)
    assert (got.sources[0].last_read_at, got.sources[0].next_retry_at) == (None, None)


def test_the_wait_is_for_the_latest_retry_time_of_the_sources_not_read() -> None:
    stats = _counts(
        **fetch_stats(
            [
                SourceFetchReport("a", 100, 5, 5, False, read_mode="held", next_retry_at=_T1),
                SourceFetchReport(
                    "b", 100, 0, 0, False, failed=True, failure_reason="fetch_error",
                    next_retry_at=_T2,
                ),
                SourceFetchReport("c", 100, 5, 5, True, read_mode="fetched"),
            ]
        )
    )
    assert stats["partial_fetch_reasons"] == [SOURCE_FAILED, SOURCE_HELD, "fetch_limit_reached"]
    got = run_outcome_from_stats(stats)
    assert (got.next_action, got.next_retry_at) == ("wait_for_source_retry", _T2)


def test_a_refusal_with_a_held_source_still_says_to_narrow_the_scout() -> None:
    from app.jobs.pipeline import refused_run_stats

    limits = [_row(read_mode="held", next_retry_at=_T1, last_read_at=_T1)]
    got = run_outcome_from_stats(refused_run_stats(scanned=200, bound=100, fetch_limits=limits))
    assert (got.kind, got.next_action, got.next_retry_at) == ("refused", "narrow_request", None)
    assert got.partial_reasons == [PartialFetchReason.SOURCE_HELD]


@pytest.mark.parametrize(
    ("facts", "expect"),
    [
        ({"read_mode": "a future mode"}, (None, None, None)),
        ({"read_mode": ["held"]}, (None, None, None)),
        ({"last_read_at": "not a time"}, ("fetched", None, None)),
        ({"last_read_at": "2026-11-05T09:00:00"}, ("fetched", None, None)),  # no zone: unknown
        ({"next_retry_at": 12}, ("fetched", None, None)),
    ],
    ids=["unknown-mode", "malformed-mode", "bad-time", "naive-time", "non-string-time"],
)
def test_facts_this_api_cannot_read_are_unknown_never_guessed(facts, expect) -> None:
    entry = {**_row(read_mode="fetched"), **facts}
    got = run_outcome_from_stats(_counts(fetch_limits=[entry], partial_fetch=False))
    [source] = got.sources
    assert (source.read_mode, source.last_read_at, source.next_retry_at) == expect
    assert got.kind == "complete"


def test_the_read_modes_are_the_writers() -> None:
    assert {m.value for m in SourceReadMode} == {"fetched", "revalidated", "reused", "held"}
    assert SOURCE_HELD == PartialFetchReason.SOURCE_HELD == "source_held"


# --------------------------------------------------------------------------- #
# 3. Through the real gate, worker, pipeline and route (mocked transport)
# --------------------------------------------------------------------------- #
class LiveSource:
    """The seeded Dallas Scout read live from one approved test record over a scripted wire,
    with the process-wide feed cache replaced by one on a fake monotonic clock."""

    def __init__(self, monkeypatch, answers: list) -> None:
        self.record = record("live-dallas", markets=(DALLAS,))
        self.clock = Clock()
        self.cache = FeedCache(clock=self.clock)
        self.wire = Wire({self.record.host: answers})
        settings = get_settings()
        monkeypatch.setattr(settings, "connector_rss_enabled", True)  # the workspace's CAP-1
        monkeypatch.setattr(settings, "connector_rss_live_enabled", True)
        monkeypatch.setattr(settings, "connector_rss_replay_origin", None)
        monkeypatch.setattr(live_mod, "APPROVED_SOURCES", (self.record,))
        monkeypatch.setattr(live_mod, "feed_cache", self.cache)
        monkeypatch.setattr(live_mod, "build_fetcher", lambda _settings: self.fetcher())

    def fetcher(self) -> SafeFeedFetcher:
        return SafeFeedFetcher(
            user_agent="SignalNestBot/test (+https://operator.test/bot)",
            resolver=lambda host, port: ["93.184.216.34"],
            transport=httpx.MockTransport(self.wire),
        )


@pytest.fixture
def dallas_rss(factory):  # noqa: F811 - the imported fixture
    """The Dallas Scout reads RSS for "coffee" (as the replay fixture of #283's tests), restored
    afterwards."""
    with factory() as session:
        req = session.get(ScoutRequest, _rid())
        seeded = (list(req.source_types or []), list(req.keywords or []))
        req.source_types, req.keywords = ["rss_news"], ["coffee"]
        session.commit()
    yield
    with factory() as session:
        req = session.get(ScoutRequest, _rid())
        req.source_types, req.keywords = seeded
        session.commit()


def _run(factory, client) -> dict:  # noqa: F811
    _queue(factory, _rid())
    _poll(factory)
    return _scout(client)


def _at(value: str) -> datetime:
    """A response time; SQLite returns ``last_run_at`` without its zone, which is UTC."""
    at = datetime.fromisoformat(value)
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _near(a: datetime, b: datetime, seconds: float = 30) -> bool:
    return abs((a - b).total_seconds()) <= seconds


@pytest.mark.parametrize("name", list(CLASSES))
def test_each_failure_class_reads_truthfully_through_the_worker_and_the_route(
    name, factory, client, dallas_rss, monkeypatch  # noqa: F811
) -> None:
    case = CLASSES[name]
    live = LiveSource(monkeypatch, [feed_response(RSS, etag='"e1"'), *case.answers])

    first = _run(factory, client)
    assert first["coverage"]["reason"] == "live_sources"
    assert first["run_outcome"]["kind"] == "complete"
    [read] = first["run_outcome"]["sources"]
    assert (read["source_key"], read["read_mode"], read["next_retry_at"]) == (
        "live-dallas",
        "fetched",
        None,
    )
    assert read["last_read_at"] is not None

    live.clock.t += INTERVAL + 1
    second = _run(factory, client)
    asked = len(live.wire.requests)
    outcome = second["run_outcome"]
    assert (outcome["kind"], outcome["partial_reasons"]) == ("partial", ["source_failed"])
    assert outcome["next_action"] == "wait_for_source_retry"
    [failed] = outcome["sources"]
    assert (failed["failed"], failed["failure_reason"]) == (True, case.reason)
    ran_at = _at(second["last_run_at"])
    assert _near(_at(failed["next_retry_at"]), ran_at + timedelta(seconds=case.hold))
    assert outcome["next_retry_at"] == failed["next_retry_at"]

    live.clock.t += 60
    third = _run(factory, client)
    assert len(live.wire.requests) == asked  # the source was not contacted
    outcome = third["run_outcome"]
    # Never complete, never "read in full": the held copy is named, with its facts.
    assert outcome["kind"] == "partial" and outcome["partial_reasons"] == ["source_held"]
    assert outcome["next_action"] == "wait_for_source_retry"
    [held] = outcome["sources"]
    assert (held["failed"], held["read_mode"], held["last_failure_reason"]) == (
        False,
        "held",
        case.held_reason,
    )
    assert held["last_read_at"] == read["last_read_at"]  # the copy's own read, not this run
    assert _near(
        _at(held["next_retry_at"]), _at(third["last_run_at"]) + timedelta(seconds=case.hold - 60)
    )
    assert outcome["next_retry_at"] == held["next_retry_at"]
    # The recorded facts behind it, and what stays as #284 disclosed.
    [entry] = third["stats"]["fetch_limits"]
    assert (entry["read_mode"], entry["last_failure_reason"]) == ("held", case.held_reason)
    assert (third["stats"]["partial_fetch"], third["stats"]["partial_fetch_reasons"]) == (
        True,
        ["source_held"],
    )
    assert (third["coverage"]["status"], third["coverage"]["sources_failed"]) == ("covered", 0)
    if name == "http-500":
        _assert_is_the_web_fixture(second, "live_source_failed_cooling")
        _assert_is_the_web_fixture(third, "held_copy_after_failure")


def test_a_304_and_a_fresh_read_after_the_cool_down_read_complete_again(
    factory, client, dallas_rss, monkeypatch  # noqa: F811
) -> None:
    live = LiveSource(
        monkeypatch,
        [
            feed_response(RSS, etag='"e1"'),
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(304, headers={"etag": '"e1"'}),
            feed_response(RSS, etag='"e2"'),
        ],
    )
    _run(factory, client)
    live.clock.t += INTERVAL + 1
    _run(factory, client)  # the failure
    live.clock.t += 60
    assert _run(factory, client)["run_outcome"]["kind"] == "partial"  # held
    for mode, step in (("revalidated", INTERVAL), ("reused", 60), ("fetched", INTERVAL)):
        live.clock.t += step
        body = _run(factory, client)
        outcome = body["run_outcome"]
        assert (outcome["kind"], outcome["next_action"], outcome["next_retry_at"]) == (
            "complete",
            None,
            None,
        ), mode
        [source] = outcome["sources"]
        assert (source["read_mode"], source["next_retry_at"]) == (mode, None)
    assert len(live.wire.requests) == 6


def test_an_older_row_reads_as_before_through_the_route_with_its_read_facts_unknown(
    factory, client  # noqa: F811
) -> None:
    """A Scout whose stored stats predate these facts (the writer's own output with them removed,
    which is the shape the older writer wrote): the route lifts it as before - here ``complete`` -
    and states each source's read facts as ``null``, not recorded."""
    body = _run(factory, client)
    assert body["run_outcome"]["sources"][0]["read_mode"] == "fetched"
    older = dict(body["stats"])
    new_facts = ("read_mode", "last_read_at", "next_retry_at", "last_failure_reason")
    older["fetch_limits"] = [
        {k: v for k, v in entry.items() if k not in new_facts}
        for entry in body["stats"]["fetch_limits"]
    ]
    with factory() as session:
        session.get(ScoutRequest, _rid()).stats = older
        session.commit()
    got = _scout(client)
    assert got["run_outcome"]["kind"] == body["run_outcome"]["kind"] == "complete"
    assert got["run_outcome"]["next_action"] is None
    for source in got["run_outcome"]["sources"]:
        assert (source["read_mode"], source["last_read_at"], source["next_retry_at"]) == (
            None,
            None,
            None,
        )
    _assert_is_the_web_fixture(got, "older_record_without_read_facts")


# --------------------------------------------------------------------------- #
# 4. Over loopback HTTP through the real egress guard: good, then failing
# --------------------------------------------------------------------------- #
class _Origin(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, body: bytes) -> None:
        super().__init__(("127.0.0.1", 0), _OriginHandler)
        self.body = body
        self.failing = False
        self.hits: list[int] = []


@contextmanager
def _offline_except_origin(port: int) -> Iterator[None]:
    """As :func:`app.tests._llm_offline.offline_llm` (the deterministic mock model, httpx's real
    transport refused), except that a request to this test's own loopback origin is let through.
    Any other real request still fails the test."""
    real = httpx.HTTPTransport.handle_request

    def only_the_origin(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        if request.url.host == "127.0.0.1" and request.url.port == port:
            return real(self, request)
        raise AssertionError(f"real HTTP request outside the loopback origin: {request.url.host}")

    settings = offline_settings()
    provider = MockLLMProvider(model=settings.llm_model or "mock-1", seed=settings.llm_mock_seed)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx.HTTPTransport, "handle_request", only_the_origin)
        # The pipeline reads the service through get_llm_service() (#291): patch it where that
        # reads it, as offline_llm does.
        mp.setattr(service_module, "llm_service", LLMService(provider, settings=settings))
        yield


def _run_with_origin(factory, client, port: int) -> dict:  # noqa: F811
    _queue(factory, _rid())
    with _offline_except_origin(port):
        ran = _runner(factory, clock=lambda: _NOW + timedelta(minutes=1)).poll_once(
            worker_id="w-w21-heldbody"
        )
    assert ran is True
    return _scout(client)


class _OriginHandler(BaseHTTPRequestHandler):
    server: _Origin

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        status, body = (500, b"") if self.server.failing else (200, self.server.body)
        self.server.hits.append(status)
        self.send_response(status)
        self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


@contextmanager
def _loopback_live(monkeypatch, tmp_path: Path) -> Iterator[tuple[_Origin, int, Clock]]:
    """The Dallas Scout's one live source, replayed from a synthetic capture by a scripted origin
    on ``127.0.0.1`` and read through the real gate and egress guard, with the process-wide feed
    cache on a fake clock. Yields the origin, its port and the clock; stopped afterwards."""
    corpus = tmp_path / "corpus"
    _write_corpus(corpus, {"loop-dallas": RSS})
    manifest = corpus / "manifest.json"
    manifest.write_text(manifest.read_text().replace('"Toronto, ON"', f'"{DALLAS}"'))
    monkeypatch.setattr(replay_mod, "CORPUS_DIR", corpus)
    clock = Clock()
    monkeypatch.setattr(feed_cache, "_clock", clock)
    feed_cache.clear()
    origin = _Origin(RSS)
    thread = threading.Thread(target=origin.serve_forever, daemon=True)
    thread.start()
    port = origin.server_address[1]
    try:
        settings = get_settings()
        monkeypatch.setattr(settings, "connector_rss_enabled", True)
        monkeypatch.setattr(settings, "connector_rss_live_enabled", True)
        monkeypatch.setattr(settings, "connector_rss_replay_origin", f"http://127.0.0.1:{port}")
        yield origin, port, clock
    finally:
        origin.shutdown()
        origin.server_close()
        thread.join(timeout=5)
        feed_cache.clear()


def test_a_loopback_source_that_goes_good_then_failing_is_never_shown_complete(
    factory, client, dallas_rss, monkeypatch, tmp_path: Path  # noqa: F811
) -> None:
    """The real gate, the real egress guard over ``127.0.0.1``, the process-wide feed cache (on a
    fake clock), the worker and the route: a 200, then 500s; the run inside the cool-down is
    partial with the held copy named, and contacts nothing."""
    with _loopback_live(monkeypatch, tmp_path) as (origin, port, clock):
        first = _run_with_origin(factory, client, port)
        origin.failing = True
        clock.t += INTERVAL + 1
        second = _run_with_origin(factory, client, port)
        clock.t += 60
        third = _run_with_origin(factory, client, port)
        hits = list(origin.hits)
    assert hits == [200, 500, 500, 500]  # one good read; one failing fetch with its retries
    assert first["run_outcome"]["kind"] == "complete"
    assert first["run_outcome"]["sources"][0]["read_mode"] == "fetched"
    assert second["run_outcome"]["partial_reasons"] == ["source_failed"]
    held = third["run_outcome"]
    assert (held["kind"], held["partial_reasons"], held["next_action"]) == (
        "partial",
        ["source_held"],
        "wait_for_source_retry",
    )
    [source] = held["sources"]
    assert (source["source_key"], source["read_mode"], source["last_failure_reason"]) == (
        "loop-dallas",
        "held",
        "retries_exhausted",
    )
    assert source["last_read_at"] == first["run_outcome"]["sources"][0]["last_read_at"]
    assert held["next_retry_at"] is not None
    listed = client.get(f"{API}/workspaces/{_ws()}/scout-requests", headers=_owner()).json()
    [row] = [r for r in listed if r["id"] == _rid()]
    assert row["run_outcome"] == held  # the list carries the same outcome as the detail


# --------------------------------------------------------------------------- #
# 5. Older complete records state the source path they recorded (packet item 48, option B)
# --------------------------------------------------------------------------- #
#: The facts this change adds to a ``fetch_limits`` entry; a row written before it has none.
_NEW_FACTS = ("read_mode", "last_read_at", "next_retry_at", "last_failure_reason")


def _as_older(stats: dict) -> dict:
    """``stats`` as the writer before this change wrote them: the same keys, without the new
    per-source facts (#268's pinned shape)."""
    older = dict(stats)
    older["fetch_limits"] = [
        {k: v for k, v in entry.items() if k not in _NEW_FACTS} for entry in stats["fetch_limits"]
    ]
    return older


def _stored_then_read(factory, client, stats: dict) -> dict:  # noqa: F811
    """Store ``stats`` as the Scout's latest result and read it back through the route. Reading
    rewrites nothing: the stored row is unchanged afterwards."""
    with factory() as session:
        session.get(ScoutRequest, _rid()).stats = stats
        session.commit()
    body = _scout(client)
    with factory() as session:
        assert session.get(ScoutRequest, _rid()).stats == stats
    return body


def _facts_unknown(body: dict) -> None:
    for source in body["run_outcome"]["sources"]:
        assert all(source[k] is None for k in _NEW_FACTS), source


def test_an_older_sample_feed_row_states_its_simulated_path_through_the_route(
    factory, client, replay  # noqa: F811
) -> None:
    """The sample feed (``sandbox_feed``) reads its bundled content in every run and holds no copy:
    an older complete row that recorded it keeps its reason, which the page reads (option B)."""
    replay(_source(3, "ok"))
    fresh = _scout(client)
    assert fresh["run_outcome"]["kind"] == "complete"
    assert fresh["coverage"]["reason"] == "sandbox_feed"
    got = _stored_then_read(factory, client, _as_older(fresh["stats"]))
    assert got["run_outcome"]["kind"] == "complete" and got["run_outcome"]["next_action"] is None
    assert got["coverage"] == fresh["coverage"]
    _facts_unknown(got)
    _assert_is_the_web_fixture(got, "older_sandbox_record_without_read_facts")


def test_an_older_replayed_live_row_states_its_live_path_through_the_route(
    factory, client, dallas_rss, monkeypatch, tmp_path: Path  # noqa: F811
) -> None:
    """A run through the live connector, replaying a synthetic capture over loopback: its coverage
    says ``live_sources`` and ``is_simulated: true`` (review r2 guardrail 1: the flag is not the
    path). The fresh body records ``read_mode``; its older form records none, so the page must
    qualify it (option B)."""
    with _loopback_live(monkeypatch, tmp_path) as (origin, port, _clock):
        fresh = _run_with_origin(factory, client, port)
        assert list(origin.hits) == [200]
    assert fresh["run_outcome"]["kind"] == "complete"
    coverage = fresh["coverage"]
    assert (coverage["reason"], coverage["is_simulated"]) == ("live_sources", True)
    assert [s["read_mode"] for s in fresh["run_outcome"]["sources"]] == ["fetched"]
    _assert_is_the_web_fixture(fresh, "live_complete_fetched")
    got = _stored_then_read(factory, client, _as_older(fresh["stats"]))
    assert got["run_outcome"]["kind"] == "complete" and got["coverage"] == fresh["coverage"]
    _facts_unknown(got)
    _assert_is_the_web_fixture(got, "older_live_record_without_read_facts")


@pytest.mark.parametrize(
    ("case", "coverage"),
    [
        ("older_record_without_coverage", None),
        (
            "older_record_unreadable_coverage",
            {
                "status": "covered",
                "reason": "a future source path",
                "is_simulated": True,
                "sources": 0,
                "sources_failed": 0,
            },
        ),
    ],
    ids=["missing", "unreadable"],
)
def test_an_older_row_without_a_readable_coverage_lifts_it_as_null(
    factory, client, case, coverage  # noqa: F811
) -> None:
    """No writer since #255 omits ``coverage`` from a completed run, but a row without it, or with
    one this API cannot read (an unknown reason), lifts ``coverage: null`` - never a guessed path -
    and the run as before; the page then qualifies the summary (review r2 guardrail 2)."""
    body = _run(factory, client)
    older = _as_older(body["stats"])
    if coverage is None:
        del older["coverage"]
    else:
        older["coverage"] = coverage
    got = _stored_then_read(factory, client, older)
    assert got["coverage"] is None
    assert got["run_outcome"]["kind"] == "complete" and got["run_outcome"]["next_action"] is None
    _facts_unknown(got)
    _assert_is_the_web_fixture(got, case)
