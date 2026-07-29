"""Tests for the dedicated revision reader (Gate 4J).

Every no-leak / absence assertion is paired with a positive control proving the path
actually executed — a suite that passes when the behaviour is absent is unacceptable.

psycopg is injected as a fake through sys.modules so these run with no database, no
driver install, and no network.
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from revision_reader import reader as R  # noqa: E402

DSN = "postgresql://svc:pw@db.invalid:5432/appdb?sslmode=require"
SENTINEL_DSN = "postgresql://svc:sn-sentinel-p4ss@db-sentinel-host:5432/appdb?sslmode=require"


# --------------------------------------------------------------------------- #
# Fake psycopg
# --------------------------------------------------------------------------- #
class _Cur:
    def __init__(self, rows, raise_exc=None):
        self._rows, self._raise = rows, raise_exc
        self.executed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def execute(self, q):
        self.executed.append(q)
        if self._raise is not None:
            raise self._raise

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows, raise_exc=None):
        self._cur = _Cur(rows, raise_exc)
        self.read_only = None
        self.closed = 0
        self.kwargs: dict = {}

    def cursor(self):
        return self._cur

    def close(self):
        self.closed += 1


def install_fake_psycopg(monkeypatch, rows=None, connect_exc=None, query_exc=None):
    """Install a fake psycopg; returns a state dict for assertions."""
    state: dict = {"connects": 0, "conn": None, "kwargs": None}
    mod = types.ModuleType("psycopg")

    def connect(dsn, **kwargs):
        state["connects"] += 1
        state["kwargs"] = kwargs
        state["dsn"] = dsn
        if connect_exc is not None:
            raise connect_exc
        c = _Conn(rows if rows is not None else [], query_exc)
        state["conn"] = c
        return c

    mod.connect = connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", mod)
    return state


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DSN)
    return monkeypatch


def run(argv=None):
    return R.main(argv if argv is not None else [])


# --------------------------------------------------------------------------- #
# 1-5. Success and each failure classification, distinctly (positive controls)
# --------------------------------------------------------------------------- #
def test_one_valid_revision_prints_exactly_one_line(env, capsys):
    install_fake_psycopg(env, rows=[("98289430a3ec",)])
    rc = run()
    cap = capsys.readouterr()  # single capture; both streams asserted
    assert rc == R.EXIT_OK
    assert cap.out == "98289430a3ec\n"
    assert cap.err == ""


def test_zero_rows_distinct(env, capsys):
    install_fake_psycopg(env, rows=[])
    rc = run()
    cap = capsys.readouterr()
    assert rc == R.EXIT_NO_ROWS
    assert cap.err == "revision-reader: READER-ZERO-REVISIONS\n"
    assert cap.out == ""


def test_multiple_rows_distinct(env, capsys):
    install_fake_psycopg(env, rows=[("98289430a3ec",), ("aaaaaaaaaaaa",)])
    rc = run()
    assert rc == R.EXIT_MULTIPLE_ROWS
    assert capsys.readouterr().err == "revision-reader: READER-MULTIPLE-REVISIONS\n"


@pytest.mark.parametrize("bad", ["ZZZZZZZZZZZZ", "98289430A3EC", "9828943", "", "98289430a3ec0"])
def test_malformed_revision_distinct(env, capsys, bad):
    install_fake_psycopg(env, rows=[(bad,)])
    rc = run()
    assert rc == R.EXIT_MALFORMED_REVISION
    assert capsys.readouterr().err == "revision-reader: READER-REVISION-MALFORMED\n"


def test_missing_table_distinct(env, capsys):
    exc = RuntimeError("relation does not exist")
    exc.sqlstate = "42P01"  # type: ignore[attr-defined]
    install_fake_psycopg(env, query_exc=exc)
    rc = run()
    assert rc == R.EXIT_TABLE_MISSING
    assert capsys.readouterr().err == "revision-reader: READER-VERSION-TABLE-MISSING\n"


def test_connection_failure_distinct(env, capsys):
    install_fake_psycopg(env, connect_exc=RuntimeError("could not connect"))
    rc = run()
    assert rc == R.EXIT_CONNECT_FAILED
    assert capsys.readouterr().err == "revision-reader: READER-CONNECTION-FAILED\n"


def test_config_failure_when_dsn_absent(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = run()
    assert rc == R.EXIT_CONFIG_FAILED
    assert capsys.readouterr().err == "revision-reader: READER-CONFIG-FAILED\n"


def test_config_failure_when_tls_not_required(monkeypatch, capsys):
    # TLS is ASSERTED here, not trusted from provisioning discipline.
    monkeypatch.setenv("DATABASE_URL", "postgresql://svc:pw@db.invalid:5432/appdb")
    rc = run()
    assert rc == R.EXIT_CONFIG_FAILED


# --------------------------------------------------------------------------- #
# DSN admission. `containerOverrides.environment` is caller-settable at RunTask
# time, so DATABASE_URL is the one input this program must treat as hostile.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "dsn",
    [
        # THE FORMAT THE REAL SECRET ACTUALLY HAS. bootstrap_app_role.py composes
        # `postgresql+psycopg://...`; libpq rejects the driver suffix, so without
        # normalisation the reader could never connect to the live database at all.
        "postgresql+psycopg://svc:pw@db.invalid:5432/appdb?sslmode=require",
        "postgresql://svc:pw@db.invalid:5432/appdb?sslmode=require",
        # Port omitted: libpq defaults to 5432, the same destination the SG permits.
        "postgresql://svc:pw@db.invalid/appdb?sslmode=require",
        "postgres://svc:pw@db.invalid:5432/appdb?sslmode=verify-full",
        "postgresql://svc:pw@db.invalid:5432/appdb?sslmode=verify-ca",
        # Zero-padded: the same destination, so admitted rather than refused on form.
        "postgresql://svc:pw@db.invalid:05432/appdb?sslmode=require",
        # A percent-encoded '@' inside the password is what a REAL secret looks like:
        # compose_database_url quotes both credentials with quote(safe=""). It must not
        # be confused with the unescaped, ambiguous form rejected below. Credentials live
        # in the netloc but NOT in `hostname`, so the encoded-host rule cannot reject them.
        "postgresql://svc:p%40ss@db.invalid:5432/appdb?sslmode=require",
        "postgresql://svc:p%2Fw@db.invalid/appdb?sslmode=require",
        # A plain IPv6 literal is legitimate; only a zone id (which needs '%') is not.
        "postgresql://svc:pw@[::1]:5432/appdb?sslmode=require",
    ],
)
def test_admissible_dsns_reach_the_connect_call(monkeypatch, dsn):
    monkeypatch.setenv("DATABASE_URL", dsn)
    state = install_fake_psycopg(monkeypatch, rows=[("98289430a3ec",)])
    assert R.main([]) == R.EXIT_OK
    assert state["connects"] == 1
    # The driver suffix must be stripped before libpq ever sees it.
    assert state["dsn"].startswith(("postgresql://", "postgres://"))
    assert "+psycopg" not in state["dsn"]


@pytest.mark.parametrize(
    "dsn",
    [
        # THE REDIRECT ATTACK. The reader's SG permits egress to 0.0.0.0/0 on 443 for
        # ECR/Secrets/Logs, and the PostgreSQL wire protocol works on any port -- so a
        # caller who can set `environment` could otherwise point the reader at a host
        # they control, answer the one query, and have it report an attacker-chosen
        # revision with exit 0. Pinning the port composes with the SG (outbound 5432 is
        # permitted ONLY to the RDS SG) to close that path.
        "postgresql://svc:pw@evil.invalid:443/appdb?sslmode=require",
        "postgresql://svc:pw@evil.invalid:80/appdb?sslmode=require",
        "postgresql://svc:pw@evil.invalid:6379/appdb?sslmode=require",
        # sslmode is parsed, not substring-matched: this CONTAINS "sslmode=require".
        "postgresql://svc:pw@db.invalid:5432/appdb?sslmode=requireXXX",
        "postgresql://svc:pw@db.invalid:5432/appdb?sslmode=prefer",
        "postgresql://svc:pw@db.invalid:5432/appdb?sslmode=disable",
        # Ambiguous duplicates must not be resolved in the caller's favour.
        "postgresql://svc:pw@db.invalid:5432/appdb?sslmode=require&sslmode=disable",
        "mysql://svc:pw@db.invalid:5432/appdb?sslmode=require",
        "http://evil.invalid:5432/?sslmode=require",
        "postgresql:///appdb?sslmode=require",
        "postgresql://svc:pw@db.invalid:notaport/appdb?sslmode=require",
        # PARSER-DIVERGENCE BYPASSES. A libpq URI accepts connection KEYWORDS in its
        # query string, so a port check that only looks at the positional slot is not
        # enough -- libpq would honour these and connect somewhere else entirely. The
        # query string is therefore an allowlist of exactly {sslmode}.
        "postgresql://svc:pw@db.invalid:5432/appdb?sslmode=require&port=443",
        "postgresql://svc:pw@db.invalid:5432/appdb?sslmode=require&host=evil.invalid",
        "postgresql://svc:pw@db.invalid:5432/appdb?sslmode=require&hostaddr=10.0.0.9",
        # `service` and `passfile` can pull an entire connection definition from a file;
        # `options` can push server settings such as clearing the read-only default.
        "postgresql://svc:pw@db.invalid:5432/appdb?sslmode=require&service=evil",
        "postgresql://svc:pw@db.invalid:5432/appdb?sslmode=require&passfile=/tmp/x",
        "postgresql://svc:pw@db.invalid:5432/appdb?sslmode=require&options=-c%20x%3Dy",
        # MULTI-HOST URIs: libpq tries each host in turn, so the attacker's can be first.
        # urlsplit knows nothing about that syntax -- it returns the whole comma-joined
        # string as `hostname` and takes everything after the FIRST colon as the port. So
        # the two forms below are caught only incidentally: their port string is
        # "443,db.invalid:5432", which fails int(). The two after them satisfy the port
        # pin outright -- one trailing port reads as a clean 5432, and the portless form
        # reads as None, which is "unspecified".
        # Rejecting ',' in the authority is what makes the single-destination guarantee
        # total instead of accidental.
        "postgresql://svc:pw@evil.invalid:443,db.invalid:5432/appdb?sslmode=require",
        "postgresql://svc:pw@db.invalid:5432,evil.invalid:443/appdb?sslmode=require",
        "postgresql://svc:pw@evil.invalid,db.invalid:5432/appdb?sslmode=require",
        "postgresql://svc:pw@evil.invalid,db.invalid/appdb?sslmode=require",
        # A fragment is not part of a libpq URI, so the two parsers would disagree.
        "postgresql://svc:pw@db.invalid:5432/appdb?sslmode=require#host=evil.invalid",
        # libpq's keyword/value form, which carries no scheme at all.
        "host=evil.invalid port=443 sslmode=require",
        # DELIMITER AMBIGUITY. Two parsers decide where userinfo ends by locating an '@'.
        # If this program and libpq ever picked a DIFFERENT one they would disagree about
        # the host, and the program would validate one destination while libpq connected
        # to another. Refusing more than one literal '@' removes the question instead of
        # answering it -- and cannot reject a legitimate DSN, since a real '@' in a
        # credential arrives percent-encoded (admitted above).
        "postgresql://svc:p@ss@evil.invalid:443/appdb?sslmode=require",
        "postgresql://svc:p@ss@db.invalid:5432/appdb?sslmode=require",
        "postgresql://svc:pw@evil.invalid:443@db.invalid:5432/appdb?sslmode=require",
        "postgresql://svc:pw@db.invalid:5432@evil.invalid:443/appdb?sslmode=require",
        # No userinfo at all: the reader always authenticates, so this is malformed here.
        "postgresql://db.invalid:5432/appdb?sslmode=require",
        # SEPARATOR DISAGREEMENT. If libpq ever tokenised the query on ';' while Python
        # splits only on '&', a smuggled second keyword could be invisible to this check.
        # It fails closed anyway, and for a reason worth keeping: the sslmode check is an
        # exact-VALUE match, so anything Python folds into the value ("require;host=evil")
        # simply is not "require". A key-only allowlist would NOT have this property.
        "postgresql://svc:pw@db.invalid:5432/appdb?sslmode=require;host=evil.invalid",
        "postgresql://svc:pw@db.invalid:5432/appdb?sslmode=require;port=443",
        "postgresql://svc:pw@db.invalid:5432/appdb?host=evil.invalid;sslmode=require",
        # PERCENT-ENCODED SEPARATORS IN THE HOST -- the same ambiguity one decoding stage
        # later. urlsplit does NOT decode the host, so the ',' and port checks above see
        # a single comma-free hostname with no port; libpq DOES decode URI components, and
        # if it decodes before splitting on ',' these become a multi-host list and a
        # redirected port. Which side libpq decodes on is not establishable here, so the
        # question is refused: a real RDS endpoint is plain ASCII and never needs an escape.
        "postgresql://svc:pw@evil.invalid%2Cdb.invalid:5432/appdb?sslmode=require",
        "postgresql://svc:pw@db.invalid%3A443/appdb?sslmode=require",
        "postgresql://svc:pw@db%2Einvalid:5432/appdb?sslmode=require",
        # An IPv6 zone id needs '%' and is not a thing an RDS endpoint ever has.
        "postgresql://svc:pw@[fe80::1%25eth0]:5432/appdb?sslmode=require",
    ],
)
def test_inadmissible_dsns_are_refused_before_any_connection(monkeypatch, capsys, dsn):
    monkeypatch.setenv("DATABASE_URL", dsn)
    state = install_fake_psycopg(monkeypatch, rows=[("98289430a3ec",)])
    rc = R.main([])
    cap = capsys.readouterr()
    assert rc == R.EXIT_CONFIG_FAILED
    # Refused BEFORE the socket is opened -- not detected afterwards.
    assert state["connects"] == 0
    assert cap.out == ""
    # One fixed token: a probing caller learns nothing about which constraint they hit.
    assert cap.err == "revision-reader: READER-CONFIG-FAILED\n"


# --------------------------------------------------------------------------- #
# 6-11. The override-resistance and hygiene properties
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("argv", [["--force"], ["upgrade"], ["head"], ["-c", "print(1)"], [""]])
def test_any_argv_rejected_before_anything_else(monkeypatch, capsys, argv):
    """Under the fixed ENTRYPOINT an override `command` arrives as argv. This is the
    point at which an attempted command override is refused — and it must happen BEFORE
    the DSN is read and BEFORE any connection is attempted."""
    monkeypatch.setenv("DATABASE_URL", DSN)
    state = install_fake_psycopg(monkeypatch, rows=[("98289430a3ec",)])
    rc = R.main(argv)
    cap = capsys.readouterr()
    assert rc == R.EXIT_ARGV_REJECTED
    assert cap.err == "revision-reader: READER-ARGV-REJECTED\n"
    assert cap.out == ""
    assert state["connects"] == 0, "argv must be rejected before any connection attempt"


def test_argv_rejection_has_its_own_exit_code(monkeypatch, capsys):
    """Distinct from config failure ON PURPOSE: stray argv is the fingerprint of an
    attempted command override, so it must be identifiable from the exit code alone."""
    assert R.EXIT_ARGV_REJECTED != R.EXIT_CONFIG_FAILED


def test_exactly_one_connection_per_run(env):
    state = install_fake_psycopg(env, rows=[("98289430a3ec",)])
    run()
    assert state["connects"] == 1


def test_connection_closed_even_on_success(env):
    state = install_fake_psycopg(env, rows=[("98289430a3ec",)])
    run()
    assert state["conn"].closed == 1


def test_read_only_enforced_two_ways(env):
    state = install_fake_psycopg(env, rows=[("98289430a3ec",)])
    run()
    # Server-side, before the first statement...
    assert "default_transaction_read_only=on" in state["kwargs"]["options"]
    # ...and reasserted at session level.
    assert state["conn"].read_only is True


def test_bounded_timeouts_passed(env):
    state = install_fake_psycopg(env, rows=[("98289430a3ec",)])
    run()
    assert state["kwargs"]["connect_timeout"] == R._CONNECT_TIMEOUT_SECONDS
    assert f"statement_timeout={R._STATEMENT_TIMEOUT_MS}" in state["kwargs"]["options"]


def test_autocommit_disabled(env):
    state = install_fake_psycopg(env, rows=[("98289430a3ec",)])
    run()
    assert state["kwargs"]["autocommit"] is False


def test_pg_environment_scrubbed_before_connect(env):
    """libpq reads PG* variables, and containerOverrides CAN set environment — so PGOPTIONS
    or PGSSLMODE could otherwise weaken TLS or clear the read-only GUC."""
    env.setenv("PGOPTIONS", "-c default_transaction_read_only=off")
    env.setenv("PGSSLMODE", "disable")
    env.setenv("PGHOST", "attacker.invalid")
    import os

    install_fake_psycopg(env, rows=[("98289430a3ec",)])
    run()
    assert not [k for k in os.environ if k.startswith("PG")], "PG* must be scrubbed"


def test_only_one_statement_executed_and_it_is_the_fixed_select(env):
    state = install_fake_psycopg(env, rows=[("98289430a3ec",)])
    run()
    assert state["conn"]._cur.executed == ["SELECT version_num FROM alembic_version"]


def test_no_dsn_or_credential_ever_reaches_output(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", SENTINEL_DSN)
    install_fake_psycopg(monkeypatch, connect_exc=RuntimeError(SENTINEL_DSN))
    rc = R.main([])
    cap = capsys.readouterr()
    # Positive control first: the classification actually emitted.
    assert rc == R.EXIT_CONNECT_FAILED
    assert cap.err == "revision-reader: READER-CONNECTION-FAILED\n"
    # Then the absence assertions.
    blob = cap.out + cap.err
    assert "sn-sentinel-p4ss" not in blob
    assert "db-sentinel-host" not in blob
    assert "Traceback" not in blob


def test_unexpected_failure_never_escapes(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", DSN)
    install_fake_psycopg(monkeypatch, query_exc=RuntimeError("boom"))
    rc = R.main([])
    assert rc == R.EXIT_UNEXPECTED
    assert capsys.readouterr().err == "revision-reader: READER-UNEXPECTED-FAILED\n"


# --------------------------------------------------------------------------- #
# 12-18. Structural properties — the safe-by-construction assertions
# --------------------------------------------------------------------------- #
def _reader_source() -> str:
    return (Path(R.__file__)).read_text()


def _imported_names() -> set[str]:
    tree = ast.parse(_reader_source())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            m = node.module or ""
            names.add(m)
            names.update(f"{m}.{a.name}" for a in node.names)
    return names


def test_imports_no_application_no_alembic_no_orm():
    """The single most important structural assertion in this gate: the reader cannot
    import migration capability because it does not depend on any of it."""
    names = _imported_names()
    for forbidden in ("app", "alembic", "sqlalchemy", "pydantic", "boto3", "fastapi"):
        assert not any(n == forbidden or n.startswith(forbidden + ".") for n in names), forbidden


def test_no_shell_or_subprocess_capability():
    names = _imported_names()
    for forbidden in ("subprocess", "os.system", "pty", "shutil"):
        assert forbidden not in names
    src = _reader_source()
    assert "os.system" not in src and "popen" not in src


def test_exactly_one_sql_statement_in_the_module():
    """A second SQL literal appearing here is a design change that must be reviewed.

    Docstrings are excluded deliberately: prose that *discusses* DDL (as this module's
    own docstring does, to state the credential limitation honestly) is not executable
    SQL, and a check that conflated the two would push authors toward vaguer comments.
    """
    tree = ast.parse(_reader_source())
    docstrings = {
        ast.get_docstring(n, clean=False)
        for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    sql = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value not in docstrings
        and any(
            k in n.value.upper()
            for k in ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP ")
        )
    ]
    assert sql == ["SELECT version_num FROM alembic_version"]


def test_no_ddl_or_dml_verbs_anywhere_in_source():
    src = _reader_source().upper()
    verbs = ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE",
             "ALTER TABLE", "DROP TABLE", "COMMIT(")
    for verb in verbs:
        assert verb not in src, verb


def test_no_logging_api_used():
    names = _imported_names()
    assert "logging" not in names
    assert "get_logger" not in _reader_source()


def test_failure_tokens_are_a_frozen_set():
    assert set(R._TOKENS.values()) == {
        "READER-ARGV-REJECTED",
        "READER-CONFIG-FAILED",
        "READER-CONNECTION-FAILED",
        "READER-VERSION-TABLE-MISSING",
        "READER-ZERO-REVISIONS",
        "READER-MULTIPLE-REVISIONS",
        "READER-REVISION-MALFORMED",
        "READER-UNEXPECTED-FAILED",
    }


def test_exit_codes_distinct_and_in_the_reserved_band():
    codes = [
        R.EXIT_ARGV_REJECTED, R.EXIT_CONFIG_FAILED, R.EXIT_CONNECT_FAILED,
        R.EXIT_TABLE_MISSING, R.EXIT_NO_ROWS, R.EXIT_MULTIPLE_ROWS,
        R.EXIT_MALFORMED_REVISION, R.EXIT_UNEXPECTED,
    ]
    assert len(set(codes)) == len(codes)
    # Band 50-57: disjoint from migrate (0,2-7), bootstrap (10-20),
    # revision_status (30-36) and revision_compare (40-44).
    assert all(50 <= c <= 57 for c in codes)
    assert R.EXIT_OK == 0


def test_no_expected_head_or_comparison_in_the_reader():
    """The expected revision never enters the container, so the reader cannot be argued
    into agreeing with itself. Comparison happens externally."""
    src = _reader_source()
    tokens = ("EXPECTED_HEAD", "expected_head", "compare",
              "ScriptDirectory", "get_current_head")
    for token in tokens:
        assert token not in src, token
