"""P6-SEC-MIGRATE-DOWNGRADE-TARGET-GUARD — the confirmation contract.

A downgrade destroys schema and rows. The hazard is target confusion: the operator
means database A, the process points at database B. A generic ``--force`` confirms
intent to downgrade *something*, so authorization here binds the whole transition —
which database, which revision it is on, what was asked for, where that lands, and
what gets destroyed on the way.

Two properties of this module are deliberate:

* **The digest is pinned by literal value.** Computing the expected value with the
  same function that produces the actual one proves only that a function equals
  itself. The constants below were derived independently from the documented
  material and reviewed, so a change in the algorithm fails here rather than
  silently re-baselining.
* **Nothing connects to anything.** ``make_url`` parses without importing a driver,
  and the end-to-end downgrade behaviour is covered by the migration modules that
  run real Alembic against a temp SQLite file.
"""

from __future__ import annotations

import pathlib

import pytest

from app.db import downgrade_guard as guard

API_DIR = pathlib.Path(__file__).resolve().parents[2]

# A synthetic target whose every component is distinctive, so a leak is obvious.
URL = "postgresql+psycopg://sn_user:s3cr3t@db.example.invalid:5432/signalnest"

SOURCE = "98289430a3ec"
DESTINATION = "4945b98229e6"

# A live identity and a chain identity fixed as literals, so the vector below is
# a property of the documented format and not of whatever the helpers return.
LIVE = "postgresql|10.0.1.5|5432|signalnest"
CHAIN_ID = "c0ffee0123456789"

# Derived independently from the documented canonical format, NOT by calling the
# code under test. Fields are length-prefixed as `<bytelen>:<utf-8 bytes>` and
# concatenated with no separator, then sha256, then the first 16 hex chars:
#
#   fingerprint = sha256("23:signalnest-downgrade-v2"
#                        "6:target"
#                        "45:postgresql|db.example.invalid|5432|signalnest")[:16]
#   token       = sha256("23:signalnest-downgrade-v2"
#                        "16:<fingerprint>"
#                        "35:postgresql|10.0.1.5|5432|signalnest"
#                        "12:98289430a3ec" "2:-1" "12:4945b98229e6"
#                        "16:c0ffee0123456789")[:16]
#
# Both values were reproduced by two constructions that import nothing from the
# application: a standalone `hashlib` script and `printf | shasum -a 256`.
PINNED_FINGERPRINT = "9fc345d65cf76cb8"
PINNED_TOKEN = "be6e216e39f93565"


def _token(**overrides):
    kwargs = {
        "database_url": URL,
        "live_identity": LIVE,
        "source_revision": SOURCE,
        "requested_target": "-1",
        "resolved_destination": DESTINATION,
        "chain_identity": CHAIN_ID,
    }
    kwargs.update(overrides)
    return guard.confirmation_token(**kwargs)


# --------------------------------------------------------------------------
# The algorithm itself, pinned independently
# --------------------------------------------------------------------------


def test_fingerprint_matches_its_independently_derived_value():
    assert guard.target_fingerprint(URL) == PINNED_FINGERPRINT


def test_token_matches_its_independently_derived_value():
    assert _token() == PINNED_TOKEN


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_revision": "b2c3d4e5f6a7"},
        {"requested_target": "base"},
        {"resolved_destination": "b2c3d4e5f6a7"},
        {"chain_identity": "0000000000000000"},
        {"live_identity": "postgresql|10.0.2.7|5432|signalnest"},
        {"database_url": "postgresql://sn_user:s3cr3t@db.example.invalid:5432/OTHER"},
    ],
    ids=["source", "requested", "destination", "chain", "live identity", "target"],
)
def test_every_bound_component_changes_the_token(overrides):
    """Each element is load-bearing: mutate one, the confirmation stops matching."""
    assert _token(**overrides) != PINNED_TOKEN


