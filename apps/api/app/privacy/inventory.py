"""The machine-checked data inventory (``P6-PRIV-1``).

``docs/operations/data-inventory.md`` is the human-readable inventory; this module is its
executable counterpart. Every table registered on ``Base.metadata`` has exactly one
:class:`TableEntry` here, and ``test_privacy_inventory.py`` fails when a table is added
without one. The entries decide three things and nothing else:

* the **category** of data the table holds, which the documentation explains;
* whether, and how, the table appears in an **organization export** (which rows belong
  to the organization, and which columns are never exported);
* which **retention class** purges it (``None``: it lives as long as its parent).

Deletion does not read this module for *what* to delete: it follows the declared foreign
keys (:mod:`app.privacy.erasure`). Verification reads neither: it reflects the live
schema (:mod:`app.privacy.verification`). Three independent views of the same tables, so
one of them being wrong cannot silently agree with itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RetentionClass(StrEnum):
    """The data classes with a time-based purge. Each has one duration setting.

    The setting is ``retention_<value>_days`` on :class:`~app.core.config.Settings`.
    """

    AUTH_SESSIONS = "auth_sessions"
    ACCOUNT_TOKENS = "account_tokens"
    INVITATIONS = "invitations"
    SIGNAL_EVIDENCE = "signal_evidence"
    OPPORTUNITIES = "opportunities"
    JOB_HISTORY = "job_history"
    AUDIT_LOGS = "audit_logs"

    @property
    def setting_name(self) -> str:
        return f"retention_{self.value}_days"


#: Classes whose rows belong to a workspace: purged by the per-workspace
#: ``retention.purge`` durable job, inside that job's tenant scope.
WORKSPACE_CLASSES: frozenset[RetentionClass] = frozenset(
    {RetentionClass.SIGNAL_EVIDENCE, RetentionClass.OPPORTUNITIES, RetentionClass.JOB_HISTORY}
)
#: Classes with no workspace (account credentials, organization invitations, audit
#: records): purged by the sweep itself in bounded, separately committed batches,
#: because a durable job row requires an organization and a workspace.
PLATFORM_CLASSES: frozenset[RetentionClass] = frozenset(RetentionClass) - WORKSPACE_CLASSES


class Category(StrEnum):
    ACCOUNT = "account"  # an account holder's identity or membership
    CREDENTIAL = "credential"  # sessions and one-time tokens of an account
    COMPLIANCE = "compliance"  # audit records
    TENANT_CONTENT = "tenant_content"  # customer-provided business configuration
    COLLECTED = "collected"  # collected third-party content and what is derived from it
    OPERATIONAL = "operational"  # job bookkeeping
    SYSTEM = "system"  # no personal or tenant data


@dataclass(frozen=True)
class TableEntry:
    table: str
    category: Category
    #: Whether the organization export includes the table.
    exported: bool = True
    #: Columns never exported: credentials, token digests and derived vectors.
    excluded_columns: tuple[str, ...] = ()
    #: The retention class whose purge removes the table's rows as a root (dependents
    #: of a purged root are removed with it whatever their own class).
    retention_class: RetentionClass | None = None


_TENANT = Category.TENANT_CONTENT
_COLLECTED = Category.COLLECTED

TABLES: dict[str, TableEntry] = {
    e.table: e
    for e in (
        TableEntry("users", Category.ACCOUNT, exported=False),
        TableEntry("organizations", Category.ACCOUNT),
        TableEntry("organization_members", Category.ACCOUNT),
        TableEntry(
            "organization_invitations",
            Category.ACCOUNT,
            excluded_columns=("token_hash",),
            retention_class=RetentionClass.INVITATIONS,
        ),
        TableEntry(
            "auth_sessions",
            Category.CREDENTIAL,
            exported=False,
            retention_class=RetentionClass.AUTH_SESSIONS,
        ),
        TableEntry(
            "password_reset_tokens",
            Category.CREDENTIAL,
            exported=False,
            retention_class=RetentionClass.ACCOUNT_TOKENS,
        ),
        TableEntry(
            "email_verification_tokens",
            Category.CREDENTIAL,
            exported=False,
            retention_class=RetentionClass.ACCOUNT_TOKENS,
        ),
        TableEntry("audit_logs", Category.COMPLIANCE, retention_class=RetentionClass.AUDIT_LOGS),
        TableEntry("workspaces", _TENANT),
        TableEntry("workspace_capability_overrides", _TENANT),
        TableEntry("brands", _TENANT),
        TableEntry("business_profiles", _TENANT),
        TableEntry("business_locations", _TENANT),
        TableEntry("geo_coverage_rules", _TENANT),
        TableEntry("product_profiles", _TENANT),
        TableEntry("audience_profiles", _TENANT),
        TableEntry("competitor_profiles", _TENANT),
        TableEntry("brand_voice_profiles", _TENANT),
        TableEntry("offer_calendar", _TENANT),
        TableEntry("claims_library", _TENANT),
        TableEntry("source_preferences", _TENANT),
        TableEntry("channel_preferences", _TENANT),
        TableEntry("campaigns", _TENANT),
        TableEntry("scout_requests", _TENANT),
        TableEntry("scout_schedules", _TENANT),
        TableEntry("raw_signals", _COLLECTED, retention_class=RetentionClass.SIGNAL_EVIDENCE),
        TableEntry("normalized_signals", _COLLECTED, excluded_columns=("embedding",)),
        TableEntry("signal_location_evidence", _COLLECTED),
        TableEntry(
            "signal_clusters",
            _COLLECTED,
            excluded_columns=("centroid",),
            retention_class=RetentionClass.SIGNAL_EVIDENCE,
        ),
        TableEntry("signal_intelligence_records", _COLLECTED),
        TableEntry("opportunities", _COLLECTED, retention_class=RetentionClass.OPPORTUNITIES),
        TableEntry("opportunity_scores", _COLLECTED),
        TableEntry("validation_evidence", _COLLECTED),
        TableEntry("opportunity_feedback", _COLLECTED),
        TableEntry(
            "jobs",
            Category.OPERATIONAL,
            exported=False,
            retention_class=RetentionClass.JOB_HISTORY,
        ),
        TableEntry("job_events", Category.OPERATIONAL, exported=False),
        # The Scout admission ledger (D-25 execution controls, PROPOSED; ``app.jobs.admission``):
        # one row per admitted external operation - keys, operation kind, attempt, worker id,
        # instants, end kind and an operator's bounded reconciliation note (a reason code plus
        # at most 200 characters, never content). Operational control state like
        # ``job_events``: not exported (it carries worker ids); deleted with its Scout,
        # workspace and organization; no time-based retention class (pruning is a follow-up).
        TableEntry("scout_admissions", Category.OPERATIONAL, exported=False),
        # The LLM usage ledger (P6-LLM-4): tenant-linked usage metadata, one row per
        # provider attempt. Operator decisions of 2026-10-27 (D-9a/D-9b/D-9c, packet
        # D9-DECISIONS-01): OPERATIONAL; included in the organization export with every
        # recorded usage/accounting column, scoped by ``organization_id`` (it has no user
        # key, so it has no supported relationship to an account and never appears in the
        # account export); no time-based retention class: kept for the life of its
        # workspace for usage accounting and reconciliation during the unpaid pilot, deleted
        # with its workspace/organization, to be revisited before paid launch.
        TableEntry("llm_usage_events", Category.OPERATIONAL, exported=True),
        # The LLM budget ledger (D-LLM-1 option B, ``app.llm.budget``): per workspace and
        # UTC month the money reserved before provider calls and settled after them
        # (``llm_budget_periods``), and one row per reserved call keyed by attempt
        # (``llm_budget_reservations``). Money, keys, states and counts only - no prompt
        # text, no content, no user id. Same class as the usage ledger: OPERATIONAL;
        # included in the organization export with every column, scoped by
        # ``organization_id`` (D-9b pattern); no time-based retention class - kept for the
        # life of the workspace for usage accounting and reconciliation, deleted with its
        # workspace/organization (D-9c pattern; ruled for these two tables by the
        # coordinator on 2026-10-27T19:27Z).
        TableEntry("llm_budget_periods", Category.OPERATIONAL, exported=True),
        TableEntry("llm_budget_reservations", Category.OPERATIONAL, exported=True),
        TableEntry("worker_registrations", Category.SYSTEM, exported=False),
        # The permanent suppression record of a live source an operator took down
        # (P6-PRIV-4). Operator decision D-9d (2026-10-27): suppression key, time and
        # counts only; attribution is in audit_logs, whose affected-workspace rows
        # (``data_source.taken_down``) carry the acting operator, a fixed generic
        # explanation and the opaque case id under that table's retention. Platform-wide
        # (no tenant key): never exported; no retention class — the row *is* the
        # suppression and is kept as long as the key must stay down.
        TableEntry("source_takedowns", Category.SYSTEM, exported=False),
        # The restricted operator case record of each takedown request (operator decision
        # D-9e, 2026-10-27): the free-text reason and the operator's own ticket id, the
        # acting operator (``actor_user_id`` is SET NULL when that account is erased) and
        # the opaque case id that customer-visible audit entries carry. Operator-only
        # (GET /internal/system/data-sources/{key}/cases); never exported; never in a
        # tenant API, log or metric. Retention duration pending operator decision (D-9e
        # access/retention policy): no retention class and no duration is set here.
        # Operator free text (reason, external_reference) is not reached by account
        # erasure: if it names a person who is later erased, the text remains and the E10
        # content verifier reports findings on this table; this is pending the operator's
        # access/retention/redaction decision (D-9e).
        TableEntry("source_takedown_cases", Category.COMPLIANCE, exported=False),
    )
}


__all__ = [
    "PLATFORM_CLASSES",
    "TABLES",
    "WORKSPACE_CLASSES",
    "Category",
    "RetentionClass",
    "TableEntry",
]
