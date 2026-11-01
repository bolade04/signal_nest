"""FU-267-C1: the scheduling environment ceiling (operator ruling D-25-c / D-SC-1 (b)).

The ruling: "the scheduling kill switch is (b) an environment ceiling that outranks
overrides" — workspace enable overrides. It is the setting
``scout_scheduling_ceiling_lifted``, read by the resolver's safety-ceiling rule (rule 1)
for ``scout_scheduling`` only. It is deny-biased: **engaged by default**, so scheduling
cannot be enabled in an environment, by the global flag or by an override, until that
environment lifts the ceiling. Lifting it restores exactly the ordinary precedence
(override → global flag → secure default).

What is pinned here:

* the setting (default engaged; read from ``SCOUT_SCHEDULING_CEILING_LIFTED``);
* the pure decision: the engaged ceiling outranks every flag/override combination; an
  omitted input is engaged; lifted, the precedence matrix is exactly the pre-ceiling one;
  the input is ignored for the other capabilities;
* ``resolve_capability``: an enable override plus the flag resolves disabled via
  ``safety_ceiling`` without reading the override table; lifted, the override decides;
* every enforcement point and both views of the decision: schedule mutations (503), the
  customer reflection, the operator effective read (``decided_by=safety_ceiling`` and the
  setting's value), tick seeding, the tick itself (``feature_disabled``, no run, no
  successor) and failure recovery — each next to its ceiling-lifted control;
* manual runs are untouched (option (c) was not chosen and they have no scheduling check);
* non-vacuity: with the ceiling read dropped (the pre-FU-267-C1 predicate) the same
  scenario enables — the ceiling, and nothing else, is what disables it here.

Override rows are synthetic intent on synthetic workspaces; settings are changed only
through ``monkeypatch`` and restored.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

import app.capabilities.resolver as resolver_mod
import app.scouting_requests.schedules as schedules_mod
from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import CAPABILITY_REGISTRY, Capability
from app.capabilities.resolver import DecisionSource, decide_capability, resolve_capability
from app.core.config import Settings, get_settings
from app.db.models import Base
from app.jobs.models import Job
from app.jobs.status import JobType
from app.organizations.models import Organization, Workspace

from .test_capability_enforcement import (  # noqa: F401  (shared harness fixture)
    _T0,
    _live_schedule,
    _tick,
    h,
)

SCHED = Capability.SCOUT_SCHEDULING
FEEDBACK = Capability.OPPORTUNITY_FEEDBACK
RSS = Capability.CONNECTOR_RSS
_CEILING = "scout_scheduling_ceiling_lifted"


def _engage(monkeypatch) -> None:
    """Engage the scheduling ceiling (the shipped default; the shared harness lifts it)."""
    monkeypatch.setattr(get_settings(), _CEILING, False)


def _flag(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(get_settings(), "scout_scheduling_enabled", enabled)


def _jobs(h, job_type: JobType) -> list[Job]:  # noqa: F811
    with h.make() as s:
        return list(s.scalars(select(Job).where(Job.job_type == job_type.value)))


# --------------------------------------------------------------------------- #
# The setting
# --------------------------------------------------------------------------- #
def test_the_ceiling_is_engaged_by_default() -> None:
    assert Settings(_env_file=None).scout_scheduling_ceiling_lifted is False


def test_the_environment_lifts_the_ceiling(monkeypatch) -> None:
    monkeypatch.setenv("SCOUT_SCHEDULING_CEILING_LIFTED", "true")
    assert Settings(_env_file=None).scout_scheduling_ceiling_lifted is True
    monkeypatch.setenv("SCOUT_SCHEDULING_CEILING_LIFTED", "false")
    assert Settings(_env_file=None).scout_scheduling_ceiling_lifted is False


# --------------------------------------------------------------------------- #
# The pure decision
# --------------------------------------------------------------------------- #
_INPUTS = [(flag, override) for flag in (False, True) for override in (None, False, True)]


@pytest.mark.parametrize(("global_flag", "override"), _INPUTS)
@pytest.mark.parametrize("lifted", [False, "omitted"])
def test_engaged_ceiling_outranks_every_override_and_flag(global_flag, override, lifted):
    kwargs = {} if lifted == "omitted" else {"environment_ceiling_lifted": lifted}
    result = decide_capability(
        capability=SCHED, workspace_id="ws", global_flag=global_flag,
        override_value=override, **kwargs,
    )
    assert result.effective_enabled is False
    assert result.decided_by is DecisionSource.SAFETY_CEILING
    assert result.has_override is False
    assert result.override_value is None
    assert result.global_flag is global_flag
    assert result.environment_ceiling_lifted is False


#: The pre-FU-267-C1 scheduling precedence (rules 2-4), which lifting must restore exactly.
_LIFTED_MATRIX = [
    # (global flag, override, effective, decided_by)
    (False, None, False, DecisionSource.GLOBAL_CONFIGURATION),
    (True, None, True, DecisionSource.GLOBAL_CONFIGURATION),
    (False, True, True, DecisionSource.WORKSPACE_OVERRIDE),
    (True, True, True, DecisionSource.WORKSPACE_OVERRIDE),
    (False, False, False, DecisionSource.WORKSPACE_OVERRIDE),
    (True, False, False, DecisionSource.WORKSPACE_OVERRIDE),
]


@pytest.mark.parametrize(("global_flag", "override", "effective", "decided_by"), _LIFTED_MATRIX)
def test_lifted_ceiling_restores_the_ordinary_precedence(
    global_flag, override, effective, decided_by
):
    result = decide_capability(
        capability=SCHED, workspace_id="ws", global_flag=global_flag,
        override_value=override, environment_ceiling_lifted=True,
    )
    assert result.effective_enabled is effective
    assert result.decided_by is decided_by
    assert result.has_override is (override is not None)
    assert result.override_value is override
    assert result.environment_ceiling_lifted is True


@pytest.mark.parametrize("capability", [FEEDBACK, RSS])
@pytest.mark.parametrize(("global_flag", "override"), _INPUTS)
def test_the_ceiling_input_is_ignored_for_other_capabilities(capability, global_flag, override):
    decisions = {
        lifted: decide_capability(
            capability=capability, workspace_id="ws", global_flag=global_flag,
            override_value=override, environment_ceiling_lifted=lifted,
        )
        for lifted in (True, False, None)
    }
    assert len(set(decisions.values())) == 1, decisions
    only = decisions[None]
    assert only.decided_by is not DecisionSource.SAFETY_CEILING
    assert only.environment_ceiling_lifted is None


def test_lifting_never_unblocks_an_unregistered_capability():
    result = decide_capability(
        capability="not_a_capability",  # type: ignore[arg-type]
        workspace_id="ws", global_flag=True, override_value=True,
        environment_ceiling_lifted=True,
    )
    assert result.effective_enabled is False
    assert result.decided_by is DecisionSource.SAFETY_CEILING
    assert result.environment_ceiling_lifted is None


# --------------------------------------------------------------------------- #
# resolve_capability (DB-backed)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ceiling.db'}", future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):  # pragma: no cover - trivial pragma hook
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with make() as s:
        s.add(Organization(id="org-c", name="C", slug="c"))
        s.add(Workspace(id="ws-c", organization_id="org-c", name="WC", slug="wc"))
        s.commit()
        s.add(WorkspaceCapabilityOverride(
            organization_id="org-c", workspace_id="ws-c", capability=SCHED.value,
            enabled=True))
        s.commit()
    try:
        yield make
    finally:
        engine.dispose()


def _resolve(factory, settings: Settings, statements: list[str] | None = None):
    with factory() as s:
        if statements is not None:
            @event.listens_for(s.bind, "before_cursor_execute")
            def _count(conn, cursor, statement, params, context, executemany):
                if statement.lstrip().upper().startswith("SELECT"):
                    statements.append(statement)

        return resolve_capability(
            session=s, settings=settings, capability=SCHED,
            organization_id="org-c", workspace_id="ws-c",
        )


def test_resolve_ceiling_plus_enable_override_and_flag_is_disabled(factory):
    statements: list[str] = []
    result = _resolve(
        factory, Settings(_env_file=None, scout_scheduling_enabled=True), statements
    )
    assert result.effective_enabled is False
    assert result.decided_by is DecisionSource.SAFETY_CEILING
    assert result.global_flag is True
    assert result.has_override is False and result.override_value is None
    assert result.environment_ceiling_lifted is False
    # The kill switch never depends on override storage: no row is read.
    assert statements == []


def test_resolve_lifted_ceiling_lets_the_override_decide(factory):
    statements: list[str] = []
    result = _resolve(
        factory, Settings(_env_file=None, scout_scheduling_ceiling_lifted=True), statements
    )
    assert result.effective_enabled is True
    assert result.decided_by is DecisionSource.WORKSPACE_OVERRIDE
    assert result.global_flag is False
    assert result.has_override is True and result.override_value is True
    assert result.environment_ceiling_lifted is True
    assert len(statements) == 1, statements


# --------------------------------------------------------------------------- #
# Enforcement points and both views of the decision (HTTP + worker)
# --------------------------------------------------------------------------- #
def test_ceiling_with_enable_override_and_flag_disables_every_view(h, monkeypatch):  # noqa: F811
    _flag(monkeypatch, True)
    h.set_override("a", SCHED, enabled=True)
    _engage(monkeypatch)

    operator = h.operator_effective("a", SCHED)
    assert operator["effective_enabled"] is False
    assert operator["decided_by"] == "safety_ceiling"
    assert operator["environment_ceiling_lifted"] is False
    assert operator["global_flag"] is True
    assert operator["has_override"] is False
    assert h.reflect("a").json() == {"enabled": False}
    created = h.create("a")
    assert created.status_code == 503, created.text
    assert created.json()["error"]["code"] == "capability_unavailable"
    assert h.schedule_count() == 0
    assert _jobs(h, JobType.SCOUT_SCHEDULE_TICK) == []


def test_lifted_ceiling_control_the_same_workspace_is_enabled(h, monkeypatch):  # noqa: F811
    _flag(monkeypatch, True)
    h.set_override("a", SCHED, enabled=True)

    operator = h.operator_effective("a", SCHED)
    assert operator["effective_enabled"] is True
    assert operator["decided_by"] == "workspace_override"
    assert operator["environment_ceiling_lifted"] is True
    assert h.reflect("a").json() == {"enabled": True}
    assert h.create("a").status_code == 201
    assert len(_jobs(h, JobType.SCOUT_SCHEDULE_TICK)) == 1


@pytest.mark.parametrize("lifted", [True, False])
def test_the_readout_reports_no_ceiling_for_other_capabilities(h, monkeypatch, lifted):  # noqa: F811
    monkeypatch.setattr(get_settings(), _CEILING, lifted)
    for capability in (FEEDBACK, RSS):
        item = h.operator_effective("a", capability)
        assert item["environment_ceiling_lifted"] is None, item
        assert item["decided_by"] == "global_configuration", item


def test_ceiling_refuses_every_mutation_of_an_existing_schedule(h, monkeypatch):  # noqa: F811
    h.set_override("a", SCHED, enabled=True)
    assert h.create("a").status_code == 201  # created while lifted
    _flag(monkeypatch, True)
    _engage(monkeypatch)
    assert h.pause("a").status_code == 503
    assert h.resume("a").status_code == 503
    assert h.client.delete(h._sched_url("a"), headers=h.m("a").owner_auth).status_code == 503
    assert h.schedule_count() == 1


@pytest.mark.parametrize("engaged", [True, False])
def test_tick_under_the_ceiling_is_feature_disabled_and_does_not_chain(
    h, monkeypatch, engaged  # noqa: F811
):
    h.set_override("a", SCHED, enabled=True)
    schedule_id = _live_schedule(h, "a")  # seeds the first tick while lifted
    assert len(_jobs(h, JobType.SCOUT_SCHEDULE_TICK)) == 1
    _flag(monkeypatch, True)
    if engaged:
        _engage(monkeypatch)

    result = _tick(h, schedule_id)

    ticks = _jobs(h, JobType.SCOUT_SCHEDULE_TICK)
    runs = _jobs(h, JobType.SCOUT_REQUEST_EXECUTE)
    if engaged:
        assert result == {"outcome": "feature_disabled", "run_enqueued": False, "chained": False}
        assert len(ticks) == 1  # no successor
        assert runs == []
    else:  # control: today's behaviour
        assert result["outcome"] == "fanned_out" and result["chained"] is True
        assert len(ticks) == 2
        assert len(runs) == 1


@pytest.mark.parametrize("engaged", [True, False])
def test_service_create_seeds_no_tick_under_the_ceiling(h, monkeypatch, engaged):  # noqa: F811
    _flag(monkeypatch, True)
    h.set_override("a", SCHED, enabled=True)
    if engaged:
        _engage(monkeypatch)
    _live_schedule(h, "a")
    assert len(_jobs(h, JobType.SCOUT_SCHEDULE_TICK)) == (0 if engaged else 1)


@pytest.mark.parametrize("engaged", [True, False])
def test_failure_recovery_does_not_reseed_under_the_ceiling(h, monkeypatch, engaged):  # noqa: F811
    _flag(monkeypatch, True)
    h.set_override("a", SCHED, enabled=True)
    _engage(monkeypatch)
    schedule_id = _live_schedule(h, "a")  # enabled row, no live tick chain
    if not engaged:
        monkeypatch.setattr(get_settings(), _CEILING, True)
    mk = h.m("a")
    occurrence = _T0 + timedelta(hours=24)
    with h.make() as s:
        result = schedules_mod.recover_after_tick_failure(
            s, schedule_id=schedule_id, occurrence_at=occurrence,
            organization_id=mk.org_id, workspace_id=mk.ws_id,
            job_status="dead_lettered", error_code="timeout",
            now=occurrence + timedelta(minutes=5),
        )
        s.commit()
    if engaged:
        assert result["outcome"] == "feature_disabled" and result["chained"] is False
        assert _jobs(h, JobType.SCOUT_SCHEDULE_TICK) == []
    else:
        assert result["outcome"] == "reseeded" and result["chained"] is True
        assert len(_jobs(h, JobType.SCOUT_SCHEDULE_TICK)) == 1


def test_manual_runs_are_not_governed_by_the_ceiling(h, monkeypatch):  # noqa: F811
    # Option (c) was not chosen: the ceiling stops scheduling only. Run now has no
    # scheduling check and still enqueues its execute job.
    _engage(monkeypatch)
    mk = h.m("a")
    r = h.client.post(
        f"{get_settings().api_prefix}/workspaces/{mk.ws_id}/scout-requests/sr-a/run",
        headers=mk.owner_auth,
    )
    assert r.status_code == 200, r.text
    assert len(_jobs(h, JobType.SCOUT_REQUEST_EXECUTE)) == 1


# --------------------------------------------------------------------------- #
# Non-vacuity: the ceiling is what disables the scenarios above
# --------------------------------------------------------------------------- #
def test_without_the_ceiling_read_the_engaged_scenario_would_enable(h, monkeypatch):  # noqa: F811
    """The pre-FU-267-C1 rule-1 predicate (unregistered capabilities only) in place of the
    real one: the same engaged-ceiling scenario then enables scheduling. So each
    engaged-ceiling assertion above fails for a resolver that drops the ceiling read."""
    _flag(monkeypatch, True)
    h.set_override("a", SCHED, enabled=True)
    _engage(monkeypatch)
    monkeypatch.setattr(
        resolver_mod, "_ceiling_blocks",
        lambda c, **_kwargs: c not in CAPABILITY_REGISTRY,
    )
    operator = h.operator_effective("a", SCHED)
    assert operator["effective_enabled"] is True
    assert operator["decided_by"] == "workspace_override"
    assert h.reflect("a").json() == {"enabled": True}
    assert h.create("a").status_code == 201
    with h.make() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1
