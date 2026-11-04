"""add creative drafts and content reviews (Project Phase 5 CF-2)

The Phase 5C contracts freeze (W21 CF-2). Dark: nothing writes these tables until the 5C
build lanes add their services, and every new capability flag defaults ``False``.

* ``creative_drafts`` - immutable, versioned marketing-text drafts generated from a
  recommendation brief: scope (organization / workspace / brief / opportunity cascade;
  location / campaign ``SET NULL``), ``draft_version`` unique per (workspace, brief) with a
  ``parent_draft_id`` revision chain, a closed text ``channel``, the text (``headline``,
  ``body``, ``call_to_action``), the LLM provenance with an immutable usage snapshot
  (``input_tokens``, ``output_tokens``, ``estimated_cost_usd``, never negative) and a
  ``SET NULL`` link to the Phase 6 ``llm_usage_events`` row of the call, which stays the
  accounting authority (no ``llm_usage_records`` table: coordinator decision I-4), and
  ``created_by`` ``SET NULL`` + the non-FK ``created_by_snapshot``.
  ``created_at`` only.
* ``content_reviews`` - one output-side review per draft (``uq_content_review_draft``), with
  explicit tenant columns, the persisted claim-safety result, blocked-claim hits, required
  disclaimers and ``passed``; CHECKs tie ``is_blocked`` to ``risk_level = 'blocked'`` and
  ``passed`` to "not blocked and disclaimers resolved". Cascades with its draft.
* ``approvals`` - ``subject_type`` admits ``creative_draft``; a ``draft_id`` live link
  (``SET NULL``) with ``ck_approval_draft_subject`` (while set, it equals ``subject_id``).
* ``claims_library.retired_at`` - soft retirement for the protected claims library.
* ``workspace_capability_overrides`` - the capability CHECK admits ``creative_generation``.

CHECK literals are frozen with this revision; ``test_cf2_contract_migration`` compares the
migrated database's CHECKs with their Python authorities in both directions.

Rollback: the downgrade **refuses before any DDL** while an override names
``creative_generation``, while an approval decides on a ``creative_draft`` (a compliance
record, O-6), or while a claims-library entry is retired (dropping ``retired_at`` would
silently re-activate it). Otherwise it narrows the CHECKs back, drops ``draft_id`` and
``retired_at``, and drops ``content_reviews`` and ``creative_drafts`` (their rows are lost -
none exist while the capability is dark). All counts are Core expressions.

Revision ID: a7d2e9f4c1b8
Revises: f3a9c1e7b2d5
Create Date: 2026-11-01 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7d2e9f4c1b8"
down_revision: str | None = "f3a9c1e7b2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DRAFTS = "creative_drafts"
_REVIEWS = "content_reviews"
_APPROVALS = "approvals"
_CLAIMS = "claims_library"
_OVERRIDES = "workspace_capability_overrides"
_CAPABILITY_CHECK = "ck_workspace_capability_override_capability"
_CAPABILITY_IN_WIDE = (
    "capability IN ('connector_rss', 'creative_generation', 'opportunity_feedback',"
    " 'recommendation_briefs', 'scout_scheduling')"
)
_CAPABILITY_IN_NARROW = (
    "capability IN ('connector_rss', 'opportunity_feedback', 'recommendation_briefs',"
    " 'scout_scheduling')"
)
_SUBJECT_CHECK = "ck_approval_subject_type"
_SUBJECT_IN_WIDE = "subject_type IN ('creative_draft', 'recommendation_brief')"
_SUBJECT_IN_NARROW = "subject_type IN ('recommendation_brief')"
_DRAFT_SUBJECT_CHECK = "ck_approval_draft_subject"
_DRAFT_SUBJECT = (
    "draft_id IS NULL OR (subject_type = 'creative_draft' AND draft_id = subject_id)"
)
_DRAFT_FK = "approvals_draft_id_fkey"
_CHANNEL = "channel IN ('email', 'landing_page', 'search_ad', 'sms', 'social_post')"
_RISK = "risk_level IN ('blocked', 'high', 'low', 'medium')"
_DRAFT_USAGE = "input_tokens >= 0 AND output_tokens >= 0 AND estimated_cost_usd >= 0"
_REVIEW_BLOCKED = (
    "(is_blocked AND risk_level = 'blocked') OR (NOT is_blocked AND risk_level <> 'blocked')"
)
_REVIEW_PASSED = "NOT passed OR (NOT is_blocked AND disclaimers_resolved)"

_DRAFT_INDEXED = (
    "organization_id",
    "workspace_id",
    "brief_id",
    "opportunity_id",
    "location_id",
    "campaign_id",
    "parent_draft_id",
    "llm_usage_event_id",
    "created_by",
)
_REVIEW_INDEXED = ("organization_id", "workspace_id")


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("(CURRENT_TIMESTAMP)"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        _DRAFTS,
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("brief_id", sa.String(length=32), nullable=False),
        sa.Column("opportunity_id", sa.String(length=32), nullable=False),
        sa.Column("location_id", sa.String(length=32), nullable=True),
        sa.Column("campaign_id", sa.String(length=32), nullable=True),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("parent_draft_id", sa.String(length=32), nullable=True),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("headline", sa.String(length=200), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("call_to_action", sa.String(length=200), nullable=True),
        sa.Column("prompt_name", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=20), nullable=False),
        sa.Column("model_identifier", sa.String(length=100), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("is_simulated", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("llm_usage_event_id", sa.String(length=32), nullable=True),
        sa.Column("created_by", sa.String(length=32), nullable=True),
        sa.Column("created_by_snapshot", sa.String(length=600), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        _created_at(),
        sa.CheckConstraint(_CHANNEL, name="ck_creative_draft_channel"),
        sa.CheckConstraint(_DRAFT_USAGE, name="ck_creative_draft_usage"),
        sa.CheckConstraint("draft_version >= 1", name="ck_creative_draft_version"),
        sa.ForeignKeyConstraint(["brief_id"], ["recommendation_briefs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["llm_usage_event_id"], ["llm_usage_events.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["location_id"], ["business_locations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_draft_id"], ["creative_drafts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "brief_id", "draft_version", name="uq_creative_draft_version"
        ),
    )
    for column in _DRAFT_INDEXED:
        op.create_index(f"ix_{_DRAFTS}_{column}", _DRAFTS, [column])

    op.create_table(
        _REVIEWS,
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("draft_id", sa.String(length=32), nullable=False),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("is_blocked", sa.Boolean(), nullable=False),
        sa.Column("findings", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("blocked_claim_ids", sa.JSON(), nullable=False),
        sa.Column("required_disclaimers", sa.JSON(), nullable=False),
        sa.Column("disclaimers_resolved", sa.Boolean(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        _created_at(),
        sa.CheckConstraint(_REVIEW_BLOCKED, name="ck_content_review_blocked"),
        sa.CheckConstraint("draft_version >= 1", name="ck_content_review_draft_version"),
        sa.CheckConstraint(_REVIEW_PASSED, name="ck_content_review_passed"),
        sa.CheckConstraint(_RISK, name="ck_content_review_risk_level"),
        sa.ForeignKeyConstraint(["draft_id"], ["creative_drafts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_id", name="uq_content_review_draft"),
    )
    for column in _REVIEW_INDEXED:
        op.create_index(f"ix_{_REVIEWS}_{column}", _REVIEWS, [column])

    with op.batch_alter_table(_APPROVALS, schema=None) as batch_op:
        batch_op.add_column(sa.Column("draft_id", sa.String(length=32), nullable=True))
        batch_op.create_foreign_key(
            _DRAFT_FK, _DRAFTS, ["draft_id"], ["id"], ondelete="SET NULL"
        )
        batch_op.drop_constraint(_SUBJECT_CHECK, type_="check")
        batch_op.create_check_constraint(_SUBJECT_CHECK, sa.text(_SUBJECT_IN_WIDE))
        batch_op.create_check_constraint(_DRAFT_SUBJECT_CHECK, sa.text(_DRAFT_SUBJECT))
    op.create_index("ix_approvals_draft_id", _APPROVALS, ["draft_id"])

    with op.batch_alter_table(_CLAIMS, schema=None) as batch_op:
        batch_op.add_column(sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table(_OVERRIDES, schema=None) as batch_op:
        batch_op.drop_constraint(_CAPABILITY_CHECK, type_="check")
        batch_op.create_check_constraint(_CAPABILITY_CHECK, sa.text(_CAPABILITY_IN_WIDE))


def _count(table: sa.TableClause, *where) -> int:
    query = sa.select(sa.func.count()).select_from(table)
    if where:
        query = query.where(*where)
    return op.get_bind().execute(query).scalar_one()


def downgrade() -> None:
    # Refuse before any DDL (Core counts only; no SQL text is assembled from strings).
    overrides = sa.table(_OVERRIDES, sa.column("capability"))
    named = _count(overrides, overrides.c.capability == "creative_generation")
    if named:
        raise RuntimeError(
            f"{named} workspace capability override(s) name 'creative_generation'; clear "
            "them before downgrading below a7d2e9f4c1b8. Nothing was changed."
        )
    approvals = sa.table(_APPROVALS, sa.column("subject_type"))
    decided = _count(approvals, approvals.c.subject_type == "creative_draft")
    if decided:
        raise RuntimeError(
            f"{decided} approval record(s) decide on a creative_draft; approvals are compliance "
            "records (O-6) and are not dropped or narrowed by a downgrade below a7d2e9f4c1b8. "
            "Nothing was changed."
        )
    claims = sa.table(_CLAIMS, sa.column("retired_at"))
    retired = _count(claims, claims.c.retired_at.is_not(None))
    if retired:
        raise RuntimeError(
            f"{retired} claims-library entr(y/ies) are retired; dropping retired_at would "
            "re-activate them. Resolve them before downgrading below a7d2e9f4c1b8. Nothing "
            "was changed."
        )

    with op.batch_alter_table(_OVERRIDES, schema=None) as batch_op:
        batch_op.drop_constraint(_CAPABILITY_CHECK, type_="check")
        batch_op.create_check_constraint(_CAPABILITY_CHECK, sa.text(_CAPABILITY_IN_NARROW))

    with op.batch_alter_table(_CLAIMS, schema=None) as batch_op:
        batch_op.drop_column("retired_at")

    op.drop_index("ix_approvals_draft_id", table_name=_APPROVALS)
    with op.batch_alter_table(_APPROVALS, schema=None) as batch_op:
        batch_op.drop_constraint(_DRAFT_SUBJECT_CHECK, type_="check")
        batch_op.drop_constraint(_SUBJECT_CHECK, type_="check")
        batch_op.create_check_constraint(_SUBJECT_CHECK, sa.text(_SUBJECT_IN_NARROW))
        batch_op.drop_constraint(_DRAFT_FK, type_="foreignkey")
        batch_op.drop_column("draft_id")

    for column in reversed(_REVIEW_INDEXED):
        op.drop_index(f"ix_{_REVIEWS}_{column}", table_name=_REVIEWS)
    op.drop_table(_REVIEWS)
    for column in reversed(_DRAFT_INDEXED):
        op.drop_index(f"ix_{_DRAFTS}_{column}", table_name=_DRAFTS)
    op.drop_table(_DRAFTS)