# --------------------------------------------------------------------------
# What counts as the same database
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://sn_user:s3cr3t@db.example.invalid:5432/signalnest",
        "postgresql://sn_user:ROTATED@db.example.invalid:5432/signalnest",
        "postgresql://other_role:s3cr3t@db.example.invalid:5432/signalnest",
        "postgresql://sn_user:s3cr3t@db.example.invalid/signalnest",
    ],
    ids=["no driver suffix", "password rotated", "username changed", "default port"],
)
def test_same_database_keeps_one_identity(url):
    """A rotated credential is not a different database.

    If it were, every rotation would invalidate confirmations and train operators
    to re-mint without reading them. The default port is folded in for the same
    reason: `host/db` and `host:5432/db` denote one server.
    """
    assert guard.target_fingerprint(url) == PINNED_FINGERPRINT


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://sn_user:s3cr3t@db.example.invalid:5432/OTHERDB",
        "postgresql://sn_user:s3cr3t@other.invalid:5432/signalnest",
        "postgresql://sn_user:s3cr3t@db.example.invalid:6543/signalnest",
    ],
    ids=["database", "host", "port"],
)
def test_a_different_database_gets_a_different_identity(url):
    assert guard.target_fingerprint(url) != PINNED_FINGERPRINT


@pytest.mark.parametrize(
    "url",
    ["postgres://u:p@h:5432/d", "mysql://u:p@h:3306/d", "not a url", "", "sqlite://"],
    ids=["legacy postgres scheme", "unsupported backend", "malformed", "empty", "memory"],
)
def test_unidentifiable_targets_are_refused(url):
    """Fail closed: a target that cannot be named can never match a confirmation.

    `postgres://` is not folded into `postgresql://` — SQLAlchemy reports its
    backend as `postgres` and will not build an engine for it, so treating them as
    synonyms would invent an identity for an unreachable target.
    """
    with pytest.raises(guard.DowngradeTargetError):
        guard.target_fingerprint(url)


# --------------------------------------------------------------------------
# Exact match, and nothing else
# --------------------------------------------------------------------------


def _require(supplied):
    guard.require_confirmation(
        supplied=supplied,
        expected=PINNED_TOKEN,
        fingerprint=PINNED_FINGERPRINT,
        live_target=LIVE,
        source_revision=SOURCE,
        requested_target="-1",
        chain=[SOURCE],
        command=f"alembic -x confirm={PINNED_TOKEN} downgrade -1",
    )


def test_the_exact_confirmation_is_accepted():
    _require(PINNED_TOKEN)


@pytest.mark.parametrize(
    "supplied",
    [
        None,
        "",
        "--force",
        "--yes",
        "true",
        "1",
        "yes",
        PINNED_TOKEN[:8],
        PINNED_TOKEN + "0",
        PINNED_TOKEN.upper(),
        f" {PINNED_TOKEN}",
        f"{PINNED_TOKEN}\n",
        f"{PINNED_TOKEN} ",
    ],
    ids=[
        "absent", "empty", "--force", "--yes", "true", "1", "yes",
        "truncated", "extended", "uppercased", "leading space", "trailing newline",
        "trailing nbsp",
    ],
)
def test_nothing_but_the_exact_confirmation_authorizes(supplied):
    """No truthiness, no prefix match, no trimming, no case folding.

    Each of those turns a near-miss into an accept. A confirmation that accepts
    near-misses is not confirming anything, and `--force` in particular is exactly
    the generic bypass this control exists to avoid.
    """
    with pytest.raises(guard.DowngradeTargetError):
        _require(supplied)


def test_the_refusal_names_the_connected_database_but_no_credential():
    """The target is disclosed on purpose; credentials never are.

    A fingerprint is a hash of what the CONFIGURATION claims, so it cannot tell
    an operator that the connection landed somewhere else. Two servers can
    report the same address, port and database name, and when they do this line
    is the only thing that can prompt a human to look. The database name, server
    address and port carry no credentials; the username, password and the raw
    DSN are never shown.
    """
    with pytest.raises(guard.DowngradeTargetError) as excinfo:
        _require(None)
    message = str(excinfo.value)

    assert PINNED_FINGERPRINT in message
    assert SOURCE in message
    assert LIVE in message, "the operator is not told which database answered"
    for credential in ("s3cr3t", "sn_user", "postgresql+psycopg://"):
        assert credential not in message, f"refusal disclosed {credential!r}"


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------


def test_a_used_confirmation_does_not_authorize_the_next_step():
    """The replay that defeated the first design.

    `-1` is relative, so a confirmation bound only to the requested expression
    authorized a second, different transition once the database moved. Binding the
    source revision makes the token stale the moment it succeeds.
    """
    first = _token(source_revision=SOURCE, resolved_destination=DESTINATION)
    after = _token(
        source_revision=DESTINATION,
        resolved_destination="b2c3d4e5f6a7",
        chain_identity="1111111111111111",
    )
    assert first != after


