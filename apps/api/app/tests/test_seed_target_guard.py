"""P6-SEC-DEMO-SEED-TARGET-GUARD — demo seeding must refuse non-local targets.

``app.db.seed`` creates a deterministic, publicly-documented OWNER account
(``demo@signalnest.dev``) and, with ``--reset``, deletes every row of every table
in ``Base.metadata``. Neither was gated on the database being seeded: the only
environment check in the module gates ``is_operator`` and reads the *process*
configuration, so ``DATABASE_URL=<postgres> python -m app.db.seed`` from a
development shell minted that owner — with operator rights — in whatever database
the URL pointed at.

The contract these tests pin:

    seeding is permitted  <=>  environment in {development, test}
                               AND the configured backend is sqlite
                               AND the session factory is bound to sqlite

It is an ALLOWLIST, never a denylist. ``postgres://`` (no ``ql``) resolves to
backend ``"postgres"``, so ``Settings.is_postgres`` is *False* for a perfectly
real PostgreSQL server; a guard phrased as "deny if is_postgres" would wave it
through. The allowlist also fails closed on ``mysql``, and on any future backend
nobody has thought about yet.

The third limb matters as much as the first two. ``seed()`` writes through the
module-global ``SessionLocal``, which is rebound by eight test modules in this
suite. Checking only ``Settings`` would adjudicate a URL the seed never touches
while the writes went somewhere else entirely.

Every assertion here is reachable without a database server: ``make_url`` parses
without importing a driver, and ``create_engine``/``sessionmaker``/``SessionLocal()``
perform no I/O. Nothing in this module connects to anything.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db import seed as seed_mod

# A DSN whose every component is an unmistakable sentinel. If any of these
# substrings reaches an error message, a log or stderr, the refusal is leaking
# the very thing it exists to protect.
SENTINEL_USER = "s3nt1nel-user"
SENTINEL_PASSWORD = "s3nt1nel-password"
SENTINEL_HOST = "s3nt1nel-host.example.invalid"
SENTINEL_DB = "s3nt1nel-database"
SENTINEL_DSN = (
    f"postgresql+psycopg://{SENTINEL_USER}:{SENTINEL_PASSWORD}"
    f"@{SENTINEL_HOST}:5432/{SENTINEL_DB}"
)
SENTINEL_PARTS = (SENTINEL_USER, SENTINEL_PASSWORD, SENTINEL_HOST, SENTINEL_DB)

# staging/production Settings refuse to construct without these; they are inert
# test values and carry no meaning beyond satisfying the validators.
_PRODUCTION_LIKE_REQUIRED: dict[str, Any] = {
    "secret_key": "x" * 64,
    "llm_provider": "openai",
    "llm_api_key": "k" * 20,
}


def _settings(environment: str, database_url: str, **extra: Any) -> Settings:
    """Build Settings directly, never through the cached get_settings()."""
    if environment in ("staging", "production"):
        extra = {**_PRODUCTION_LIKE_REQUIRED, **extra}
    if environment == "production":
        extra = {
            "app_mode": "full",
            "queue_backend": "redis",
            "cache_backend": "redis",
            "vector_backend": "pgvector",
            "storage_backend": "s3",
            "redis_url": "redis://unused.invalid:6379/0",
            "s3_bucket": "unused",
            **extra,
        }
    return Settings(
        _env_file=None, environment=environment, database_url=database_url, **extra
    )


def _sqlite_factory(tmp_path) -> sessionmaker:
    """A session factory bound to a real, local, throwaway SQLite file."""
    engine = create_engine(f"sqlite:///{tmp_path / 'guard.db'}", future=True)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _postgres_factory() -> sessionmaker:
    """A factory bound to a PostgreSQL URL. Constructed only, never connected."""
    return sessionmaker(bind=create_engine(SENTINEL_DSN), future=True)


class Exploded(Exception):
    """Raised by a sentinel to prove the guard did not dominate a code path."""


def _sentinel(label: str):
    def _boom(*_args: Any, **_kwargs: Any):
        raise Exploded(f"guard did not run before {label}")

    return _boom


# --------------------------------------------------------------------------
# Policy — the allowlist itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize("environment", ["development", "test"])
def test_local_sqlite_is_permitted(environment, tmp_path):
    """The only supported demo workflow must keep working.

    `development` is what a developer's shell defaults to; `test` is what CI's
    workflow-level env (ci.yml) sets for all five of its seed executions --
    three direct (`:200`, `:202`, `:208`, the last being `--reset`) and two via
    scripts/demo-setup.sh (`:149`, `:403`). Narrowing the allowlist to either
    value alone breaks the other.
    """
    seed_mod._require_safe_seed_target(
        _settings(environment, "sqlite:///./signalnest.db"),
        session_factory=_sqlite_factory(tmp_path),
    )


@pytest.mark.parametrize(
    "environment,database_url,why",
    [
        # The exact reported hazard: a development process aimed elsewhere.
        ("development", "postgresql+psycopg://u:p@h:5432/d", "dev + postgres"),
        ("test", "postgresql+psycopg://u:p@h:5432/d", "test + postgres"),
        ("staging", "postgresql+psycopg://u:p@h:5432/d", "staging + postgres"),
        ("production", "postgresql+psycopg://u:p@h:5432/d", "production + postgres"),
        # Only the environment limb rejects this one, which is why the contract
        # is a conjunction and not just a backend check.
        ("staging", "sqlite:///./signalnest.db", "staging + sqlite"),
        # `postgres://` resolves to backend "postgres", so is_postgres is False.
        # A denylist keyed on is_postgres would admit a real PostgreSQL server.
        ("development", "postgres://u:p@h:5432/d", "legacy postgres:// scheme"),
        # Anything unrecognised is refused rather than assumed harmless.
        ("development", "mysql://u:p@h:3306/d", "unsupported backend"),
    ],
)
def test_unsafe_targets_are_refused(environment, database_url, why, tmp_path):
    with pytest.raises(seed_mod.SeedTargetError):
        seed_mod._require_safe_seed_target(
            _settings(environment, database_url),
            session_factory=_sqlite_factory(tmp_path),
        )


def test_refusal_does_not_depend_on_the_url_being_reachable():
    """The decision is made from configuration alone — nothing is contacted.

    The sentinel DSN points at `.invalid`, which by RFC 6761 can never resolve.
    If the guard tried to connect in order to classify the target, this would
    hang or raise a connection error instead of refusing cleanly.
    """
    with pytest.raises(seed_mod.SeedTargetError):
        seed_mod._require_safe_seed_target(
            _settings("development", SENTINEL_DSN),
            session_factory=_postgres_factory(),
        )


# --------------------------------------------------------------------------
# The bound session factory is the real write target
# --------------------------------------------------------------------------


def test_safe_settings_cannot_launder_a_postgres_bind():
    """Settings saying "sqlite" must not license writes to a PostgreSQL bind.

    `seed()` writes through the module-global `SessionLocal`, which is rebindable
    (eight modules in this suite rebind it). A guard that consulted only Settings
    would approve this call and then wipe and repopulate whatever the factory is
    actually bound to.
    """
    with pytest.raises(seed_mod.SeedTargetError):
        seed_mod._require_safe_seed_target(
            _settings("development", "sqlite:///./signalnest.db"),
            session_factory=_postgres_factory(),
        )


def test_the_default_factory_is_adjudicated_when_none_is_injected(monkeypatch):
    """The bind limb must work on the path that actually ships.

    No production caller passes `session_factory`: `seed`, `_reset` and `main`
    all rely on the module-global `SessionLocal`. A test that only ever reaches
    the limb through the injected parameter leaves the real path uncovered --
    deleting the limb for the default case would stay invisible. This asserts it
    against the global, with settings that are entirely safe, so the bind is the
    only thing that can refuse.
    """
    monkeypatch.setattr(seed_mod, "SessionLocal", _postgres_factory())

    with pytest.raises(seed_mod.SeedTargetError):
        seed_mod._require_safe_seed_target(
            _settings("development", "sqlite:///./signalnest.db")
        )


def test_the_default_factory_admits_a_safe_target(monkeypatch, tmp_path):
    """The default path must still ADMIT the supported workflow.

    Paired deliberately with the refusal test above. A guard that refuses
    everything on the default path would satisfy the refusal test while breaking
    `npm run demo:setup`, CI, and the eight modules that seed in-process -- so
    the permissive direction needs its own assertion, not just the restrictive one.
    """
    monkeypatch.setattr(seed_mod, "SessionLocal", _sqlite_factory(tmp_path))

    seed_mod._require_safe_seed_target(
        _settings("development", "sqlite:///./signalnest.db")
    )


def test_a_bind_that_is_not_an_engine_is_refused_rather_than_crashing(tmp_path):
    """An unclassifiable bind fails closed with the guard's own error type.

    A Connection, a mock, or any stray object has no `.url`. Raising
    AttributeError here would still stop the seed, but it would be an untested
    failure mode wearing the wrong exception type -- and callers that catch
    SeedTargetError (main(), converting to SystemExit) would miss it entirely.
    """
    with pytest.raises(seed_mod.SeedTargetError):
        seed_mod._require_safe_seed_target(
            _settings("development", "sqlite:///./signalnest.db"),
            bind=object(),
        )


def test_reset_adjudicates_the_session_it_deletes_through(monkeypatch, tmp_path):
    """`_reset` must judge its own argument, not the module global.

    It takes a caller-supplied Session and issues an unfiltered DELETE against
    every mapped table. With a safe global and safe settings, trusting the global
    would let a foreign Session through and delete every row behind it.
    """
    monkeypatch.setattr(seed_mod, "SessionLocal", _sqlite_factory(tmp_path))
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./signalnest.db")
    from app.core.config import get_settings

    get_settings.cache_clear()
    foreign = _postgres_factory()()  # a Session, not connected
    try:
        with pytest.raises(seed_mod.SeedTargetError):
            seed_mod._reset(foreign)
    finally:
        foreign.close()
        get_settings.cache_clear()


def test_an_unbound_session_factory_is_refused():
    """An undeterminable target is refused, not assumed safe."""
    with pytest.raises(seed_mod.SeedTargetError):
        seed_mod._require_safe_seed_target(
            _settings("development", "sqlite:///./signalnest.db"),
            session_factory=sessionmaker(),
        )


# --------------------------------------------------------------------------
# The guard must dominate every path that touches or destroys data
# --------------------------------------------------------------------------


def test_unsafe_direct_seed_is_refused_before_a_session_exists(monkeypatch, tmp_path):
    """UNSAFE_DIRECT_SEED_REFUSED_BEFORE_SESSION.

    `seed()` is a public function whose first statement opens a session. Guarding
    only `main()` would leave this path — the one any future code reaches
    silently, and the shape eight test modules already use — wide open.
    """
    monkeypatch.setattr(seed_mod, "SessionLocal", _sentinel("SessionLocal()"))
    monkeypatch.setattr(seed_mod, "_reset", _sentinel("_reset()"))

    with pytest.raises(seed_mod.SeedTargetError):
        seed_mod.seed(settings=_settings("development", SENTINEL_DSN))


def test_unsafe_reset_is_refused_before_any_delete(monkeypatch, tmp_path):
    """UNSAFE_RESET_REFUSED_BEFORE_DELETE.

    `_reset` issues an unfiltered DELETE against every table in Base.metadata —
    every tenant's data, not just the demo fixture. It must be unreachable for an
    unsafe target.
    """
    monkeypatch.setattr(seed_mod, "SessionLocal", _sentinel("SessionLocal()"))
    monkeypatch.setattr(seed_mod, "_reset", _sentinel("_reset()"))

    with pytest.raises(seed_mod.SeedTargetError):
        seed_mod.seed(reset=True, settings=_settings("development", SENTINEL_DSN))


def test_reset_helper_refuses_on_its_own(monkeypatch):
    """`_reset` is guarded itself, not merely behind a guarded caller.

    The leading underscore is a convention, not an access control; the function is
    importable and takes a caller-supplied Session.
    """
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DATABASE_URL", SENTINEL_DSN)
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(seed_mod.SeedTargetError):
            seed_mod._reset(_sentinel("db.execute()"))
    finally:
        get_settings.cache_clear()


def test_unsafe_cli_is_refused_before_schema_inspection(monkeypatch):
    """UNSAFE_CLI_REFUSED_BEFORE_DB_INSPECT.

    `main()` calls `inspect(engine).get_table_names()` before it ever calls
    `seed()`, and that call opens a real connection. The guard has to precede it,
    so a guard placed only inside `seed()` is insufficient for the CLI.
    """
    import sqlalchemy

    monkeypatch.setattr(sqlalchemy, "inspect", _sentinel("inspect(engine)"))
    monkeypatch.setattr(seed_mod, "SessionLocal", _sentinel("SessionLocal()"))
    monkeypatch.setattr(sys, "argv", ["app.db.seed"])
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DATABASE_URL", SENTINEL_DSN)

    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        # SystemExit, not a traceback: this is an operator configuration mistake,
        # and it must read as one line. The type is still SeedTargetError for
        # programmatic callers.
        with pytest.raises(SystemExit) as excinfo:
            seed_mod.main()
    finally:
        get_settings.cache_clear()

    message = str(excinfo.value)
    assert "development/test SQLite targets" in message
    for part in SENTINEL_PARTS:
        assert part not in message


def test_unsafe_cli_reset_is_refused_before_schema_inspection(monkeypatch):
    """The destructive CLI flag gets the same treatment, and gets it first."""
    import sqlalchemy

    monkeypatch.setattr(sqlalchemy, "inspect", _sentinel("inspect(engine)"))
    monkeypatch.setattr(seed_mod, "SessionLocal", _sentinel("SessionLocal()"))
    monkeypatch.setattr(seed_mod, "_reset", _sentinel("_reset()"))
    monkeypatch.setattr(sys, "argv", ["app.db.seed", "--reset"])
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DATABASE_URL", SENTINEL_DSN)

    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as excinfo:
            seed_mod.main()
    finally:
        get_settings.cache_clear()

    message = str(excinfo.value)
    assert "development/test SQLite targets" in message
    for part in SENTINEL_PARTS:
        assert part not in message


# --------------------------------------------------------------------------
# Secret hygiene
# --------------------------------------------------------------------------


def test_refusal_never_discloses_the_connection_string(capsys, tmp_path):
    """UNSAFE_SEED_ERROR_DSN_DISCLOSURE = false.

    Not just the password: a masked DSN still names the user, host, port and
    database. The refusal reports the two non-secret facts that make it
    self-diagnosing — the environment and the backend — and nothing else.
    """
    with pytest.raises(seed_mod.SeedTargetError) as excinfo:
        seed_mod._require_safe_seed_target(
            _settings("development", SENTINEL_DSN),
            session_factory=_postgres_factory(),
        )

    message = str(excinfo.value)
    captured = capsys.readouterr()
    haystack = f"{message}\n{captured.out}\n{captured.err}"

    for part in SENTINEL_PARTS:
        assert part not in haystack, f"refusal disclosed {part!r}"

    # Self-diagnosing without being disclosing.
    assert "development" in message
    assert "postgresql" in message
