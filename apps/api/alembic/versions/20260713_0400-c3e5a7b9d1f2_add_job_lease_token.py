"""add job lease token

Adds a single additive, nullable ``lease_token`` column to the ``jobs`` table.
The token is an opaque, per-claim ownership credential: it is minted fresh each
time a worker claims a job and is required for every subsequent worker mutation
(heartbeat / running / success / failure / retry / dead-letter / cancellation
acknowledgement). A reclaim rotates it, which fences out a stale worker whose
lease has already been recovered by another worker.

Purely **additive and safe on a live database**:

* The column is ``NULL``-able with no server default, so adding it touches no
  existing row's data and needs no backfill.
* **Currently-claimed rows** (any ``jobs`` row already in ``claimed``/``running``
  when this migration runs) get ``lease_token = NULL``. Their owning worker
  captured ``NULL`` at claim time, and the fenced UPDATE predicate requires the
  token to match exactly, so those in-flight jobs can no longer be completed by
  their pre-upgrade owner. That is the intended, safe outcome: their leases
  simply expire and :meth:`recover_expired_leases` returns them to the queue for
  a clean, fully-fenced re-claim. No job or audit data is lost.

Rollback: ``downgrade`` drops the column. Because it carries only transient
ownership state (never business data), dropping it is safe; any in-flight leases
are again recovered by lease expiry after a downgrade.

Revision ID: c3e5a7b9d1f2
Revises: dacbf2b1e915
Create Date: 2026-07-13 04:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3e5a7b9d1f2'
down_revision: str | None = 'dacbf2b1e915'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lease_token', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('lease_token')