def test_a_confirmation_for_one_step_does_not_authorize_base():
    assert _token(requested_target="-1") != _token(
        requested_target="base", resolved_destination=None, chain_identity="2222222222222222"
    )


# --------------------------------------------------------------------------
# End to end, through the real Alembic CLI
# --------------------------------------------------------------------------


def _alembic(db_path, *args):
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
    )


def test_an_unconfirmed_downgrade_is_refused_by_the_real_cli(tmp_path):
    """The one test that fails if the guard is simply deleted.

    Everything else here exercises the guard's functions directly, and the
    migration modules supply a confirmation before downgrading -- so all of them
    still pass with the gate removed from `env.py`. This drives the real CLI with
    no confirmation and asserts the database did not move, which is the behaviour
    an operator actually depends on.
    """
    assert _alembic(tmp_path / "e2e.db", "upgrade", "head").returncode == 0
    before = _alembic(tmp_path / "e2e.db", "current").stdout

    refused = _alembic(tmp_path / "e2e.db", "downgrade", "-1")

    assert refused.returncode != 0, refused.stdout
    after = _alembic(tmp_path / "e2e.db", "current").stdout
    assert after == before, "an unconfirmed downgrade changed the database revision"
    # The refusal has to be actionable, not merely non-zero.
    assert "confirm=" in (refused.stdout + refused.stderr)


def test_upgrade_needs_no_confirmation_through_the_real_cli(tmp_path):
    """A false positive here would break every deployment migration actor."""
    assert _alembic(tmp_path / "up.db", "upgrade", "head").returncode == 0
    assert _alembic(tmp_path / "up.db", "check").returncode == 0


# ==========================================================================
# P6-SEC-6U-1F BLOCKER REGRESSIONS
#
# Each test below pins a property the four-lane recovery review proved absent
# from the first implementation. They are written against the repaired
# contract, so on the pre-repair tree each fails for its own reason rather
# than collapsing collection -- hence the function-local imports.
# ==========================================================================


# --- B1: a query parameter must not be able to alias the fingerprint -------


def test_b1_query_parameter_cannot_alias_two_different_servers():
    """`?host=` overrides the authority host in libpq; identity must follow it.

    The psycopg dialects build connect kwargs as `opts.update(url.query)`, so a
    query parameter silently wins over the authority. Two URLs that reach
    different servers must never share a fingerprint.
    """
    authority = "postgresql+psycopg://u:p@authority-host:5432/signalnest"
    overridden = "postgresql+psycopg://u:p@authority-host:5432/signalnest?host=evil-host"
    assert guard.target_fingerprint(authority) != guard.target_fingerprint(overridden)


def test_b1_effective_target_follows_the_routing_override():
    """The identity names where the connection actually lands, not the authority."""
    overridden = "postgresql+psycopg://u:p@authority-host:5432/signalnest?host=evil-host"
    assert "evil-host" in guard.canonical_target(overridden)
    assert "authority-host" not in guard.canonical_target(overridden)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@h:5432/d?service=prod",
        "postgresql://u:p@h:5432/d?target_session_attrs=read-write",
        "postgresql://u:p@a.invalid,b.invalid:5432/d",
        "postgresql://u:p@h:5432/d?host=a.invalid,b.invalid",
        "postgresql://u:p@h:5432/d?sslmode=require&sslmode=disable",
        "postgresql://u:p@h:5432/d?passfile=/tmp/x",
    ],
    ids=["service", "target_session_attrs", "multihost authority",
         "multihost override", "repeated key", "unknown key"],
)
def test_b1_ambiguous_or_unrecognised_routing_is_refused(url):
    """Fail closed: anything that could redirect the target and is not proven
    target-neutral is refused rather than guessed at."""
    with pytest.raises(guard.DowngradeTargetError):
        guard.target_fingerprint(url)


def test_b1_target_neutral_parameters_are_still_accepted():
    """`sslmode=require` is the documented production minimum -- it must work,
    and must not change which database the identity names."""
    plain = "postgresql+psycopg://u:p@db.example.invalid:5432/signalnest"
    tls = "postgresql+psycopg://u:p@db.example.invalid:5432/signalnest?sslmode=require"
    assert guard.target_fingerprint(plain) == guard.target_fingerprint(tls)


# --- Digest encoding must be injective at field boundaries -----------------


def test_digest_field_boundaries_are_unambiguous():
    """A separator join lets a field's content impersonate a field boundary."""
    assert guard._digest("X", "Y\nZ") != guard._digest("X\nY", "Z")
    assert guard._digest("a", "") != guard._digest("", "a")


