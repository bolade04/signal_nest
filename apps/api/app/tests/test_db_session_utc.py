"""PostgreSQL sessions are pinned to UTC and verified (``app.db.utc_session``; W21 TZPIN).

psycopg returns a ``timestamptz`` in the session's time zone, and Python compares, subtracts
and adds to two aware datetimes that share a ``tzinfo`` by wall time, ignoring ``fold``
(PEP 495). In America/Chicago on 2026-11-01 the hour 01:00-02:00 happens twice
(06:00-08:00Z), so a lease 50 minutes in the future reads as already expired; and, outside
that hour, any difference or addition across the 07:00Z change is off by 3600 s (review
W21-REV-BLOCKEDOBS r1, NOTE-1; the BLOCKEDOBS lane's corrected TZ-NOTE).

The always-run tests check the pin and the guard without a database. The PostgreSQL tests
(``TEST_POSTGRES_URL``; they fail in CI if it is unset) give each test a database whose
default time zone is America/Chicago (``ALTER DATABASE ... SET timezone``), so they prove the
pin on any cluster, CI's UTC service included. One more test uses the cluster's own server
default, which is the brief's America/Chicago cluster when run locally.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db import utc_session
from app.db.session import build_engine
from app.db.utc_session import (
    UTC_SESSION_OPTION,
    SessionTimezoneError,
    create_utc_engine,
    enforce_utc_session,
    refuse_non_utc_session,
    utc_connect_args,
)
from app.scouting_requests.execution import attempt_holds_authority, is_eligible
from app.tests._auth2_support import pg_database, require_postgres

API_DIR = Path(__file__).resolve().parents[2]
PG_URL = "postgresql+psycopg://u@127.0.0.1:5432/db"
CHICAGO = "America/Chicago"

# Inside the 2026-11-01 US fall-back: 01:00-02:00 local happens at 06:00-07:00Z (CDT, the
# first pass) and again at 07:00-08:00Z (CST, the second pass).
FIRST = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)  # 01:30 CDT
DEADLINE = datetime(2026, 11, 1, 6, 50, tzinfo=UTC)  # 01:50 CDT
AFTER = datetime(2026, 11, 1, 7, 10, tzinfo=UTC)  # 01:10 CST: 20 min after DEADLINE
LATER = datetime(2026, 11, 1, 7, 20, tzinfo=UTC)  # 01:20 CST: 50 min after FIRST

# Outside the fold, on either side of the 07:00Z change. Wall time is monotonic here, so a
# comparison of two read values holds; a difference or an addition across the change does not.
NOON_CDT = datetime(2026, 10, 31, 17, 0, tzinfo=UTC)  # 12:00 CDT
NOON_CST = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)  # 12:00 CST: 25 h later
ADMITTED = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # 00:30 CDT, before the fold
CHECKED = datetime(2026, 11, 1, 8, 10, tzinfo=UTC)  # 02:10 CST, after the fold: 2 h 40 min later
BASIS = timedelta(hours=2)  # a deadline basis added to ADMITTED: the deadline is 07:30Z


@pytest.fixture(autouse=True)
def _no_libpq_environment(monkeypatch) -> None:
    """libpq reads PGTZ and PGOPTIONS at connect time; no test inherits them."""
    monkeypatch.delenv("PGTZ", raising=False)
    monkeypatch.delenv("PGOPTIONS", raising=False)


class _Captured(Exception):
    """Raised by a ``do_connect`` listener: the connect parameters were captured, no socket."""


def _connect_params(engine) -> dict:
    """The parameters the engine would hand to psycopg, captured before any connection."""
    seen: dict = {}

    def capture(_dialect, _record, _cargs, cparams):
        seen.update(cparams)
        raise _Captured

    event.listen(engine, "do_connect", capture)
    try:
        with pytest.raises(_Captured):
            engine.connect()
    finally:
        event.remove(engine, "do_connect", capture)
    return seen


def _session(zone):
    return SimpleNamespace(info=SimpleNamespace(parameter_status=lambda name: zone))


# --------------------------------------------------------------------------- #
# Always run: the pin and the guard, without a database
# --------------------------------------------------------------------------- #
def test_the_pin_is_the_whole_options_string_when_none_is_given() -> None:
    assert utc_connect_args(PG_URL) == {"options": UTC_SESSION_OPTION}
    assert UTC_SESSION_OPTION == "-c timezone=UTC"


def test_the_pin_comes_last_after_options_already_given(monkeypatch) -> None:
    given = {"options": "-c lock_timeout=5000", "connect_timeout": 3}
    assert utc_connect_args(PG_URL, given) == {
        "options": "-c lock_timeout=5000 -c timezone=UTC",
        "connect_timeout": 3,
    }
    assert given == {"options": "-c lock_timeout=5000", "connect_timeout": 3}  # not mutated
    # The URL's own options: an explicit connect_args "options" would otherwise replace them.
    in_url = PG_URL + "?options=-c%20statement_timeout%3D1000"
    assert utc_connect_args(in_url) == {"options": "-c statement_timeout=1000 -c timezone=UTC"}
    # A later pin also beats an earlier timezone in the same string.
    assert utc_connect_args(PG_URL, {"options": "-c timezone=America/Chicago"}) == {
        "options": "-c timezone=America/Chicago -c timezone=UTC"
    }
    # PGOPTIONS is what libpq uses when no options are given; it is kept, not replaced.
    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=2000")
    assert utc_connect_args(PG_URL) == {"options": "-c statement_timeout=2000 -c timezone=UTC"}
    assert utc_connect_args(in_url) == {"options": "-c statement_timeout=1000 -c timezone=UTC"}


def test_sqlite_is_untouched(tmp_path) -> None:
    assert utc_connect_args("sqlite:///x.db", {"check_same_thread": False}) == {
        "check_same_thread": False
    }
    engine = build_engine(Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/u.db"))
    try:
        assert not event.contains(engine, "connect", refuse_non_utc_session)
    finally:
        engine.dispose()


def test_the_process_engine_pins_and_verifies_postgresql_sessions() -> None:
    settings = Settings(_env_file=None, database_url=PG_URL, db_connect_timeout_seconds=7)
    engine = build_engine(settings)
    try:
        assert event.contains(engine, "connect", refuse_non_utc_session)
        params = _connect_params(engine)
    finally:
        engine.dispose()
    assert params["options"] == UTC_SESSION_OPTION
    assert params["connect_timeout"] == 7  # the existing connect_args are kept
    assert "application_name" in params


def test_the_guard_refuses_any_session_not_reported_as_utc() -> None:
    refuse_non_utc_session(_session("UTC"), None)
    for zone in (CHICAGO, "Etc/UTC", "GMT", None):
        with pytest.raises(SessionTimezoneError) as refused:
            refuse_non_utc_session(_session(zone), None)
        assert repr(zone) in str(refused.value)
    with pytest.raises(SessionTimezoneError):
        refuse_non_utc_session(object(), None)  # a driver that reports nothing is refused


def test_enforce_is_idempotent_and_postgresql_only() -> None:
    engine = create_engine(PG_URL)
    try:
        enforce_utc_session(enforce_utc_session(engine))
        guards = [fn for fn in engine.pool.dispatch.connect if fn is refuse_non_utc_session]
        assert len(guards) == 1
    finally:
        engine.dispose()
    lite = create_utc_engine("sqlite://")
    try:
        assert not event.contains(lite, "connect", refuse_non_utc_session)
    finally:
        lite.dispose()


def test_location_refs_builds_its_engine_through_the_pin(monkeypatch, capsys) -> None:
    from app.campaign_context import location_refs

    built: list[str] = []

    def spy(url, **kwargs):
        built.append(str(url))
        raise RuntimeError("stop before connecting")

    monkeypatch.setattr(utc_session, "create_utc_engine", spy)
    assert location_refs.main([]) == location_refs.EXIT_CONFIG_OR_CONNECT_FAILURE
    assert len(built) == 1
    assert "configuration-failure" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# PostgreSQL
# --------------------------------------------------------------------------- #
def _admin():
    return create_engine(make_url(os.environ["TEST_POSTGRES_URL"]), isolation_level="AUTOCOMMIT")


@pytest.fixture
def chicago_db() -> Iterator[str]:
    """A database of this test's own whose default time zone is America/Chicago."""
    require_postgres()
    with pg_database("sn_tzpin") as url:
        admin = _admin()
        try:
            with admin.connect() as c:
                c.execute(
                    text(
                        f'ALTER DATABASE "{make_url(url).database}" SET timezone = \'{CHICAGO}\''
                    )
                )
        finally:
            admin.dispose()
        yield url


