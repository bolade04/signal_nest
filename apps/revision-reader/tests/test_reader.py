"""Tests for the dedicated revision reader (Gate 4J / 4J.1).

The 4J.1 remediation moved destination authenticity into the image: host, database and role
are baked (``_pinned``) and the reader connects with discrete psycopg keyword arguments to
``sslmode=verify-full`` against a committed CA bundle, taking ONLY the password from the
injected DSN. So the tests assert two things the old suite could not: that the raw DSN is
never forwarded to libpq, and that every value which decides *which* database is read is a
baked constant the DSN cannot influence.

Every no-leak / absence assertion is paired with a positive control proving the path
actually executed. psycopg is injected as a fake through sys.modules so these run with no
database, no driver install, and no network.

The two DSN attack corpora at the bottom are DELIBERATELY DISJOINT and labelled by
provenance: SECURITY_LANE_CORPUS (TLS/parser-model derived) and ADVERSARIAL_LANE_CORPUS
(control-byte / bracket / confusable, derived by driving real libpq). They are not copies of
one enumeration.
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from revision_reader import reader as R  # noqa: E402

BAKED_HOST = "test-db.abc123.us-east-1.rds.amazonaws.com"
BAKED_DBNAME = "signalnest"
BAKED_USER = "app_role"
SENTINEL_PW = "sn-sentinel-p4ss-do-not-leak"


# --------------------------------------------------------------------------- #
# Fake psycopg — connect takes NO positional dsn now; it records every kwarg.
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

    def cursor(self):
        return self._cur

    def close(self):
        self.closed += 1


def install_fake_psycopg(monkeypatch, rows=None, connect_exc=None, query_exc=None):
    """Install a fake psycopg; returns a state dict for assertions."""
    state: dict = {"connects": 0, "conn": None, "kwargs": None, "args": None}
    mod = types.ModuleType("psycopg")

    def connect(*args, **kwargs):
        state["connects"] += 1
        state["args"] = args
        state["kwargs"] = kwargs
        if connect_exc is not None:
            raise connect_exc
        c = _Conn(rows if rows is not None else [], query_exc)
        state["conn"] = c
        return c

    mod.connect = connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", mod)
    return state


@pytest.fixture()
def baked(monkeypatch, tmp_path):
    """Bake valid test pins + a real CA file, and set a MATCHING DATABASE_URL."""
    ca = tmp_path / "rds-global-bundle.pem"
    ca.write_bytes(b"-----BEGIN CERTIFICATE-----\n" + b"x" * 4000 + b"\n")
    monkeypatch.setattr(R._pinned, "EXPECTED_DB_HOST", BAKED_HOST)
    monkeypatch.setattr(R._pinned, "EXPECTED_DB_NAME", BAKED_DBNAME)
    monkeypatch.setattr(R._pinned, "EXPECTED_DB_USER", BAKED_USER)
    monkeypatch.setattr(R._pinned, "CA_BUNDLE_PATH", str(ca))
    dsn = f"postgresql+psycopg://{BAKED_USER}:{SENTINEL_PW}@{BAKED_HOST}:5432/{BAKED_DBNAME}?sslmode=require"
    monkeypatch.setenv("DATABASE_URL", dsn)
    return SimpleNamespace(mp=monkeypatch, ca=str(ca), dsn=dsn)


def set_dsn(baked, dsn):
    baked.mp.setenv("DATABASE_URL", dsn)


def run(argv=None):
    return R.main(argv if argv is not None else [])


def matching_dsn(user=BAKED_USER, pw=SENTINEL_PW, host=BAKED_HOST, db=BAKED_DBNAME,
                 query="?sslmode=require"):
    return f"postgresql+psycopg://{user}:{pw}@{host}:5432/{db}{query}"


# --------------------------------------------------------------------------- #
# 1-5. Success and each failure classification, distinctly (positive controls)
# --------------------------------------------------------------------------- #
def test_one_valid_revision_prints_exactly_one_line(baked, capsys):
    install_fake_psycopg(baked.mp, rows=[("98289430a3ec",)])
    rc = run()
    cap = capsys.readouterr()
    assert rc == R.EXIT_OK
    assert cap.out == "98289430a3ec\n"
    assert cap.err == ""


def test_zero_rows_distinct(baked, capsys):
    install_fake_psycopg(baked.mp, rows=[])
    assert run() == R.EXIT_NO_ROWS
    assert capsys.readouterr().err == "revision-reader: READER-ZERO-REVISIONS\n"


def test_multiple_rows_distinct(baked, capsys):
    install_fake_psycopg(baked.mp, rows=[("98289430a3ec",), ("aaaaaaaaaaaa",)])
    assert run() == R.EXIT_MULTIPLE_ROWS
    assert capsys.readouterr().err == "revision-reader: READER-MULTIPLE-REVISIONS\n"


@pytest.mark.parametrize("bad", ["ZZZZZZZZZZZZ", "98289430A3EC", "9828943", "", "98289430a3ec0"])
def test_malformed_revision_distinct(baked, capsys, bad):
    install_fake_psycopg(baked.mp, rows=[(bad,)])
    assert run() == R.EXIT_MALFORMED_REVISION
    assert capsys.readouterr().err == "revision-reader: READER-REVISION-MALFORMED\n"


def test_missing_table_distinct(baked, capsys):
    exc = RuntimeError("relation does not exist")
    exc.sqlstate = "42P01"  # type: ignore[attr-defined]
    install_fake_psycopg(baked.mp, query_exc=exc)
    assert run() == R.EXIT_TABLE_MISSING
    assert capsys.readouterr().err == "revision-reader: READER-VERSION-TABLE-MISSING\n"


def test_connection_failure_distinct(baked, capsys):
    install_fake_psycopg(baked.mp, connect_exc=RuntimeError("could not connect"))
    assert run() == R.EXIT_CONNECT_FAILED
    assert capsys.readouterr().err == "revision-reader: READER-CONNECTION-FAILED\n"


def test_unexpected_query_failure_distinct(baked, capsys):
    install_fake_psycopg(baked.mp, query_exc=RuntimeError("boom"))  # no sqlstate 42P01
    assert run() == R.EXIT_UNEXPECTED
    assert capsys.readouterr().err == "revision-reader: READER-UNEXPECTED-FAILED\n"


def test_config_failure_when_dsn_absent(baked, capsys):
    baked.mp.delenv("DATABASE_URL", raising=False)
    state = install_fake_psycopg(baked.mp)
    assert run() == R.EXIT_CONFIG_FAILED
    assert state["connects"] == 0
    assert capsys.readouterr().err == "revision-reader: READER-CONFIG-FAILED\n"


# --------------------------------------------------------------------------- #
# THE DISCRETE-PARAMETER CONTRACT — the primary regression guard for 4J.1.
# The raw DSN is never forwarded; every destination value is a baked constant.
# --------------------------------------------------------------------------- #
def test_connect_uses_discrete_kwargs_and_never_forwards_the_dsn(baked):
    state = install_fake_psycopg(baked.mp, rows=[("98289430a3ec",)])
    assert run() == R.EXIT_OK
    assert state["connects"] == 1
    assert state["args"] == ()  # NOTHING positional — no DSN string handed to libpq
    k = state["kwargs"]
    assert k["host"] == BAKED_HOST
    assert k["port"] == R._ALLOWED_PORT == 5432
    assert k["dbname"] == BAKED_DBNAME
    assert k["user"] == BAKED_USER
    assert k["sslmode"] == "verify-full"
    assert k["sslrootcert"] == baked.ca
    assert k["password"] == SENTINEL_PW
    assert k["connect_timeout"] == R._CONNECT_TIMEOUT_SECONDS
    assert "default_transaction_read_only=on" in k["options"]
    assert f"statement_timeout={R._STATEMENT_TIMEOUT_MS}" in k["options"]
    # The full DSN (with its host and sslmode=require) must not appear in ANY connect value.
    for v in list(state["args"]) + list(k.values()):
        assert "sslmode=require" not in str(v)
        assert baked.dsn not in str(v)


def test_password_is_the_only_dsn_derived_value(baked):
    # Change the DSN's host/db/user to junk but keep host==baked (tamper detector) and a
    # valid password; the connection still targets the baked db/user, not the DSN's.
    state = install_fake_psycopg(baked.mp, rows=[("98289430a3ec",)])
    set_dsn(baked, matching_dsn(user="ATTACKER", pw="realpw123", db="otherdb"))
    assert run() == R.EXIT_OK
    assert state["kwargs"]["user"] == BAKED_USER          # not "ATTACKER"
    assert state["kwargs"]["dbname"] == BAKED_DBNAME      # not "otherdb"
    assert state["kwargs"]["password"] == "realpw123"     # the one value from the DSN


def test_query_is_schema_qualified_public(baked):
    st = install_fake_psycopg(baked.mp, rows=[("98289430a3ec",)])
    assert run() == R.EXIT_OK
    assert st["conn"]._cur.executed == ["SELECT version_num FROM public.alembic_version"]


def test_read_only_enforced_two_ways_and_connection_closed(baked):
    st = install_fake_psycopg(baked.mp, rows=[("98289430a3ec",)])
    assert run() == R.EXIT_OK
    assert st["conn"].read_only is True                    # session level
    assert "default_transaction_read_only=on" in st["kwargs"]["options"]  # server level
    assert st["conn"].closed == 1                          # disposed even on success


# --------------------------------------------------------------------------- #
# BLOCKING 1 + 2 — the two CONFIRMED exploits, now structurally closed.
# The DSN host is never used to connect, so neither can steer the destination.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "dsn",
    [
        matching_dsn(host="attacker.example.com"),           # arbitrary DNS host
        matching_dsn(host="203.0.113.9"),                    # arbitrary IP
        # The exact confirmed bracketed-authority bypass from Gate 4J.
        "postgresql://svc:pw@evil.invalid%2Cdb.invalid[v1.x]/appdb?sslmode=require",
    ],
)
def test_confirmed_redirect_exploits_fail_closed_without_connecting(baked, dsn, capsys):
    state = install_fake_psycopg(baked.mp, rows=[("98289430a3ec",)])
    set_dsn(baked, dsn)
    assert run() == R.EXIT_CONFIG_FAILED
    assert state["connects"] == 0                            # never dialled anything
    assert capsys.readouterr().err == "revision-reader: READER-CONFIG-FAILED\n"


def test_host_pin_holds_even_if_tamper_detector_were_bypassed(baked):
    # Belt-and-suspenders: even if a host slipped past the detector, connect() still targets
    # the baked host because the DSN host is never read for the connection.
    state = install_fake_psycopg(baked.mp, rows=[("98289430a3ec",)])
    # A DSN with no host at all is rejected earlier, but prove the connect host is constant:
    assert run() == R.EXIT_OK
    assert state["kwargs"]["host"] == BAKED_HOST


# --------------------------------------------------------------------------- #
# BLOCKING 3 — the DECODED password is gated; a percent-encoded control byte is
# rejected AFTER unquote, before it can truncate libpq's conninfo.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw_pw",
    [
        "s3cr3t%00PW",          # percent-encoded NUL — the BLOCKING 3 vector
        "pw%0Ahost=evil",       # encoded LF
        "pw%09tab",             # encoded TAB
        "pw%20host=evil",       # encoded space + keyword-looking payload
        "pw%7f",                # DEL
        "pw%01ctrl",            # C0 control
        quote("a" * 257, safe=""),  # over length bound
        "",                     # empty password
    ],
)
def test_decoded_password_control_and_bound_rejected(baked, raw_pw):
    state = install_fake_psycopg(baked.mp, rows=[("98289430a3ec",)])
    set_dsn(baked, matching_dsn(pw=raw_pw))
    assert run() == R.EXIT_CONFIG_FAILED
    assert state["connects"] == 0


@pytest.mark.parametrize(
    "raw_pw,decoded",
    [
        ("S3cr3t-x", "S3cr3t-x"),                 # token_urlsafe-shaped
        ("p%40ss", "p@ss"),                       # encoded '@' in a real secret
        ("p%2Fw", "p/w"),                         # encoded '/'
        (quote("pw'x", safe=""), "pw'x"),         # a quote is inert as a discrete kwarg
        ("a" * 256, "a" * 256),                   # exactly at the bound
    ],
)
def test_valid_decoded_passwords_admitted_and_passed_verbatim(baked, raw_pw, decoded):
    state = install_fake_psycopg(baked.mp, rows=[("98289430a3ec",)])
    set_dsn(baked, matching_dsn(pw=raw_pw))
    assert run() == R.EXIT_OK
    assert state["kwargs"]["password"] == decoded


# --------------------------------------------------------------------------- #
# Tamper detector — DSN host must equal the baked host byte-for-byte.
# --------------------------------------------------------------------------- #
def test_tamper_detector_fires_on_host_mismatch(baked):
    state = install_fake_psycopg(baked.mp, rows=[("98289430a3ec",)])
    set_dsn(baked, matching_dsn(host="test-db.abc123.us-east-1.rds.amazonaws.NET"))  # wrong TLD
    assert run() == R.EXIT_CONFIG_FAILED
    assert state["connects"] == 0


# --------------------------------------------------------------------------- #
# Scrub-first — PG* and HOME removed as the very first action, before the DSN.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("var", ["PGSSLMODE", "PGSSLROOTCERT", "PGHOST", "PGHOSTADDR",
                                 "PGPORT", "PGSERVICE", "PGSERVICEFILE", "PGPASSFILE",
                                 "PGOPTIONS", "PGSSLCERT", "HOME"])
def test_connection_environment_scrubbed_before_connect(baked, var):
    import os
    baked.mp.setenv(var, "attacker-value")
    install_fake_psycopg(baked.mp, rows=[("98289430a3ec",)])
    assert run() == R.EXIT_OK
    assert var not in os.environ


def test_scrub_happens_before_argv_rejection(baked):
    import os
    baked.mp.setenv("PGSSLMODE", "disable")
    install_fake_psycopg(baked.mp)
    assert run(["upgrade"]) == R.EXIT_ARGV_REJECTED   # argv still rejected
    assert "PGSSLMODE" not in os.environ              # but the scrub already ran first


# --------------------------------------------------------------------------- #
# argv rejection — before any connection.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("argv", [["--help"], ["-c", "import os"], ["-m", "app.db.migrate"],
                                  ["upgrade"], ["downgrade", "base"], [""]])
def test_any_argv_rejected_before_connect(baked, argv):
    state = install_fake_psycopg(baked.mp)
    assert run(argv) == R.EXIT_ARGV_REJECTED
    assert state["connects"] == 0


# --------------------------------------------------------------------------- #
# Fail-closed on unbaked / malformed pins.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("attr,value", [
    ("EXPECTED_DB_HOST", ""),                       # committed sentinel
    ("EXPECTED_DB_HOST", "nohostdot"),              # no dot -> not a plausible endpoint
    ("EXPECTED_DB_HOST", "UPPER.rds.amazonaws.com"),# uppercase -> rejected
    ("EXPECTED_DB_NAME", ""),
    ("EXPECTED_DB_USER", ""),
])
def test_unbaked_or_malformed_pins_fail_closed(baked, attr, value):
    baked.mp.setattr(R._pinned, attr, value)
    state = install_fake_psycopg(baked.mp, rows=[("98289430a3ec",)])
    assert run() == R.EXIT_CONFIG_FAILED
    assert state["connects"] == 0


def test_missing_ca_bundle_fails_closed(baked, tmp_path):
    baked.mp.setattr(R._pinned, "CA_BUNDLE_PATH", str(tmp_path / "does-not-exist.pem"))
    state = install_fake_psycopg(baked.mp, rows=[("98289430a3ec",)])
    assert run() == R.EXIT_CONFIG_FAILED
    assert state["connects"] == 0


# --------------------------------------------------------------------------- #
# No leakage — the sentinel password never reaches stdout+stderr on any failure,
# each paired with a positive control that the failure path actually executed.
# --------------------------------------------------------------------------- #
def test_no_password_or_dsn_reaches_output_on_connect_failure(baked, capsys):
    install_fake_psycopg(baked.mp, connect_exc=RuntimeError(f"auth failed for {SENTINEL_PW}"))
    rc = run()
    cap = capsys.readouterr()
    assert rc == R.EXIT_CONNECT_FAILED                       # positive control: path ran
    blob = cap.out + cap.err
    assert SENTINEL_PW not in blob
    assert BAKED_HOST not in blob
    assert "Traceback" not in blob


# --------------------------------------------------------------------------- #
# Structural — asserted against the source, so they cannot rot silently.
# --------------------------------------------------------------------------- #
READER_SRC = Path(R.__file__).read_text()
READER_AST = ast.parse(READER_SRC)


def test_exactly_one_sql_string_and_it_is_schema_qualified():
    sql = [
        n.value for n in ast.walk(READER_AST)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value.strip().upper().startswith("SELECT ")
    ]
    assert sql == ["SELECT version_num FROM public.alembic_version"]


def test_no_subprocess_shell_or_dynamic_exec_in_source():
    for bad in ("subprocess", "os.system", "os.popen", "import pty", "eval(", "exec(",
                "__import__("):
        assert bad not in READER_SRC


def test_reader_imports_no_application_or_migration_modules():
    imported: set[str] = set()
    for node in ast.walk(READER_AST):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("app", "alembic", "sqlalchemy", "pydantic", "boto3", "fastapi"):
        assert forbidden not in imported


def test_no_expected_head_or_comparison_in_the_reader():
    # The reader must not know the expected revision; comparison happens offline.
    for token in ("get_current_head", "ScriptDirectory", "EXPECTED_HEAD",
                  "expected_head", "== rev"):
        assert token not in READER_SRC


# --------------------------------------------------------------------------- #
# INDEPENDENT DSN ATTACK CORPORA — two disjoint sets, labelled by provenance.
# Each entry must fail closed (CONFIG, zero connects) OR, if it would be admitted,
# still connect to the baked host with the baked destination values.
# --------------------------------------------------------------------------- #

# Provenance: SECURITY LANE — TLS/parser-model derived (scheme, sslmode, keyword smuggling,
# service/passfile/options, multi-host, fragment, keyword-value form, separators).
SECURITY_LANE_CORPUS = [
    f"mysql://svc:pw@{BAKED_HOST}:5432/db?sslmode=require",            # wrong scheme
    f"http://{BAKED_HOST}:5432/?sslmode=require",                     # wrong scheme
    "postgresql:///db?sslmode=require",                              # no host/userinfo
    f"postgresql://{BAKED_HOST}:5432/db?sslmode=require",            # no userinfo (no pw)
    f"host={BAKED_HOST} port=5432 sslmode=require",                  # libpq keyword-value form
    matching_dsn(query="?sslmode=require&service=evil"),             # service= smuggling
    matching_dsn(query="?sslmode=require&passfile=/tmp/x"),          # passfile= smuggling
    matching_dsn(query="?sslmode=require&options=-c%20x%3Dy"),       # options= smuggling
    matching_dsn(host=f"{BAKED_HOST},evil.invalid"),                # multi-host, comma
    matching_dsn(host=f"evil.invalid,{BAKED_HOST}"),                # multi-host, comma
    "postgresql://svc:p@ss@evil.invalid:443/db?sslmode=require",     # two literal '@'
    matching_dsn() + "#host=evil.invalid",                          # fragment
]

# Provenance: ADVERSARIAL LANE — control-byte / bracket / confusable, derived by driving real
# libpq. Category B (control bytes incl. percent-encoded), A (bracket family), C (confusables
# under case-fold/NFKC), F (port-smuggling to 443), H (multi-level percent-encoding).
ADVERSARIAL_LANE_CORPUS = [
    "postgresql://svc:pw@evil.invalid%2Cdb.invalid[v1.x]/db?sslmode=require",   # A: the exploit
    "postgresql://svc:pw@[v1.x]/db?sslmode=require",                            # A: bracket only
    "postgresql://svc:pw@%2Fvar%2Frun%2Fpg[x]/db?sslmode=require",             # A: unix bracket
    f"postgresql://svc:pw%00@{BAKED_HOST}/db?sslmode=require",                 # B: NUL in userinfo
    f"postgresql://svc:pw%0d%0a@{BAKED_HOST}/db?sslmode=require",              # B: CRLF
    matching_dsn(host="test-db.abc123.us-east-1.rds.amazonaws.coа"),      # C: Cyrillic homoglyph
    matching_dsn(host="TEST-DB.abc123.us-east-1.rds.amazonaws.com"),          # C: case variance
    matching_dsn(host=f"{BAKED_HOST}."),                                      # D: trailing dot
    matching_dsn(host="attacker.example.com"),                                # F: redirect
    "postgresql://svc:pw@x%2Eevil.invalid/db?sslmode=require",                # H: multi-level %
]


@pytest.mark.parametrize("dsn", SECURITY_LANE_CORPUS)
def test_security_lane_corpus_fails_closed_or_targets_baked_host(baked, dsn):
    state = install_fake_psycopg(baked.mp, rows=[("98289430a3ec",)])
    set_dsn(baked, dsn)
    rc = run()
    if rc == R.EXIT_OK:
        # If admitted at all, the destination MUST still be the baked host — never the DSN's.
        assert state["kwargs"]["host"] == BAKED_HOST
        assert state["connects"] == 1
    else:
        assert rc in (R.EXIT_CONFIG_FAILED,)
        assert state["connects"] == 0


@pytest.mark.parametrize("dsn", ADVERSARIAL_LANE_CORPUS)
def test_adversarial_lane_corpus_fails_closed_or_targets_baked_host(baked, dsn):
    state = install_fake_psycopg(baked.mp, rows=[("98289430a3ec",)])
    set_dsn(baked, dsn)
    rc = run()
    if rc == R.EXIT_OK:
        assert state["kwargs"]["host"] == BAKED_HOST
        assert state["kwargs"]["dbname"] == BAKED_DBNAME
        assert state["kwargs"]["user"] == BAKED_USER
        assert state["connects"] == 1
    else:
        # Pin the classification, not just the absence of a connect: a fail-closed case that
        # silently degraded 51 -> 57 must not pass. Every non-admitted adversarial DSN is
        # refused at config validation (host tamper / bad scheme / bad password / bracket).
        assert rc == R.EXIT_CONFIG_FAILED
        assert state["connects"] == 0


def test_the_two_corpora_are_disjoint():
    assert set(SECURITY_LANE_CORPUS).isdisjoint(set(ADVERSARIAL_LANE_CORPUS))