# --- SQLite in-memory targets ---------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///:memory:",
        "sqlite://",
        "sqlite:///",
        "sqlite:///file::memory:",
        "sqlite:///file:test.db?mode=memory&uri=true",
    ],
    ids=["explicit memory", "bare memory", "empty path", "file memory", "uri mode"],
)
def test_memory_sqlite_targets_are_refused_explicitly(url):
    """An in-memory database cannot be re-identified on a later connection, so
    it can never satisfy a confirmation. Refuse rather than mint a path-shaped
    identity for a target that does not persist."""
    with pytest.raises(guard.DowngradeTargetError):
        guard.target_fingerprint(url)


# --- B4: live identity must be BOUND into the token, not asserted after ----


class _FakeRow(tuple):
    pass


class _FakeConn:
    """Minimal stand-in for a live connection; returns one canned row."""

    def __init__(self, row, raises=None):
        self._row = row
        self._raises = raises

    def exec_driver_sql(self, sql):
        if self._raises is not None:
            raise self._raises
        conn = self

        class _Result:
            def fetchone(self):
                return conn._row

            def fetchall(self):
                return [conn._row]

        return _Result()


def test_b4_live_identity_is_a_hashed_input_to_the_token():
    """Two servers answering the same DSN must not share a confirmation.

    This is the tunnel case: the configured endpoint is identical and only the
    server behind it differs, so nothing but a live-identity limb can separate
    them.
    """
    url = "postgresql+psycopg://u:p@localhost:5432/signalnest"
    common = {
        "database_url": url,
        "source_revision": SOURCE,
        "requested_target": "-1",
        "resolved_destination": DESTINATION,
        "chain_identity": "chain-x",
    }
    staging = guard.confirmation_token(
        live_identity="postgresql|10.0.1.5|5432|signalnest", **common
    )
    production = guard.confirmation_token(
        live_identity="postgresql|10.0.2.7|5432|signalnest", **common
    )
    assert staging != production


def test_b4_hostname_dsn_yields_a_usable_live_identity():
    """The primary backend must actually work: a hostname DSN against a server
    that reports a numeric address must not be refused."""
    conn = _FakeConn(_FakeRow(("signalnest", "10.0.1.5", 5432)))
    identity = guard.live_identity(conn, "postgresql+psycopg://u:p@db.example.invalid:5432/signalnest")
    assert "10.0.1.5" in identity
    assert "signalnest" in identity


def test_b4_a_different_answering_server_changes_the_identity():
    """Binding: the server that answered at minting must answer at enforcing.

    This is the limb that survives when the database NAMES agree, so it is
    tested on the server address rather than on the database name.
    """
    url = "postgresql+psycopg://u:p@db.example.invalid:5432/signalnest"
    first = guard.live_identity(_FakeConn(_FakeRow(("signalnest", "10.0.1.5", 5432))), url)
    second = guard.live_identity(_FakeConn(_FakeRow(("signalnest", "10.0.2.7", 5432))), url)
    assert first != second


def test_b4_a_pool_name_differing_from_the_backend_is_refused():
    """A pool name that differs from the backend database is REFUSED.

    This assertion is deliberately the inverse of one that briefly existed here.
    The gate was removed to accommodate a pooler that remaps database names, on
    a premise with no basis in this repository -- the only occurrences of pooler
    language were the change and the tests written to justify it, citing each
    other. Removing it reopened the persistent-misroute case, which is the
    module's primary documented threat and is not pooler-specific at all.

    Adopting a name-remapping pooler is therefore a DELIBERATE, REVIEWED change,
    not a bug to be fixed by deleting this assertion.
    """
    conn = _FakeConn(_FakeRow(("realdb", "10.0.1.5", 5432)))
    with pytest.raises(guard.DowngradeTargetError):
        guard.live_identity(conn, "postgresql+psycopg://u:p@pooler:6432/mydb")


def test_b4_a_unix_socket_target_is_refused():
    """`inet_server_addr()` and `inet_server_port()` are both NULL over a unix
    socket, so every socket connection reports the same live identity regardless
    of which server answered -- the bound live-identity limb would silently
    degrade to database-name-only. Refuse the transport instead of shipping a
    limb that does not discriminate on it."""
    with pytest.raises(guard.DowngradeTargetError):
        guard.target_fingerprint("postgresql:///signalnest?host=/var/run/postgresql")


