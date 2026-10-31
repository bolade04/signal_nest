"""W16 customer-workflow repairs on SQLite (PROPOSED; contract W16-CUSTOMER-REPAIR-CONTRACT-01).

The real worker, handler and routes over the four-market demo seed (the harness of
``test_scout_pause_promise``). PostgreSQL behaviour (the unique-index races, the key order, a
stopped run, the edit-vs-Run-now lock) is in ``test_customer_repairs_w16_postgres.py``.

* A: a re-run creates no card for a finding that has one: it records a ``seen_again`` sighting;
  the card's status survives; the run's stats split new and seen-again findings; legacy cards
  (no key) are adopted oldest-first and younger copies are untouched; evidence-only identity.
* B: the edit route's restriction, validation and audit.
* C: the list carries the same execution state as the detail, in a bounded number of statements.
* D: every run outcome of the table, and the jobs API.
* E: a location's market text is read in the location's country at source selection.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, func, select, update

from app.audit.models import AuditLog
from app.core.enums import Role, ScoutRequestStatus
from app.db import seed as seed_mod
from app.geography.markets import qualify_for_location
from app.jobs import pipeline
from app.jobs.models import Job
from app.jobs.outcomes import RunOutcome, RunOutcomeReason, run_outcome
from app.jobs.schemas import JobOut
from app.llm.models import LLMUsageEvent
from app.locations.models import BusinessLocation
from app.opportunities.identity import finding_key, item_identity, legacy_cards_by_key
from app.opportunities.models import Opportunity, OpportunityObservation
from app.organizations.models import OrganizationMember, User
from app.scouting_requests.execution import derive_execution, derive_executions
from app.scouting_requests.models import ScoutAdmission, ScoutRequest
from app.tests import test_scout_pause_promise as promise
from app.tests._auth2_support import live_bearer
from app.tests._llm_offline import offline_llm
from app.tests.test_scout_pause_promise import _NOW, API, _queue, _runner

factory = promise.factory
s = promise.s
flag_on = promise.flag_on
committed = promise.committed
client = promise.client

DALLAS = "dallas"


def _rid(market: str = DALLAS) -> str:
    return seed_mod.sid("scout", market)


def _ws() -> str:
    return seed_mod.sid("ws")


def _owner() -> dict:
    return {"Authorization": f"Bearer {live_bearer(seed_mod.sid('user'))}"}


def _poll(factory) -> bool:
    with offline_llm():
        return _runner(factory, clock=lambda: _NOW + timedelta(minutes=1)).poll_once(
            worker_id="w-w16"
        )


def _run(factory, rid: str) -> str:
    """One run through the real worker; returns its job id."""
    job_id = _queue(factory, rid)
    assert _poll(factory) is True
    with factory() as session:
        assert session.get(Job, job_id).status == "succeeded"
    return job_id


def _cards(factory, rid: str) -> list[Opportunity]:
    with factory() as session:
        return list(
            session.scalars(
                select(Opportunity)
                .where(Opportunity.scout_request_id == rid)
                .order_by(Opportunity.created_at, Opportunity.id)
            )
        )


def _sightings(factory, rid: str) -> list[OpportunityObservation]:
    with factory() as session:
        return list(
            session.scalars(
                select(OpportunityObservation)
                .where(OpportunityObservation.scout_request_id == rid)
                .order_by(OpportunityObservation.observed_at, OpportunityObservation.id)
            )
        )


@pytest.fixture
def clean_findings(factory, committed):
    """Start the Scout with no cards and no sightings; leave it so."""

    def wipe() -> None:
        with factory() as session:
            for market in promise._MARKETS:
                rid = _rid(market)
                session.execute(
                    delete(OpportunityObservation).where(
                        OpportunityObservation.scout_request_id == rid
                    )
                )
                session.execute(delete(Opportunity).where(Opportunity.scout_request_id == rid))
            session.commit()

    wipe()
    yield
    wipe()


# --------------------------------------------------------------------------- #
# A: one current card per finding
# --------------------------------------------------------------------------- #
def test_a_rerun_records_seen_again_and_creates_no_card(factory, clean_findings):
    first_job = _run(factory, _rid())
    cards = _cards(factory, _rid())
    assert len(cards) == 3 and all(c.finding_key and c.finding_key.startswith("f2:") for c in cards)
    assert len({c.finding_key for c in cards}) == 3
    first = _sightings(factory, _rid())
    assert {o.kind for o in first} == {"first_seen"} and len(first) == 3
    assert {o.job_id for o in first} == {first_job}
    with factory() as session:
        session.execute(
            update(Opportunity).where(Opportunity.id == cards[0].id).values(status="saved")
        )
        session.commit()

    second_job = _run(factory, _rid())
    again = _cards(factory, _rid())
    assert [c.id for c in again] == [c.id for c in cards]  # no new card
    assert {c.id: c.status for c in again}[cards[0].id] == "saved"  # the customer's status kept
    for before, after in zip(cards, again, strict=True):  # text and scores untouched
        assert (after.title, after.opportunity_score, after.why_it_matters) == (
            before.title,
            before.opportunity_score,
            before.why_it_matters,
        )
    seen = [o for o in _sightings(factory, _rid()) if o.kind == "seen_again"]
    assert len(seen) == 3 and {o.job_id for o in seen} == {second_job}
    assert {o.opportunity_id for o in seen} == {c.id for c in cards}
    # Each sighting names THIS run's cluster (its own members), not the card's first cluster.
    assert {o.cluster_id for o in seen}.isdisjoint({c.cluster_id for c in cards})
    with factory() as session:
        req = session.get(ScoutRequest, _rid())
        assert req.stats["opportunities"] == 3
        assert req.stats["opportunities_new"] == 0
        assert req.stats["opportunities_seen_again"] == 3
        summary = session.get(Job, second_job).result_summary
        assert (summary["opportunities_new"], summary["opportunities_seen_again"]) == (0, 3)
        assert session.get(Job, first_job).result_summary["opportunities_new"] == 3


def test_a_changed_member_set_is_a_new_finding_even_with_the_same_title(
    factory, clean_findings, monkeypatch
):
    """Distinct evidence -> a distinct card (contract §A): a finding whose member set changed
    (one item gone) is a new card, though its title is the same; unchanged findings are
    seen again. Kills an identity taken from the title."""
    _run(factory, _rid())
    first = _cards(factory, _rid())
    biggest = max(first, key=lambda c: len(c.observed_evidence))
    assert len(biggest.observed_evidence) > 1
    dropped = biggest.observed_evidence[0]["excerpt"]
    real_fetch = pipeline.fetch_signals

    def fetch_without_one(**kwargs):
        batch = real_fetch(**kwargs)
        batch.signals = [x for x in batch.signals if not x.content.startswith(dropped[:60])]
        return batch

    monkeypatch.setattr(pipeline, "fetch_signals", fetch_without_one)
    _run(factory, _rid())
    after = _cards(factory, _rid())
    new = [c for c in after if c.id not in {f.id for f in first}]
    assert [c.title for c in new] == [biggest.title]
    assert new[0].finding_key != biggest.finding_key
    seen = [o for o in _sightings(factory, _rid()) if o.kind == "seen_again"]
    assert {o.opportunity_id for o in seen} == {c.id for c in first} - {biggest.id}


def test_a_rerun_does_not_attach_its_intelligence_to_the_existing_card(factory, clean_findings):
    from app.intelligence.records import SignalIntelligenceRecord

    _run(factory, _rid())
    with factory() as session:
        linked = session.scalar(
            select(func.count())
            .select_from(SignalIntelligenceRecord)
            .where(
                SignalIntelligenceRecord.scout_request_id == _rid(),
                SignalIntelligenceRecord.opportunity_id.is_not(None),
            )
        )
    _run(factory, _rid())
    with factory() as session:
        assert linked and linked == session.scalar(
            select(func.count())
            .select_from(SignalIntelligenceRecord)
            .where(
                SignalIntelligenceRecord.scout_request_id == _rid(),
                SignalIntelligenceRecord.opportunity_id.is_not(None),
            )
        )


def test_a_rerun_makes_the_same_model_calls(factory, clean_findings):
    """Accounting (contract §I.4): a re-run is charged exactly as the first run was."""
    jobs = [_run(factory, _rid()), _run(factory, _rid())]
    with factory() as session:
        rows = [
            session.execute(
                select(LLMUsageEvent.task, func.count(), func.sum(LLMUsageEvent.input_tokens))
                .where(LLMUsageEvent.job_id == job)
                .group_by(LLMUsageEvent.task)
                .order_by(LLMUsageEvent.task)
            ).all()
            for job in jobs
        ]
    assert rows[0] and rows[0] == rows[1]


def test_a_the_same_evidence_in_another_scout_is_another_card(factory, clean_findings, client):
    made = client.post(
        f"{API}/workspaces/{_ws()}/scout-requests",
        json={
            "name": "Second Dallas scout",
            "location_id": seed_mod.sid("loc", DALLAS),
            "keywords": [],
        },
        headers=_owner(),
    )
    assert made.status_code == 201, made.text
    other = made.json()["id"]
    try:
        with factory() as session:
            session.execute(
                update(ScoutRequest)
                .where(ScoutRequest.id == other)
                .values(
                    keywords=session.get(ScoutRequest, _rid()).keywords,
                    source_types=session.get(ScoutRequest, _rid()).source_types,
                )
            )
            session.commit()
        _run(factory, _rid())
        _run(factory, other)
        mine, theirs = _cards(factory, _rid()), _cards(factory, other)
        assert len(mine) == len(theirs) == 3
        assert {c.finding_key for c in mine} == {c.finding_key for c in theirs}
        assert {c.id for c in mine}.isdisjoint({c.id for c in theirs})
    finally:
        with factory() as session:
            session.execute(
                delete(OpportunityObservation).where(
                    OpportunityObservation.scout_request_id == other
                )
            )
            session.execute(delete(Opportunity).where(Opportunity.scout_request_id == other))
            session.execute(delete(Job).where(Job.scout_request_id == other))
            session.execute(delete(ScoutRequest).where(ScoutRequest.id == other))
            session.commit()


def _make_legacy(factory, rid: str) -> list[str]:
    """Turn the Scout's cards into cards created before the identity: no key, no sighting."""
    with factory() as session:
        ids = list(
            session.scalars(
                select(Opportunity.id)
                .where(Opportunity.scout_request_id == rid)
                .order_by(Opportunity.created_at, Opportunity.id)
            )
        )
        session.execute(
            update(Opportunity).where(Opportunity.scout_request_id == rid).values(finding_key=None)
        )
        session.execute(
            delete(OpportunityObservation).where(OpportunityObservation.scout_request_id == rid)
        )
        session.commit()
    return ids


