"""O9 (FU-267-F7) on PostgreSQL: the operator override routes (#1, #2) commit before they answer.

``test_o9_write_routes_commit.py`` covers all 19 converted routes on SQLite. There, the
override-set service's SAVEPOINT commits the override row on its own (O9-X1, matrix O14),
so a failed request commit cannot be shown to leave *no row* for route #1. PostgreSQL nests
the SAVEPOINT in the request's transaction, as production (``app_mode=full``) does. On a
database of the test's own, with the real routes and only ``get_db`` overridden:

* **T1**: the request commit fails, the operator gets a 500, and neither the override row nor
  its audit row is committed (set: no row; clear: the row is still there, no ``.cleared``);
* **T2**: ``get_db``'s teardown never commits, yet the change and its audit row are durable;
* **T3/T6**: the request commit precedes ``http.response.start``, and a read made when the
  final body is sent sees the change and its audit row.

Gate: :func:`require_postgres` -- with ``TEST_POSTGRES_URL`` unset these tests FAIL in CI
(``CI=true``) and skip only on a developer machine.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.audit.models import AuditLog
from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import Capability
from app.capabilities.service import set_capability_override
from app.core.config import get_settings
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.organizations.models import Organization, User, Workspace
from app.tests._auth2_support import active_rate_limiter, live_bearer, pg_database, require_postgres
from app.tests._write_commit_support import (
    CommitSwitch,
    SendRecorder,
    commit_precedes_response,
    failing_commit_factory,
    mirror_get_db,
    never_commits,
    record_commits,
)

OVERRIDES = f"{get_settings().api_prefix}/internal/system/capabilities/overrides"
OPERATOR, ORG, WS = "o9-pg-operator", "o9-pg-org", "o9-pg-ws"
FEEDBACK = Capability.OPPORTUNITY_FEEDBACK
CREATED = "workspace_capability_override.created"
CLEARED = "workspace_capability_override.cleared"


@pytest.fixture(autouse=True)
def _postgres_required() -> None:
    require_postgres()


@dataclass
class World:
    witness: Engine
    factory: sessionmaker
    switch: CommitSwitch
    operator: dict
    events: list[str] = field(default_factory=list)

    def state(self) -> tuple[int, int, int]:
        """Committed (override rows, ``.created`` audits, ``.cleared`` audits)."""
        with Session(self.witness) as s:

            def audits(action: str) -> int:
                return s.scalar(
                    select(func.count()).select_from(AuditLog).where(AuditLog.action == action)
                )

            overrides = s.scalar(
                select(func.count())
                .select_from(WorkspaceCapabilityOverride)
                .where(WorkspaceCapabilityOverride.workspace_id == WS)
            )
            return overrides, audits(CREATED), audits(CLEARED)

    def override_present(self) -> None:
        with Session(self.witness) as s:
            set_capability_override(
                s,
                organization_id=ORG,
                workspace_id=WS,
                capability=FEEDBACK,
                enabled=False,
                actor_user_id=OPERATOR,
            )
            s.commit()

    def set(self, client: TestClient):
        active_rate_limiter()._hits.clear()
        body = {
            "organization_id": ORG,
            "workspace_id": WS,
            "capability": FEEDBACK.value,
            "enabled": False,
        }
        return client.put(OVERRIDES, json=body, headers=self.operator)

    def clear(self, client: TestClient):
        active_rate_limiter()._hits.clear()
        params = {"organization_id": ORG, "workspace_id": WS, "capability": FEEDBACK.value}
        return client.delete(OVERRIDES, params=params, headers=self.operator)


@pytest.fixture
def world() -> Iterator[World]:
    with pg_database("sn_o9") as url:
        request_engine = create_engine(url, future=True)
        witness = create_engine(url, future=True)
        assert request_engine.dialect.name == "postgresql"
        Base.metadata.create_all(request_engine)
        with Session(witness) as s:
            s.add(
                User(
                    id=OPERATOR,
                    email="o9-pg-operator@example.com",
                    full_name="O9 PG operator",
                    hashed_password="x",
                    is_active=True,
                    is_operator=True,
                )
            )
            s.add(Organization(id=ORG, name="O9 PG", slug="o9-pg"))
            s.flush()
            s.add(Workspace(id=WS, organization_id=ORG, name="O9 PG", slug="o9-pg"))
            s.commit()
        switch = CommitSwitch()
        factory = failing_commit_factory(request_engine, switch)
        previous = app.dependency_overrides.get(get_db)
        app.dependency_overrides[get_db] = mirror_get_db(factory)
        try:
            yield World(
                witness=witness,
                factory=factory,
                switch=switch,
                operator={"Authorization": f"Bearer {live_bearer(OPERATOR)}"},
            )
        finally:
            app.dependency_overrides.pop(get_db, None)
            if previous is not None:
                app.dependency_overrides[get_db] = previous
            active_rate_limiter()._hits.clear()
            request_engine.dispose()
            witness.dispose()


# --------------------------------------------------------------------------- #
# T1: a failed commit answers 500 and commits neither the row nor its audit
# --------------------------------------------------------------------------- #
def test_t1_override_set_failed_commit_answers_500_with_no_row_and_no_audit(world):
    world.switch.armed = True
    reply = world.set(TestClient(app, raise_server_exceptions=False))

    assert reply.status_code == 500 and reply.json()["error"]["code"] == "internal_error"
    assert world.switch.failures == 1
    assert world.state() == (0, 0, 0)


def test_t1_override_clear_failed_commit_answers_500_and_the_override_stays(world):
    world.override_present()
    assert world.state() == (1, 1, 0)

    world.switch.armed = True
    reply = world.clear(TestClient(app, raise_server_exceptions=False))

    assert reply.status_code == 500 and reply.json()["error"]["code"] == "internal_error"
    assert world.switch.failures == 1
    assert world.state() == (1, 1, 0)


# --------------------------------------------------------------------------- #
# T2: durable although get_db's teardown never commits
# --------------------------------------------------------------------------- #
def test_t2_override_set_and_clear_are_durable_when_teardown_never_commits(world):
    app.dependency_overrides[get_db] = never_commits(world.factory)

    assert world.set(TestClient(app)).status_code == 200
    assert world.state() == (1, 1, 0)
    assert world.clear(TestClient(app)).status_code == 200
    assert world.state() == (0, 1, 1)


# --------------------------------------------------------------------------- #
# T3/T6: the commit precedes the response; a read at the final body sees it
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("route", ["set", "clear"])
def test_t3_t6_override_commit_precedes_the_response_and_is_read_at_the_final_body(world, route):
    if route == "clear":
        world.override_present()
    expected = (1, 1, 0) if route == "set" else (0, 1, 1)
    recorder = SendRecorder(app, world.events, at_final_body=world.state)
    stop = record_commits(world.switch, world.events)
    try:
        reply = getattr(world, route)(TestClient(recorder))
    finally:
        stop()

    assert reply.status_code == 200
    assert commit_precedes_response(world.events), world.events
    assert recorder.seen_at_final_body == [expected]
