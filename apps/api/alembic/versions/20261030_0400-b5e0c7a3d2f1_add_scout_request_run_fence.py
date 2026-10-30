"""add scout request run fence

Adds ``scout_requests.run_fence`` for the D-25 cooperative-pause candidate (PROPOSED,
pending the operator's ruling; ``app.jobs.admission``): a durable per-request counter that
every pause advances and every ``scout_request.execute`` job carries in its payload. A run
may continue only while the request is ``running`` and its fence still equals the job's,
so a run queued or in flight before a pause cannot continue after a resume.

Additive and safe on a live database:

* One ``INTEGER NOT NULL`` column with server default ``0``: existing rows read 0, the
  value a job enqueued before this revision is treated as carrying (its payload has no
  ``run_fence`` key).
* One data step: a request that is ``paused`` when this revision runs gets fence 1, so a
  job queued before that pause stays fenced even after a later resume. A job queued
  before a Pause -> Resume that both happened before this revision behaves as before it
  (documented residual).
* No index: the column is read and written only together with the row's primary key.

Rollback: ``downgrade`` drops the column; the fence (and with it the "no escape through
Pause -> Resume" guarantee) is lost, the code at the previous revision does not read it.

Revision ID: b5e0c7a3d2f1
Revises: 8005724d811a
Create Date: 2026-10-30 04:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5e0c7a3d2f1"
down_revision: str | None = "8005724d811a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("scout_requests", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("run_fence", sa.Integer(), server_default="0", nullable=False)
        )
    op.execute("UPDATE scout_requests SET run_fence = 1 WHERE status = 'paused'")


def downgrade() -> None:
    with op.batch_alter_table("scout_requests", schema=None) as batch_op:
        batch_op.drop_column("run_fence")