def _show(engine) -> tuple[str, str | None]:
    with engine.connect() as c:
        shown = c.execute(text("show timezone")).scalar_one()
        reported = c.connection.dbapi_connection.info.parameter_status("TimeZone")
    return shown, reported


def _app_engine(url: str):
    return build_engine(Settings(_env_file=None, database_url=url))


def test_an_app_built_session_is_utc_where_the_database_default_is_chicago(chicago_db) -> None:
    control = create_engine(chicago_db)
    engine = _app_engine(chicago_db)
    try:
        # The setup bites: a session opened without the pin is in America/Chicago.
        assert _show(control) == (CHICAGO, CHICAGO)
        assert _show(engine) == ("UTC", "UTC")
        with engine.connect() as c:
            now = c.execute(text("select now()")).scalar_one()
        assert now.utcoffset() == timedelta(0)
    finally:
        control.dispose()
        engine.dispose()


def test_an_app_built_session_is_utc_under_the_cluster_server_default(capsys) -> None:
    """The brief's case: the cluster's own default (America/Chicago on the lane's cluster)."""
    require_postgres()
    with pg_database("sn_tzsrv") as url:
        control = create_engine(url)
        engine = _app_engine(url)
        try:
            server_default = _show(control)[0]
            assert _show(engine) == ("UTC", "UTC")
        finally:
            control.dispose()
            engine.dispose()
    with capsys.disabled():
        print(f"\nTZPIN cluster server default: {server_default}; app-built session: UTC")


