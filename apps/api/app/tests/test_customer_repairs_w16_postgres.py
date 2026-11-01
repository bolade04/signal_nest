"""W16 customer-workflow repairs on PostgreSQL 16 (PROPOSED; contract W16 §A/§B/§I).

The harness of ``test_scout_pause_postgres`` (one Alembic-migrated template per module, one clone
per test, the real worker in a thread with its own connections, fake sources serving the demo
library, the mock model). Run with ``TEST_POSTGRES_URL`` set (CI: ``CI=true``).

* A: repeated runs through the worker keep one card per finding and record ``seen_again``
  sightings, charged exactly as the first run; a stopped run records no card and no sighting;
  two attempts writing the same finding make ONE card (the loser re-uses the winner); two
  attempts writing two findings in opposite input orders do not deadlock, because the write step
  sorts by key (contract §I.2: the unsorted mutant must deadlock or fail).
* B: an edit holds the Scout row, so a Run now waits for it and then runs the edited Scout; an
  edit after a committed Run now is refused (``scout_in_progress``).
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from sqlalchemy import func, select, text, update

from app.core.errors import ValidationDomainError
from app.jobs import pipeline
from app.jobs.models import Job
from app.jobs.service import enqueue_scout_request
from app.llm.models import LLMBudgetReservation, LLMUsageEvent
from app.opportunities.models import Opportunity, OpportunityObservation
from app.scouting_requests.models import ScoutRequest
from app.scouting_requests.routes import run_scout_request_endpoint, update_scout_request
from app.scouting_requests.schemas import ScoutRequestUpdate
from app.signals.models import NormalizedSignal, RawSignal, SignalCluster
from app.tests import test_scout_pause_postgres as harness
from app.tests._llm_offline import offline_llm
from app.tests.test_scout_pause_postgres import (
    JOIN_SECONDS,
    Background,
    Sources,
    _pause,
    _start_worker,
)

template_url = harness.template_url
world = harness.world
_postgres_required = harness._postgres_required


def _requeue(w) -> str:
    """Queue another run of the Scout, as ``POST .../run`` does."""
    with w.factory() as s:
        req = s.get(ScoutRequest, w.rid)
        req.status = "queued"
        job = enqueue_scout_request(
            s,
            organization_id=w.org,
            workspace_id=w.ws,
            scout_request_id=w.rid,
            location_id=req.location_id,
            request_id=w.rid,
            run_fence=req.run_fence,
            idempotency_key=f"w16-{uuid.uuid4().hex}",
        )
        s.commit()
        return job.id


def _run(w, monkeypatch, job_id: str | None = None) -> str:
    monkeypatch.setattr(pipeline, "fetch_signals", Sources())
    with offline_llm():
        assert _start_worker(w).join_ok() is True
    job_id = job_id or w.job_id
    with w.factory() as s:
        job = s.get(Job, job_id)
        assert job.status == "succeeded", (job.status, job.last_error_summary)
    return job_id


def _cards(w) -> list[Opportunity]:
    with w.factory() as s:
        return list(
            s.scalars(
                select(Opportunity)
                .where(Opportunity.scout_request_id == w.rid)
                .order_by(Opportunity.created_at, Opportunity.id)
            )
        )


def _sightings(w) -> list[OpportunityObservation]:
    with w.factory() as s:
        return list(
            s.scalars(
                select(OpportunityObservation).where(
                    OpportunityObservation.scout_request_id == w.rid
                )
            )
        )


def _accounting(w, job_id: str) -> tuple:
    with w.factory() as s:
        usage = s.execute(
            select(LLMUsageEvent.task, func.count(), func.sum(LLMUsageEvent.input_tokens))
            .where(LLMUsageEvent.job_id == job_id)
            .group_by(LLMUsageEvent.task)
            .order_by(LLMUsageEvent.task)
        ).all()
        reservations = s.execute(
            select(LLMBudgetReservation.state, func.count())
            .where(LLMBudgetReservation.job_id == job_id)
            .group_by(LLMBudgetReservation.state)
        ).all()
    return tuple(map(tuple, usage)), tuple(map(tuple, reservations))


# --------------------------------------------------------------------------- #
# A through the real worker
# --------------------------------------------------------------------------- #
def test_repeated_runs_keep_one_card_per_finding_and_charge_the_same(world, monkeypatch):
    w = world
    first = _run(w, monkeypatch)
    cards = _cards(w)
    assert cards and len({c.finding_key for c in cards}) == len(cards)
    with w.factory() as s:
        s.execute(update(Opportunity).where(Opportunity.id == cards[0].id).values(status="saved"))
        s.commit()
    second = _run(w, monkeypatch, _requeue(w))
    third = _run(w, monkeypatch, _requeue(w))
    again = _cards(w)
    assert [c.id for c in again] == [c.id for c in cards]
    assert {c.id: c.status for c in again}[cards[0].id] == "saved"
    kinds = sorted((o.job_id, o.kind) for o in _sightings(w))
    expected = sorted(
        [(first, "first_seen")] * len(cards)
        + [(second, "seen_again")] * len(cards)
        + [(third, "seen_again")] * len(cards)
    )
    assert kinds == expected
    assert _accounting(w, first) == _accounting(w, second) == _accounting(w, third)
    assert _accounting(w, first)[0], "the run made model calls"


def test_a_stopped_run_records_no_card_and_no_sighting(world, monkeypatch):
    w = world
    _run(w, monkeypatch)
    cards, sightings = _cards(w), _sightings(w)
    real_build = pipeline._build_opportunities

    def build_then_pause(*args, **kwargs):
        built = real_build(*args, **kwargs)
        _pause(w)  # commits: the run's transaction holds only FK key-share locks
        return built

    monkeypatch.setattr(pipeline, "_build_opportunities", build_then_pause)
    job = _run(w, monkeypatch, _requeue(w))
    with w.factory() as s:
        assert s.get(Job, job).result_summary["outcome"] == "stopped_request_paused"
    assert [c.id for c in _cards(w)] == [c.id for c in cards]
    assert len(_sightings(w)) == len(sightings)


# --------------------------------------------------------------------------- #
# A: the write step's races (two attempts of one Scout, each with its own run rows)
# --------------------------------------------------------------------------- #
def _finding(s, req: ScoutRequest, contents: list[str]) -> pipeline._Finding:
    """One finding as step 1 leaves it: this attempt's own signals and cluster, a transient card."""
    import hashlib

    cluster = SignalCluster(
        workspace_id=req.workspace_id, scout_request_id=req.id, label="race", size=len(contents)
    )
    s.add(cluster)
    s.flush()
    members = []
    for content in contents:
        raw = RawSignal(
            organization_id=req.organization_id,
            workspace_id=req.workspace_id,
            scout_request_id=req.id,
            source_type="reviews",
            source_url="https://example.com/simulated",
            content=content,
            raw_metadata={},
        )
        s.add(raw)
        s.flush()
        norm = NormalizedSignal(
            organization_id=req.organization_id,
            workspace_id=req.workspace_id,
            scout_request_id=req.id,
            raw_signal_id=raw.id,
            source_type="reviews",
            source_url="https://example.com/simulated",
            excerpt=content,
            content_hash=hashlib.sha256(content.strip().lower().encode()).hexdigest(),
            ingest_metadata={},
            cluster_id=cluster.id,
        )
        s.add(norm)
        members.append(norm)
    s.flush()
    card = Opportunity(
        organization_id=req.organization_id,
        workspace_id=req.workspace_id,
        brand_id=req.brand_id,
        scout_request_id=req.id,
        location_id=req.location_id,
        cluster_id=cluster.id,
        title=f"Race {contents[0][:20]}",
        classification="watch",
        decision="stay_silent",
    )
    from app.opportunities.identity import finding_key

    return pipeline._Finding(
        key=finding_key(members),
        cluster=cluster,
        members=members,
        card=card,
        scores=[],
        evidence=[],
    )


