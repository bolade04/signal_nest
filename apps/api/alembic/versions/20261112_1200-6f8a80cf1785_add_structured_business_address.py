"""structured business address and its locality confirmation on geo_coverage_rules (W21 ADDR)

The operator's instruction of 2026-11-12 authorizes "the limited CF-3 migration-freeze exception
needed for this package’s address storage and validation". This revision is that exception and
nothing else: it touches only ``geo_coverage_rules``. It is not the Q-3 schema package, and it is
applied only to disposable test databases; applying it to staging or production follows the
existing deployment gates.

**Added, all nullable, no server default:**

* the business address as five separate fields, beside the free-text ``business_address``
  (which is kept unchanged): ``business_address_street`` (400), ``business_address_locality``
  (160), ``business_address_region`` (160), ``business_address_postcode`` (40) and
  ``business_address_country`` (120), the lengths of the location's own address fields;
* a customer's confirmation that the business town lies within the location's area, bound to
  what it was made for: ``locality_confirmed_locality`` and ``locality_confirmed_city`` (160),
  ``locality_confirmed_jurisdiction`` (6, with the jurisdiction-code shape CHECK),
  ``locality_confirmed_at`` and ``locality_confirmed_by_user_id`` (a key to ``users``,
  ``ON DELETE SET NULL``, indexed);
* ``ck_geo_coverage_rule_locality_confirmation_complete``: the four binding values are set
  together or not at all.

**No data statement.** Nothing is back-filled, defaulted or copied from the location: every
existing row keeps every value it had, and the new columns read NULL. No jurisdiction value is
written or rewritten anywhere.

Rollback: the downgrade drops exactly these objects. It needs no guard: no other table refers to
them, and the free-text ``business_address`` is untouched (dropping the new columns loses only
what was entered into them after this revision).

Revision ID: 6f8a80cf1785
Revises: d5f616c0ec97
Create Date: 2026-11-12 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "6f8a80cf1785"
down_revision: str | None = "d5f616c0ec97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "geo_coverage_rules"
_ADDRESS = (
    ("business_address_street", 400),
    ("business_address_locality", 160),
    ("business_address_region", 160),
    ("business_address_postcode", 40),
    ("business_address_country", 120),
)
_BINDING = (
    ("locality_confirmed_locality", 160),
    ("locality_confirmed_city", 160),
    ("locality_confirmed_jurisdiction", 6),
)
_BY = "locality_confirmed_by_user_id"
_FK = "geo_coverage_rules_locality_confirmed_by_user_id_fkey"
_INDEX = "ix_geo_coverage_rules_locality_confirmed_by_user_id"
_JURISDICTION_CHECK = "ck_geo_coverage_rule_locality_confirmed_jurisdiction"
_COMPLETE_CHECK = "ck_geo_coverage_rule_locality_confirmation_complete"
# Frozen with this revision (the Python authorities are app.jurisdiction.codes and
# app.locations.models; test_structured_address_migration compares them).
_JURISDICTION_SHAPE = (
    "locality_confirmed_jurisdiction IS NULL OR (upper(locality_confirmed_jurisdiction) = "
    "locality_confirmed_jurisdiction AND (length(locality_confirmed_jurisdiction) = 2 OR "
    "(length(locality_confirmed_jurisdiction) BETWEEN 4 AND 6 AND "
    "substr(locality_confirmed_jurisdiction, 3, 1) = '-')))"
)
_COMPLETE = (
    "(locality_confirmed_at IS NULL) = (locality_confirmed_jurisdiction IS NULL) AND "
    "(locality_confirmed_at IS NULL) = (locality_confirmed_locality IS NULL) AND "
    "(locality_confirmed_at IS NULL) = (locality_confirmed_city IS NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        for name, length in (*_ADDRESS, *_BINDING):
            batch_op.add_column(sa.Column(name, sa.String(length=length), nullable=True))
        batch_op.add_column(
            sa.Column("locality_confirmed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column(_BY, sa.String(length=32), nullable=True))
        batch_op.create_foreign_key(_FK, "users", [_BY], ["id"], ondelete="SET NULL")
        batch_op.create_check_constraint(_JURISDICTION_CHECK, sa.text(_JURISDICTION_SHAPE))
        batch_op.create_check_constraint(_COMPLETE_CHECK, sa.text(_COMPLETE))
    op.create_index(_INDEX, _TABLE, [_BY])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.drop_constraint(_COMPLETE_CHECK, type_="check")
        batch_op.drop_constraint(_JURISDICTION_CHECK, type_="check")
        batch_op.drop_constraint(_FK, type_="foreignkey")
        batch_op.drop_column(_BY)
        batch_op.drop_column("locality_confirmed_at")
        for name, _length in reversed((*_ADDRESS, *_BINDING)):
            batch_op.drop_column(name)