def _as_at_the_base(db, request, findings):
    """The write step as 9279c1d2 had it: every finding a new card, no key, no sighting."""
    built = pipeline.BuiltFindings()
    for finding in findings:
        db.add(finding.card)
        db.flush()
        pipeline._attach_card(db, request, finding.card, finding)
        built.append(finding.card)
    return built


def test_a_legacy_duplicates_the_oldest_is_adopted_the_younger_untouched(
    factory, clean_findings, monkeypatch
):
    with monkeypatch.context() as base:
        base.setattr(pipeline, "_record_findings", _as_at_the_base)
        _run(factory, _rid())
        oldest = [c.id for c in _cards(factory, _rid())]
        _run(factory, _rid())  # 9279c1d2's duplicates
    cards = _cards(factory, _rid())
    younger = [c.id for c in cards if c.id not in oldest]
    assert len(oldest) == len(younger) == 3 and all(c.finding_key is None for c in cards)
    with factory() as session:
        session.execute(
            update(Opportunity).where(Opportunity.id.in_(younger)).values(status="ignored")
        )
        session.commit()
    before = {c.id: (c.status, c.title) for c in _cards(factory, _rid())}

    job = _run(factory, _rid())
    after = _cards(factory, _rid())
    assert len(after) == 6  # no new card
    assert {c.id for c in after if c.finding_key} == set(oldest)  # the OLDEST copy adopted
    assert all(c.finding_key is None for c in after if c.id in younger)
    assert {c.id: (c.status, c.title) for c in after} == before  # nothing else changed
    seen = _sightings(factory, _rid())
    assert {(o.opportunity_id, o.kind, o.job_id) for o in seen} == {
        (cid, "seen_again", job) for cid in oldest
    }
    # Review r1 F4 (RM18): an adopted card's creation counts as its first sighting, so after one
    # run at the candidate it reads "seen in 2 runs"; a younger copy has no sighting (seen once).
    from app.opportunities.identity import sightings

    with factory() as session:
        counts = sightings(session, workspace_id=_ws(), opportunity_ids=oldest + younger)
    assert {cid: counts[cid][0] for cid in oldest} == {cid: 2 for cid in oldest}
    assert not set(younger) & set(counts)