def test_b4_unreadable_live_identity_fails_closed():
    conn = _FakeConn(None, raises=RuntimeError("permission denied"))
    with pytest.raises(guard.DowngradeTargetError):
        guard.live_identity(conn, "postgresql+psycopg://u:p@db.example.invalid:5432/signalnest")


def test_b4_live_identity_failure_does_not_leak_the_dsn():
    conn = _FakeConn(None, raises=RuntimeError("password=s3cr3t host=db.example.invalid"))
    with pytest.raises(guard.DowngradeTargetError) as excinfo:
        guard.live_identity(conn, "postgresql+psycopg://sn_user:s3cr3t@db.example.invalid:5432/signalnest")
    message = str(excinfo.value)
    for secret in ("s3cr3t", "sn_user", "db.example.invalid"):
        assert secret not in message


# --- B5: the migration BODY must be bound, not just the revision ids -------


def _write_script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


class _FakeScript:
    def __init__(self, mapping):
        self._mapping = mapping

    def get_revision(self, rev):
        class _Rev:
            path = str(self._mapping[rev])

        return _Rev()


def test_b5_same_revision_ids_different_body_changes_the_identity(tmp_path):
    """R2's executed bypass: identical ids, identical graph shape, a different
    `downgrade()` body -- and the same token, authorizing different destruction."""
    a = _write_script(tmp_path, "a.py", "def downgrade():\n    op.drop_table('x')\n")
    b = _write_script(
        tmp_path, "b.py", "def downgrade():\n    op.drop_table('x')\n    op.drop_table('y')\n"
    )
    first = guard.chain_identity(_FakeScript({SOURCE: a}), [SOURCE])
    second = guard.chain_identity(_FakeScript({SOURCE: b}), [SOURCE])
    assert first != second


def test_b5_identical_body_in_a_different_location_is_the_same_identity(tmp_path):
    """Deterministic from content, so a token survives a different checkout path
    and never depends on mtimes or filesystem metadata."""
    body = "def downgrade():\n    op.drop_table('x')\n"
    (tmp_path / "checkout_a").mkdir()
    (tmp_path / "checkout_b").mkdir()
    one = _write_script(tmp_path / "checkout_a", "m.py", body)
    two = _write_script(tmp_path / "checkout_b", "different_name.py", body)
    assert guard.chain_identity(_FakeScript({SOURCE: one}), [SOURCE]) == guard.chain_identity(
        _FakeScript({SOURCE: two}), [SOURCE]
    )


def test_b5_chain_identity_covers_every_revision_in_the_chain(tmp_path):
    a = _write_script(tmp_path, "a.py", "A")
    b1 = _write_script(tmp_path, "b1.py", "B")
    b2 = _write_script(tmp_path, "b2.py", "B-CHANGED")
    first = guard.chain_identity(_FakeScript({SOURCE: a, DESTINATION: b1}), [SOURCE, DESTINATION])
    second = guard.chain_identity(_FakeScript({SOURCE: a, DESTINATION: b2}), [SOURCE, DESTINATION])
    assert first != second


# --- B6: operator copy must not claim single-use ---------------------------


def test_b6_refusal_does_not_claim_the_confirmation_is_single_use():
    """Decision 1 accepts identical-transition replay. Telling the operator the
    confirmation expires on use asserts a property the design does not provide."""
    with pytest.raises(guard.DowngradeTargetError) as excinfo:
        _require(None)
    message = str(excinfo.value).lower()
    # The false claim the first implementation made.
    assert "stops being valid once the downgrade succeeds" not in message
    # Mentioning single use is fine only as a denial; an unqualified claim is not.
    for claim in ("single-use", "single use"):
        if claim in message:
            assert f"not a {claim}" in message, "operator copy claims single-use semantics"


def test_b6_refusal_says_what_the_confirmation_is_actually_bound_to():
    with pytest.raises(guard.DowngradeTargetError) as excinfo:
        _require(None)
    message = str(excinfo.value).lower()
    assert "bound" in message


# --- R1-F1: unknown Alembic operation names must fail closed ---------------


@pytest.mark.parametrize(
    "name",
    ["upgrade", "retrieve_migrations", "display_version", "do_ensure_version"],
    ids=["upgrade", "check", "current", "ensure_version"],
)
def test_known_safe_operations_are_classified_safe(name):
    assert guard.classify_operation(name) == guard.OP_SAFE


