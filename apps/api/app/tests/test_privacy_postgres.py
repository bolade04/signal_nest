"""P6-PRIV-1 on PostgreSQL: deletion, verification and purge against the production dialect.

The SQLite modules prove the contracts; this module proves the parts that depend on the
dialect, each in a PostgreSQL database of its own (``TEST_POSTGRES_URL``; unset FAILS in CI and
skips elsewhere):

* the foreign-key walk and the ``SELECT … FOR UPDATE`` locks of account and organization
  deletion run, the receipts' reference checks are clear, and the full verification of the
  account is ``verified``;
* the verifier's case-insensitive text search over ``json`` columns (``lower(CAST(… AS
  TEXT)) LIKE … ESCAPE``) finds a planted copy — the positive control — and the SQL ``NULL``
  written into an anonymised audit record's ``json`` columns reads as cleared;
* the retention purge deletes past-cut-off rows through both paths (sweep batch, durable job).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, update

from app.audit.models import AuditLog
from app.core.config import Settings, get_settings
from app.jobs.models import Job
from app.jobs.store import DurableJobStore
from app.jobs.worker import JobRunner
from app.opportunities.models import Opportunity
from app.organizations.models import User
from app.privacy import retention
from app.privacy.erasure import delete_account
from app.privacy.inventory import RetentionClass
from app.privacy.verification import verify_subject_erased
from app.tests._auth2_support import require_postgres
from app.tests._privacy_support import (
    ALICE,
    API,
    CAROL,
    EMAIL,
    NAME,
    ORG_A,
    ORG_B,
    PASSWORD,
    PrivacyEnv,
    belongs_to,
    privacy_world,
)


@pytest.fixture
def env() -> Iterator[PrivacyEnv]:
    require_postgres()
    with privacy_world(None) as e:
        yield e


def test_account_deletion_on_postgres(env: PrivacyEnv):
    b_rows = env.snapshot(belongs_to(ORG_B))
    headers = env.auth(CAROL)
    with env.session() as s:
        assert not verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL]).verified
    r = env.client.post(
        f"{API}/account/delete", headers=headers,
        json={"current_password": PASSWORD, "confirmation": "DELETE"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["verification"]["references_cleared"] is True
    assert r.json()["counts"]["anonymised"] == {"audit_logs": 5}
    with env.session() as s:
        assert verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL]).verified
    # B's only change: its invitation addressed to CAROL (other letter case) is gone and its
    # audit record of that invitation is anonymised.
    after = env.snapshot(belongs_to(ORG_B))
    changed = {name for name in b_rows if b_rows[name] != after[name]}
    assert changed == {"organization_invitations", "audit_logs"}


def test_organization_deletion_on_postgres(env: PrivacyEnv):
    r = env.client.post(
        f"{API}/organizations/{ORG_A}/delete", headers=env.auth(ALICE),
        json={"current_password": PASSWORD, "confirm_organization_name": NAME[ORG_A]},
    )
    assert r.status_code == 200, r.text
    report = r.json()["verification"]
    assert report["references_cleared"] is True
    assert report["retained_anonymised"] == [
        {"table": "audit_logs", "column": "organization_id", "rows": 6}
    ]
    remaining = env.snapshot(belongs_to(ORG_A))
    assert {name for name, rows in remaining.items() if rows} == {"audit_logs"}


def test_the_verifier_searches_json_case_insensitively_on_postgres(env: PrivacyEnv):
    with env.session() as s:
        delete_account(s, user=s.get(User, CAROL), current_password=PASSWORD)
        s.commit()
    with env.engine.begin() as conn:
        conn.execute(
            update(Job.__table__).where(Job.id == "b1-job")
            .values(result_summary={"note": f"for {EMAIL[CAROL].upper()}"})
        )
    with env.session() as s:
        report = verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])
    assert not report.verified
    assert [(f.table, f.column) for f in report.content_findings] == [("jobs", "result_summary")]


def test_retention_purge_on_postgres(env: PrivacyEnv, monkeypatch):
    settings = get_settings()
    for data_class in RetentionClass:
        monkeypatch.setattr(settings, data_class.setting_name, None)
    monkeypatch.setattr(settings, RetentionClass.AUDIT_LOGS.setting_name, 30)
    monkeypatch.setattr(settings, RetentionClass.OPPORTUNITIES.setting_name, 30)
    old = datetime.now(UTC) - timedelta(days=90)
    with env.engine.begin() as conn:
        conn.execute(
            insert(AuditLog.__table__).values(
                id="old-audit", organization_id=ORG_B, action="x", context={},
                created_at=old, updated_at=old,
            )
        )
    outcomes = {
        o.data_class: o
        for o in retention.sweep(
            env.session, classes=(RetentionClass.AUDIT_LOGS, RetentionClass.OPPORTUNITIES)
        )
    }
    assert outcomes["audit_logs"].deleted == {"audit_logs": 1}
    assert outcomes["opportunities"].jobs_enqueued == 4
    runner = JobRunner(
        settings=Settings(_env_file=None), session_factory=env.session,
        store=DurableJobStore(), clock=lambda: datetime.now(UTC),
    )
    ran = 0
    while runner.poll_once(worker_id="w-prv-pg"):
        ran += 1
        assert ran <= 10
    assert ran == 4
    assert env.count(Opportunity) == 0