def _attempt(w, contents_in_order: list[list[str]], pids: list, results: dict, name: str):
    def go() -> None:
        with w.factory() as s:
            pids.append(s.execute(text("select pg_backend_pid()")).scalar_one())
            req = s.get(ScoutRequest, w.rid)
            findings = [_finding(s, req, c) for c in contents_in_order]
            built = pipeline._record_findings(s, req, findings)
            s.commit()
            results[name] = (built.new, built.seen_again)

    return Background(go)


def _interleave(monkeypatch, wait_seconds: float = 2.0) -> None:
    """Each attempt, after inserting its FIRST card, lets the other insert its first one."""
    real = pipeline._attach_card
    firsts: dict[int, threading.Event] = {}
    lock = threading.Lock()
    seen: set[int] = set()

    def attach(db, request, card, finding):
        real(db, request, card, finding)
        me = threading.get_ident()
        with lock:
            if me in seen:
                return
            seen.add(me)
            firsts.setdefault(me, threading.Event()).set()
            others = [e for t, e in firsts.items() if t != me]
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            with lock:
                others = [e for t, e in firsts.items() if t != me]
            if others and all(e.is_set() for e in others):
                return
            time.sleep(0.02)

    monkeypatch.setattr(pipeline, "_attach_card", attach)


def _settle(w) -> None:
    with w.factory() as s:  # the Scout settled; its seeded job is not under test here
        s.execute(update(Job).where(Job.id == w.job_id).values(status="cancelled"))
        s.execute(update(ScoutRequest).where(ScoutRequest.id == w.rid).values(status="completed"))
        s.commit()


