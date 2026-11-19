"""Project Phase 5E (lane 5E-W4): the notifications purge cannot run without a policy (exit 6).

Operator ruling D-N4 item 30 (2026-11-12), verbatim: "accept the retention-class structure.
Implement notifications retention with its duration unset and fail closed. O-5 durations
remain unruled; do not invent them." Interface sheet B: the class is
``RetentionClass.NOTIFICATIONS``, kept out of ``WORKSPACE_CLASSES``, "so ``enqueue_purge``
raises for it; no ``JobType`` member; the sweep refuses it with ``policy_unset`` while unset".

Proven here:

* **Unset means untouched.** ``retention_notifications_days`` defaults to ``None``; the sweep
  reports ``policy_unset`` for the class (also in a dry run, and in a sweep of every class)
  and deletes nothing, however old the rows are.
* **It cannot be dispatched.** ``enqueue_purge`` raises for the class (duration set or not)
  and writes no job; a ``retention.purge`` job naming it is refused by the handler as a
  validation error and deletes nothing; no ``JobType`` member exists for it and no 5E module
  names ``JobType``.
* **Production refuses to start** while ``retention_notifications_days`` is unset; development
  and staging accept it unset.
* The control: once a duration is set, the sweep (the platform path) purges exactly the rows
  past the cut-off and keeps the fresh ones, and a second sweep deletes nothing.

The world is the 5C harness on SQLite and, with ``TEST_POSTGRES_URL``, PostgreSQL at head.
"""

from __future__ import annotations

import ast
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert

from app.core.config import Settings, get_settings
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.status import JobErrorCode, JobExecutionError, JobType
from app.notifications.models import Notification
from app.privacy import retention
from app.privacy.inventory import PLATFORM_CLASSES, TABLES, WORKSPACE_CLASSES, RetentionClass
from app.tests._creative_support import CreativeWorld, creative_world, postgres_creative_world
from app.tests._platform_support import production_vector_kwargs

NOTIFICATIONS = RetentionClass.NOTIFICATIONS
NOW = datetime.now(UTC)


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def world(request, tmp_path_factory) -> Iterator[CreativeWorld]:
    if request.param == "sqlite":
        with creative_world(tmp_path_factory.mktemp("w4rp") / "r.db", "w4rp") as w:
            yield w
        return
    with postgres_creative_world("w4rp") as w:  # skips unless TEST_POSTGRES_URL is set
        yield w


@pytest.fixture(autouse=True)
def every_duration_unset(monkeypatch) -> None:
    """The shipped state of every class (O-5 unruled), whatever the environment says."""
    settings = get_settings()
    for data_class in RetentionClass:
        monkeypatch.setattr(settings, data_class.setting_name, None)
    monkeypatch.setattr(settings, "retention_purge_batch_size", 500)
    monkeypatch.setattr(settings, "retention_purge_max_batches", 10)


@pytest.fixture
def ws(world: CreativeWorld, request) -> str:
    return world.workspace(f"r {request.node.name[:40]}", enabled=False)


def plant(world: CreativeWorld, ws: str, *, age_days: int, n: int = 2) -> list[str]:
    """``n`` notifications for the marketer, created ``age_days`` ago (direct rows)."""
    at = NOW - timedelta(days=age_days)
    ids = [uuid.uuid4().hex for _ in range(n)]
    with world.engine.begin() as conn:
        for nid in ids:
            conn.execute(insert(Notification.__table__).values(
                id=nid, organization_id=world.org, workspace_id=ws,
                user_id=world.users["marketer"], event_code="brief.created",
                subject_id=uuid.uuid4().hex, created_at=at,
            ))  # fmt: skip
    return ids


def present(world: CreativeWorld, ids: list[str]) -> list[str]:
    return [i for i in ids if world.row(Notification, i) is not None]


# --------------------------------------------------------------------------- #
# Unset means untouched
# --------------------------------------------------------------------------- #
def test_the_duration_is_unset_by_default():
    assert Settings(_env_file=None).retention_notifications_days is None
    assert NOTIFICATIONS.setting_name == "retention_notifications_days"


def test_an_unset_duration_reports_policy_unset_and_deletes_nothing(world, ws):
    old = plant(world, ws, age_days=4000)
    fresh = plant(world, ws, age_days=0)
    for dry_run in (False, True):
        outcomes = retention.sweep(world.factory, classes=(NOTIFICATIONS,), dry_run=dry_run)
        assert [(o.data_class, o.status) for o in outcomes] == [("notifications", "policy_unset")]
        (outcome,) = outcomes
        assert (outcome.batches, outcome.deleted, outcome.candidates) == (0, {}, None)
        assert outcome.retention_days is None and outcome.cutoff is None
    assert present(world, old + fresh) == old + fresh


def test_a_sweep_of_every_class_leaves_notifications_untouched(world, ws):
    old = plant(world, ws, age_days=4000)
    before = world.count(Job)
    outcomes = {o.data_class: o.status for o in retention.sweep(world.factory)}
    assert outcomes == {c.value: "policy_unset" for c in RetentionClass}
    assert outcomes["notifications"] == "policy_unset"
    assert present(world, old) == old
    assert world.count(Job) == before


