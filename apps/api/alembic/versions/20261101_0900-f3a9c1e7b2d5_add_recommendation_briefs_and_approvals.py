"""add recommendation briefs and approvals (Project Phase 5 CF-1)

The Phase 5A + 5B contracts freeze (W21 CF-1). Dark: nothing reads or writes these tables
until the 5A/5B build lanes add their services, and every new capability flag defaults
``False``.

* ``recommendation_briefs`` - immutable, versioned recommendation briefs (Phase 5A plan §2):
  bound to the exact ``signal_intelligence_records`` row they were derived from, with the
  full LLM provenance (prompt name/version, model, input hash, trace id, simulation flag,
  and a ``SET NULL`` reference to the Phase 6 ``llm_usage_events`` row of the call) and
  authorship (``created_by`` ``SET NULL`` + the non-FK ``created_by_snapshot``). One row per
  ``(workspace_id, opportunity_id, brief_version)``. Tenant columns cascade with the
  workspace/organization; ``created_at`` only (append-only, no ``updated_at``).
* ``approvals`` - append-only decisions (approved / rejected / revoked) on one exact subject
  version (Phase 5B plan §2): FK-less ``organization_id`` / ``workspace_id`` so a decision
  survives its scope's deletion (operator decision O-6, the ``audit_logs`` shape); the
  subject named by ``subject_type`` / ``subject_id`` / ``subject_version``, with ``brief_id``
  the live ``SET NULL`` link; ``decided_by`` ``SET NULL`` + ``decided_by_snapshot`` +
  ``decided_role``; a monotonic ``decision_seq`` unique per subject version. Closed
  vocabularies (subject type, decision, decided role, reason-code polarity) are portable
  CHECKs on SQLite and PostgreSQL alike.
* ``workspace_capability_overrides`` - ``ck_workspace_capability_override_capability`` widened
  to admit ``recommendation_briefs`` (the new ``Capability`` member). On SQLite the table is
  rebuilt in batch mode (the established pattern); on PostgreSQL the CHECK is dropped and
  re-created.

The CHECK lists below are literals, frozen with this revision. ``test_cf1_contract_migration``
compares the migrated database's CHECKs with the Python authorities they were rendered from
(``app.capabilities.registry``, ``app.core.enums``, ``app.approvals.reasons``), in both
directions, so a later vocabulary change without its own widening revision fails there.

Rollback: ``downgrade`` narrows the capability CHECK back, then drops ``approvals`` and
``recommendation_briefs`` (their rows are lost - none exist while the capability is dark). It
**refuses, before any DDL,** while any override row names ``recommendation_briefs``: clear
that override first. No override is deleted implicitly.

Revision ID: f3a9c1e7b2d5
Revises: e7b3c5a9d1f4
Create Date: 2026-11-01 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3a9c1e7b2d5"
down_revision: str | None = "e7b3c5a9d1f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BRIEFS = "recommendation_briefs"
_APPROVALS = "approvals"
_OVERRIDES = "workspace_capability_overrides"
_CAPABILITY_CHECK = "ck_workspace_capability_override_capability"
_CAPABILITY_IN_WIDE = (
    "capability IN ('connector_rss', 'opportunity_feedback', 'recommendation_briefs',"
    " 'scout_scheduling')"
)
_CAPABILITY_IN_NARROW = (
    "capability IN ('connector_rss', 'opportunity_feedback', 'scout_scheduling')"
)
_REASON_POLARITY = (
    "reason_code IS NULL"
    " OR (decision = 'approved' AND reason_code IN"
    " ('aligned_with_strategy', 'approved_other', 'evidence_sufficient'))"
    " OR (decision = 'rejected' AND reason_code IN"
    " ('brand_or_compliance_risk', 'inaccurate', 'insufficient_evidence', 'off_strategy',"
    " 'rejected_other'))"
    " OR (decision = 'revoked' AND reason_code IN"
    " ('approved_in_error', 'circumstances_changed', 'revoked_other', 'superseded'))"
)
_DECIDED_ROLE = (
    "decided_role IN ('admin', 'compliance_reviewer', 'marketer', 'owner', 'reviewer',"
    " 'viewer')"
)
_BRIEF_SUBJECT = (
    "brief_id IS NULL OR (subject_type = 'recommendation_brief' AND brief_id = subject_id)"
)

_BRIEF_INDEXED = (
    "organization_id",
    "workspace_id",
    "opportunity_id",
    "intelligence_record_id",
    "location_id",
    "campaign_id",
    "parent_brief_id",
    "llm_usage_event_id",
    "created_by",
)
_APPROVAL_INDEXED = ("organization_id", "workspace_id", "brief_id", "decided_by")


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("(CURRENT_TIMESTAMP)"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        _BRIEFS,
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("opportunity_id", sa.String(length=32), nullable=False),
        sa.Column("intelligence_record_id", sa.String(length=32), nullable=False),
        sa.Column("location_id", sa.String(length=32), nullable=True),
        sa.Column("campaign_id", sa.String(length=32), nullable=True),
        sa.Column("brief_version", sa.Integer(), nullable=False),
        sa.Column("parent_brief_id", sa.String(length=32), nullable=True),
        sa.Column("analysis_version", sa.String(length=20), nullable=False),
        sa.Column("scoring_version", sa.String(length=20), nullable=False),
        sa.Column("prompt_name", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=20), nullable=False),
        sa.Column("model_identifier", sa.String(length=100), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("is_simulated", sa.Boolean(), nullable=False),
        sa.Column("llm_usage_event_id", sa.String(length=32), nullable=True),
        sa.Column("created_by", sa.String(length=32), nullable=True),
        sa.Column("created_by_snapshot", sa.String(length=600), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("recommended_actions", sa.JSON(), nullable=False),
        sa.Column("suggested_angles", sa.JSON(), nullable=False),
        sa.Column("risk_notes", sa.JSON(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        _created_at(),
        sa.CheckConstraint("brief_version >= 1", name="ck_recommendation_brief_version"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["intelligence_record_id"], ["signal_intelligence_records.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["llm_usage_event_id"], ["llm_usage_events.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["location_id"], ["business_locations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_brief_id"], ["recommendation_briefs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "opportunity_id",
            "brief_version",
            name="uq_recommendation_brief_version",
        ),
    )
    for column in _BRIEF_INDEXED:
        op.create_index(f"ix_{_BRIEFS}_{column}", _BRIEFS, [column])

    op.create_table(
        _APPROVALS,
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.String(length=32), nullable=False),
        sa.Column("subject_version", sa.Integer(), nullable=False),
        sa.Column("brief_id", sa.String(length=32), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("decision_seq", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=40), nullable=True),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.Column("decided_by", sa.String(length=32), nullable=True),
        sa.Column("decided_by_snapshot", sa.String(length=600), nullable=False),
        sa.Column("decided_role", sa.String(length=32), nullable=False),
        sa.Column("is_simulated", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        _created_at(),
        sa.CheckConstraint(_BRIEF_SUBJECT, name="ck_approval_brief_subject"),
        sa.CheckConstraint(_DECIDED_ROLE, name="ck_approval_decided_role"),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected', 'revoked')", name="ck_approval_decision"
        ),
        sa.CheckConstraint("decision_seq >= 1", name="ck_approval_decision_seq"),
        sa.CheckConstraint(_REASON_POLARITY, name="ck_approval_reason_polarity"),
        sa.CheckConstraint(
            "subject_type IN ('recommendation_brief')", name="ck_approval_subject_type"
        ),
        sa.CheckConstraint("subject_version >= 1", name="ck_approval_subject_version"),
        sa.ForeignKeyConstraint(["brief_id"], ["recommendation_briefs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subject_type",
            "subject_id",
            "subject_version",
            "decision_seq",
            name="uq_approval_decision_seq",
        ),
    )
    for column in _APPROVAL_INDEXED:
        op.create_index(f"ix_{_APPROVALS}_{column}", _APPROVALS, [column])

    with op.batch_alter_table(_OVERRIDES, schema=None) as batch_op:
        batch_op.drop_constraint(_CAPABILITY_CHECK, type_="check")
        batch_op.create_check_constraint(_CAPABILITY_CHECK, sa.text(_CAPABILITY_IN_WIDE))


def downgrade() -> None:
    # Refuse before any DDL while an override names the capability: the narrowed CHECK could
    # not hold it, and an override is operator intent that is never deleted implicitly.
    named = op.get_bind().execute(
        sa.text(f"SELECT count(*) FROM {_OVERRIDES} WHERE capability = 'recommendation_briefs'")
    ).scalar_one()
    if named:
        raise RuntimeError(
            f"{named} workspace capability override(s) name 'recommendation_briefs'; clear "
            "them before downgrading below f3a9c1e7b2d5. Nothing was changed."
        )
    with op.batch_alter_table(_OVERRIDES, schema=None) as batch_op:
        batch_op.drop_constraint(_CAPABILITY_CHECK, type_="check")
        batch_op.create_check_constraint(_CAPABILITY_CHECK, sa.text(_CAPABILITY_IN_NARROW))
    for column in reversed(_APPROVAL_INDEXED):
        op.drop_index(f"ix_{_APPROVALS}_{column}", table_name=_APPROVALS)
    op.drop_table(_APPROVALS)
    for column in reversed(_BRIEF_INDEXED):
        op.drop_index(f"ix_{_BRIEFS}_{column}", table_name=_BRIEFS)
    op.drop_table(_BRIEFS)