def test_instants_inside_the_fall_back_fold_round_trip_and_compare_correctly(chicago_db) -> None:
    engine = _app_engine(chicago_db)
    control = create_engine(chicago_db)
    rows = {"first": FIRST, "deadline": DEADLINE, "after": AFTER, "later": LATER}
    try:
        with engine.begin() as c:
            c.execute(text("create table tz_fold (k text primary key, at timestamptz not null)"))
            for k, at in rows.items():
                c.execute(text("insert into tz_fold (k, at) values (:k, :at)"), {"k": k, "at": at})
            # The same instants as SQL literals, so binding is not the only path in.
            c.execute(
                text(
                    "insert into tz_fold (k, at) values"
                    " ('first_sql', '2026-11-01 01:30:00-05'),"  # CDT
                    " ('later_sql', '2026-11-01 01:20:00-06')"  # CST
                )
            )

        def read(eng) -> dict:
            with eng.connect() as c:
                return dict(c.execute(text("select k, at from tz_fold")).all())

        got = read(engine)
        for k, at in rows.items():
            assert got[k] == at and got[k].utcoffset() == timedelta(0), (k, got[k])
        assert got["first_sql"] == FIRST and got["later_sql"] == LATER
        assert got["later"] > got["first"]
        assert got["later"] - got["first"] == timedelta(minutes=50)
        assert got["after"] - got["deadline"] == timedelta(minutes=20)

        lease = SimpleNamespace(
            status="running", attempt_count=1, worker_id="w", lease_expires_at=got["later"]
        )
        admission = SimpleNamespace(
            attempt=1, worker_id="w", end_kind=None, ended_at=None, deadline_at=got["deadline"]
        )
        # A lease 50 min ahead holds authority; a deadline 20 min past has passed.
        assert attempt_holds_authority(lease, admission, got["first"]) is True
        assert is_eligible(admission, holds=False, now=got["after"]) is True

        # Control: the same rows read in an America/Chicago session (no pin). This is the
        # misjudgment the pin removes, and it shows the instants really are in the fold.
        bad = read(control)
        assert str(bad["later"].tzinfo) == CHICAGO and bad["later"].fold == 1
        assert not (bad["later"] > bad["first"])
        lease.lease_expires_at = bad["later"]
        admission.deadline_at = bad["deadline"]
        assert attempt_holds_authority(lease, admission, bad["first"]) is False
        assert is_eligible(admission, holds=False, now=bad["after"]) is False
    finally:
        engine.dispose()
        control.dispose()


