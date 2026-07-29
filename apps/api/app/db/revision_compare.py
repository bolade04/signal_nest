"""Offline strict revision comparator: ``python -m app.db.revision_compare``.

Reads the live database revision (one line, as printed by
:mod:`app.db.revision_status`) from **stdin**, resolves the repository's single
code head from the migration script directory, and exits ``0`` only on an
**exact string match**. Strictly offline: it never opens a database connection
and never imports engine construction (:func:`sqlalchemy.create_engine` or
:mod:`app.db.session`).

This is deliberately stricter than the replica startup gate
(:data:`app.db.schema._STARTUP_SAFE` admits ``ahead`` for rolling deploys): the
migration actor's post-apply comparison requires exact equality, mirroring
``migrate.upgrade_and_verify``.

Input handling is anti-vacuous by construction: the **complete** stream is
read; exactly one line is required (a single trailing newline is tolerated, a
second line — even a truncated one — is not); the line must be a well-formed
12-lowercase-hex revision (case mutations are rejected).

Exit-code map (stable, unique; does not overlap migrate 0-7, bootstrap 10-20,
or revision_status 30-36):

* ``0``  exact match (prints ``revision-compare: match`` to stdout)
* ``40`` unexpected arguments
* ``41`` invalid input (empty, not exactly one line, malformed revision)
* ``42`` repository code head unresolved (zero or multiple heads, or the
  script directory could not be loaded)
* ``43`` mismatch (well-formed input that does not equal the code head)
* ``44`` unexpected safe failure
"""

from __future__ import annotations

import re
import sys

EXIT_OK = 0
EXIT_ARGS = 40
EXIT_INPUT_INVALID = 41
EXIT_CODE_HEAD_UNRESOLVED = 42
EXIT_MISMATCH = 43
EXIT_UNEXPECTED = 44

#: This repository's Alembic revision ids are 12 lowercase hex characters.
_REVISION_RE = re.compile(r"[0-9a-f]{12}")


def _fail(token: str, code: int) -> int:
    print(f"revision-compare: {token}", file=sys.stderr)
    return code


def _single_code_head() -> str | None:
    """The lone repository head, or ``None`` on zero/multiple/unloadable."""
    try:
        from alembic.script import ScriptDirectory

        from app.db.schema import alembic_config

        heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    except Exception:
        return None
    return heads[0] if len(heads) == 1 else None


def _run(argv: list[str], stdin_text: str) -> int:
    if argv:
        return _fail("unexpected-arguments", EXIT_ARGS)

    # The complete input is significant: a comparator that reads only the first
    # line would silently accept "matching line + garbage".
    lines = stdin_text.splitlines()
    if len(lines) != 1:
        return _fail("invalid-input", EXIT_INPUT_INVALID)
    live_revision = lines[0]
    if _REVISION_RE.fullmatch(live_revision) is None:
        return _fail("invalid-input", EXIT_INPUT_INVALID)

    code_head = _single_code_head()
    if code_head is None:
        return _fail("code-head-unresolved", EXIT_CODE_HEAD_UNRESOLVED)

    if live_revision != code_head:
        return _fail("mismatch", EXIT_MISMATCH)

    print("revision-compare: match")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Entry point. Never lets any exception reach the default excepthook."""
    try:
        return _run(sys.argv[1:] if argv is None else argv, sys.stdin.read())
    except BaseException:  # noqa: BLE001 - fixed classification, never a traceback
        try:
            print("revision-compare: unexpected-failure", file=sys.stderr)
        except BaseException:  # pragma: no cover - stderr itself unusable
            pass
        return EXIT_UNEXPECTED


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    sys.exit(main())