def test_another_class_set_does_not_set_this_one(world, ws, monkeypatch):
    old = plant(world, ws, age_days=4000)
    monkeypatch.setattr(get_settings(), RetentionClass.AUDIT_LOGS.setting_name, 30)
    swept = retention.sweep(world.factory, classes=(RetentionClass.AUDIT_LOGS, NOTIFICATIONS))
    outcomes = {o.data_class: o.status for o in swept}
    assert outcomes == {"audit_logs": "purged", "notifications": "policy_unset"}
    assert present(world, old) == old


# --------------------------------------------------------------------------- #
# It cannot be dispatched
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("days", [None, 30])
def test_the_class_cannot_be_enqueued(world, ws, monkeypatch, days):
    monkeypatch.setattr(get_settings(), NOTIFICATIONS.setting_name, days)
    before = world.count(Job)
    with world.session() as s:
        with pytest.raises(ValueError, match="not a workspace retention class"):
            retention.enqueue_purge(
                s, organization_id=world.org, workspace_id=ws, data_class=NOTIFICATIONS,
                cutoff=retention.cutoff_for(NOW, 30),
            )
    assert world.count(Job) == before


def test_a_purge_job_naming_the_class_is_refused_and_deletes_nothing(world, ws, monkeypatch):
    monkeypatch.setattr(get_settings(), NOTIFICATIONS.setting_name, 30)
    old = plant(world, ws, age_days=4000)
    payload = {"data_class": "notifications", "cutoff": retention.cutoff_for(NOW, 30).isoformat(),
               "sequence": 0}  # fmt: skip
    with world.session() as s:
        with pytest.raises(JobExecutionError) as refused:
            retention.run_purge_job(
                s, context=ExecutionContext(organization_id=world.org, workspace_id=ws),
                payload=payload, now=NOW,
            )
    assert refused.value.error.code == JobErrorCode.VALIDATION
    assert present(world, old) == old


def test_notifications_is_a_platform_class_not_a_workspace_class():
    assert NOTIFICATIONS in PLATFORM_CLASSES
    assert NOTIFICATIONS not in WORKSPACE_CLASSES
    assert TABLES["notifications"].retention_class is NOTIFICATIONS
    assert TABLES["notification_preferences"].retention_class is None


def test_no_job_type_exists_for_it_and_no_5e_module_names_job_type():
    for member in JobType:
        assert not any(word in f"{member.name} {member.value}".lower()
                       for word in ("notif", "export", "analytic"))  # fmt: skip
    root = Path(__file__).resolve().parents[1]
    named = []
    for package in ("notifications", "exports", "analytics"):
        for source in sorted((root / package).glob("*.py")):
            for node in ast.walk(ast.parse(source.read_text())):
                if (isinstance(node, ast.Name) and node.id == "JobType") or (
                    isinstance(node, ast.alias) and node.name.endswith("JobType")
                ):
                    named.append(f"{package}/{source.name}")
    assert named == []
    from app.jobs import registry

    assert not [k for k in registry._HANDLERS if "notif" in str(k).lower()]


# --------------------------------------------------------------------------- #
# Production refuses to start without it
# --------------------------------------------------------------------------- #
def _production(**overrides) -> dict:
    kwargs: dict = dict(
        _env_file=None, environment="production", app_mode="full",
        database_url="postgresql+psycopg://u:p@db.internal:5432/signalnest",
        queue_backend="redis", cache_backend="redis", redis_url="redis://cache.internal:6379/0",
        storage_backend="s3", s3_bucket="example-bucket", **production_vector_kwargs(),
        rate_limit_backend="redis", secret_key="a-real-production-secret-value-0123456789",
        llm_provider="anthropic", llm_api_key="inert-test-value", mail_backend="ses",
        mail_from_address="no-reply@example.com", mail_ses_region="us-east-1",
        public_web_origin="https://app.example.com",
    )  # fmt: skip
    kwargs.update({c.setting_name: 30 for c in RetentionClass})
    kwargs.update(overrides)
    return kwargs


def test_production_refuses_an_unset_notifications_duration():
    assert Settings(**_production()).is_production  # the control: every duration set
    with pytest.raises(ValueError) as refused:
        Settings(**_production(retention_notifications_days=None))
    assert "retention_notifications_days must be set explicitly in production" in str(
        refused.value
    )


def test_development_and_staging_accept_it_unset():
    for environment in ("development", "test"):
        built = Settings(_env_file=None, environment=environment)
        assert built.retention_notifications_days is None
    staging = Settings(**_production(environment="staging", retention_notifications_days=None))
    assert staging.retention_notifications_days is None


# --------------------------------------------------------------------------- #
# The control: a set duration purges exactly the expired rows
# --------------------------------------------------------------------------- #
def test_once_set_the_sweep_purges_only_expired_notifications(world, ws, monkeypatch):
    old = plant(world, ws, age_days=400, n=3)
    fresh = plant(world, ws, age_days=0, n=2)
    monkeypatch.setattr(get_settings(), NOTIFICATIONS.setting_name, 30)
    (dry,) = retention.sweep(world.factory, classes=(NOTIFICATIONS,), dry_run=True)
    assert dry.status == "dry_run" and dry.candidates >= 3
    assert present(world, old + fresh) == old + fresh
    (outcome,) = retention.sweep(world.factory, classes=(NOTIFICATIONS,))
    assert outcome.status == "purged" and outcome.complete is True
    assert outcome.deleted.get("notifications", 0) >= 3
    assert present(world, old) == [] and present(world, fresh) == fresh
    (again,) = retention.sweep(world.factory, classes=(NOTIFICATIONS,))
    assert again.deleted.get("notifications", 0) == 0
    assert present(world, fresh) == fresh
