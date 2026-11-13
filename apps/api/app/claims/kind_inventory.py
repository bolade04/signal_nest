"""Read-only inventory of stored claims-library kinds outside the closed set (D-N4 item 36).

``python -m app.claims.kind_inventory`` is run by an operator, under the database's access
controls, against the database ``DATABASE_URL`` names. It prints one line per distinct stored
``kind`` that is not one of the four (``app.campaign_context.schemas.CLAIM_KINDS``):
- the value's digest (``sha256:`` and the first 12 hexadecimal characters of the SHA-256 of
  its UTF-8 bytes; the same digest the claim-kinds revision ``1574637e56d3`` logs);
- the value itself, escaped (``repr``) and capped;
- its count and entry ids.

It ends with a total.

**Why this script and not the migration's log** (coordinator ruling 36-R-1). A ``kind`` is free
text a steward typed and may name a person. By analogy with the operator's ruling on item 40
("Free-form claim text must not automatically be classified as containing no personal data"),
the value text must not reach shared logs. So the revision logs digests, counts and ids only,
and this script is the one place that prints the values: to the operator who runs it, and
nowhere else. It writes nothing. On PostgreSQL it runs in a ``READ ONLY`` transaction, and it
never logs a value.
"""

from __future__ import annotations

import hashlib
import sys
from collections import defaultdict

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.campaign_context.models import ClaimsLibraryEntry
from app.campaign_context.schemas import CLAIM_KINDS

#: Hexadecimal characters of the digest (as the revision's log), characters of a value shown
#: (``repr``), and entry ids per value.
DIGEST = 12
SHOWN = 80
MAX_IDS = 50


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:DIGEST]


def shown(value: str) -> str:
    """``value`` escaped and capped: control characters and quotes cannot break a line."""
    escaped = repr(value)
    if len(escaped) > SHOWN:
        return f"{escaped[:SHOWN]}... ({len(value)} characters)"
    return escaped


def unsupported_kinds(db: Session) -> dict[str, list[str]]:
    """``{stored kind: [entry ids]}`` for every kind outside the closed set, retired entries
    included, ordered by digest. Reads only; on PostgreSQL the transaction is ``READ ONLY``."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SET TRANSACTION READ ONLY"))
    found: dict[str, list[str]] = defaultdict(list)
    rows = db.execute(
        select(ClaimsLibraryEntry.id, ClaimsLibraryEntry.kind).order_by(ClaimsLibraryEntry.id)
    )
    for entry_id, kind in rows:
        if kind not in CLAIM_KINDS:
            found[kind].append(entry_id)
    return {value: found[value] for value in sorted(found, key=digest)}


def report_lines(inventory: dict[str, list[str]]) -> list[str]:
    lines = []
    for value, ids in inventory.items():
        listed = ", ".join(ids[:MAX_IDS]) + (", ..." if len(ids) > MAX_IDS else "")
        lines.append(
            f"sha256:{digest(value)} {shown(value)}: {len(ids)} entr(y/ies) ids=[{listed}]"
        )
    total = sum(len(ids) for ids in inventory.values())
    lines.append(
        f"{total} entr(y/ies) with an unsupported kind ({len(inventory)} distinct value(s))"
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        inventory = unsupported_kinds(db)
        db.rollback()
    for line in report_lines(inventory):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
