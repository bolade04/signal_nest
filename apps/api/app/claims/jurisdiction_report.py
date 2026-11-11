"""Claims-library entries whose jurisdiction codes no draft can carry: the pre-activation report
(Project Phase 5D; operator ruling Q-2, 2026-11-08).

Run it against a database before the Phase 5 activation. It reads and never writes:

    cd apps/api && .venv/bin/python -m app.claims.jurisdiction_report

It lists every claims-library entry that is not retired and whose stored ``jurisdiction_codes``
holds a value the shared resolver cannot produce
(:func:`app.claims.engine.is_reachable_jurisdiction_code`: ``US-PR``, ``US-ZZ``, ``GB-ENG``, a
non-string), or is not a list at all. Such an entry can only
have been written before the Q-2 rule or by a direct database write; the API refuses it now.
Each line gives the workspace, the entry id, the entry's kind in canonical form, how the review
reads the entry until it is re-coded, the values no draft can carry, and the codes still read.
Ids, kinds and codes only, never the claim's text. Exit status: 0 when there is no such entry,
1 when there is at least one.

How the review reads such an entry meanwhile (:func:`app.claims.engine.jurisdiction_restriction`):

* ``applies-to-every-draft``: a blocking or required-disclaimer entry. The unreadable value
  cannot be ruled out of any jurisdiction, so the entry applies to every draft (fail closed).
* ``offered-only-inside-kept-codes`` / ``offered-nowhere``: an approved entry. The unreadable
  value clears nothing, so the entry is offered only inside its readable codes, if any.
* ``no-effect``: any other kind blocks, requires and approves nothing, whatever its codes.

A retired entry is left out and only counted: it applies nowhere and can no longer be updated.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.campaign_context.models import ClaimsLibraryEntry
from app.claims.engine import (
    canonical_kind,
    is_approved_kind,
    is_blocking_kind,
    is_disclaimer_kind,
    is_reachable_jurisdiction_code,
)


@dataclass(frozen=True)
class UnreachableEntry:
    """One entry the report lists."""

    workspace_id: str
    entry_id: str
    kind: str  # canonical form
    effect: str
    #: The stored values no draft can carry (the whole stored value when it is not a list).
    unreachable: tuple[object, ...]
    #: The stored codes the review still reads.
    kept: tuple[str, ...]


def _effect(kind: str | None, kept: tuple[str, ...]) -> str:
    if is_blocking_kind(kind) or is_disclaimer_kind(kind):
        return "applies-to-every-draft"
    if is_approved_kind(kind):
        return "offered-only-inside-kept-codes" if kept else "offered-nowhere"
    return "no-effect"


def _split(stored: object) -> tuple[tuple[object, ...], tuple[str, ...]] | None:
    """``(values no draft can carry, codes still read)``, or ``None`` when every value is
    readable or the entry is unrestricted (``NULL`` or an empty list)."""
    if stored is None or (isinstance(stored, list | tuple) and not stored):
        return None
    if not isinstance(stored, list | tuple):
        return (stored,), ()
    unreachable = tuple(v for v in stored if not is_reachable_jurisdiction_code(v))
    if not unreachable:
        return None
    return unreachable, tuple(v for v in stored if is_reachable_jurisdiction_code(v))


def unreachable_jurisdiction_entries(db: Session) -> tuple[list[UnreachableEntry], int]:
    """Every entry that is not retired and carries a value no draft can carry, by workspace and
    age; and the number of retired entries left out for the same reason."""
    rows = db.execute(
        select(
            ClaimsLibraryEntry.id,
            ClaimsLibraryEntry.workspace_id,
            ClaimsLibraryEntry.kind,
            ClaimsLibraryEntry.retired_at,
            ClaimsLibraryEntry.jurisdiction_codes,
        ).order_by(
            ClaimsLibraryEntry.workspace_id, ClaimsLibraryEntry.created_at, ClaimsLibraryEntry.id
        )
    ).all()
    listed: list[UnreachableEntry] = []
    retired = 0
    for row in rows:
        split = _split(row.jurisdiction_codes)
        if split is None:
            continue
        if row.retired_at is not None:
            retired += 1
            continue
        unreachable, kept = split
        listed.append(
            UnreachableEntry(
                workspace_id=row.workspace_id,
                entry_id=row.id,
                kind=canonical_kind(row.kind),
                effect=_effect(row.kind, kept),
                unreachable=unreachable,
                kept=kept,
            )
        )
    return listed, retired


def _json(values: tuple[object, ...]) -> str:
    return json.dumps(list(values), ensure_ascii=True, default=str)


def main(session_factory: Callable[[], Session] | None = None, out: TextIO | None = None) -> int:
    """Print the report (see the module docstring) and return the exit status."""
    if session_factory is None:
        from app.db.session import SessionLocal

        session_factory = SessionLocal
    out = out or sys.stdout
    with session_factory() as db:
        listed, retired = unreachable_jurisdiction_entries(db)
        db.rollback()
    print(
        f"claims-library jurisdiction report (Q-2): {len(listed)} active entr"
        f"{'y' if len(listed) == 1 else 'ies'} carry a jurisdiction value no draft can carry",
        file=out,
    )
    for entry in listed:
        print(
            f"workspace={entry.workspace_id} entry={entry.entry_id} kind={json.dumps(entry.kind)} "
            f"reads_as={entry.effect} unreachable={_json(entry.unreachable)} "
            f"kept={_json(entry.kept)}",
            file=out,
        )
    print(f"retired entries left out: {retired}", file=out)
    return 1 if listed else 0


if __name__ == "__main__":  # pragma: no cover - exercised through ``python -m`` in the tests
    raise SystemExit(main())
