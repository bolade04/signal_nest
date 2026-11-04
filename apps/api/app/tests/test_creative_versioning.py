"""Project Phase 5C-W4 adversarial battery, item 1: draft versioning.

A draft is never updated: a revision is a new row whose ``draft_version`` is the brief's
highest plus one and whose ``parent_draft_id`` names the version it revised (Phase 5C plan §2).
Generation is a durable job keyed so a repeated submit joins the job already queued
(:func:`app.creative.service.request_generation`).

* **Allocation under concurrency** (PostgreSQL): concurrent generations of one brief in ONE
  channel — different requesters and parents — and across channels, briefs and workspaces
  take distinct, contiguous versions per brief, one draft and one review each.
* **Contiguity**: a withheld output, a model failure or a refused ceiling between two drafts
  leaves no gap and stores nothing.
* **Revision chains**: chains and branches record each parent, and never change an ancestor;
  a parent from another brief, another workspace or another organization, or one deleted after
  the request, is refused by the job itself (the route's 404s are in ``test_creative_isolation``).
* **Idempotency**: two different requests (requester, channel or parent) never join one job;
  one request never creates two drafts — it joins its job while that job is pending, running
  or waiting for a retry, racing identical requests make one job (also right after a failure),
  and an attempt that lost its lease is rolled back with its draft.
* **A failed job never blocks a retry**, whatever its terminal state (failed, cancelled,
  dead-lettered), however many times.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, update

from app.core.config import get_settings
from app.creative import service as creative_service
from app.creative.models import ContentReview, CreativeDraft
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.service import enqueue_job
from app.jobs.status import JobType
from app.llm import service as service_module
from app.llm.mock import MockLLMProvider
from app.llm.models import LLMUsageEvent
from app.tests._creative_support import CEILINGS, CreativeWorld, creative_world

EDITORS = ("owner", "admin", "marketer")
CHANNELS = ("social_post", "email", "search_ad", "sms", "landing_page")
GENEROUS = {**CEILINGS, "llm_max_calls_per_workspace_per_day": 10_000}


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Iterator[CreativeWorld]:
    with creative_world(tmp_path_factory.mktemp("w4-ver") / "ver.db", "w4ver") as w:
        yield w


@pytest.fixture(autouse=True)
def ceilings(monkeypatch) -> None:
    for name, value in GENEROUS.items():
        monkeypatch.setattr(get_settings(), name, value)


def _drafts(world: CreativeWorld, brief_id: str) -> list[dict]:
    return sorted(world.rows(CreativeDraft, brief_id=brief_id), key=lambda d: d["draft_version"])


def _job_id(r) -> str:
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def _make_due(world: CreativeWorld, job_id: str) -> None:
    table = Job.__table__
    with world.witness.begin() as conn:
        conn.execute(update(table).where(table.c.id == job_id)
                     .values(available_at=datetime.now(UTC) - timedelta(seconds=1)))


class _Spy:
    def __init__(self, real, on_call=None):
        self.real, self.on_call, self.calls = real, on_call, 0
        self.name, self.model = real.name, real.model

    def generate(self, request, prompt_name, prompt_version):
        self.calls += 1
        if self.on_call is not None:
            self.on_call(self.calls)
        return self.real.generate(request, prompt_name, prompt_version)


def _spy(monkeypatch, on_call=None) -> _Spy:
    spy = _Spy(service_module.llm_service._provider, on_call)
    monkeypatch.setattr(service_module.llm_service, "_provider", spy)
    return spy


# --------------------------------------------------------------------------- #
# Idempotency: different requests never join; one request never makes two drafts
# --------------------------------------------------------------------------- #
def test_different_requests_never_join_one_job_and_each_job_makes_one_draft(world):
    """Every (requester, channel, parent) combination is its own job — 30 requests, 30 jobs,
    30 drafts — while repeating any of them joins the job it already has."""
    ws = world.workspace("matrix")
    brief_id = world.seed_brief(ws)
    assert world.generated(brief_id, ws=ws)["status"] == "succeeded"
    [v1] = _drafts(world, brief_id)
    requests: dict[tuple, str] = {}
    for role in EDITORS:
        for channel in CHANNELS:
            for parent in (None, v1["id"]):
                body = {"channel": channel, **({"parent_draft_id": parent} if parent else {})}
                job_id = _job_id(world.generate(brief_id, role, ws=ws, **body))
                assert _job_id(world.generate(brief_id, role, ws=ws, **body)) == job_id
                requests[(role, channel, parent)] = job_id
    assert len(set(requests.values())) == len(requests) == 30
    world.drain(limit=40)
    made = {}
    for (role, channel, parent), job_id in requests.items():
        job = world.job(job_id)
        assert job["status"] == "succeeded", (role, channel, parent, job["last_error_summary"])
        draft = world.row(CreativeDraft, job["result_summary"]["draft_id"])
        assert (draft["channel"], draft["parent_draft_id"], draft["created_by"]) == (
            channel, parent, world.users[role]
        )
        made[job_id] = draft["id"]
    assert len(set(made.values())) == 30
    assert [d["draft_version"] for d in _drafts(world, brief_id)] == list(range(1, 32))
    assert world.count(ContentReview, workspace_id=ws) == 31


def test_the_same_request_on_another_brief_or_workspace_is_another_job(world):
    ws, other = world.workspace("key scope a"), world.workspace("key scope b")
    briefs = [world.seed_brief(ws), world.seed_brief(ws), world.seed_brief(other)]
    jobs = [
        _job_id(world.generate(b, ws=w, channel="email"))
        for b, w in zip(briefs, (ws, ws, other), strict=True)
    ]
    assert len(set(jobs)) == 3
    keys = [world.job(j)["idempotency_key"] for j in jobs]
    assert len(set(keys)) == 3
    world.drain()
    assert all(world.job(j)["status"] == "succeeded" for j in jobs)


def test_a_request_repeated_while_its_job_runs_joins_it(world, monkeypatch):
    """The identical request, made while the job's model call is in flight (status
    ``running``), answers that job: one request, one draft."""
    ws = world.workspace("join running")
    brief_id = world.seed_brief(ws)
    seen: dict = {}

    def during_call(n: int) -> None:
        if n == 1:
            seen["status"] = world.job(seen["job"])["status"]
            seen["again"] = _job_id(world.generate(brief_id, ws=ws, channel="sms"))

    _spy(monkeypatch, during_call)
    seen["job"] = _job_id(world.generate(brief_id, ws=ws, channel="sms"))
    world.drain()
    assert seen["status"] == "running"
    assert seen["again"] == seen["job"]
    assert world.job(seen["job"])["status"] == "succeeded"
    assert world.count(Job, workspace_id=ws) == 1
    assert len(_drafts(world, brief_id)) == 1


def test_a_request_repeated_while_its_job_waits_for_a_retry_joins_it(world, monkeypatch):
    ws = world.workspace("join retry")
    brief_id = world.seed_brief(ws)
    settings = service_module.llm_service._settings
    monkeypatch.setattr(settings, "llm_max_retries", 0)
    marker = {"value": "timeout"}
    real = creative_service.generation_context

    def flaky(db, brief, channel, now):
        context = real(db, brief, channel, now)
        if marker["value"]:
            context.inputs["_simulate"] = marker["value"]
        return context

    monkeypatch.setattr(creative_service, "generation_context", flaky)
    job_id = _job_id(world.generate(brief_id, ws=ws))
    world.drain()
    assert world.job(job_id)["status"] == "retry_wait"
    assert _job_id(world.generate(brief_id, ws=ws)) == job_id  # joined, not a second job
    marker["value"] = None
    _make_due(world, job_id)
    world.drain()
    assert world.job(job_id)["status"] == "succeeded"
    assert world.count(Job, workspace_id=ws) == 1
    assert len(_drafts(world, brief_id)) == 1


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "dead_lettered"])
def test_a_failed_job_never_blocks_a_retry_whatever_its_terminal_state(world, terminal):
    """A job that ended without a draft — failed, cancelled or dead-lettered — is not joined:
    the identical request gets a new job, again and again, and the first that succeeds makes
    the one draft."""
    ws = world.workspace(f"retry after {terminal}")
    brief_id = world.seed_brief(ws)
    jobs = []
    for attempt in range(3):
        job_id = _job_id(world.generate(brief_id, ws=ws))
        assert job_id not in jobs
        jobs.append(job_id)
        assert world.job(job_id)["idempotency_key"].endswith(f":{attempt}")
        if attempt < 2:
            if terminal == "cancelled":
                r = world.send("POST", f"/workspaces/{ws}/jobs/{job_id}/cancel", "marketer")
                assert r.status_code == 200, r.text
            elif terminal == "failed":
                world.enable(ws, enabled=False)
                world.drain()
                world.enable(ws)
            else:
                with world.witness.begin() as conn:
                    conn.execute(update(Job.__table__).where(Job.__table__.c.id == job_id)
                                 .values(status="dead_lettered"))
            assert world.job(job_id)["status"] == terminal
    world.drain()
    assert world.job(jobs[-1])["status"] == "succeeded"
    assert len(_drafts(world, brief_id)) == 1


def test_an_attempt_that_lost_its_lease_is_rolled_back_with_its_draft(world, monkeypatch):
    """At-least-once delivery: while the first attempt's model call is in flight, its lease is
    taken over (a reclaim rotates the token). That attempt's completion is fenced, so its
    draft, review and audit row roll back (its usage row is kept); the job's next run makes
    the one draft."""
    ws = world.workspace("lease lost")
    brief_id = world.seed_brief(ws)
    job_id: dict = {}

    def steal_lease(n: int) -> None:
        if n == 1:
            table = Job.__table__
            with world.witness.begin() as conn:
                conn.execute(update(table).where(table.c.id == job_id["id"])
                             .values(lease_token="stolen-by-another-worker",
                                     worker_id="another-worker"))

    _spy(monkeypatch, steal_lease)
    job_id["id"] = _job_id(world.generate(brief_id, ws=ws))
    world.drain()
    assert _drafts(world, brief_id) == []
    assert world.count(ContentReview, workspace_id=ws) == 0
    assert world.count(LLMUsageEvent, job_id=job_id["id"]) == 1  # the lost attempt's call
    table = Job.__table__
    with world.witness.begin() as conn:  # the new owner hands the job back to the queue
        conn.execute(update(table).where(table.c.id == job_id["id"]).values(
            status="pending", worker_id=None, lease_token=None, lease_expires_at=None,
            available_at=datetime.now(UTC) - timedelta(seconds=1),
        ))
    world.drain()
    assert world.job(job_id["id"])["status"] == "succeeded"
    [draft] = _drafts(world, brief_id)
    assert draft["draft_version"] == 1
    assert world.count(LLMUsageEvent, job_id=job_id["id"]) == 2


# --------------------------------------------------------------------------- #
# Contiguity: failures between drafts leave no gap
# --------------------------------------------------------------------------- #
def test_versions_stay_contiguous_through_withheld_failed_and_refused_attempts(world, monkeypatch):
    ws = world.workspace("contiguous")
    brief_id = world.seed_brief(ws)
    assert world.generated(brief_id, ws=ws)["status"] == "succeeded"
    original = MockLLMProvider._creative

    def failing(kind):
        def creative(self, inputs, rng):
            if kind == "withheld":
                return {"headline": "", "body": "Visit www.example.com today", "call_to_action": ""}
            return original(self, inputs, rng)
        return creative

    monkeypatch.setattr(MockLLMProvider, "_creative", failing("withheld"))
    assert world.generated(brief_id, ws=ws, channel="sms")["last_error_summary"] == (
        "withheld:link"
    )
    monkeypatch.setattr(MockLLMProvider, "_creative", original)
    real = creative_service.generation_context

    def refusing(db, brief, channel, now):
        context = real(db, brief, channel, now)
        context.inputs["_simulate"] = "refusal"
        return context

    monkeypatch.setattr(creative_service, "generation_context", refusing)
    assert world.generated(brief_id, ws=ws, channel="email")["last_error_summary"] == (
        "model_failed:LLMRefusalError"
    )
    monkeypatch.setattr(creative_service, "generation_context", real)
    # A job refused by its ceilings (one unset after the request): no call, nothing stored.
    refused = _job_id(world.generate(brief_id, ws=ws, channel="search_ad"))
    monkeypatch.setattr(get_settings(), "llm_max_calls_per_workspace_per_day", None)
    world.drain()
    assert world.job(refused)["last_error_summary"] == "ceilings_unset"
    monkeypatch.setattr(get_settings(), "llm_max_calls_per_workspace_per_day", 10_000)
    assert world.generated(brief_id, ws=ws, channel="landing_page")["status"] == "succeeded"
    assert [(d["draft_version"], d["channel"]) for d in _drafts(world, brief_id)] == [
        (1, "social_post"), (2, "landing_page")
    ]


# --------------------------------------------------------------------------- #
# Revision chains
# --------------------------------------------------------------------------- #
def test_a_revision_chain_records_each_parent_and_never_changes_an_ancestor(world):
    ws = world.workspace("chain")
    brief_id = world.seed_brief(ws)
    world.generated(brief_id, ws=ws)
    [v1] = _drafts(world, brief_id)
    plan = [(v1, "email"), (None, "sms"), (v1, "search_ad"), (None, "landing_page")]
    made = [v1]
    for parent, channel in plan:
        parent = parent or made[-1]
        before = {d["id"]: d for d in _drafts(world, brief_id)}
        job = world.generated(brief_id, ws=ws, channel=channel, parent_draft_id=parent["id"])
        assert job["status"] == "succeeded", job["last_error_summary"]
        after = {d["id"]: d for d in _drafts(world, brief_id)}
        assert {k: after[k] for k in before} == before  # every ancestor is untouched
        made.append(world.row(CreativeDraft, job["result_summary"]["draft_id"]))
    chain = [(d["draft_version"], d["parent_draft_id"]) for d in _drafts(world, brief_id)]
    ids = [d["id"] for d in made]
    assert chain == [(1, None), (2, ids[0]), (3, ids[1]), (4, ids[0]), (5, ids[3])]
    for d in _drafts(world, brief_id):
        [review] = world.rows(ContentReview, draft_id=d["id"])
        assert review["draft_version"] == d["draft_version"]
    listed = world.send("GET", f"/workspaces/{ws}/briefs/{brief_id}/drafts", "viewer").json()
    assert [(i["draft_version"], i["parent_draft_id"]) for i in listed["items"]] == chain[::-1]


def test_a_parent_from_elsewhere_or_gone_is_refused_by_the_job(world, monkeypatch):
    """The job re-checks the parent itself: a draft of another brief in a sibling workspace or
    in another organization, or the brief's own draft deleted after the request, is
    ``parent_not_found`` before any model call."""
    spy = _spy(monkeypatch)
    ws, sibling = world.workspace("parent own"), world.workspace("parent sibling")
    world.enable(world.ws_b)
    brief_id = world.seed_brief(ws)
    world.generated(brief_id, ws=ws)
    [own] = _drafts(world, brief_id)
    sib_brief = world.seed_brief(sibling)
    world.generated(sib_brief, ws=sibling)
    [sib] = _drafts(world, sib_brief)
    b_brief = world.seed_brief(world.ws_b)
    r = world.send("POST", f"/workspaces/{world.ws_b}/briefs/{b_brief}/drafts", body={
        "channel": "email"}, token=world.owner_b)
    world.drain()
    b_draft = world.job(_job_id(r))["result_summary"]["draft_id"]
    # A request the route accepted (the own parent), whose parent is then deleted.
    gone = _job_id(world.generate(brief_id, ws=ws, channel="sms", parent_draft_id=own["id"]))
    jobs = {"gone": gone}
    with world.session() as s:
        for label, parent in (("sibling", sib["id"]), ("other_org", b_draft)):
            jobs[label] = enqueue_job(
                s, job_type=JobType.CREATIVE_GENERATE,
                context=ExecutionContext(organization_id=world.org, workspace_id=ws),
                payload={"brief_id": brief_id, "channel": "email", "parent_draft_id": parent,
                         "requested_by": world.users["marketer"]},
            ).id
    with world.witness.begin() as conn:
        conn.execute(delete(CreativeDraft.__table__)
                     .where(CreativeDraft.__table__.c.id == own["id"]))
    spy.calls = 0
    world.drain()
    for label, job_id in jobs.items():
        job = world.job(job_id)
        assert (job["status"], job["last_error_code"], job["last_error_summary"]) == (
            "failed", "validation", "parent_not_found"
        ), label
    assert spy.calls == 0
    assert _drafts(world, brief_id) == []


# --------------------------------------------------------------------------- #
# PostgreSQL: allocation and idempotency under real concurrency
# --------------------------------------------------------------------------- #
def _run_threads(targets, timeout: float = 90.0) -> None:
    threads = [threading.Thread(target=t, daemon=True) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    assert not any(t.is_alive() for t in threads), "a worker did not finish"


def test_postgres_concurrent_generations_take_distinct_contiguous_versions_per_brief(
    monkeypatch,
):
    """One brief, ONE channel: six concurrent jobs (three requesters, with and without a
    parent) after version 1; plus a second brief of the same workspace and a brief of the
    sibling workspace generating across channels at the same time. Four workers drain them
    concurrently: every brief is numbered 1..n with no gap or duplicate, and every job made
    exactly one draft with one review.

    The allocation window is held open (review W21-REV-5CW4 r1 MINOR-1): every allocation
    holds 0.3 s after it has read ``max + 1`` (``creative_service._next_version`` wrapped,
    still inside the attempt's transaction and locks), so attempts of one brief that are not
    serialized are certain to meet there (the unique key then fails one as
    ``version_conflict``). Under the generation locks the hold only adds latency, and the
    recorded windows of one brief never overlap.

    The provider is priced (an offline fake with the real adapter's ``prepare``), so the
    meter writes the budget ledger on its own short-lived session — the shared-ledger path a
    real provider takes. On the mock's same-session path ``meter.flush`` updates the
    workspace's ``llm_budget_periods`` row inside the attempt's transaction, *before* the
    allocation, and that row lock alone serializes every attempt of the workspace until it
    commits: a test on the mock passes with every generation lock removed (the reviewer's
    RV10), so it cannot tell whether the locks hold."""
    import time

    from app.llm.base import LLMResponse, LLMUsage
    from app.tests._creative_support import postgres_creative_world

    for name, value in GENEROUS.items():
        monkeypatch.setattr(get_settings(), name, value)

    class _Priced:
        name, model = "anthropic", "claude-sonnet-4-6"

        def prepare(self, request):
            from app.llm.providers_real import AnthropicProvider

            return AnthropicProvider("k", self.model, 5).prepare(request)

        def generate(self, request, prompt_name, prompt_version):
            out = MockLLMProvider().generate(request, prompt_name, prompt_version).output
            return LLMResponse(task=request.task, provider=self.name, model=self.model,
                               output=out, usage=LLMUsage(input_tokens=1000, output_tokens=200),
                               prompt_name=prompt_name, prompt_version=prompt_version)

    prices = {_Priced.model: [3.0, 15.0]}
    for settings in (get_settings(), service_module.llm_service._settings):
        monkeypatch.setattr(settings, "llm_prices_usd_per_mtok", prices)
    monkeypatch.setattr(get_settings(), "llm_workspace_monthly_budget_usd", 50.0)
    monkeypatch.setattr(service_module.llm_service, "_provider", _Priced())
    windows: list[tuple[str, float, float]] = []
    real_next_version = creative_service._next_version

    def held(db, brief_row):
        version = real_next_version(db, brief_row)
        opened = time.monotonic()
        time.sleep(0.3)  # the window between reading max + 1 and the insert, widened
        windows.append((brief_row.id, opened, time.monotonic()))
        return version

    with postgres_creative_world("w4alloc") as world:
        world.enable()
        world.enable(world.ws2)
        brief = world.seed_brief()
        assert world.generated(brief)["status"] == "succeeded"
        [v1] = world.rows(CreativeDraft, brief_id=brief)
        monkeypatch.setattr(creative_service, "_next_version", held)
        second, sibling = world.seed_brief(), world.seed_brief(world.ws2)
        jobs: dict[str, list[str]] = {brief: [], second: [], sibling: []}
        for role in EDITORS:
            for parent in (None, v1["id"]):
                body = {"channel": "email", **({"parent_draft_id": parent} if parent else {})}
                jobs[brief].append(_job_id(world.generate(brief, role, **body)))
        for channel in CHANNELS[:4]:
            jobs[second].append(_job_id(world.generate(second, channel=channel)))
            jobs[sibling].append(_job_id(world.generate(sibling, ws=world.ws2, channel=channel)))
        _run_threads([world.drain] * 4)
        for brief_id, job_ids in jobs.items():
            statuses = [world.job(j)["status"] for j in job_ids]
            assert statuses == ["succeeded"] * len(job_ids), (brief_id, statuses)
            made = {world.job(j)["result_summary"]["draft_id"] for j in job_ids}
            assert len(made) == len(job_ids)
        versions = {
            b: sorted(d["draft_version"] for d in world.rows(CreativeDraft, brief_id=b))
            for b in jobs
        }
        assert versions == {brief: list(range(1, 8)), second: [1, 2, 3, 4],
                            sibling: [1, 2, 3, 4]}
        assert world.count(ContentReview) == 15
        assert world.count(CreativeDraft) == 15
        assert len(windows) == 14  # every concurrent attempt allocated once, none retried
        for brief_id in jobs:
            spans = sorted((a, b) for w, a, b in windows if w == brief_id)
            for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
                assert start >= end, f"two allocations of brief {brief_id} overlapped"


def test_postgres_racing_identical_requests_make_one_job_then_one_draft_even_after_a_failure(
    monkeypatch,
):
    """Six identical requests released together answer one job; after that job fails, six
    identical requests released together again answer one NEW job (they all compute the same
    retry suffix; the store absorbs the insert race); one draft results."""
    from app.tests._creative_support import postgres_creative_world

    for name, value in GENEROUS.items():
        monkeypatch.setattr(get_settings(), name, value)
    with postgres_creative_world("w4race") as world:
        world.enable()
        brief = world.seed_brief()

        def race() -> set[str]:
            barrier = threading.Barrier(6)
            answers: list = []

            def post():
                barrier.wait(10)
                answers.append(world.generate(brief, channel="email"))

            _run_threads([post] * 6, timeout=60)
            assert [r.status_code for r in answers] == [202] * 6, [r.text for r in answers]
            return {r.json()["job_id"] for r in answers}

        [first] = race()
        world.enable(enabled=False)
        world.drain()
        assert world.job(first)["status"] == "failed"
        world.enable()
        [second] = race()
        assert second != first
        assert world.job(second)["idempotency_key"].endswith(":1")
        world.drain()
        assert world.job(second)["status"] == "succeeded"
        assert world.count(Job, workspace_id=world.ws) == 2
        assert len(world.rows(CreativeDraft, brief_id=brief)) == 1