def test_two_attempts_writing_the_same_finding_make_one_card(world, monkeypatch):
    w = world
    _settle(w)
    _interleave(monkeypatch)
    results: dict = {}
    pids: list = []
    one = _attempt(w, [["same evidence a", "same evidence b"]], pids, results, "one")
    two = _attempt(w, [["same evidence b", "same evidence a"]], pids, results, "two")
    one.start()
    two.start()
    one.join_ok()
    two.join_ok()
    cards = _cards(w)
    assert len(cards) == 1
    assert sorted(results.values()) == [(0, 1), (1, 0)]
    assert sorted(o.kind for o in _sightings(w)) == ["first_seen", "seen_again"]


def test_two_attempts_in_opposite_orders_do_not_deadlock(world, monkeypatch):
    """Contract §I.2: the write step sorts by key. Without the sort these two attempts each
    insert their first finding, then wait for the other's: PostgreSQL aborts one with 40P01
    (W16 Phase A raw/007 P5)."""
    w = world
    _settle(w)
    _interleave(monkeypatch)
    x, y = ["finding x item"], ["finding y item"]
    results: dict = {}
    pids: list = []
    one = _attempt(w, [x, y], pids, results, "one")
    two = _attempt(w, [y, x], pids, results, "two")
    started = time.monotonic()
    one.start()
    two.start()
    one.join_ok()
    two.join_ok()
    assert time.monotonic() - started < JOIN_SECONDS
    assert len(_cards(w)) == 2
    assert sorted(results.values()) == [(0, 2), (2, 0)]


# --------------------------------------------------------------------------- #
# B: the edit and Run now serialise on the Scout row
# --------------------------------------------------------------------------- #
def _waiting(w, pid: int) -> bool:
    with w.observer.connect() as conn:
        return (
            conn.execute(
                text("select wait_event_type from pg_stat_activity where pid = :p"), {"p": pid}
            ).scalar()
            == "Lock"
        )