def test_a_a_legacy_card_without_members_is_not_adopted(factory, clean_findings):
    from app.signals.models import NormalizedSignal

    _run(factory, _rid())
    legacy = _make_legacy(factory, _rid())
    with factory() as session:  # evidence retention removed the first run's members
        session.execute(
            update(NormalizedSignal)
            .where(NormalizedSignal.scout_request_id == _rid())
            .values(cluster_id=None)
        )
        session.commit()
        assert (
            legacy_cards_by_key(
                session,
                organization_id=seed_mod.sid("org"),
                workspace_id=_ws(),
                scout_request_id=_rid(),
            )
            == {}
        )
    _run(factory, _rid())
    cards = _cards(factory, _rid())
    assert len(cards) == 6  # one more card per finding, then none
    assert all(c.finding_key is None for c in cards if c.id in legacy)


def test_a_identity_is_the_evidence_never_the_title_or_the_url_alone():
    base = dict(source_type="reviews", source_key=None, ingest_metadata={})
    one = item_identity(source_url="https://example.com/simulated", content_hash="a" * 64, **base)
    two = item_identity(source_url="https://example.com/simulated", content_hash="b" * 64, **base)
    assert one != two  # one shared URL, two items
    rows = [SimpleNamespace(**base, source_url="u", content_hash=h) for h in ("x", "y")]
    assert finding_key(rows) == finding_key(list(reversed(rows)))  # order-free
    assert finding_key(rows) != finding_key(rows[:1])  # a different member set
    keyed = item_identity(
        source_type="rss_news",
        source_key="feed-1",
        source_url="https://n.example/a",
        content_hash="c" * 64,
        ingest_metadata={"provenance": {"item_guid": "g-1"}},
    )
    assert keyed == ("rss_news", "key:feed-1|g-1", "c" * 64)
    guid = item_identity(
        source_type="rss_news",
        source_key=None,
        source_url="https://n.example/a",
        content_hash="c" * 64,
        ingest_metadata={"provenance": {"item_guid": "g-1"}},
    )
    assert guid == ("rss_news", "guid:g-1", "c" * 64)
    assert finding_key([]) is None


def test_a_identity_includes_the_market_the_item_was_collected_for():
    """Contract §J.1 (review r1 F1): the same item text collected for another market is other
    evidence; only whitespace and case are normalized; a row with no market keeps the triple."""
    base = dict(
        source_type="competitor_scan",
        source_key=None,
        source_url="https://example.com/simulated",
        content_hash="d" * 64,
    )
    dallas = item_identity(**base, ingest_metadata={"market": "Dallas, TX"})
    lagos = item_identity(**base, ingest_metadata={"market": "Lagos, NG"})
    assert dallas != lagos
    assert dallas == item_identity(**base, ingest_metadata={"market": "  dallas,   TX "})
    assert item_identity(**base, ingest_metadata={"market": None}) == (
        "competitor_scan",
        "url:https://example.com/simulated",
        "d" * 64,
    )
    assert item_identity(**base, ingest_metadata={}) == item_identity(**base, ingest_metadata=None)
    rows = [
        SimpleNamespace(**base, ingest_metadata={"market": m}) for m in ("Dallas, TX", "Lagos, NG")
    ]
    assert finding_key(rows[:1]) != finding_key(rows[1:])
    assert finding_key(rows[:1]).startswith("f2:")


#: FU-W16-4 (review r2 G2): same-named places in different markets, and spellings of one market
#: the contract treats as equal (whitespace collapsed, case folded; nothing else).
_SAME_CITY_OTHER_MARKET = [("Paris, TX", "Paris, FR"), ("Lagos, NG", "Lagos, Lagos")]
_SAME_MARKET_OTHER_SPELLING = [("Paris, TX", "  PARIS,   tx "), ("Lagos, NG", "lagos,  NG ")]
_OTHER_MARKET_IDS = ["paris_tx-paris_fr", "lagos_ng-lagos_lagos"]
_SAME_MARKET_IDS = ["paris_tx-respaced_recased", "lagos_ng-respaced_recased"]


@pytest.mark.parametrize(("one", "other"), _SAME_CITY_OTHER_MARKET, ids=_OTHER_MARKET_IDS)
def test_a_identity_keeps_a_same_named_city_in_another_market_apart(one, other):
    """Contract §J.1 (review r2 G2): nothing may merge two different markets. Identical evidence
    recorded for "Paris, TX" and "Paris, FR" (or "Lagos, NG" and "Lagos, Lagos") differs only in
    the market part, and that part keeps the items and their findings apart."""
    base = dict(
        source_type="rss_news",
        source_key="w16-market-proof",
        source_url="https://w16-market-proof.example/item",
        content_hash="e" * 64,
    )
    a = item_identity(**base, ingest_metadata={"market": one})
    b = item_identity(**base, ingest_metadata={"market": other})
    assert a[:3] == b[:3]  # the evidence is identical: only the market differs
    assert a != b
    rows = [SimpleNamespace(**base, ingest_metadata={"market": m}) for m in (one, other)]
    assert finding_key(rows[:1]) != finding_key(rows[1:])


