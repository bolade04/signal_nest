"""P6-SEC-MIGRATE-DOWNGRADE-TARGET-GUARD — confirmation for destructive migrations.

A downgrade destroys schema and the rows inside it. The failure this guards is not
"someone ran the wrong subcommand" but **target confusion**: the operator means to
downgrade database A while the process configuration points at database B. A stale
shell variable, the wrong ``.env``, a command copied out of a runbook, or an SSH
tunnel that makes production answer on ``localhost`` all produce it.

A generic confirmation cannot help, because ``--force`` and friends confirm intent
to downgrade *something* and say nothing about *where*. So authorization here is
bound to the whole transition:

    signalnest-downgrade-v2
      + target fingerprint      -- which database the configuration names
      + live identity           -- which database actually answered
      + source revision         -- read live from that database
      + requested target        -- the literal expression, e.g. "-1" or "base"
      + resolved destination    -- where "-1" actually lands
      + chain identity          -- the revisions destroyed AND their bodies

Every element is load-bearing, and each was demonstrated necessary by an executed
attack rather than assumed:

* **Source revision.** Without it a single token minted for ``-1`` authorized two
  consecutive downgrades, because ``-1`` is relative and the database moved beneath
  it. Binding the source makes a used token stale the moment it succeeds.
* **Resolved destination.** The same fingerprint, source and ``-1`` resolve to
  *different* destinations under a different script directory. The destination --
  not the chain -- is what closes that one.
* **Chain identity, including migration bodies.** Revision ids alone do not
  distinguish two histories with the same shape and different ``downgrade()``
  bodies; a token minted against one authorized the other, which dropped an extra
  table. The body hash is what makes the confirmation describe the destruction
  rather than merely its length.
* **Live identity.** A URL is a claim about a destination. A tunnel, port-forward
  or DNS alias can make that claim false while the fingerprint still matches, so
  the server is asked who it is and the answer is *hashed in*. An equality check
  against the URL cannot do this job for the host: a client-supplied hostname and
  a server-reported address are different namespaces and never compare equal.
  The database NAME is the exception -- it is genuinely comparable, so
  ``current_database()`` is checked against the URL's effective database name as
  well as bound, which is what catches a misroute that is constant rather than
  merely changing.

Deliberately absent: any ``--force``/``--yes``/``ALLOW_*`` bypass. There is nothing
to find in a shell history, and comparison is exact bytes -- no ``strip``, no
``lower``, no Unicode normalisation, no prefix match.

Known residual: **two PostgreSQL servers that report the same
``(inet_server_addr, inet_server_port, current_database)`` triple share one
identity and one confirmation.** The dominant instance is loopback -- an
``ssh -L`` tunnel, ``kubectl port-forward`` or a Cloud SQL Auth Proxy all
terminate on the remote host and connect to *its* loopback, so the server
honestly reports ``127.0.0.1``; a token minted against a local database then
matches against production. Private-range collisions across disjoint networks
(two clusters each at ``10.0.0.5:5432``) and container bridges behave the same
way. This passes every check above: the database names agree, the address is
populated, and ``localhost`` is a valid hostname. **It is the only path in this
module that fails OPEN** -- every other unidentifiable condition refuses, while
this one computes a confident, matching identity for two different servers.

Closing it needs a value whose uniqueness is a property of the value rather than
of the deployment topology, and no such value is available to this role:

* ``pg_control_system().system_identifier`` is the structural answer, but
  ``EXECUTE`` is revoked from ``PUBLIC`` and is not covered by ``pg_monitor`` or
  ``pg_read_all_stats``. This application's role is provisioned ``NOSUPERUSER``
  (``app/db/bootstrap_app_role.py``) and no ``GRANT EXECUTE`` on it exists in
  this repository, so the migration actor cannot read it.
* ``pg_postmaster_start_time()`` is executable by ``PUBLIC`` and has microsecond
  resolution, making it the strongest available -- but the argument for it is
  probabilistic, not structural, and a restart or failover between minting and
  enforcing changes it. (Refusing after a failover is correct: a different
  server is answering. Refusing after a planned restart costs one re-mint.)
* The ``pg_database`` OID of the current database looks unique and is not: OIDs
  come from a cluster-wide counter, so two clusters built by one provisioning
  script assign the same value. It collides systematically in exactly the
  dev-versus-production case this control exists for -- the same trap as
  ``inet_server_addr()``, which is why it is not used here.

None of this could be executed: no PostgreSQL server is reachable in the
environment where this was written, and shipping an unverified identity query is
precisely what made the first version of this control refuse every legitimate
downgrade. Closing the gap therefore requires an operator decision -- either
``GRANT EXECUTE ON FUNCTION pg_control_system()`` to the migration role, or a
provisioned instance-identity row -- so the gap is disclosed rather than papered
over, and the refusal names the connected database so an operator can see what
actually answered.

Second residual, accepted rather than hidden: a confirmation is **not a
single-use nonce**. It stays valid while every fact it binds stays true, so deliberately
returning a database to the source revision re-authorizes the transition the
operator already reviewed. That is the same transition, not a different one. A
time-bucketed freshness term was rejected: it is a validity period, not a single
use, and a downgrade/re-upgrade/downgrade loop completes inside any window that
would still be usable, so it would defeat only the slow case while presenting
itself as a control.

The same applies to a confirmed ``stamp``: it moves ``alembic_version`` WITHOUT
running any DDL, so it returns the version table to the source revision without
returning the schema. The spent confirmation matches again, and ``migrate
check`` reports ``compatible`` on a schema missing its objects. Gating the stamp
makes that a reviewed act against a named target; it does not prevent it.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.core.errors import ConfigurationError

#: Versioned so the material can change without a stale token ever matching.
#: v2 adds the live-identity and migration-body limbs and switches the digest to
#: a length-prefixed encoding, so no v1 token can survive the change.
CONTRACT: Final = "signalnest-downgrade-v2"

#: ``alembic -x confirm=<token> downgrade <target>``. ``-x`` is a *global* option
#: and must precede the subcommand. Chosen over an environment variable so the
#: confirmation cannot sit exported in a shell for the rest of the session.
CONFIRM_KEY: Final = "confirm"

#: Long enough that guessing is not a threat model anyone should rely on, short
#: enough that an operator can compare it by eye against what they minted.
_DIGEST_CHARS: Final = 16

_DEFAULT_PORTS: Final = {"postgresql": 5432}


class DowngradeTargetError(ConfigurationError):
    """A destructive migration was attempted without a matching confirmation."""


# ---------------------------------------------------------------------------
# Which Alembic operation is running
# ---------------------------------------------------------------------------

OP_SAFE: Final = "safe"
OP_DOWNGRADE: Final = "downgrade"
OP_STAMP: Final = "stamp"
OP_REFUSE: Final = "refuse"

#: The operation names Alembic 1.18 installs for commands that cannot alter
#: downgrade-authorization state. Enumerated from ``alembic/command.py``.
#:
#: This is an ALLOWLIST on purpose. Direction is read from a private attribute,
#: so the question is what happens when a release renames something. Refusing
#: only known-destructive names would mean a renamed ``downgrade`` silently
#: becomes "unknown, therefore allowed" -- the guard disarming itself on a
#: routine ``pip install``, with no error anywhere. Allowing only known-safe
#: names inverts that: a rename fails closed, and it fails on the upgrade path
#: that every CI job exercises rather than on the downgrade path that nothing
#: routine does.
_SAFE_OPERATIONS: Final = frozenset(
    {
        "upgrade",  # command.upgrade
        "retrieve_migrations",  # command.revision --autogenerate, command.check
        "nothing",  # command.merge
        "show_current",  # command.show
        "_display_current_history",  # command.history --indicate-current
        "display_version",  # command.current
        "edit_current",  # command.edit
        "do_ensure_version",  # command.ensure_version
    }
)

_DOWNGRADE_OPERATION: Final = "downgrade"
_STAMP_OPERATION: Final = "do_stamp"


def classify_operation(name: str | None) -> str:
    """Sort an Alembic operation name into a gate decision.

    ``None`` means direction could not be determined at all. It is refused for
    the same reason an unrecognised name is: an operation that cannot be
    identified cannot be ruled non-destructive.
    """
    if name in _SAFE_OPERATIONS:
        return OP_SAFE
    if name == _DOWNGRADE_OPERATION:
        return OP_DOWNGRADE
    if name == _STAMP_OPERATION:
        return OP_STAMP
    return OP_REFUSE


# ---------------------------------------------------------------------------
# Which database the configuration names
# ---------------------------------------------------------------------------

#: libpq keywords that decide WHICH server/database is reached. The psycopg
#: dialects build connect arguments as ``opts.update(url.query)``, so any of
#: these appearing in the query string silently overrides the authority section
#: of the URL. They are therefore resolved into the identity, not ignored.
_ROUTING_QUERY_KEYS: Final = frozenset({"host", "hostaddr", "port", "dbname"})

#: Keywords that make the target unresolvable from the URL alone: ``service``
#: defers host/port/dbname to an external file this process cannot read
#: reliably, and ``target_session_attrs`` selects among several candidates at
#: connect time. Neither can be pinned, so both are refused.
_AMBIGUOUS_QUERY_KEYS: Final = frozenset({"service", "target_session_attrs"})

#: Parameters proven not to change which server or database is reached. TLS
#: settings, timeouts and labels qualify; ``sslmode=require`` is the documented
#: production minimum, so refusing it would break the primary deployment.
#: Anything absent from every set above is refused rather than assumed benign --
#: a future libpq routing keyword must not slip through as "unrecognised but
#: probably fine".
_TARGET_NEUTRAL_QUERY_KEYS: Final = frozenset(
    {
        "sslmode",
        "sslcert",
        "sslkey",
        "sslcrl",
        "sslrootcert",
        "sslcompression",
        "connect_timeout",
        "application_name",
        "fallback_application_name",
        "client_encoding",
    }
)


#: RFC 1123 label, plus underscore. Underscores are allowed DELIBERATELY: Docker
#: and some internal DNS zones use them, and an underscore cannot appear in a
#: libpq socket marker, so refusing one buys no security and would break real
#: deployments such as Docker Compose service names.
#:
#: Anchored with ``\Z``, not ``$``: in Python ``$`` also matches just before a
#: trailing newline, so ``"h\n"`` would satisfy a ``$``-anchored label pattern
#: and a percent-encoded newline in a host would pass this allowlist.
_HOST_LABEL: Final = re.compile(r"\A[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\Z")


def _canonical_host(host: object) -> str:
    """One comparable identity per server, or a refusal.

    Two jobs, and they are separate:

    * **Normalise.** DNS is case-insensitive, an FQDN may carry a trailing dot,
      and IPv6 has many spellings for one address -- but a digest is none of
      those things. Without folding them, one server yields several
      fingerprints, and a confirmation minted under one spelling refuses under
      another for no security reason and with no obvious cause.
    * **Refuse anything that is not a TCP host.** This is an ALLOWLIST. libpq
      selects a unix socket with a leading ``/`` or ``@``, but enumerating those
      two markers only refuses the spellings known today; requiring the value to
      *be* a hostname or an IP literal refuses the ones that are not.

    What this does NOT do: establish that the host discriminates between
    servers. ``localhost`` is a perfectly valid hostname and is the tunnel case.
    See the module docstring's residual.

    A scoped IPv6 literal (``fe80::1%eth0``) is ACCEPTED, not refused:
    ``ipaddress`` has parsed zone identifiers since Python 3.9, so the IP branch
    returns before the label pattern is consulted. Nothing folds the zone away,
    so ``fe80::1%eth0`` and ``fe80::1`` are two identities, as are the ``%`` and
    ``%25`` spellings -- ``make_url`` unquotes a query-string ``?host=`` value
    but leaves an authority ``[fe80::1%25eth0]`` literal. A link-local address
    is per-link, so it discriminates no better than the loopback case in the
    module residual.
    """
    text = str(host)
    try:
        # Canonical form folds IPv6 spellings together and rejects octal and
        # integer IPv4 aliases outright, which is the fail-closed answer.
        return ipaddress.ip_address(text).compressed
    except ValueError:
        pass

    name = text[:-1] if text.endswith(".") else text
    name = name.lower()
    labels = name.split(".")
    if (
        not name
        or len(name) > 253
        or not all(_HOST_LABEL.match(label) for label in labels)
        # RFC 1123: the highest-level label is never all-numeric. This is what
        # rejects `0177.0.0.1` and `2130706433`, which an IP parser refuses but
        # a label test would otherwise wave through as an ordinary name.
        or labels[-1].isdigit()
    ):
        raise DowngradeTargetError(
            "the database host is not a hostname or IP address, so the target "
            "cannot be identified (unix-socket and other non-TCP transports are "
            "not supported). Refusing the destructive operation."
        )
    return name


def _postgres_target_parts(url: Any) -> tuple[str, int, str]:
    """Effective (host, port, database) after query-string overrides.

    The URL's authority is a default, not a decision: libpq lets the query
    string override it, and the dialect passes the query through verbatim. So
    the identity is computed from what the driver will actually connect to.
    """
    effective: dict[str, str] = {}
    for raw_key, value in (url.query or {}).items():
        key = raw_key.lower()
        if isinstance(value, (tuple, list)):
            raise DowngradeTargetError(
                f"connection parameter {key!r} is repeated, so the downgrade "
                "target is ambiguous. Refusing the destructive operation."
            )
        if key in _AMBIGUOUS_QUERY_KEYS:
            raise DowngradeTargetError(
                f"connection parameter {key!r} resolves the target outside this "
                "URL, so it cannot be pinned. Refusing the destructive operation."
            )
        if key in _ROUTING_QUERY_KEYS:
            effective[key] = str(value)
        elif key not in _TARGET_NEUTRAL_QUERY_KEYS:
            raise DowngradeTargetError(
                f"connection parameter {key!r} is not known to leave the "
                "downgrade target unchanged. Refusing the destructive operation."
            )

    # hostaddr wins over host in libpq: it skips name resolution entirely.
    host = effective.get("hostaddr") or effective.get("host") or url.host
    database = effective.get("dbname") or url.database
    raw_port = effective.get("port") or url.port or _DEFAULT_PORTS["postgresql"]

    if not host or not database:
        raise DowngradeTargetError(
            "PostgreSQL target is missing a host or database name, so it cannot "
            "be identified. Refusing the destructive operation."
        )
    host = _canonical_host(host)
    for field, value in (("host", host), ("database", database), ("port", raw_port)):
        if "," in str(value):
            raise DowngradeTargetError(
                f"the {field} names several candidates, so the downgrade target "
                "is ambiguous. Refusing the destructive operation."
            )
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        raise DowngradeTargetError(
            "the port is not a number, so the downgrade target cannot be "
            "identified. Refusing the destructive operation."
        ) from None
    return str(host), port, str(database)


def canonical_target(database_url: str) -> str:
    """The non-secret identity of the database a URL denotes.

    Included: backend, and the *effective* coordinates that decide which
    database receives the DDL. Excluded: username, password and every
    target-neutral parameter -- rotating a credential does not make it a
    different database, and a confirmation that changed on a password rotation
    would train operators to re-mint blindly.

    Also excluded: the driver suffix, so ``postgresql://`` and
    ``postgresql+psycopg://`` are one identity. ``postgres://`` is *not* a
    synonym -- SQLAlchemy reports its backend as ``postgres`` and refuses to
    build an engine for it, so it is unsupported and refused here rather than
    silently folded in.

    Raises :class:`DowngradeTargetError` for anything it cannot identify, which
    is the fail-closed direction: an unidentifiable target can never match a
    confirmation.
    """
    try:
        url = make_url(database_url)
    except (ArgumentError, ValueError):
        raise DowngradeTargetError(
            "database URL could not be parsed, so the downgrade target cannot be "
            "identified. Refusing the destructive operation."
        ) from None

    backend = url.get_backend_name()

    if backend == "sqlite":
        # A relative path is resolved against the process CWD, so minting and
        # running must happen from the same directory. That is a property of the
        # path, not of this guard, and resolving makes ./x.db and /abs/x.db one
        # identity rather than two.
        # Test what the URL *means*, not what its text contains. A substring
        # scan over the path refuses an ordinary on-disk file whose name merely
        # contains the text, and still misses the query-parameter form, because
        # SQLAlchemy splits `?mode=memory` into the query rather than leaving it
        # in the path. `file::memory:` is the third spelling and reaches neither.
        for key, value in (url.query or {}).items():
            if isinstance(value, (tuple, list)):
                raise DowngradeTargetError(
                    f"connection parameter {key.lower()!r} is repeated, so the "
                    "downgrade target is ambiguous. Refusing the destructive "
                    "operation."
                )
        database = url.database
        if (
            not database
            or database == ":memory:"
            or database.startswith("file::memory:")
            or (url.query or {}).get("mode") == "memory"
        ):
            raise DowngradeTargetError(
                "SQLite target is in-memory or has no file path. Such a database "
                "cannot be re-identified on a later connection, so it can never "
                "match a confirmation. Refusing the destructive operation."
            )
        return f"sqlite|{Path(database).expanduser().resolve()}"

    if backend == "postgresql":
        host, port, database = _postgres_target_parts(url)
        return f"postgresql|{host}|{port}|{database}"

    raise DowngradeTargetError(
        f"unsupported database backend={backend or 'unknown'!s} for a destructive "
        "migration. Refusing the destructive operation."
    )


def _digest(*parts: str) -> str:
    """Length-prefixed digest over the field sequence.

    A plain separator join is not injective: ``("X", "Y\\nZ")`` and
    ``("X\\nY", "Z")`` produce identical bytes, so one field's content can
    impersonate a field boundary. Prefixing each field with its byte length
    makes the encoding unambiguous.
    """
    hasher = hashlib.sha256()
    for part in parts:
        raw = part.encode("utf-8")
        hasher.update(f"{len(raw)}:".encode("ascii"))
        hasher.update(raw)
    return hasher.hexdigest()[:_DIGEST_CHARS]


def target_fingerprint(database_url: str) -> str:
    """Short, stable, non-secret handle for a database target."""
    return _digest(CONTRACT, "target", canonical_target(database_url))


# ---------------------------------------------------------------------------
# Which database actually answered
# ---------------------------------------------------------------------------


def live_identity(connection: Any, database_url: str) -> str:
    """What the *server* says it is, independent of the URL used to reach it.

    Three mechanisms, each closing a different class. They are listed with what
    they do NOT close, because describing a control by the problem that
    motivated it is how this module previously acquired a hole:

    * **Address and port are hashed in, not compared.** A client-supplied
      hostname and a server-reported address are different namespaces, so
      requiring equality would refuse every hostname-based deployment while
      proving nothing. Binding means "the server that answered at minting must
      answer at enforcing" -- it detects a CHANGE. It does **not** detect a
      misroute that is constant, and it does not establish that the reported
      address is unique to one server.
    * **``current_database()`` is compared against the URL's effective database
      name.** This is the one genuinely comparable pair, so it is the one place
      a *persistent* misroute is caught -- the stale ``.env`` pointing at a
      differently-named database. It closes the database-NAME axis only, and is
      silent whenever the names agree. Compared against the *effective* name so
      a ``?dbname=`` override is folded into the comparison as it is into the
      fingerprint.
    * **A missing address or port is refused.** Both functions return NULL over
      a unix-domain socket, so every socket connection reports one identity no
      matter which server answered. Checked on what the server reported rather
      than inferred from the URL, so it closes the transport whatever the
      spelling. It establishes only that the value is not the one known
      non-discriminating value -- not that a populated value discriminates.

    Fails closed: if the identity cannot be read, the destructive operation is
    refused rather than assumed safe.
    """
    try:
        url = make_url(database_url)
    except (ArgumentError, ValueError):
        raise DowngradeTargetError(
            "database URL could not be parsed, so the live database cannot be "
            "identified. Refusing the destructive operation."
        ) from None

    backend = url.get_backend_name()
    try:
        if backend == "sqlite":
            # `PRAGMA database_list` reports the file actually opened, which is
            # what a relative path or a symlink resolves to at runtime.
            rows = connection.exec_driver_sql("PRAGMA database_list").fetchall()
            main = next((row for row in rows if row[1] == "main"), None)
            if main is None or not main[2]:
                raise DowngradeTargetError(
                    "the SQLite connection did not report an open database file. "
                    "Refusing the destructive operation."
                )
            return f"sqlite|{Path(main[2]).resolve()}"

        if backend == "postgresql":
            _, _, expected_database = _postgres_target_parts(url)
            row = connection.exec_driver_sql(
                "SELECT current_database(), "
                "coalesce(host(inet_server_addr()), ''), "
                "coalesce(inet_server_port(), 0)"
            ).fetchone()
            if row is None:
                raise DowngradeTargetError(
                    "the PostgreSQL server did not report its identity. "
                    "Refusing the destructive operation."
                )
            live_database, server_address, server_port = row[0], row[1], row[2]

            # Both functions return NULL over a unix-domain socket, so every
            # socket connection reports one identity regardless of which server
            # answered. Checked here, at the point of truth, rather than guessed
            # from the URL -- that closes the transport whatever its spelling.
            #
            # Note precisely what this establishes: that the reported value is
            # not the one known non-discriminating value. It does NOT establish
            # that a populated address discriminates. See the residual.
            #
            # `is None` is checked explicitly and first: `str(None)` is the
            # truthy string "None", so a bare `not str(...)` would wave a real
            # NULL through and bind a non-discriminating identity. The SQL's
            # `coalesce` happens to prevent that today, but this refusal must
            # not depend on a clause three lines away staying as written.
            if (
                server_address is None
                or not str(server_address)
                or server_port is None
                or not int(server_port)
            ):
                raise DowngradeTargetError(
                    "the database server did not report a network address, so "
                    "the connection cannot be attributed to a particular server "
                    "(unix-socket transports report none). Connect over TCP. "
                    "Refusing the destructive operation."
                )

            # `current_database()` against the URL's EFFECTIVE database name is
            # the one genuinely comparable pair, so it is compared as well as
            # bound. Binding alone detects only a CHANGE between minting and
            # enforcing; it cannot see a misroute that is constant, which is
            # exactly the stale-`.env` case. Compared against the effective name
            # so a `?dbname=` override -- already folded into the fingerprint --
            # is folded into the comparison too.
            if str(live_database) != expected_database:
                raise DowngradeTargetError(
                    f"the connected database is {str(live_database)!r}, but the "
                    f"configured URL names {expected_database!r}. The connection "
                    "did not land on the configured target. Refusing the "
                    "destructive operation."
                )
            return (
                f"postgresql|{_canonical_host(server_address)}"
                f"|{server_port}|{live_database}"
            )
    except DowngradeTargetError:
        raise
    except Exception as exc:  # driver/permission failure -- fail closed, stay quiet
        raise DowngradeTargetError(
            "the database server's identity could not be read "
            f"({type(exc).__name__}). Refusing the destructive operation."
        ) from None

    raise DowngradeTargetError(
        f"unsupported database backend={backend or 'unknown'!s} for a destructive "
        "migration. Refusing the destructive operation."
    )


# ---------------------------------------------------------------------------
# What the downgrade would actually destroy
# ---------------------------------------------------------------------------


def resolve_downgrade(
    script: Any, source_revision: str, requested_target: str
) -> tuple[str | None, list[str]]:
    """Where a downgrade lands, and which revisions it destroys.

    Uses only public :class:`~alembic.script.ScriptDirectory` API
    (``get_revision``/``walk_revisions``) so the guard does not rest on Alembic
    internals that move between releases.

    Returns ``(destination, chain)`` where ``destination`` is ``None`` for
    ``base`` and ``chain`` is the ordered revisions that would be reverted.
    """
    walked = [rev.revision for rev in script.walk_revisions(base="base", head=source_revision)]
    if not walked or walked[0] != source_revision:
        # Unreachable via today's Alembic call path -- `walk_revisions` raises
        # `CommandError` for an unknown head before this is reached. Retained
        # because that reachability is a property of ALEMBIC's ordering, not of
        # this guard, and the branch stays correct if the ordering changes.
        raise DowngradeTargetError(
            "the database's current revision is not present in this migration "
            "history. Refusing the destructive operation."
        )

    if requested_target == "base":
        return None, walked

    if requested_target in ("-1", "-01"):
        destination = script.get_revision(source_revision).down_revision
    else:
        try:
            destination = script.get_revision(requested_target).revision
        except Exception:
            raise DowngradeTargetError(
                "the requested downgrade target is not a revision in this "
                "migration history. Refusing the destructive operation."
            ) from None

    if destination is None:
        return None, walked
    if destination not in walked:
        raise DowngradeTargetError(
            "the requested downgrade target is not an ancestor of the database's "
            "current revision. Refusing the destructive operation."
        )
    return destination, walked[: walked.index(destination)]


def chain_identity(script: Any, chain: Sequence[str]) -> str:
    """Identity of the revisions to be reverted **and of their bodies**.

    Revision ids alone describe the shape of the destruction, not its content:
    two histories with the same ids and a different ``downgrade()`` body
    produced the same confirmation, and a token minted against one authorized
    the other to drop an additional table. Hashing each migration's full source
    bytes makes the confirmation describe what will actually run.

    Full file bytes are used rather than a parsed subset: a downgrade's effect
    can come from a module-level constant, an import, or a helper elsewhere in
    the same file, so any "relevant subset" rule would have to prove a negative.
    Content only -- never a path, mtime or any other filesystem metadata -- so
    the identity is identical across checkout locations and stable in CI.
    """
    parts: list[str] = [CONTRACT, "chain"]
    for revision in chain:
        try:
            path = script.get_revision(revision).path
        except Exception:
            raise DowngradeTargetError(
                "a revision in the downgrade path could not be located in this "
                "migration history. Refusing the destructive operation."
            ) from None
        if not path:
            raise DowngradeTargetError(
                "a revision in the downgrade path has no source file, so its "
                "effect cannot be pinned. Refusing the destructive operation."
            )
        try:
            body = Path(path).read_bytes()
        except OSError:
            # Unreachable via today's Alembic call path -- `ScriptDirectory`
            # loads and scans the version files before the guard runs, so an
            # unreadable migration surfaces as a raw error first. Retained for
            # the same reason as the branch in `resolve_downgrade`: lazy script
            # loading, a future programmatic caller, or a file removed between
            # the scan and this read would all reach it.
            raise DowngradeTargetError(
                "a migration in the downgrade path could not be read, so its "
                "effect cannot be pinned. Refusing the destructive operation."
            ) from None
        parts.append(revision)
        parts.append(hashlib.sha256(body).hexdigest())
    return _digest(*parts)


# ---------------------------------------------------------------------------
# The confirmation itself
# ---------------------------------------------------------------------------


def confirmation_token(
    *,
    database_url: str,
    live_identity: str,
    source_revision: str,
    requested_target: str,
    resolved_destination: str | None,
    chain_identity: str,
) -> str:
    """The exact value that authorizes one specific destructive transition."""
    return _digest(
        CONTRACT,
        target_fingerprint(database_url),
        live_identity,
        source_revision,
        requested_target,
        resolved_destination or "base",
        chain_identity,
    )


def require_confirmation(
    *,
    supplied: str | None,
    expected: str,
    fingerprint: str,
    live_target: str,
    source_revision: str,
    requested_target: str,
    chain: Sequence[str],
    command: str,
) -> None:
    """Accept only an exact match, and explain how to obtain one.

    Comparison is on raw bytes. No trimming, case folding or Unicode
    normalisation: each of those turns a near-miss into an accept, and a
    confirmation that accepts near-misses is not confirming anything.
    """
    if supplied is not None and supplied == expected:
        return

    raise DowngradeTargetError(
        "Destructive migration downgrade refused.\n"
        # The database that ANSWERED, in plain text. A fingerprint is a hash of
        # what the configuration claims; it cannot tell an operator that the
        # connection landed somewhere else. Two servers can present the same
        # address, port and database name -- see the module residual -- and when
        # they do, this line is the only thing that can prompt a human to look.
        f"  connected database : {live_target}\n"
        f"  target fingerprint : {fingerprint}\n"
        f"  current revision   : {source_revision}\n"
        f"  requested target   : {requested_target}\n"
        f"  would revert       : {len(chain)} migration(s)\n"
        "\n"
        "Check the connected database above is the one you mean, then re-run\n"
        "with the confirmation for exactly this database and transition:\n"
        f"  {command}\n"
        "\n"
        "The confirmation is bound to the database that answers, the revision it\n"
        "is on, the transition requested, and the migrations that would run. It\n"
        "is not a single-use nonce: it stops matching when any of those change."
    )