def test_an_edit_holds_the_scout_so_run_now_waits_then_runs_the_edited_scout(world, monkeypatch):
    w = world
    _settle(w)
    edit = w.factory()
    try:
        update_scout_request(
            workspace_id=w.ws,
            request_id=w.rid,
            body=ScoutRequestUpdate(name="Edited before the run"),
            db=edit,
            ctx=w.ctx,
        )
        holder: dict = {}

        def run_now():
            with w.factory() as s:
                holder["pid"] = s.execute(text("select pg_backend_pid()")).scalar_one()
                return run_scout_request_endpoint(
                    workspace_id=w.ws, request_id=w.rid, db=s, ctx=w.ctx
                )

        runner = Background(run_now)
        runner.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (
            holder.get("pid") and _waiting(w, holder["pid"])
        ):
            time.sleep(0.05)
        assert holder.get("pid") and _waiting(w, holder["pid"]), "Run now did not wait"
        assert runner.is_alive()
        edit.commit()
    finally:
        edit.close()
    result = runner.join_ok()
    assert result.status == "queued"
    with w.factory() as s:
        req = s.get(ScoutRequest, w.rid)
        assert (req.name, req.status) == ("Edited before the run", "queued")


def test_an_edit_after_a_committed_run_now_is_refused(world):
    w = world
    _settle(w)
    with w.factory() as s:
        run_scout_request_endpoint(workspace_id=w.ws, request_id=w.rid, db=s, ctx=w.ctx)
    with w.factory() as s:
        with pytest.raises(ValidationDomainError) as refused:
            update_scout_request(
                workspace_id=w.ws,
                request_id=w.rid,
                body=ScoutRequestUpdate(name="Too late"),
                db=s,
                ctx=w.ctx,
            )
        assert refused.value.code == "scout_in_progress"
        s.rollback()
    with w.factory() as s:
        assert s.get(ScoutRequest, w.rid).name != "Too late"


def test_a_run_now_between_the_edits_check_and_write_cannot_slip_through(world, monkeypatch):
    """§B atomicity: a Run now issued after the edit's in-progress check and before its write
    either waits for the edit (the Scout row is locked) or makes the edit refuse; it can never
    commit in between with the edit then succeeding over a queued Scout."""
    from app.scouting_requests import routes as scout_routes

    w = world
    _settle(w)
    real = scout_routes.live_run_exists
    seen: dict = {}

    def check_then_race(db, request, *, fence):
        result = real(db, request, fence=fence)

        def run_now():
            with w.factory() as s:
                seen["pid"] = s.execute(text("select pg_backend_pid()")).scalar_one()
                return run_scout_request_endpoint(
                    workspace_id=w.ws, request_id=w.rid, db=s, ctx=w.ctx
                )

        racer = Background(run_now)
        racer.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and racer.is_alive():
            if seen.get("pid") and _waiting(w, seen["pid"]):
                seen["waited"] = True
                break
            time.sleep(0.05)
        seen["racer"] = racer
        return result

    monkeypatch.setattr(scout_routes, "live_run_exists", check_then_race)
    refused = None
    with w.factory() as s:
        try:
            update_scout_request(
                workspace_id=w.ws,
                request_id=w.rid,
                body=ScoutRequestUpdate(name="Edited during a Run now"),
                db=s,
                ctx=w.ctx,
            )
            s.commit()
        except ValidationDomainError as exc:
            refused = exc.code
            s.rollback()
    seen["racer"].join_ok()
    with w.factory() as s:
        req = s.get(ScoutRequest, w.rid)
    assert req.status == "queued"
    if refused is None:
        assert seen.get("waited"), "the edit succeeded although Run now committed in between"
        assert req.name == "Edited during a Run now"
    else:
        assert refused == "scout_in_progress" and req.name != "Edited during a Run now"


# --------------------------------------------------------------------------- #
# A: the market is part of the identity (contract §J.1; the reviewer's RV-A1b, review r1 F1)
# --------------------------------------------------------------------------- #
class _MarketSources:
    """``pipeline.fetch_signals`` stand-in serving the demo library FOR THE MARKET THE RUN ASKS
    FOR (the harness's ``Sources`` always serves Dallas), through the real fixture connector, so
    a market edit is visible. Adopted from the reviewer's probe (helpers only)."""

    def __call__(
        self, *, market, keywords, source_types, rss_enabled, takedowns=frozenset(), settings=None
    ):
        from app.connectors.coverage import Coverage, CoverageReason
        from app.scouting_requests.connectors import FixtureConnector, SignalBatch

        out = FixtureConnector().fetch_reported(
            market=market, keywords=list(keywords), source_types=list(source_types)
        )
        return SignalBatch(
            signals=out.signals,
            coverage=Coverage(CoverageReason.FIXTURE_LIBRARY, is_simulated=True),
            reports=out.reports,
        )


