"""P6-PRIV-1: the retention purge per data class (SQLite).

Durations are an operator/legal decision (O-5 / P6-D05) and are never assumed:

* **Undecided means untouched.** With no duration configured, every class is reported
  ``policy_unset``, nothing in the database changes, and the CLI exits 2.
* **Platform classes** (sessions, account tokens, invitations, audit records) are purged by the
  sweep in bounded, separately committed batches: only rows past the cut-off go, fresh rows
  stay, a second sweep deletes nothing (idempotent), a batch never exceeds its size and the
  batch budget stops a sweep early (reported incomplete).
* **Workspace classes** (signal evidence, opportunities, job history) are enqueued as one
  ``retention.purge`` job per workspace — idempotent per day — and a real worker executes them:
  each job deletes only inside its own workspace, dependents go with their root, a full batch
  chains a successor, and the chain ends.
* **The job re-checks the policy.** A job whose class is no longer configured, or whose cut-off
  is later than the current duration allows, is refused (non-retryable ``policy``) and
  deletes nothing.
* **Dry run** counts and changes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert

from app.audit.models import AuditLog
from app.auth.models import AuthSession
from app.core.config import Settings, get_settings
from app.intelligence.records import SignalIntelligenceRecord
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.status import JobErrorCode, JobExecutionError, JobType
from app.jobs.store import DurableJobStore
from app.jobs.worker import JobRunner
from app.opportunities.models import Opportunity, OpportunityScore
from app.organizations.models import OrganizationInvitation
from app.privacy import cli, retention
from app.privacy.inventory import PLATFORM_CLASSES, WORKSPACE_CLASSES, RetentionClass
from app.signals.models import NormalizedSignal, RawSignal, SignalCluster
from app.tests._privacy_support import (
    ALICE,
    ORG_A,
    ORG_B,
    WS_A,
    WS_A2,
    WS_B,
    PrivacyEnv,
    privacy_world,
)

NOW = datetime.now(UTC)


@pytest.fixture
def env(tmp_path, monkeypatch) -> Iterator[PrivacyEnv]:
    settings = get_settings()
    for data_class in RetentionClass:
        monkeypatch.setattr(settings, data_class.setting_name, None)
    monkeypatch.setattr(settings, "retention_purge_batch_size", 500)
    monkeypatch.setattr(settings, "retention_purge_max_batches", 100)
    with privacy_world(tmp_path / "retention.db") as e:
        yield e


def _set(monkeypatch, days: int = 30, classes=tuple(RetentionClass)) -> None:
    for data_class in classes:
        monkeypatch.setattr(get_settings(), data_class.setting_name, days)


def _sweep(env: PrivacyEnv, **kw):
    return {o.data_class: o for o in retention.sweep(env.session, **kw)}


def _plant_audit(env: PrivacyEnv, n: int, *, age_days: int) -> list[str]:
    at = NOW - timedelta(days=age_days)
    ids = [f"old-audit-{i}" for i in range(n)]
    with env.engine.begin() as conn:
        for audit_id in ids:
            conn.execute(
                insert(AuditLog.__table__).values(
                    id=audit_id, organization_id=ORG_B, action="x", context={},
                    created_at=at, updated_at=at,
                )
            )
    return ids


# --------------------------------------------------------------------------- #
# Undecided
# --------------------------------------------------------------------------- #
def test_with_no_duration_configured_nothing_is_purged(env: PrivacyEnv, capsys):
    before = env.snapshot()
    outcomes = _sweep(env)
    assert {c: o.status for c, o in outcomes.items()} == {
        c.value: "policy_unset" for c in RetentionClass
    }
    assert env.snapshot() == before
    assert cli.main(["sweep"]) == cli.EXIT_POLICY_UNSET
    printed = json.loads(capsys.readouterr().out)
    assert {c["status"] for c in printed["classes"]} == {"policy_unset"}
    assert env.snapshot() == before


def test_one_undecided_class_is_skipped_while_another_runs(env: PrivacyEnv, monkeypatch):
    _set(monkeypatch, classes=(RetentionClass.AUDIT_LOGS,))
    _plant_audit(env, 2, age_days=90)
    outcomes = _sweep(env, classes=(RetentionClass.AUDIT_LOGS, RetentionClass.AUTH_SESSIONS))
    assert outcomes["audit_logs"].status == "purged"
    assert outcomes["auth_sessions"].status == "policy_unset"


# --------------------------------------------------------------------------- #
# Platform classes
# --------------------------------------------------------------------------- #
def test_platform_classes_purge_only_rows_past_the_cutoff(env: PrivacyEnv, monkeypatch):
    _set(monkeypatch, classes=tuple(PLATFORM_CLASSES))
    env.auth(ALICE)  # a fresh session: kept
    with env.engine.begin() as conn:
        conn.execute(
            insert(AuthSession.__table__).values(
                id="old-session", user_id=ALICE, created_at=NOW - timedelta(days=100),
                expires_at=NOW - timedelta(days=99),
            )
        )
        conn.execute(
            insert(OrganizationInvitation.__table__).values(
                id="fresh-invitation", organization_id=ORG_A, email="new@privacy.example",
                role="viewer", token_hash="9" * 64, expires_at=NOW + timedelta(days=3),
                created_at=NOW, updated_at=NOW,
            )
        )
    old_audit = _plant_audit(env, 3, age_days=31 + 1)
    fresh_audit = {r["id"] for r in env.rows(AuditLog)} - set(old_audit)

    outcomes = _sweep(env, classes=tuple(sorted(PLATFORM_CLASSES)))

    assert {o.status for o in outcomes.values()} == {"purged"}
    assert all(o.complete for o in outcomes.values())
    assert outcomes["auth_sessions"].deleted == {"auth_sessions": 1}
    assert outcomes["account_tokens"].deleted == {
        "email_verification_tokens": 1, "password_reset_tokens": 1,
    }
    # The three seeded invitations are accepted or expired long ago; the fresh one stays.
    assert outcomes["invitations"].deleted == {"organization_invitations": 3}
    assert outcomes["audit_logs"].deleted == {"audit_logs": 3}
    assert env.count(AuthSession, AuthSession.id == "old-session") == 0
    assert env.count(AuthSession) == 1
    assert [r["id"] for r in env.rows(OrganizationInvitation)] == ["fresh-invitation"]
    assert {r["id"] for r in env.rows(AuditLog)} == fresh_audit
    # Idempotent: a second sweep finds nothing more.
    again = _sweep(env, classes=tuple(sorted(PLATFORM_CLASSES)))
    assert all(o.deleted == {} for o in again.values())


def test_batches_are_bounded_and_the_budget_stops_early(env: PrivacyEnv, monkeypatch):
    _set(monkeypatch, classes=(RetentionClass.AUDIT_LOGS,))
    _plant_audit(env, 5, age_days=90)
    monkeypatch.setattr(get_settings(), "retention_purge_batch_size", 2)
    monkeypatch.setattr(get_settings(), "retention_purge_max_batches", 2)
    first = _sweep(env, classes=(RetentionClass.AUDIT_LOGS,))["audit_logs"]
    assert (first.batches, first.complete, first.deleted) == (2, False, {"audit_logs": 4})
    second = _sweep(env, classes=(RetentionClass.AUDIT_LOGS,))["audit_logs"]
    assert (second.batches, second.complete, second.deleted) == (1, True, {"audit_logs": 1})


def test_dry_run_counts_and_changes_nothing(env: PrivacyEnv, monkeypatch):
    _set(monkeypatch)
    _plant_audit(env, 2, age_days=90)
    before = env.snapshot()
    outcomes = _sweep(env, dry_run=True)
    assert env.snapshot() == before
    assert outcomes["audit_logs"].candidates == 2
    assert outcomes["signal_evidence"].candidates == 8  # 4 raw signals + 4 clusters
    assert {o.status for o in outcomes.values()} == {"dry_run"}


# --------------------------------------------------------------------------- #
# Workspace classes, through the job store and a real worker
# --------------------------------------------------------------------------- #
def _purge_jobs(env: PrivacyEnv) -> list[dict]:
    return env.rows(Job, Job.job_type == JobType.RETENTION_PURGE.value)


def _drain(env: PrivacyEnv, limit: int = 50) -> int:
    runner = JobRunner(
        settings=Settings(_env_file=None), session_factory=env.session,
        store=DurableJobStore(), clock=lambda: datetime.now(UTC),
    )
    ran = 0
    for _ in range(limit):
        if not runner.poll_once(worker_id="w-retention"):
            return ran
        ran += 1
    raise AssertionError("the purge chain did not end within the poll budget")


def test_workspace_classes_run_as_one_job_per_workspace(env: PrivacyEnv, monkeypatch):
    _set(monkeypatch, classes=(RetentionClass.SIGNAL_EVIDENCE,))
    outcome = _sweep(env, classes=(RetentionClass.SIGNAL_EVIDENCE,))["signal_evidence"]
    assert (outcome.status, outcome.jobs_enqueued) == ("enqueued", 4)
    jobs = _purge_jobs(env)
    assert sorted(j["workspace_id"] for j in jobs) == sorted([WS_A, WS_A2, WS_B, "prv-ws-c"])
    # Idempotent per day: the same sweep again enqueues no new row.
    _sweep(env, classes=(RetentionClass.SIGNAL_EVIDENCE,))
    assert len(_purge_jobs(env)) == 4

    assert _drain(env) == 4

    assert {j["status"] for j in _purge_jobs(env)} == {"succeeded"}
    for model in (RawSignal, NormalizedSignal, SignalCluster, SignalIntelligenceRecord):
        assert env.count(model) == 0, model.__tablename__
    # Opportunities are a different class: kept (their cluster reference cleared).
    assert env.count(Opportunity) == 4
    assert {r["cluster_id"] for r in env.rows(Opportunity)} == {None}
    summary = next(j for j in _purge_jobs(env) if j["workspace_id"] == WS_A)["result_summary"]
    assert summary["data_class"] == "signal_evidence" and summary["complete"] is True
    assert summary["deleted"]["raw_signals"] == 1


def test_a_job_purges_only_its_own_workspace_and_chains_when_full(env: PrivacyEnv, monkeypatch):
    _set(monkeypatch, classes=(RetentionClass.OPPORTUNITIES,))
    monkeypatch.setattr(get_settings(), "retention_purge_batch_size", 1)
    cutoff = retention.cutoff_for(NOW, 30)
    with env.session() as s:
        retention.enqueue_purge(
            s, organization_id=ORG_A, workspace_id=WS_A,
            data_class=RetentionClass.OPPORTUNITIES, cutoff=cutoff,
        )
        s.commit()

    assert _drain(env) == 2  # a full batch of one chains exactly one successor

    assert env.count(Opportunity, Opportunity.workspace_id == WS_A) == 0
    assert env.count(OpportunityScore, OpportunityScore.workspace_id == WS_A) == 0
    for ws in (WS_A2, WS_B):
        assert env.count(Opportunity, Opportunity.workspace_id == ws) == 1
    links = sorted(_purge_jobs(env), key=lambda j: j["payload"]["sequence"])
    assert [j["payload"]["sequence"] for j in links] == [0, 1]
    assert [j["result_summary"]["successor_enqueued"] for j in links] == [True, False]


def test_the_job_refuses_when_the_policy_is_unset_or_shorter(env: PrivacyEnv, monkeypatch):
    context = ExecutionContext(organization_id=ORG_A, workspace_id=WS_A)
    cutoff = retention.cutoff_for(NOW, 30)
    payload = {"data_class": "opportunities", "cutoff": cutoff.isoformat(), "sequence": 0}
    with env.session() as s:
        with pytest.raises(JobExecutionError) as unset:
            retention.run_purge_job(s, context=context, payload=payload, now=NOW)
        assert unset.value.error.code is JobErrorCode.POLICY
        _set(monkeypatch, days=60, classes=(RetentionClass.OPPORTUNITIES,))
        with pytest.raises(JobExecutionError) as longer:
            retention.run_purge_job(s, context=context, payload=payload, now=NOW)
        assert longer.value.error.code is JobErrorCode.POLICY
        s.rollback()
    assert env.count(Opportunity) == 4


@pytest.mark.parametrize(
    "payload",
    [
        {"data_class": "audit_logs", "cutoff": "2020-01-01T00:00:00+00:00", "sequence": 0},
        {"data_class": "nope", "cutoff": "2020-01-01T00:00:00+00:00", "sequence": 0},
        {"data_class": "opportunities", "cutoff": "not-a-date", "sequence": 0},
        {"data_class": "opportunities", "cutoff": "2020-01-01T00:00:00+00:00", "sequence": -1},
        {"data_class": "opportunities", "sequence": 0},
    ],
)
def test_malformed_payloads_are_refused(env: PrivacyEnv, payload):
    context = ExecutionContext(organization_id=ORG_A, workspace_id=WS_A)
    with env.session() as s, pytest.raises(JobExecutionError) as exc:
        retention.run_purge_job(s, context=context, payload=payload, now=NOW)
    assert exc.value.error.code is JobErrorCode.VALIDATION


def test_job_history_keeps_running_and_recent_jobs(env: PrivacyEnv, monkeypatch):
    _set(monkeypatch, classes=(RetentionClass.JOB_HISTORY,))
    _sweep(env, classes=(RetentionClass.JOB_HISTORY,))
    _drain(env)
    seeded = {"a1-job", "a2-job", "b1-job", "c1-job"}
    remaining = {j["id"] for j in env.rows(Job)}
    assert not remaining & seeded  # the seeded terminal jobs were 400 days old
    assert len(remaining) == 4  # the four purge jobs themselves: recent, kept


def test_every_class_is_partitioned_between_sweep_and_job():
    assert {c.value for c in WORKSPACE_CLASSES} == {
        "signal_evidence", "opportunities", "job_history"
    }