def test_an_interval_spanning_the_transition_outside_the_fold_measures_correctly(
    chicago_db,
) -> None:
    """Not only the repeated hour: across the 07:00Z change, an America/Chicago session's
    values subtract and add by wall time, so a difference is 3600 s short and a deadline
    computed by addition (``admitted + basis``, the shape of ``app.jobs.admission``'s
    deadline) lands an hour late, although no value is in the fold."""
    engine = _app_engine(chicago_db)
    control = create_engine(chicago_db)
    rows = {"noon_cdt": NOON_CDT, "noon_cst": NOON_CST, "admitted": ADMITTED, "checked": CHECKED}
    try:
        with engine.begin() as c:
            c.execute(text("create table tz_span (k text primary key, at timestamptz not null)"))
            for k, at in rows.items():
                c.execute(text("insert into tz_span (k, at) values (:k, :at)"), {"k": k, "at": at})
            truth = c.execute(
                text(
                    "select extract(epoch from (select at from tz_span where k = 'noon_cst')"
                    " - (select at from tz_span where k = 'noon_cdt'))"
                )
            ).scalar_one()
        assert float(truth) == 25 * 3600  # the database's own interval

        def read(eng) -> dict:
            with eng.connect() as c:
                return dict(c.execute(text("select k, at from tz_span")).all())

        got = read(engine)
        assert {k: got[k] for k in rows} == rows
        assert (got["noon_cst"] - got["noon_cdt"]).total_seconds() == float(truth)
        assert got["checked"] - got["admitted"] == timedelta(hours=2, minutes=40)  # an age
        deadline = got["admitted"] + BASIS
        assert deadline == datetime(2026, 11, 1, 7, 30, tzinfo=UTC)
        admission = SimpleNamespace(
            attempt=1, worker_id="w", end_kind=None, ended_at=None, deadline_at=deadline
        )
        lease = SimpleNamespace(
            status="running", attempt_count=1, worker_id="w", lease_expires_at=deadline
        )
        # Checked 40 min after the deadline: eligible, and a lease that expired then is gone.
        assert is_eligible(admission, holds=False, now=got["checked"]) is True
        assert attempt_holds_authority(lease, admission, got["checked"]) is False

        # Control: the same rows in an America/Chicago session (no pin). No value is
        # ambiguous and comparisons hold, but the arithmetic across the change is wall time.
        bad = read(control)
        for k in rows:
            assert str(bad[k].tzinfo) == CHICAGO
            assert bad[k].replace(fold=1).utcoffset() == bad[k].utcoffset(), k  # not in a fold
        assert bad["checked"] > bad["admitted"]
        assert (bad["noon_cst"] - bad["noon_cdt"]).total_seconds() == float(truth) - 3600
        assert bad["checked"] - bad["admitted"] == timedelta(hours=1, minutes=40)
        late = bad["admitted"] + BASIS  # wall 02:30, read as CST: 08:30Z, an hour late
        assert late.astimezone(UTC) == datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
        admission.deadline_at = late
        lease.lease_expires_at = late
        assert is_eligible(admission, holds=False, now=bad["checked"]) is False
        assert attempt_holds_authority(lease, admission, bad["checked"]) is True
    finally:
        engine.dispose()
        control.dispose()