def _market_scout(w, *, market, name, source_types, keywords) -> str:
    from app.brands.models import Brand

    with w.factory() as s:
        brand = s.scalar(select(Brand).where(Brand.workspace_id == w.ws))
        req = ScoutRequest(
            organization_id=w.org,
            workspace_id=w.ws,
            brand_id=brand.id,
            location_id=None,
            name=name,
            status="draft",
            source_types=list(source_types),
            keywords=list(keywords),
            resolved_market=market,
        )
        s.add(req)
        s.commit()
        return req.id


def _run_market(w, monkeypatch, rid: str) -> str:
    monkeypatch.setattr(pipeline, "fetch_signals", _MarketSources())
    with w.factory() as s:
        req = s.get(ScoutRequest, rid)
        req.status = "queued"
        job = enqueue_scout_request(
            s,
            organization_id=req.organization_id,
            workspace_id=req.workspace_id,
            scout_request_id=rid,
            location_id=req.location_id,
            request_id=rid,
            run_fence=req.run_fence,
            idempotency_key=f"w16-market-{uuid.uuid4().hex}",
        )
        s.commit()
        job_id = job.id
    with offline_llm():
        assert _start_worker(w).join_ok() is True
    with w.factory() as s:
        job = s.get(Job, job_id)
        assert job.status == "succeeded", (job.status, job.last_error_summary)
    return job_id


def test_rv_a1b_a_market_edit_with_market_neutral_evidence(world, monkeypatch):
    """The reviewer's RV-A1b: the demo library's competitor item has the same text and the shared
    simulated URL in every market. A Scout reading only competitor scans, edited from Dallas to
    Lagos, then re-run: the Lagos findings must not be recorded as sightings of Dallas cards; a
    new Lagos card appears and the Dallas card stays as history."""
    from app.signals.models import NormalizedSignal as _Norm

    w = world
    _settle(w)
    rid = _market_scout(
        w,
        market="Dallas, TX",
        name="competitor-watch",
        source_types=["competitor_scan"],
        keywords=["cafe"],
    )
    _run_market(w, monkeypatch, rid)
    with w.factory() as s:
        dallas = [
            (c.id, c.resolved_market)
            for c in s.scalars(select(Opportunity).where(Opportunity.scout_request_id == rid))
        ]
        update_scout_request(
            workspace_id=w.ws,
            request_id=rid,
            body=ScoutRequestUpdate(resolved_market="Lagos, NG"),
            db=s,
            ctx=w.ctx,
        )
        s.commit()
    second = _run_market(w, monkeypatch, rid)
    seen = []
    with w.factory() as s:
        for o in s.scalars(
            select(OpportunityObservation).where(OpportunityObservation.job_id == second)
        ):
            markets = {
                (row.ingest_metadata or {}).get("market")
                for row in s.scalars(select(_Norm).where(_Norm.cluster_id == o.cluster_id))
            }
            card = s.get(Opportunity, o.opportunity_id)
            seen.append((o.kind, card.resolved_market, markets))
        after = list(s.scalars(select(Opportunity).where(Opportunity.scout_request_id == rid)))
    crossed = [x for x in seen if x[0] == "seen_again" and x[1] not in x[2]]
    assert crossed == [], f"Lagos-run findings recorded as sightings of Dallas cards: {crossed}"
    assert dallas and all(market == "Dallas, TX" for _, market in dallas)
    assert {c.id for c in after} >= {cid for cid, _ in dallas}  # the Dallas cards stay as history
    assert any(c.resolved_market == "Lagos, NG" for c in after)  # a new Lagos card