def test_destructive_operations_are_classified_for_the_gate():
    assert guard.classify_operation("downgrade") == guard.OP_DOWNGRADE
    assert guard.classify_operation("do_stamp") == guard.OP_STAMP


@pytest.mark.parametrize(
    "name",
    [None, "_downgrade", "downgrade_impl", "do_stamp_v2", "", "DOWNGRADE"],
    ids=["undetectable", "renamed downgrade", "renamed downgrade 2",
         "renamed stamp", "empty", "case variant"],
)
def test_unknown_operation_names_fail_closed(name):
    """Detection rests on a private Alembic attribute. A rename must refuse, not
    wave the operation through -- the floor pin is `alembic>=1.13`, so a rename
    can arrive on any install."""
    assert guard.classify_operation(name) == guard.OP_REFUSE


# --- CLI-level blockers ----------------------------------------------------


def _migrate(db_path, *args):
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    return subprocess.run(
        [sys.executable, "-m", "app.db.migrate", *args],
        cwd=API_DIR, env=env, capture_output=True, text=True,
    )


def _current(db_path):
    """The bare revision id. `alembic current` appends " (head)", and feeding
    that back as a revision would make a stamp fail for the wrong reason."""
    for line in reversed(_alembic(db_path, "current").stdout.strip().splitlines()):
        line = line.strip()
        if line and not line.startswith(("INFO", "Context", "Will assume")):
            return line.split()[0]
    return ""


def test_b2_an_unconfirmed_stamp_cannot_rearm_a_spent_confirmation(tmp_path):
    """The executed re-arm: a stamp restores `alembic_version` without touching
    schema, and every token input is a pure function of that state -- so a spent
    confirmation becomes valid again with no confirmation anywhere in the loop."""
    db = tmp_path / "rearm.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    head = _current(db)

    token = _migrate(db, "downgrade-confirmation", "-1").stdout.strip()
    assert _alembic(db, "-x", f"confirm={token}", "downgrade", "-1").returncode == 0
    lowered = _current(db)
    assert lowered != head

    # The stamp back up is the re-arm. It must not be free.
    restamp = _alembic(db, "stamp", head)
    assert restamp.returncode != 0, "an unconfirmed forward stamp re-armed the confirmation"
    assert _current(db) == lowered, "an unconfirmed stamp moved alembic_version"


def test_b2_a_backward_stamp_remains_guarded(tmp_path):
    db = tmp_path / "back.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    before = _current(db)
    assert _alembic(db, "stamp", "base").returncode != 0
    assert _current(db) == before


def test_b2_an_unrelated_revision_stamp_remains_guarded(tmp_path):
    db = tmp_path / "unrelated.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    before = _current(db)
    assert _alembic(db, "stamp", "deadbeef1234").returncode != 0
    assert _current(db) == before


def test_b3_offline_destructive_stamp_emits_no_sql(tmp_path):
    """An offline stamp is an authorization-state mutation artifact that can be
    applied later against any database, so it is refused like an offline
    downgrade."""
    db = tmp_path / "offline_stamp.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    result = _alembic(db, "stamp", "base", "--sql")
    assert result.returncode != 0
    assert "UPDATE alembic_version" not in result.stdout
    assert "DELETE FROM alembic_version" not in result.stdout


def test_b3_offline_downgrade_emits_no_destructive_sql(tmp_path):
    db = tmp_path / "offline_down.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    result = _alembic(db, "downgrade", "head:base", "--sql")
    assert result.returncode != 0
    for destructive in ("DROP TABLE", "DROP INDEX", "ALTER TABLE"):
        assert destructive not in result.stdout.upper()


def test_b7_the_wrapper_can_complete_a_confirmed_downgrade(tmp_path):
    """The documented operator escape hatch must actually be usable."""
    db = tmp_path / "wrapper.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    head = _current(db)

    token = _migrate(db, "downgrade-confirmation", "-1").stdout.strip()
    assert token, "no confirmation was printed"
    done = _migrate(db, "downgrade", "-1", "--confirm", token)
    assert done.returncode == 0, done.stderr
    assert _current(db) != head


def test_b7_the_wrapper_refusal_is_actionable_and_secret_free(tmp_path):
    db = tmp_path / "wrapper_refuse.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    before = _current(db)

    refused = _migrate(db, "downgrade", "-1")
    assert refused.returncode != 0
    combined = refused.stdout + refused.stderr
    assert "confirm" in combined, "refusal did not tell the operator how to proceed"
    assert "Traceback" not in combined
    assert _current(db) == before