@pytest.mark.parametrize(("one", "same"), _SAME_MARKET_OTHER_SPELLING, ids=_SAME_MARKET_IDS)
def test_a_identity_treats_spacing_and_case_of_one_market_as_equal(one, same):
    """The control for the test above: the normalization the contract permits (whitespace and
    case) still compares one market equal, so the market part is not merely always different."""
    base = dict(
        source_type="rss_news",
        source_key="w16-market-proof",
        source_url="https://w16-market-proof.example/item",
        content_hash="e" * 64,
    )
    assert one != same
    assert item_identity(**base, ingest_metadata={"market": one}) == item_identity(
        **base, ingest_metadata={"market": same}
    )


def _run_recorded_for(factory, monkeypatch, label: str) -> str:
    """One run of the Dallas Scout whose evidence is the sample library's Dallas items, recorded
    for ``label``: the live and sandbox connectors stamp the market they were asked for
    (``scope.market``) on every item, so this is the same evidence collected for another market.
    Nothing else about the items changes."""
    real_fetch = pipeline.fetch_signals

    def fetch_recorded_for(**kwargs):
        batch = real_fetch(**kwargs)
        assert batch.signals, "the sample library returned no evidence"
        batch.signals = [dataclasses.replace(x, market=label) for x in batch.signals]
        return batch

    monkeypatch.setattr(pipeline, "fetch_signals", fetch_recorded_for)
    return _run(factory, _rid())


def _member_markets(session, cluster_id: str) -> set[str | None]:
    """The market labels a cluster's member rows store, as stored (read without the identity
    module, so a change to its market rule cannot change what this reports)."""
    from app.signals.models import NormalizedSignal

    return {
        (row.ingest_metadata or {}).get("market")
        for row in session.scalars(
            select(NormalizedSignal).where(NormalizedSignal.cluster_id == cluster_id)
        )
    }


@pytest.mark.parametrize(
    ("one", "other", "same_market"),
    [(a, b, False) for a, b in _SAME_CITY_OTHER_MARKET]
    + [(a, b, True) for a, b in _SAME_MARKET_OTHER_SPELLING],
    ids=[f"other_market-{i}" for i in _OTHER_MARKET_IDS]
    + [f"same_market-{i}" for i in _SAME_MARKET_IDS],
)
def test_a_a_sighting_never_attaches_to_another_markets_card(
    factory, clean_findings, monkeypatch, one, other, same_market
):
    """Review r2 G2 through the real worker and pipeline: the same evidence recorded first for
    ``one``, then for ``other``. For another market (Paris, TX / Paris, FR; Lagos, NG / Lagos,
    Lagos) the second run's findings are new cards and none of its sightings lands on a first-run
    card; for another spelling of the same market (the control) every finding is seen again on
    its first-run card. A second-run sighting's card was built from evidence recorded for the
    second run's market, never the first's, unless the two are one market."""
    first_job = _run_recorded_for(factory, monkeypatch, one)
    first = _cards(factory, _rid())
    assert len(first) == 3
    second_job = _run_recorded_for(factory, monkeypatch, other)
    after = _cards(factory, _rid())
    sightings = [o for o in _sightings(factory, _rid()) if o.job_id == second_job]
    assert len(sightings) == 3
    first_ids = {c.id for c in first}
    if same_market:
        assert [c.id for c in after] == [c.id for c in first]  # no new card
        assert {o.kind for o in sightings} == {"seen_again"}
        assert {o.opportunity_id for o in sightings} == first_ids
    else:
        assert len(after) == 6
        assert {o.kind for o in sightings} == {"first_seen"}
        assert {o.opportunity_id for o in sightings}.isdisjoint(first_ids)
    with factory() as session:
        for o in _sightings(factory, _rid()):
            card = session.get(Opportunity, o.opportunity_id)
            label = one if o.job_id == first_job else other
            card_label = one if (o.job_id == first_job or same_market) else other
            assert _member_markets(session, o.cluster_id) == {label}
            assert _member_markets(session, card.cluster_id) == {card_label}, (o.kind, label)
        stats = session.get(ScoutRequest, _rid()).stats
    assert (stats["opportunities_new"], stats["opportunities_seen_again"]) == (
        (0, 3) if same_market else (3, 0)
    )


def test_a_the_sample_library_records_its_own_label_so_a_relabel_keeps_the_cards(
    factory, client, committed
):
    """The documented sample-library behaviour, kept (#278 Limitation 4; review r2 G4): the
    library records its own market label, so relabelling a Lagos, NG location's Scout from
    "Lagos, Lagos" to "Lagos, NG" reads the same sample market, and its cards are seen again.
    The Lagos, NG / Lagos, Lagos distinction above applies where the recorded label is the text
    the sources were asked for (the live and sandbox connectors)."""
    rid, loc_id = _lagos_location_scout(factory, client)
    try:
        _run(factory, rid)
        first = _cards(factory, rid)
        assert first
        assert _edit(client, {"resolved_market": "Lagos, NG"}, rid).status_code == 200
        second_job = _run(factory, rid)
        after = _cards(factory, rid)
        assert [c.id for c in after] == [c.id for c in first]
        sightings = [o for o in _sightings(factory, rid) if o.job_id == second_job]
        assert sightings and {o.kind for o in sightings} == {"seen_again"}
        with factory() as session:
            assert session.get(ScoutRequest, rid).resolved_market == "Lagos, NG"
            for card in after:  # the library's own label, whatever text the Scout sent
                assert _member_markets(session, card.cluster_id) == {"Lagos, NG"}
            for o in sightings:
                assert _member_markets(session, o.cluster_id) == {"Lagos, NG"}
    finally:
        _drop_scout_and_location(factory, rid, loc_id)


def test_a_opportunity_reads_carry_the_sightings(factory, clean_findings, client):
    _run(factory, _rid())
    _run(factory, _rid())
    got = client.get(
        f"{API}/workspaces/{_ws()}/opportunities?scout_request_id={_rid()}", headers=_owner()
    )
    assert got.status_code == 200
    assert {(o["seen_count"], o["last_seen_at"] is not None) for o in got.json()} == {(2, True)}
    one = got.json()[0]["id"]
    detail = client.get(f"{API}/workspaces/{_ws()}/opportunities/{one}", headers=_owner())
    assert detail.json()["seen_count"] == 2
    assert "finding_key" not in detail.json()


