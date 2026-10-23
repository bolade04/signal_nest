"""add opportunity explanation source

Adds ``opportunities.explanation_source`` (P6-LLM-5): the provenance of an
opportunity's explanation text - ``model`` (validated provider output),
``degraded`` (the provider failed; placeholder copy stored) or ``rule_based``
(below the relevance action floor; no explanation requested from the model).

Additive and safe on a live database: one nullable column, no default, no
backfill. Existing rows stay NULL, which the API reports as "not recorded" -
whether an older row's copy came from a model cannot be reconstructed, so it is
never guessed. ``downgrade`` drops only this column.

Revision ID: 6ad2ff77f691
Revises: 87198ab57b59
Create Date: 2026-10-20 20:46:30.465464
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '6ad2ff77f691'
down_revision: str | None = '87198ab57b59'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('opportunities', schema=None) as batch_op:
        batch_op.add_column(sa.Column('explanation_source', sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('opportunities', schema=None) as batch_op:
        batch_op.drop_column('explanation_source')