def test_b7_the_wrapper_rejects_a_wrong_confirmation(tmp_path):
    db = tmp_path / "wrapper_wrong.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    before = _current(db)
    assert _migrate(db, "downgrade", "-1", "--confirm", "0" * 16).returncode != 0
    assert _current(db) == before


def test_upgrade_path_needs_no_confirmation_through_the_wrapper(tmp_path):
    """DEPLOYMENT_UPGRADE_PATH_UNCHANGED: the ECS/Docker actors are ordinary
    upgrade actors and must never be asked for a destructive confirmation."""
    db = tmp_path / "deploy.db"
    assert _migrate(db, "upgrade", "head").returncode == 0
    assert _migrate(db, "check").returncode == 0
    assert _migrate(db).returncode == 0


def test_an_ordinary_path_is_not_mistaken_for_an_in_memory_target(tmp_path):
    """The in-memory precondition must test the URL's mode, not scan the path.

    A substring check over the filename refuses an ordinary on-disk database
    whose path merely contains the text -- a real false refusal on a legitimate
    target, and it still misses the query-parameter form it appears aimed at,
    because SQLAlchemy splits `?mode=memory` into the query rather than leaving
    it in the path.
    """
    assert guard.target_fingerprint(f"sqlite:///{tmp_path}/my_mode=memory_db.sqlite")


# ==========================================================================
# P6-SEC-6U-1F SECOND-ROUND REGRESSIONS
#
# Each of these pins a defect introduced by a FIX rather than by the original
# implementation. They are grouped because that is the pattern worth seeing:
# repairs written under review pressure got less scrutiny than the code they
# repaired.
# ==========================================================================


# --- The database-name axis: a persistent misroute, not merely a change ----


def test_a_persistently_misrouted_database_is_refused():
    """The defect that binding alone could not see.

    Binding detects a CHANGE between minting and enforcing. It cannot detect a
    lie told consistently: a DSN that has always named one database while always
    being answered by another produces the same identity at both ends, so the
    tokens agree and the downgrade proceeds. This is the stale-`.env` case the
    module opens with, so it is refused outright on the one pair that is
    genuinely comparable -- the database NAME.
    """
    conn = _FakeConn(_FakeRow(("signalnest_prod", "10.9.9.9", 6432)))
    with pytest.raises(guard.DowngradeTargetError) as excinfo:
        guard.live_identity(conn, "postgresql+psycopg://u:p@localhost:5432/signalnest")
    message = str(excinfo.value)
    # An undiagnosable refusal is how a bypass gets added later.
    assert "signalnest_prod" in message
    assert "signalnest" in message


def test_the_effective_database_is_what_gets_compared():
    """`?dbname=` overrides the path, and the comparison must follow it.

    Comparing the URL's path component instead would refuse a correct
    connection whenever the routing override is used -- the override this guard
    deliberately folds into the fingerprint.
    """
    conn = _FakeConn(_FakeRow(("other", "10.0.0.5", 5432)))
    assert guard.live_identity(conn, "postgresql://u:p@h:5432/signalnest?dbname=other")


def test_a_shape_refusal_is_not_swallowed_by_the_generic_handler():
    """`DowngradeTargetError` must re-raise ahead of the catch-all, or a precise
    refusal degrades into 'the identity could not be read'."""
    conn = _FakeConn(_FakeRow(("signalnest", "10.0.0.5", 5432)))
    with pytest.raises(guard.DowngradeTargetError) as excinfo:
        guard.live_identity(conn, "postgresql://u:p@h:5432/signalnest?host=@/tmp")
    assert "could not be read" not in str(excinfo.value)


# --- The NULL-identity class: refuse at the point of truth -----------------


@pytest.mark.parametrize(
    "row",
    [
        ("signalnest", "", 0),
        ("signalnest", "", 5432),
        ("signalnest", "10.0.0.5", 0),
        # A driver that returns SQL NULL as Python None rather than the
        # coalesced values. `str(None)` is the truthy string "None", so a check
        # written only as `not str(...)` binds `postgresql|none|<port>|<db>` --
        # a confident, non-discriminating identity. The refusal must not depend
        # on the coalesce in the query text staying as written.
        ("signalnest", None, 5432),
        ("signalnest", "10.0.0.5", None),
        ("signalnest", None, None),
    ],
    ids=["both null", "address null", "port null",
         "address None", "port None", "both None"],
)
def test_a_non_discriminating_live_identity_is_refused(row):
    """Both functions return NULL over a unix socket, so every socket connection
    reports one identity no matter which server answered.

    This is checked at the point of truth -- what the server actually reported --
    rather than inferred from the URL's spelling, so it closes every socket
    transport including spellings not anticipated here.
    """
    conn = _FakeConn(_FakeRow(row))
    with pytest.raises(guard.DowngradeTargetError):
        guard.live_identity(conn, "postgresql://u:p@db.example.invalid:5432/signalnest")