# --------------------------------------------------------------------------- #
# B: edit
# --------------------------------------------------------------------------- #
def _edit(client, body, rid: str | None = None, headers=None):
    return client.put(
        f"{API}/workspaces/{_ws()}/scout-requests/{rid or _rid()}",
        json=body,
        headers=headers or _owner(),
    )


def test_b_edit_is_refused_while_queued_and_while_a_run_is_live(factory, client):
    _queue(factory, _rid())
    r = _edit(client, {"name": "Renamed while queued"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "scout_in_progress"
    with factory() as session:  # a live job with the Scout's fence, the request settled
        session.execute(
            update(ScoutRequest).where(ScoutRequest.id == _rid()).values(status="completed")
        )
        session.commit()
    r = _edit(client, {"name": "Renamed while a job is live"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "scout_in_progress"
    with factory() as session:
        assert session.get(ScoutRequest, _rid()).name != "Renamed while a job is live"


def test_b_edit_is_allowed_while_paused_and_changes_nothing_else(factory, client):
    _queue(factory, _rid())  # a run queued, then paused: its job is fenced out
    paused = client.post(
        f"{API}/workspaces/{_ws()}/scout-requests/{_rid()}/pause", headers=_owner()
    )
    assert paused.status_code == 200
    with factory() as session:
        before = session.get(ScoutRequest, _rid())
        state = (before.status, before.run_fence, before.owner_job_id, before.owner_attempt)
    r = _edit(
        client, {"name": "  Dallas, edited  ", "keywords": [" oat milk ", "", "OAT MILK", "latte"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Dallas, edited"
    assert r.json()["keywords"] == ["oat milk", "latte"]
    assert r.json()["execution"]["state"] == "paused"
    with factory() as session:
        after = session.get(ScoutRequest, _rid())
        assert (after.status, after.run_fence, after.owner_job_id, after.owner_attempt) == state
        audit = session.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == _rid(), AuditLog.action == "scout_request.updated"
            )
        ).all()
        assert [a.context for a in audit] == [{"fields": ["keywords", "name"]}]


def test_b_edit_is_allowed_when_the_scout_needs_attention(factory, client):
    with factory() as session:
        req = session.get(ScoutRequest, _rid())
        now = datetime.now(UTC)
        session.add(
            ScoutAdmission(
                id=uuid.uuid4().hex,
                scout_request_id=req.id,
                organization_id=req.organization_id,
                workspace_id=req.workspace_id,
                attempt=1,
                worker_id="w-gone",
                operation="model",
                admitted_at=now,
                ended_at=now,
                end_kind="unknown",
                deadline_rule="unrecorded",
            )
        )
        session.commit()
    try:
        r = _edit(client, {"notes": "edited while blocked"})
        assert r.status_code == 200, r.text
        assert r.json()["execution"]["state"] == "needs_attention"
    finally:
        with factory() as session:
            session.execute(delete(ScoutAdmission))
            session.commit()


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": "   "},
        {"name": None},
        {"name": "x" * 201},
        {"source_types": ["not_a_source"]},
        {"source_types": None},
        {"keywords": None},
        {"status": "completed"},
        {"location_id": None},
        {"run_fence": 0},
    ],
)
def test_b_edit_validation_refuses_with_422_and_writes_nothing(factory, client, committed, body):
    with factory() as session:
        before = session.get(ScoutRequest, _rid())
        snapshot = (before.name, before.source_types, before.keywords, before.status)
    r = _edit(client, body)
    assert r.status_code == 422, r.text
    with factory() as session:
        after = session.get(ScoutRequest, _rid())
        assert (after.name, after.source_types, after.keywords, after.status) == snapshot


def test_b_create_keeps_todays_validation(client, committed):
    """Contract §I.5 / FU-W16-1: the new rules apply to edit only."""
    made = client.post(
        f"{API}/workspaces/{_ws()}/scout-requests",
        json={"name": "Lenient create", "source_types": ["not_a_source"], "keywords": [" a "]},
        headers=_owner(),
    )
    assert made.status_code == 201
    assert made.json()["source_types"] == ["not_a_source"]


def test_b_a_viewer_cannot_edit(factory, client):
    viewer = User(
        id=uuid.uuid4().hex,
        email=f"viewer-{uuid.uuid4().hex[:6]}@example.test",
        full_name="v",
        hashed_password="x",
    )
    with factory() as session:
        session.add(viewer)
        session.flush()
        session.add(
            OrganizationMember(
                id=uuid.uuid4().hex,
                organization_id=seed_mod.sid("org"),
                user_id=viewer.id,
                role=Role.VIEWER.value,
            )
        )
        session.commit()
    r = _edit(
        client,
        {"name": "By a viewer"},
        headers={"Authorization": f"Bearer {live_bearer(viewer.id)}"},
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# C: the list's execution equals the detail's, in a bounded number of statements
# --------------------------------------------------------------------------- #
def test_c_list_execution_equals_detail_for_every_scout(factory, client):
    _queue(factory, _rid("london"))  # in progress
    client.post(f"{API}/workspaces/{_ws()}/scout-requests/{_rid('lagos')}/pause", headers=_owner())
    listed = client.get(f"{API}/workspaces/{_ws()}/scout-requests", headers=_owner())
    assert listed.status_code == 200
    states = set()
    for row in listed.json():
        detail = client.get(
            f"{API}/workspaces/{_ws()}/scout-requests/{row['id']}", headers=_owner()
        )
        assert row["execution"] == detail.json()["execution"], row["name"]
        states.add(row["execution"]["state"])
    assert {"in_progress", "paused", "idle"} <= states


def test_c_the_page_derivation_takes_a_bounded_number_of_statements(factory, committed):
    with factory() as session:
        requests = list(session.scalars(select(ScoutRequest)))
        assert len(requests) >= 4
        counted = {"n": 0}

        def count(*_a, **_k):
            counted["n"] += 1

        bind = session.get_bind()
        event.listen(bind, "before_cursor_execute", count)
        try:
            states = derive_executions(session, requests)
            for_page = counted["n"]
            counted["n"] = 0
            single = derive_execution(session, requests[0])
            for_one = counted["n"]
        finally:
            event.remove(bind, "before_cursor_execute", count)
        assert states[requests[0].id] == single
    # SQLite: no clock statement; admissions (+ jobs), live jobs, latest job per Scout.
    assert for_page == for_one == 3


# --------------------------------------------------------------------------- #
# D: run outcomes
# --------------------------------------------------------------------------- #
EXECUTE = "scout_request.execute"


@pytest.mark.parametrize(
    ("status", "summary", "code", "error", "expected"),
    [
        ("pending", None, None, None, (None, None)),
        ("running", None, None, None, (None, None)),
        ("retry_wait", None, "transient", "authority_lapsed", (None, None)),
        ("cancel_requested", None, None, None, (None, None)),
        ("succeeded", {"outcome": "completed"}, None, None, (RunOutcome.COMPLETED, None)),
        ("succeeded", {"scanned": 1}, None, None, (RunOutcome.COMPLETED, None)),
        (
            "succeeded",
            {"outcome": "paused_during_run"},
            None,
            None,
            (RunOutcome.COMPLETED, RunOutcomeReason.PAUSED_DURING_RUN),
        ),
        (
            "succeeded",
            {"outcome": "stopped_request_paused"},
            None,
            None,
            (RunOutcome.STOPPED_PAUSED, None),
        ),
        (
            "succeeded",
            {"outcome": "skipped_request_paused"},
            None,
            None,
            (RunOutcome.SKIPPED_PAUSED, None),
        ),
        (
            "succeeded",
            {"outcome": "skipped_owner_active"},
            None,
            None,
            (RunOutcome.SKIPPED, RunOutcomeReason.ANOTHER_RUN_ACTIVE),
        ),
        (
            "succeeded",
            {"outcome": "stopped_owner_superseded"},
            None,
            None,
            (RunOutcome.STOPPED, RunOutcomeReason.ANOTHER_RUN_TOOK_OVER),
        ),
        (
            "succeeded",
            {"outcome": "stopped_lease_lost"},
            None,
            None,
            (RunOutcome.STOPPED, RunOutcomeReason.WORKER_LOST),
        ),
        (
            "succeeded",
            {"outcome": "status_changed_during_run"},
            None,
            None,
            (RunOutcome.STOPPED, RunOutcomeReason.SCOUT_CHANGED),
        ),
        (
            "succeeded",
            {"outcome": "request_gone"},
            None,
            None,
            (RunOutcome.STOPPED, RunOutcomeReason.SCOUT_REMOVED),
        ),
        (
            "succeeded",
            {"outcome": "a future outcome"},
            None,
            None,
            (RunOutcome.STOPPED, RunOutcomeReason.INTERNAL),
        ),
        ("cancelled", None, None, None, (RunOutcome.CANCELLED, None)),
        (
            "failed",
            None,
            "non_retryable",
            "admission_unresolved",
            (RunOutcome.FAILED, RunOutcomeReason.EARLIER_OPERATION_TIMED_OUT),
        ),
        (
            "failed",
            None,
            "non_retryable",
            "signal_volume_above_bound",
            (RunOutcome.FAILED, RunOutcomeReason.TOO_MANY_SIGNALS),
        ),
        (
            "failed",
            None,
            "non_retryable",
            "claim_identity_incomplete",
            (RunOutcome.FAILED, RunOutcomeReason.INTERNAL),
        ),
        (
            "failed",
            None,
            "validation",
            "bad payload",
            (RunOutcome.FAILED, RunOutcomeReason.INTERNAL),
        ),
        (
            "dead_lettered",
            None,
            "transient",
            "OperationalError",
            (RunOutcome.FAILED, RunOutcomeReason.RETRIES_EXHAUSTED),
        ),
        (
            "dead_lettered",
            None,
            "timeout",
            "lease expired and attempts exhausted",
            (RunOutcome.FAILED, RunOutcomeReason.WORKER_STOPPED_RESPONDING),
        ),
        ("failed", None, None, None, (RunOutcome.FAILED, None)),
    ],
)
def test_d_every_outcome_of_the_table(status, summary, code, error, expected):
    assert (
        run_outcome(
            job_type=EXECUTE,
            status=status,
            result_summary=summary,
            last_error_code=code,
            last_error_summary=error,
        )
        == expected
    )


def test_d_a_stop_or_skip_is_never_completed_and_other_job_types_have_none():
    for outcome in ("stopped_request_paused", "skipped_request_paused", "skipped_owner_active"):
        got, _ = run_outcome(
            job_type=EXECUTE,
            status="succeeded",
            result_summary={"outcome": outcome},
            last_error_code=None,
            last_error_summary=None,
        )
        assert got is not RunOutcome.COMPLETED
    assert run_outcome(
        job_type="scout_schedule.tick",
        status="succeeded",
        result_summary={"outcome": "completed"},
        last_error_code=None,
        last_error_summary=None,
    ) == (None, None)


def test_d_the_jobs_api_labels_a_skip_and_never_exposes_the_summary(factory, client):
    job_id = _queue(factory, _rid())
    client.post(f"{API}/workspaces/{_ws()}/scout-requests/{_rid()}/pause", headers=_owner())
    assert _poll(factory) is True
    got = client.get(f"{API}/workspaces/{_ws()}/jobs/{job_id}", headers=_owner())
    assert got.status_code == 200
    body = got.json()
    assert (body["status"], body["outcome"], body["outcome_reason"]) == (
        "succeeded",
        "skipped_paused",
        None,
    )
    assert "last_error_summary" not in body
    runs = client.get(f"{API}/workspaces/{_ws()}/scout-requests/{_rid()}/runs", headers=_owner())
    assert runs.json()["items"][0]["outcome"] == "skipped_paused"
    with factory() as session:
        job = session.get(Job, job_id)
        job.status, job.last_error_code = "failed", "non_retryable"
        job.last_error_summary = "admission_unresolved"
        assert (
            JobOut.model_validate(job).outcome_reason
            == RunOutcomeReason.EARLIER_OPERATION_TIMED_OUT
        )
        session.rollback()


# --------------------------------------------------------------------------- #
# E: the location's country at source selection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "country", "expected"),
    [
        ("Lagos, Lagos", "NG", "Lagos, Lagos, NG"),
        ("Nairobi, Nairobi", "KE", "Nairobi, Nairobi, KE"),
        ("Lagos, Lagos", None, "Lagos, Lagos"),  # no location country: nothing guessed
        ("Lagos", "NG", "Lagos, NG"),
        ("Nigeria", "NG", "Nigeria"),  # a country stays a country: never narrowed or broadened
        ("London, UK", "NG", "London, UK"),  # an explicit market wins
        ("Lagos, NG", "KE", "Lagos, NG"),
        ("Dallas, TX", "US", "Dallas, TX"),
        ("London, England", "GB", "London, England"),
        ("", "NG", ""),
        (None, "NG", None),
        ("Lagos, Lagos", "Nigeria", "Lagos, Lagos, NG"),  # a country typed by name
    ],
)
def test_e_qualify_for_location(text, country, expected):
    assert qualify_for_location(text, country=country) == expected


@pytest.mark.parametrize(
    ("city", "state", "country", "market", "covered"),
    [
        ("Lagos", "Lagos", "NG", None, True),
        ("Nairobi", "Nairobi", "KE", None, True),
        ("Lagos", "Lagos", "KE", None, False),  # Kenya has no Lagos sample market
    ],
)
def test_e_a_location_scout_is_served_in_its_country(
    factory, client, committed, city, state, country, market, covered
):
    with factory() as session:
        loc = BusinessLocation(
            organization_id=seed_mod.sid("org"),
            workspace_id=_ws(),
            brand_id=seed_mod.sid("brand"),
            name=f"{city} test {uuid.uuid4().hex[:4]}",
            city=city,
            state_province=state,
            country=country,
            is_active=True,
        )
        session.add(loc)
        session.commit()
        loc_id = loc.id
    made = client.post(
        f"{API}/workspaces/{_ws()}/scout-requests",
        json={"name": f"{city} scout", "location_id": loc_id, "keywords": ["coffee"]},
        headers=_owner(),
    )
    assert made.status_code == 201, made.text
    rid = made.json()["id"]
    assert made.json()["resolved_market"] == f"{city}, {state}"  # stored text not rewritten
    try:
        _run(factory, rid)
        with factory() as session:
            stats = session.get(ScoutRequest, rid).stats
        assert (stats["coverage"]["status"] == "covered") is covered, stats["coverage"]
        assert (stats["scanned"] > 0) is covered
        if covered:
            assert session.get(ScoutRequest, rid).resolved_market == f"{city}, {state}"
    finally:
        with factory() as session:
            session.execute(
                delete(OpportunityObservation).where(OpportunityObservation.scout_request_id == rid)
            )
            session.execute(delete(Opportunity).where(Opportunity.scout_request_id == rid))
            session.execute(delete(Job).where(Job.scout_request_id == rid))
            session.execute(delete(ScoutRequest).where(ScoutRequest.id == rid))
            session.execute(delete(BusinessLocation).where(BusinessLocation.id == loc_id))
            session.commit()


@pytest.mark.parametrize("live_serves_stored", [True, False])
def test_e_the_live_path_keeps_the_stored_market_text(
    factory, client, committed, monkeypatch, live_serves_stored
):
    """Contract §J.4 (review r1 F5): when an approved live source serves the Scout's stored market
    text, the sources get exactly that text, as at 9279c1d2; otherwise the location's country
    qualifies it for the sample library and coverage."""
    import app.connectors.live as live_mod

    with factory() as session:
        loc = BusinessLocation(
            organization_id=seed_mod.sid("org"),
            workspace_id=_ws(),
            brand_id=seed_mod.sid("brand"),
            name=f"Lagos live {uuid.uuid4().hex[:4]}",
            city="Lagos",
            state_province="Lagos",
            country="NG",
            is_active=True,
        )
        session.add(loc)
        session.commit()
        loc_id = loc.id
    made = client.post(
        f"{API}/workspaces/{_ws()}/scout-requests",
        json={"name": "Lagos live scout", "location_id": loc_id, "keywords": ["coffee"]},
        headers=_owner(),
    )
    rid = made.json()["id"]
    asked: list = []
    real_fetch = pipeline.fetch_signals

    def recording_fetch(**kwargs):
        asked.append(kwargs["market"])
        return real_fetch(**{**kwargs, "rss_enabled": False})

    monkeypatch.setattr(pipeline, "_rss_enabled_for", lambda db, **_k: True)
    monkeypatch.setattr(
        live_mod,
        "live_targets_for",
        lambda *, market, **_k: (
            ["approved"] if live_serves_stored and market == "Lagos, Lagos" else []
        ),
    )
    monkeypatch.setattr(pipeline, "fetch_signals", recording_fetch)
    try:
        _run(factory, rid)
        assert asked == (["Lagos, Lagos"] if live_serves_stored else ["Lagos, Lagos, NG"])
    finally:
        with factory() as session:
            session.execute(
                delete(OpportunityObservation).where(OpportunityObservation.scout_request_id == rid)
            )
            session.execute(delete(Opportunity).where(Opportunity.scout_request_id == rid))
            session.execute(delete(Job).where(Job.scout_request_id == rid))
            session.execute(delete(ScoutRequest).where(ScoutRequest.id == rid))
            session.execute(delete(BusinessLocation).where(BusinessLocation.id == loc_id))
            session.commit()


def _lagos_location_scout(factory, client) -> tuple[str, str]:
    """A Scout on a new Lagos, Lagos, NG location, created through the API (stored market
    "Lagos, Lagos"); returns ``(scout id, location id)``."""
    with factory() as session:
        loc = BusinessLocation(
            organization_id=seed_mod.sid("org"),
            workspace_id=_ws(),
            brand_id=seed_mod.sid("brand"),
            name=f"Lagos fu {uuid.uuid4().hex[:4]}",
            city="Lagos",
            state_province="Lagos",
            country="NG",
            is_active=True,
        )
        session.add(loc)
        session.commit()
        loc_id = loc.id
    made = client.post(
        f"{API}/workspaces/{_ws()}/scout-requests",
        json={"name": "Lagos fu scout", "location_id": loc_id, "keywords": ["coffee"]},
        headers=_owner(),
    )
    assert made.status_code == 201, made.text
    assert made.json()["resolved_market"] == "Lagos, Lagos"
    return made.json()["id"], loc_id


def _drop_scout_and_location(factory, rid: str, loc_id: str) -> None:
    with factory() as session:
        session.execute(
            delete(OpportunityObservation).where(OpportunityObservation.scout_request_id == rid)
        )
        session.execute(delete(Opportunity).where(Opportunity.scout_request_id == rid))
        session.execute(delete(Job).where(Job.scout_request_id == rid))
        session.execute(delete(ScoutRequest).where(ScoutRequest.id == rid))
        session.execute(delete(BusinessLocation).where(BusinessLocation.id == loc_id))
        session.commit()


#: FU-W16-3 (review r2 G1): an approved live source cleared for the stored text "Lagos, Lagos",
#: and an approved source for another market.
_SERVING_KEY = "w16-lagos-lagos-proof"
_UNRELATED_KEY = "w16-toronto-proof"


def _approved_target(key: str, market: str):
    from datetime import date

    from app.connectors.live import SourceTarget
    from app.connectors.sources import LegalStatus, SourceRecord

    record = SourceRecord(
        source_key=key,
        connector="rss_news",
        display_name=key,
        publisher="W16 proof publisher",
        feed_url=f"https://{key}.example/feed.xml",
        markets=(market,),
        tos_url=f"https://{key}.example/terms",
        approval_reference="DEC-W16-FU3",
        approved_on=date(2026, 1, 1),
        legal_status=LegalStatus.APPROVED,
        enabled=True,
    )
    assert record.is_activatable()
    return SourceTarget(source=record, fetch_url=record.feed_url, delivery="live")


@pytest.mark.parametrize(
    ("workspace_rss_off", "taken_down", "expected"),
    [
        # Positive controls: an eligible source serves the stored text, so it is kept.
        (False, (), "Lagos, Lagos"),
        (False, (_UNRELATED_KEY,), "Lagos, Lagos"),
        # The workspace's effective RSS decision is off: no live source can serve the run.
        (True, (), "Lagos, Lagos, NG"),
        # The only serving source is taken down: no live source can serve the run.
        (False, (_SERVING_KEY,), "Lagos, Lagos, NG"),
    ],
    ids=["serving", "unrelated_source_taken_down", "rss_off", "serving_source_taken_down"],
)
def test_e_the_live_check_honours_the_workspace_rss_decision_and_takedowns(
    factory, client, committed, monkeypatch, workspace_rss_off, taken_down, expected
):
    """Contract §J.4 (review r2 G1): the stored market text goes to the sources only when a live
    source would actually serve it. The production selection decides: the capability resolver
    (RSS on globally; a workspace override row turns it off), ``live_targets_for`` over an
    approved list holding one source cleared for "Lagos, Lagos", and the takedown rows. When the
    workspace's RSS is off, or the serving source is taken down, the run asks for the qualified
    text, as for any other location Scout. Nothing is fetched: the run reads the sample library
    with RSS off, and a live collection fails the test."""
    import app.connectors.live as live_mod
    import app.core.config as config_mod
    from app.capabilities.models import WorkspaceCapabilityOverride
    from app.capabilities.registry import Capability
    from app.connectors.models import SourceTakedown

    live = config_mod.get_settings().model_copy(
        update={
            "connector_rss_enabled": True,
            "connector_rss_live_enabled": True,
            "connector_rss_kill_switch": False,
            "connector_rss_replay_origin": None,
        }
    )
    monkeypatch.setattr(config_mod, "get_settings", lambda: live)  # the resolver's global flag
    monkeypatch.setattr(pipeline, "get_settings", lambda: live)  # the live selection's switch
    monkeypatch.setattr(
        live_mod,
        "_targets",
        lambda settings: [
            _approved_target(_SERVING_KEY, "Lagos, Lagos"),
            _approved_target(_UNRELATED_KEY, "Toronto, ON"),
        ],
    )

    def no_live_collection(self, scope):
        raise AssertionError("a live source was collected")

    monkeypatch.setattr(live_mod.LiveRssConnector, "collect", no_live_collection)
    asked: list = []
    real_fetch = pipeline.fetch_signals

    def recording_fetch(**kwargs):
        asked.append((kwargs["market"], kwargs["rss_enabled"], kwargs["takedowns"]))
        return real_fetch(**{**kwargs, "rss_enabled": False})

    monkeypatch.setattr(pipeline, "fetch_signals", recording_fetch)
    rid, loc_id = _lagos_location_scout(factory, client)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(SourceTakedown)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkspaceCapabilityOverride)
                .where(WorkspaceCapabilityOverride.workspace_id == _ws())
            )
            == 0
        )
        for key in taken_down:
            session.add(SourceTakedown(source_key=key))
        if workspace_rss_off:
            session.add(
                WorkspaceCapabilityOverride(
                    organization_id=seed_mod.sid("org"),
                    workspace_id=_ws(),
                    capability=Capability.CONNECTOR_RSS.value,
                    enabled=False,
                )
            )
        session.commit()
    try:
        _run(factory, rid)
        assert asked == [(expected, not workspace_rss_off, frozenset(taken_down))]
    finally:
        with factory() as session:
            session.execute(delete(SourceTakedown))
            session.execute(
                delete(WorkspaceCapabilityOverride).where(
                    WorkspaceCapabilityOverride.workspace_id == _ws()
                )
            )
            session.commit()
        _drop_scout_and_location(factory, rid, loc_id)


def test_e_a_scout_without_a_location_gets_no_country(factory, committed):
    with factory() as session:
        req = ScoutRequest(
            organization_id=seed_mod.sid("org"),
            workspace_id=_ws(),
            brand_id=seed_mod.sid("brand"),
            name="No location",
            status=ScoutRequestStatus.DRAFT.value,
            source_types=[],
            keywords=["coffee"],
            resolved_market="Lagos, Lagos",
            stats={},
        )
        session.add(req)
        session.flush()
        _cov, market, source_market = pipeline._coverage_rule_for(session, req)
        assert market == source_market == "Lagos, Lagos"
        session.rollback()