def test_a_session_the_pin_cannot_reach_is_refused(chicago_db, monkeypatch) -> None:
    """PGTZ outranks the startup option (libpq sends it as its own startup parameter); the
    guard refuses the session instead of using it."""
    monkeypatch.setenv("PGTZ", CHICAGO)
    pinned_only = create_engine(chicago_db, connect_args=utc_connect_args(chicago_db))
    try:
        assert _show(pinned_only)[1] == CHICAGO  # measured: PGTZ beats '-c timezone=UTC'
    finally:
        pinned_only.dispose()
    engine = _app_engine(chicago_db)
    try:
        with pytest.raises(SessionTimezoneError, match="'America/Chicago', not 'UTC'"):
            engine.connect()
        assert engine.pool.checkedout() == 0
        monkeypatch.setenv("PGTZ", "UTC")
        assert _show(engine) == ("UTC", "UTC")
        # Every new connection is checked, not only the engine's first: with one held, the
        # pool must open another, which the pin cannot reach.
        with engine.connect() as held:
            monkeypatch.setenv("PGTZ", CHICAGO)
            with pytest.raises(SessionTimezoneError, match="'America/Chicago', not 'UTC'"):
                engine.connect()
            assert held.execute(text("show timezone")).scalar_one() == "UTC"
    finally:
        engine.dispose()


def _alembic_upgrade(url: str, **extra_env: str) -> tuple[subprocess.CompletedProcess, list]:
    """``alembic upgrade head`` through the real env.py in a child process; returns the result
    and the ``TimeZone`` of every connection its pools opened."""
    code = (
        "from sqlalchemy import event\n"
        "from sqlalchemy.pool import Pool\n"
        "seen = []\n"
        "def _seen(conn, _record):\n"
        "    seen.append(conn.info.parameter_status('TimeZone'))\n"
        "event.listen(Pool, 'connect', _seen)\n"
        "from alembic.config import main\n"
        "try:\n"
        "    main(argv=['upgrade', 'head'])\n"
        "finally:\n"
        "    print('TZPIN-SEEN', ','.join(map(str, seen)))\n"
    )
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env.pop("SN_MIGRATION_MODE", None)
    env.update(extra_env)
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=API_DIR, env=env, capture_output=True, text=True
    )
    seen = [line for line in result.stdout.splitlines() if line.startswith("TZPIN-SEEN ")]
    assert len(seen) == 1, result.stdout[-2000:] + result.stderr[-4000:]
    return result, seen[0].removeprefix("TZPIN-SEEN ").split(",")


def test_migrations_run_in_a_utc_session(chicago_db) -> None:
    """``alembic upgrade head`` through the real env.py: every connection it opens is UTC."""
    result, zones = _alembic_upgrade(chicago_db)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-4000:]
    assert zones and set(zones) == {"UTC"}, zones


def test_migrations_refuse_a_session_the_pin_cannot_reach(chicago_db) -> None:
    """The guard on Alembic's online engine: under ``PGTZ`` (which outranks the pin) the
    migration is refused at connect, before any statement, and nothing is applied."""
    result, _zones = _alembic_upgrade(chicago_db, PGTZ=CHICAGO)
    assert result.returncode != 0, result.stdout[-2000:]
    assert "SessionTimezoneError" in result.stderr, result.stderr[-4000:]
    assert "'America/Chicago', not 'UTC'" in result.stderr, result.stderr[-4000:]
    control = create_engine(chicago_db)
    try:
        with control.connect() as c:
            assert not inspect(c).has_table("alembic_version")
    finally:
        control.dispose()