# --- Host shape: an allowlist, and the forms that must keep working --------


@pytest.mark.parametrize(
    "host",
    ["/var/run/postgresql", "@", "@/tmp", "%2Fvar%2Frun%2Fpostgresql", "h%20x", "h%0A"],
    ids=["socket path", "abstract", "abstract path", "encoded path",
         "encoded space", "encoded newline"],
)
def test_socket_shaped_hosts_are_refused(host):
    """libpq selects a unix socket with a leading `/` or `@`. A prefix blacklist
    misses spellings; a shape ALLOWLIST refuses everything that is not a
    hostname or an IP literal, including forms not enumerated here."""
    with pytest.raises(guard.DowngradeTargetError):
        guard.target_fingerprint(f"postgresql://u:p@h:5432/db?host={host}")


@pytest.mark.parametrize(
    "host",
    ["postgres", "localhost", "db.example.invalid", "my_db.internal",
     "127.0.0.1", "::1", "2001:db8::1", "sn.abc123.eu-west-2.rds.amazonaws.com"],
    ids=["compose service", "localhost", "fqdn", "underscore label",
         "ipv4", "ipv6 loopback", "ipv6", "rds endpoint"],
)
def test_real_host_forms_are_accepted(host):
    """Every host form this repository actually uses must keep working.

    Two of them are single-label and dotless (`postgres` from Compose and CI,
    `localhost`), so any FQDN-shaped rule would break the primary deployment and
    every CI PostgreSQL job. Underscores are allowed deliberately: internal DNS
    and Docker use them, and an underscore cannot appear in a socket marker, so
    refusing it buys nothing.
    """
    assert guard.target_fingerprint(f"postgresql://u:p@h:5432/db?host={host}")


def test_ipv6_is_matched_unbracketed():
    """SQLAlchemy strips the brackets before the guard sees the host, so a rule
    written against a bracketed literal would refuse every IPv6 deployment."""
    assert guard.target_fingerprint("postgresql://u:p@[2001:db8::1]:5432/db")


@pytest.mark.parametrize(
    "pair",
    [
        ("PROD.INTERNAL", "prod.internal"),
        ("prod.internal.", "prod.internal"),
        ("2001:DB8::1", "2001:db8::1"),
        ("2001:db8:0:0:0:0:0:1", "2001:db8::1"),
    ],
    ids=["case", "trailing dot", "ipv6 case", "ipv6 expanded"],
)
def test_one_server_has_one_identity(pair):
    """DNS is case-insensitive and IPv6 has many spellings; the digest is neither.

    Without normalisation the same server yields two fingerprints, so a token
    minted under one spelling silently refuses under the other -- a mint/enforce
    mismatch with no security meaning and no obvious cause.
    """
    first, second = pair
    assert guard.target_fingerprint(
        f"postgresql://u:p@h:5432/db?host={first}"
    ) == guard.target_fingerprint(f"postgresql://u:p@h:5432/db?host={second}")


@pytest.mark.parametrize(
    "host", ["0177.0.0.1", "2130706433"], ids=["octal", "integer"],
)
def test_numeric_loopback_aliases_are_refused(host):
    """An all-numeric final label is never a valid hostname (RFC 1123), and these
    are loopback spellings an IP parser rejects. Refusing is the fail-closed
    answer rather than treating them as distinct hostnames."""
    with pytest.raises(guard.DowngradeTargetError):
        guard.target_fingerprint(f"postgresql://u:p@h:5432/db?host={host}")


# --- F-C: repeated keys in the SQLite branch -------------------------------


def test_a_repeated_sqlite_parameter_is_refused():
    """SQLAlchemy represents a repeated parameter as a tuple, so an equality test
    against a single value silently passes it. The PostgreSQL branch already
    refuses repeated keys; this one must too, or an in-memory database slips a
    precondition written to refuse it."""
    with pytest.raises(guard.DowngradeTargetError):
        guard.target_fingerprint("sqlite:///file:x.db?mode=memory&mode=rw&uri=true")
