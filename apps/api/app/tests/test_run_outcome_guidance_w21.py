"""W21 FU-Q1-1 and FU-Q1-2 (PROPOSED): how a refused, partial or complete run is presented, and
the wait-expiry reason.

FU-Q1-1 (Q1 (a+), DECISION-PACK-closeout-02 Addendum v8 §C): ``ScoutRequestOut.run_outcome`` is
a typed lift of what the latest recorded run wrote to ``stats`` - what ran (the sources read; the
counts stay in ``stats`` under the ``RunStats`` keys), what did not (the refusal reason and bound,
clipped sources, failed sources) and one next action. Each outcome is produced here by the real
worker, handler and pipeline (replay feeds through the sandbox seam of
``test_fetch_limit_boundary``; no network) and read back through the real route.

FU-Q1-2: a failure to admit an external operation (``admission_unresolved``) records whether the
run waited (``result_summary.admission_wait``); only a wait that ended at the deadline is worded
as one that timed out. ``AdmissionUnresolved`` is raised on PostgreSQL only (the K = 1 ledger), so
here the waiting loop is driven with stubs and the worker's recording is driven through the
handler; ``test_scout_execution_controls_postgres`` pins the recorded value on PostgreSQL.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app.connectors import fetch_limits
from app.connectors import registry as registry_mod
from app.connectors.fetch_limits import SourceFetchReport, fetch_stats
from app.connectors.multi import MultiSourceConnector
from app.connectors.rss import RssNewsConnector
from app.core.config import get_settings
from app.db import seed as seed_mod
from app.infra.vector import SIGNAL_VOLUME_ABOVE_BOUND
from app.jobs import handlers
from app.jobs.admission import (
    ADMISSION_WAIT_NONE,
    ADMISSION_WAIT_UNTIL_DEADLINE,
    ADMISSION_WAIT_UNTIL_UNKNOWN_END,
    AdmissionUnresolved,
    RunAdmission,
)
from app.jobs.models import Job
from app.jobs.outcomes import RunOutcome, RunOutcomeReason, run_outcome
from app.jobs.pipeline import refused_run_stats
from app.scouting_requests.models import ScoutRequest
from app.scouting_requests.schemas import (
    PartialFetchReason,
    RunRefusalReason,
    ScoutRequestOut,
    SourceFailureReason,
    run_outcome_from_stats,
)
from app.tests import test_scout_pause_promise as promise
from app.tests._auth2_support import live_bearer
from app.tests._llm_offline import offline_llm
from app.tests.test_scout_pause_promise import _NOW, API, _queue, _runner

factory = promise.factory
flag_on = promise.flag_on
committed = promise.committed
client = promise.client

_BOUND = 100
EXECUTE = "scout_request.execute"

#: The web tests' fixture of these responses (``apps/web/src/test/fixtures``): each API test below
#: asserts that the real route returns exactly it, so the web renders real shapes (LANE-BRIEF §5).
_WEB_FIXTURE = (
    Path(__file__).resolve().parents[3] / "web" / "src" / "test" / "fixtures"
) / "scout-run-outcomes.json"
#: How many findings were new or seen again depends on the cards earlier runs left; the web
#: fixture keeps the captured split, and only the sum is pinned.
_SPLIT = ("opportunities_new", "opportunities_seen_again")


def _web_fixture() -> dict:
    return json.loads(_WEB_FIXTURE.read_text(encoding="utf-8"))


def _assert_is_the_web_fixture(body: dict, case: str) -> None:
    want = _web_fixture()["cases"][case]
    assert body["status"] == want["status"]
    assert body["run_outcome"] == want["run_outcome"]

    def unsplit(stats: dict) -> dict:
        return {k: v for k, v in stats.items() if k not in _SPLIT}

    assert unsplit(body["stats"]) == unsplit(want["stats"])
    if _SPLIT[0] in body["stats"]:
        assert sum(body["stats"][k] for k in _SPLIT) == body["stats"]["opportunities"]


def _rid() -> str:
    return seed_mod.sid("scout", "dallas")


def _ws() -> str:
    return seed_mod.sid("ws")


def _owner() -> dict:
    return {"Authorization": f"Bearer {live_bearer(seed_mod.sid('user'))}"}


def _poll(factory) -> None:
    with offline_llm():
        ran = _runner(factory, clock=lambda: _NOW + timedelta(minutes=1)).poll_once(
            worker_id="w-w21-q1"
        )
    assert ran is True


def _scout(client) -> dict:
    got = client.get(f"{API}/workspaces/{_ws()}/scout-requests/{_rid()}", headers=_owner())
    assert got.status_code == 200
    return got.json()


def _job(client, job_id: str) -> dict:
    got = client.get(f"{API}/workspaces/{_ws()}/jobs/{job_id}", headers=_owner())
    assert got.status_code == 200
    return got.json()


# --------------------------------------------------------------------------- #
# Replay sources (as ``test_fetch_limit_boundary``): no network, no approved source
# --------------------------------------------------------------------------- #
def _feed(count: int, tag: str) -> bytes:
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
    return RssNewsConnector(feed_provider=lambda _m: _feed(count, tag), source_key=f"replay-{tag}")


def _unsafe(tag: str) -> RssNewsConnector:
    doc = b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "b">]><rss><channel></channel></rss>'
    return RssNewsConnector(feed_provider=lambda _m: doc, source_key=f"replay-{tag}")


@pytest.fixture
def replay(factory, committed, monkeypatch):
    """Run the Dallas Scout on the given sandbox connector (the registry seam), at a bound of
    100, with the keywords and source types the replay feeds match; restored afterwards."""
    monkeypatch.setattr(get_settings(), "vector_inprocess_max_signals_per_run", _BOUND)
    with factory() as session:
        req = session.get(ScoutRequest, _rid())
        seeded = (list(req.source_types or []), list(req.keywords or []))
        req.source_types, req.keywords = ["rss_news"], ["coffee"]
        session.commit()

    def use(connector) -> str:
        monkeypatch.setattr(registry_mod, "resolve_connector", lambda **_kw: connector)
        job_id = _queue(factory, _rid())
        _poll(factory)
        return job_id

    yield use
    with factory() as session:
        req = session.get(ScoutRequest, _rid())
        req.source_types, req.keywords = seeded
        session.commit()


def _source_row(key: str, *, max_items, fetched: int, clipped=False, failed=False, reason=None):
    return {
        "source_key": key,
        "max_items": max_items,
        "fetched": fetched,
        "eligible_seen": fetched,
        "clipped": clipped,
        "failed": failed,
        "failure_reason": reason,
    }


# --------------------------------------------------------------------------- #
# FU-Q1-1: one API test per outcome, from a real run, read through the route
# --------------------------------------------------------------------------- #
def test_complete_run_reads_complete_with_its_source(factory, client):
    job_id = _queue(factory, _rid())
    _poll(factory)
    assert _job(client, job_id)["outcome"] == "completed"
    body = _scout(client)
    assert body["status"] == "completed"
    assert body["run_outcome"] == {
        "kind": "complete",
        "sources": [
            _source_row("fixture", max_items=None, fetched=body["stats"]["scanned"]),
        ],
        "partial_reasons": [],
        "refusal_reason": None,
        "bound": None,
        "next_action": None,
    }
    assert body["stats"]["scanned"] > 0  # what ran: the counts stay in stats (RunStats keys)
    _assert_is_the_web_fixture(body, "complete")


def test_clipped_run_is_partial_fetch_limit_reached_with_no_action(client, replay):
    job_id = replay(_source(150, "big"))
    assert _job(client, job_id)["outcome"] == "completed"  # a clip is not a failure
    body = _scout(client)
    assert body["status"] == "completed" and body["stats"]["scanned"] == 100
    assert body["run_outcome"] == {
        "kind": "partial",
        "sources": [_source_row("replay-big", max_items=100, fetched=100, clipped=True)],
        "partial_reasons": ["fetch_limit_reached"],
        "refusal_reason": None,
        "bound": None,
        "next_action": "no_action",
    }
    _assert_is_the_web_fixture(body, "partial_fetch_limit_reached")


def test_failed_source_is_partial_source_failed_run_again_later(client, replay):
    job_id = replay(MultiSourceConnector([_unsafe("bad"), _source(3, "ok")]))
    assert _job(client, job_id)["outcome"] == "completed"
    body = _scout(client)
    assert body["status"] == "completed" and body["stats"]["scanned"] == 3
    assert body["run_outcome"] == {
        "kind": "partial",
        "sources": [
            _source_row("replay-bad", max_items=100, fetched=0, failed=True, reason="xml_unsafe"),
            _source_row("replay-ok", max_items=100, fetched=3),
        ],
        "partial_reasons": ["source_failed"],
        "refusal_reason": None,
        "bound": None,
        "next_action": "run_again_later",
    }
    _assert_is_the_web_fixture(body, "partial_source_failed")


def test_refused_run_at_the_bound_says_why_and_to_narrow_the_scout(client, replay):
    job_id = replay(MultiSourceConnector([_source(60, "a"), _source(60, "b")]))
    job = _job(client, job_id)
    assert (job["outcome"], job["outcome_reason"]) == ("failed", "too_many_signals")
    body = _scout(client)
    assert body["status"] == "failed"
    assert body["stats"]["scanned"] == 120  # what ran: 120 items collected, none analysed
    assert body["run_outcome"] == {
        "kind": "refused",
        "sources": [
            _source_row("replay-a", max_items=100, fetched=60),
            _source_row("replay-b", max_items=100, fetched=60),
        ],
        "partial_reasons": [],
        "refusal_reason": "signal_volume_above_bound",
        "bound": _BOUND,
        "next_action": "narrow_request",
    }
    assert body["coverage"] is None  # a refused run did not complete (P6-DATA-2, unchanged)
    _assert_is_the_web_fixture(body, "refused_at_bound")


def test_the_list_carries_the_same_run_outcome_as_the_detail(client, replay):
    replay(_source(150, "big"))
    listed = client.get(f"{API}/workspaces/{_ws()}/scout-requests", headers=_owner())
    assert listed.status_code == 200
    [row] = [r for r in listed.json() if r["id"] == _rid()]
    assert row["run_outcome"] == _scout(client)["run_outcome"]
    assert row["run_outcome"]["kind"] == "partial"


# --------------------------------------------------------------------------- #
# FU-Q1-1: the lift over the writers' own output (every combination; never a guess)
# --------------------------------------------------------------------------- #
def _report(key, *, fetched=5, clipped=False, failed=False, reason=None) -> SourceFetchReport:
    return SourceFetchReport(
        source_key=key,
        max_items=100,
        fetched=0 if failed else fetched,
        eligible_seen=0 if failed else fetched,
        clipped_by_fetch_limit=clipped,
        failed=failed,
        failure_reason=reason,
    )


def _completed(*reports: SourceFetchReport) -> dict:
    return {
        "scanned": 5,
        "noise_filtered": 0,
        "signals_analyzed": 5,
        "opportunities": 1,
        **fetch_stats(list(reports)),
    }


def test_both_partial_reasons_are_kept_and_a_failed_source_decides_the_action():
    got = run_outcome_from_stats(
        _completed(_report("a", clipped=True), _report("b", failed=True, reason="rate_limited"))
    )
    assert got.kind == "partial"
    assert got.partial_reasons == ["source_failed", "fetch_limit_reached"]
    assert got.next_action == "run_again_later"
    assert [(s.source_key, s.clipped, s.failed, s.failure_reason) for s in got.sources] == [
        ("a", True, False, None),
        ("b", False, True, "rate_limited"),
    ]


def test_a_refusal_keeps_its_clipped_and_failed_sources_and_still_says_narrow():
    stats = refused_run_stats(
        scanned=200,
        bound=_BOUND,
        fetch_limits=fetch_stats(
            [
                _report("a", fetched=100, clipped=True),
                _report("b", failed=True, reason="xml_unsafe"),
            ]
        )["fetch_limits"],
    )
    got = run_outcome_from_stats(stats)
    assert (got.kind, got.refusal_reason, got.bound) == ("refused", SIGNAL_VOLUME_ABOVE_BOUND, 100)
    assert got.partial_reasons == ["source_failed", "fetch_limit_reached"]
    assert got.next_action == "narrow_request"


def test_a_refusal_recorded_before_per_source_facts_is_still_a_refusal():
    got = run_outcome_from_stats(refused_run_stats(scanned=140, bound=_BOUND))
    assert (got.kind, got.sources, got.partial_reasons, got.next_action) == (
        "refused",
        None,
        [],
        "narrow_request",
    )


def test_a_complete_run_with_no_source_for_the_market_lists_none():
    got = run_outcome_from_stats(_completed())
    assert (got.kind, got.sources, got.next_action) == ("complete", [], None)


@pytest.mark.parametrize(
    "stats",
    [
        {},  # never produced a result
        None,
        # A completed run recorded before #268: complete cannot be told from partial.
        {"scanned": 5, "noise_filtered": 0, "signals_analyzed": 5, "opportunities": 1},
        {**refused_run_stats(scanned=1, bound=1), "refusal_reason": "a future reason"},
        {"scanned": 5, "partial_fetch": True, "partial_fetch_reasons": ["a future reason"]},
        {"scanned": 5, "partial_fetch": False, "fetch_limits": [{"source_key": "a"}]},
    ],
    ids=["empty", "none", "pre-268", "unknown-refusal", "unknown-partial", "malformed-source"],
)
def test_stats_that_record_no_outcome_lift_to_none_never_a_guess(stats):
    assert run_outcome_from_stats(stats) is None


def test_an_unknown_failure_code_keeps_the_failed_source():
    stats = _completed(_report("a", failed=True, reason="xml_unsafe"))
    stats["fetch_limits"][0]["failure_reason"] = "a future code"
    [source] = run_outcome_from_stats(stats).sources
    assert (source.failed, source.failure_reason) == (True, None)


def test_the_closed_vocabularies_match_the_writers():
    """Every per-source failure code the connectors record is a ``SourceFailureReason``, every
    partial-fetch reason a ``PartialFetchReason``, and the refusal reason a ``RunRefusalReason``:
    a new code added to the writers without the contract fails here, not silently in the UI."""
    written = {
        value
        for name, value in vars(fetch_limits).items()
        if name.isupper() and isinstance(value, str)
    }
    partial = {fetch_limits.SOURCE_FAILED, fetch_limits.FETCH_LIMIT_REACHED}
    assert {r.value for r in PartialFetchReason} == partial
    assert {r.value for r in SourceFailureReason} == written - partial
    assert {r.value for r in RunRefusalReason} == {SIGNAL_VOLUME_ABOVE_BOUND}


def test_the_response_model_lifts_run_outcome_from_stats():
    out = ScoutRequestOut.model_validate(
        {
            "id": "r",
            "organization_id": "o",
            "workspace_id": "w",
            "brand_id": "b",
            "location_id": None,
            "campaign_id": None,
            "name": "n",
            "status": "completed",
            "source_types": [],
            "keywords": [],
            "product_profile_id": None,
            "resolved_market": None,
            "notes": None,
            "last_run_at": None,
            "stats": _completed(_report("a", clipped=True)),
            "created_at": _NOW,
            "updated_at": _NOW,
        }
    )
    assert out.run_outcome.kind == "partial"
    assert out.run_outcome.partial_reasons == ["fetch_limit_reached"]


# --------------------------------------------------------------------------- #
# FU-Q1-2: an immediate refusal is never a wait that timed out
# --------------------------------------------------------------------------- #
def _k_violation() -> IntegrityError:
    return IntegrityError(
        "INSERT INTO scout_admissions ...",
        {},
        Exception('duplicate key value violates unique constraint "uq_scout_admission_unresolved"'),
    )


def _blocked_admission(states: list[tuple[bool, bool]]) -> tuple[RunAdmission, list[float]]:
    """A PostgreSQL-path admission whose every insert is refused by K = 1, and whose blocking
    admission reads ``(ended, deadline_passed)`` from ``states`` in turn."""
    admission = RunAdmission(
        scout_request_id="r",
        organization_id="o",
        workspace_id="w",
        fence=0,
        session_factory=lambda: None,
        _dialect="postgresql",
    )
    reads = iter(states)

    def refuse(_work):
        raise _k_violation()

    admission._in_own_transaction = refuse
    admission._blocking = lambda _op: ("blocking-admission", *next(reads))
    return admission, []


@pytest.mark.parametrize(
    ("states", "sleeps", "wait"),
    [
        ([(True, False)], 0, ADMISSION_WAIT_NONE),  # an unknown end already recorded
        ([(False, True)], 0, ADMISSION_WAIT_NONE),  # the deadline had already passed
        ([(True, True)], 0, ADMISSION_WAIT_NONE),
        ([(False, False), (False, False), (False, True)], 2, ADMISSION_WAIT_UNTIL_DEADLINE),
        ([(False, False), (True, False)], 1, ADMISSION_WAIT_UNTIL_UNKNOWN_END),
    ],
    ids=["unknown-end", "overdue", "both", "waited-to-deadline", "waited-to-unknown-end"],
)
def test_the_admission_records_whether_it_waited(states, sleeps, wait):
    admission, slept = _blocked_admission(states)
    with pytest.raises(AdmissionUnresolved) as refused:
        admission.admit_external("model", sleep=slept.append)
    assert len(slept) == sleeps
    assert refused.value.wait == wait
    assert refused.value.blocking_id == "blocking-admission"


@pytest.mark.parametrize(
    ("wait", "reason", "case"),
    [
        (ADMISSION_WAIT_NONE, "earlier_operation_unresolved", "admission_refused_at_once"),
        (
            ADMISSION_WAIT_UNTIL_UNKNOWN_END,
            "earlier_operation_unresolved",
            "admission_waited_until_unknown_end",
        ),
        (
            ADMISSION_WAIT_UNTIL_DEADLINE,
            "earlier_operation_timed_out",
            "admission_waited_until_deadline",
        ),
    ],
)
def test_the_failed_job_records_the_wait_and_the_api_words_only_a_deadline_as_timed_out(
    factory, client, monkeypatch, wait, reason, case
):
    def blocked(_db, _rid, _context=None):
        raise AdmissionUnresolved("blocking-admission", "model", wait)

    monkeypatch.setattr(handlers, "_run", blocked)
    job_id = _queue(factory, _rid())
    _poll(factory)
    job = _job(client, job_id)
    # Accepted (Addendum v8 §C) and unchanged: wait expiry is a failed run, NON_RETRYABLE,
    # ``admission_unresolved``, and the Scout becomes ``failed``.
    assert (job["status"], job["last_error_code"]) == ("failed", "non_retryable")
    with factory() as session:
        row = session.get(Job, job_id)
        assert row.last_error_summary == "admission_unresolved"
        assert row.result_summary == {"admission_wait": wait}
    assert (job["outcome"], job["outcome_reason"]) == ("failed", reason)
    assert job["result_summary"] == {"admission_wait": wait}  # a closed code, nothing else
    want = _web_fixture()["jobs"][case]
    assert {k: job[k] for k in want} == want
    runs = client.get(f"{API}/workspaces/{_ws()}/scout-requests/{_rid()}/runs", headers=_owner())
    assert runs.json()["items"][0]["outcome_reason"] == reason
    scout = _scout(client)
    assert scout["status"] == "failed"
    assert scout["execution"]["reason"] == "admission_unresolved"  # D-25 surface unchanged


@pytest.mark.parametrize(
    ("summary", "reason"),
    [
        ({"admission_wait": "until_deadline"}, RunOutcomeReason.EARLIER_OPERATION_TIMED_OUT),
        ({"admission_wait": "none"}, RunOutcomeReason.EARLIER_OPERATION_UNRESOLVED),
        ({"admission_wait": "until_unknown_end"}, RunOutcomeReason.EARLIER_OPERATION_UNRESOLVED),
        # Recorded before the wait was recorded: whether it waited is unknown, so no wait is said.
        (None, RunOutcomeReason.EARLIER_OPERATION_UNRESOLVED),
        ({}, RunOutcomeReason.EARLIER_OPERATION_UNRESOLVED),
        ({"admission_wait": "a future value"}, RunOutcomeReason.EARLIER_OPERATION_UNRESOLVED),
    ],
)
def test_only_a_recorded_wait_until_the_deadline_is_worded_as_timed_out(summary, reason):
    assert run_outcome(
        job_type=EXECUTE,
        status="failed",
        result_summary=summary,
        last_error_code="non_retryable",
        last_error_summary="admission_unresolved",
    ) == (RunOutcome.FAILED, reason)


def test_the_wait_record_never_changes_another_failure():
    assert run_outcome(
        job_type=EXECUTE,
        status="failed",
        result_summary={"admission_wait": "until_deadline"},
        last_error_code="non_retryable",
        last_error_summary="signal_volume_above_bound",
    ) == (RunOutcome.FAILED, RunOutcomeReason.TOO_MANY_SIGNALS)
