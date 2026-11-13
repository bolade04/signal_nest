"""closed claim kinds: migrate the documented alias 'prohibited' to 'blocked'; inventory the rest (D-N4 item 36)

The operator's ruling of 2026-11-12 on D-N4 item 36: "migrate the documented prohibited alias to
blocked. Inventory other unsupported stored values and report them; do not silently delete or
guess their meaning." Built under the operator's narrowly scoped migration-freeze exception for
items 36 and 37, which grants no Q-3 change, database-role change or staging/production
migration. **Data only**: no DDL, no constraint, index, model or enum change.

**The closed set** (``app.campaign_context.schemas.ClaimKind``): ``approved``, ``restricted``,
``blocked``, ``required_disclaimer``. From this revision's code on, the API refuses any other
kind with 422. This revision touches exactly one column, ``claims_library.kind``.

**What it does, inside its own migration transaction** (``env.py`` runs Alembic with
``transaction_per_migration=True``):

1. *Lock and read.* On PostgreSQL it first takes ``LOCK TABLE claims_library IN SHARE ROW
   EXCLUSIVE MODE`` (a concurrent write waits until this transaction ends; reads go on), then
   reads every entry's id and stored ``kind``, retired entries included. SQLite admits one
   writer at a time.
2. *The alias.* Every entry whose stored kind, in the 5C review's canonical form, is
   ``prohibited`` has its kind set to ``blocked``. The canonical form is the one the review
   reads every stored kind through (``app.claims.engine.canonical_kind``): NFKC, invisible
   characters removed, NFKC again, the ideographic, fullwidth and halfwidth full stops mapped
   to ".", whitespace runs collapsed, trimmed, case-folded. So "prohibited", "Prohibited",
   " PROHIBITED " and a "prohibited" with an invisible character all become ``blocked``: exactly
   the entries the review already treated as blocking, so no entry changes what it does. The
   copy below is frozen with this revision; ``test_claim_kinds.py`` pins it to the engine's.
3. *The inventory.* Every other entry whose stored kind is not exactly one of the four kinds is
   **left exactly as it is**: not deleted, not mapped, its meaning not guessed. That includes a
   value whose canonical form is one of the four ("Blocked", " approved"). They are counted and
   reported. The 5C review keeps reading them through the canonical form, fail closed:
   "Blocked" still blocks, "foo" blocks nothing and is never offered as approved.
4. *The report*, on the ``alembic.runtime.migration`` logger, with ids, counts and digests
   only (coordinator ruling 36-R-1):
   - one line with the ids of the migrated entries;
   - one line per distinct unsupported value: ``sha256:`` and the first :data:`_DIGEST`
     hexadecimal characters of the SHA-256 of its UTF-8 bytes, its count and up to
     :data:`_MAX_IDS` entry ids;
   - a summary with the number of entries read, migrated and kept unsupported.

   **The value text is never logged**, not even escaped or shortened. A ``kind`` is free text a
   steward typed and may name a person (by analogy with the ruling on D-N4 item 40: "Free-form
   claim text must not automatically be classified as containing no personal data"), and a
   migration's log ends up in shared logs. The inventory with the values is produced, in any
   real environment, only by an operator running the read-only script
   ``python -m app.claims.kind_inventory`` under the database's access controls. It prints each
   unsupported value, escaped and capped, with the same digest, so its lines can be matched
   with this log's. This revision's log is never that inventory.

Running it again changes nothing: no ``prohibited`` entry is left, and the unsupported ones are
reported again. A failure rolls the whole revision back with its ``alembic_version`` update on
PostgreSQL.

**How the 422 and an existing unsupported entry interact on update.** A PATCH that leaves
``kind`` out keeps the stored value, supported or not, and changes the other fields. A PATCH
that sends ``kind`` must send one of the four (else 422 and nothing changes). So a steward
fixes an unsupported entry by choosing one of the four.

**Downgrade: a no-op.** The original spelling of a migrated alias is not recorded in the table
(the ids are in this revision's log). Under the review's canonical reading ``blocked`` and
``prohibited`` do the same thing, so nothing a downgrade could restore changes behaviour.

Revision ID: 1574637e56d3
Revises: d0a8bce8de6a
Create Date: 2026-11-12 17:00:00.000000
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "1574637e56d3"
down_revision: str | None = "d0a8bce8de6a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

_PREFIX = "D-N4 item 36 claim kinds"
#: The closed set, literal on purpose: a revision never imports the models or schemas.
CLAIM_KINDS: tuple[str, ...] = ("approved", "restricted", "blocked", "required_disclaimer")
#: The documented alias (the campaign-context UI's former "Prohibited") and its kind.
ALIAS = "prohibited"
ALIAS_TARGET = "blocked"
#: Ids per UPDATE statement (a bound-parameter list; well under SQLite's limit).
_CHUNK = 500
#: Hexadecimal characters of a value's SHA-256 in the report, and entry ids per value.
_DIGEST = 12
_MAX_IDS = 50
#: PostgreSQL only: concurrent writers of the table wait for this transaction; reads go on.
_LOCK = sa.text("LOCK TABLE claims_library IN SHARE ROW EXCLUSIVE MODE")

_CLAIMS = sa.table("claims_library", sa.column("id"), sa.column("kind"))

# The 5C review's canonical form (app.claims.engine.canonical_kind = fold(normalise(...))),
# frozen with this revision.
_DEFAULT_IGNORABLE = re.compile(
    "[­͏؜ᅟᅠ឴឵᠋-᠏​-‏‪-‮⁠-⁯ㅤ︀-️﻿ﾠ￰-￸\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)
_FULL_STOPS = str.maketrans({"。": ".", "．": ".", "｡": "."})
_WHITESPACE = re.compile(r"\s+")


def canonical(kind: str | None) -> str:
    """A stored kind in the review's canonical form (see the module docstring)."""
    text = unicodedata.normalize("NFKC", kind or "")
    text = _DEFAULT_IGNORABLE.sub("", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = unicodedata.normalize("NFKC", text).translate(_FULL_STOPS)
    return _WHITESPACE.sub(" ", text).strip().casefold()


class KindReport:
    """What the revision did: entries read, the ids it migrated, and the unsupported values
    it kept, as ``{stored value: [entry ids]}``. (A plain class: Alembic loads a revision
    without registering its module, which a dataclass's annotations need.)"""

    def __init__(self) -> None:
        self.read = 0
        self.migrated: list[str] = []
        self.unsupported: dict[str, list[str]] = {}

    @property
    def unsupported_count(self) -> int:
        return sum(len(ids) for ids in self.unsupported.values())


def digest(value: str) -> str:
    """The short digest a stored value is reported by (``app.claims.kind_inventory`` prints the
    same one beside the value)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_DIGEST]


def close_claim_kinds(bind) -> KindReport:
    """Migrate the alias and inventory every other unsupported kind (see the module docstring).
    Core only; every value is bound."""
    if bind.dialect.name == "postgresql":
        bind.execute(_LOCK)
    report = KindReport()
    unsupported: dict[str, list[str]] = defaultdict(list)
    rows = bind.execute(sa.select(_CLAIMS.c.id, _CLAIMS.c.kind).order_by(_CLAIMS.c.id)).all()
    for entry_id, kind in rows:
        report.read += 1
        if kind in CLAIM_KINDS:
            continue
        if canonical(kind) == ALIAS:
            report.migrated.append(entry_id)
        else:
            unsupported[kind].append(entry_id)
    for start in range(0, len(report.migrated), _CHUNK):
        chunk = report.migrated[start : start + _CHUNK]
        bind.execute(sa.update(_CLAIMS).where(_CLAIMS.c.id.in_(chunk)).values(kind=ALIAS_TARGET))
    report.unsupported = {value: unsupported[value] for value in sorted(unsupported, key=digest)}
    if not report.read:
        return report
    if report.migrated:
        logger.info(
            "%s: %r -> %r: %d entr(y/ies) ids=[%s]",
            _PREFIX,
            ALIAS,
            ALIAS_TARGET,
            len(report.migrated),
            ", ".join(report.migrated),
        )
    for value, ids in report.unsupported.items():
        shown_ids = ", ".join(ids[:_MAX_IDS]) + (", ..." if len(ids) > _MAX_IDS else "")
        logger.info(
            "%s: unsupported kind kept as stored: sha256:%s: %d entr(y/ies) ids=[%s]",
            _PREFIX,
            digest(value),
            len(ids),
            shown_ids,
        )
    logger.info(
        "%s: %d entr(y/ies) read, %d migrated to %r, %d with an unsupported kind kept as "
        "stored (%d distinct value(s))",
        _PREFIX,
        report.read,
        len(report.migrated),
        ALIAS_TARGET,
        report.unsupported_count,
        len(report.unsupported),
    )
    return report


def upgrade() -> None:
    close_claim_kinds(op.get_bind())


def downgrade() -> None:
    """A no-op (see the module docstring): the alias's original spelling is not recorded, and
    ``blocked`` and ``prohibited`` do the same thing under the review's canonical reading."""
