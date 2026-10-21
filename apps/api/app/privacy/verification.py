"""Independent erasure verification — the E10 proof path (``P6-PRIV-1``).

Phase-6 demonstration E10 ("A data-deletion request has been serviced end to end by an
implemented mechanism") must be independently verifiable and may not rest on audit
records, because audit coverage (``P6-PRIV-5``) is P1. So this module does not read the
audit trail, does not read :mod:`app.privacy.inventory` and does not call
:mod:`app.privacy.erasure`. It **reflects the live database schema** and asks the database
two questions about the subject:

1. **References.** Every column that can point at the subject — any column with a foreign
   key to ``users.id`` / ``organizations.id`` / ``workspaces.id``, any column *named*
   ``user_id``, ``*_user_id``, ``organization_id`` or ``workspace_id`` (foreign key or
   not), and the subject table's own ``id`` — is counted for the subject's ids.
2. **Content.** Every text or JSON column of every table is scanned, case-insensitively,
   for the subject's identifiers (the account id and email address; or the organization
   and workspace ids), so a copy left in free text or a JSON payload is found too.

A report is ``verified`` only when both come back empty. The single declared exception is
the organization's own audit records, which ``O-6`` keeps after the organization is gone:
for an organization subject, ``audit_logs`` rows whose ``organization_id`` is the subject
are reported as *retained* — and each must have every identifying column cleared, or it
is a finding. Nothing here writes.

Cost: the content scan reads every text column of every table once per identifier. That
is acceptable for an operator tool and for the pilot's data volume; it is not an online
path for a large database.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sqlalchemy import JSON, MetaData, String, Table, Text, cast, func, inspect, or_, select
from sqlalchemy.orm import Session

_SKIPPED_TABLES = frozenset({"alembic_version"})


@dataclass(frozen=True)
class Finding:
    table: str
    column: str
    rows: int


@dataclass
class ErasureReport:
    subject_type: str  # "account" | "organization" | "email"
    subject_id: str | None
    #: SHA-256 of the lower-cased email checked, so a report can be kept as evidence
    #: without carrying the address itself.
    email_sha256: str | None
    tables_checked: int = 0
    reference_columns_checked: int = 0
    content_columns_checked: int = 0
    reference_findings: list[Finding] = field(default_factory=list)
    content_findings: list[Finding] = field(default_factory=list)
    retained_anonymised: list[Finding] = field(default_factory=list)
    generated_at: str = ""

    @property
    def verified(self) -> bool:
        return not self.reference_findings and not self.content_findings

    def as_dict(self) -> dict:
        out = asdict(self)
        out["verified"] = self.verified
        return out


def _reflect(db: Session) -> list[Table]:
    connection = db.connection()
    inspector = inspect(connection)
    metadata = MetaData()
    tables = []
    for name in sorted(inspector.get_table_names()):
        if name in _SKIPPED_TABLES:
            continue
        tables.append(Table(name, metadata, autoload_with=connection))
    return tables


def _points_at(column, target_table: str) -> bool:
    return any(fk.column.table.name == target_table for fk in column.foreign_keys)


def _reference_columns(table: Table, target_table: str, names: tuple[str, ...]) -> list:
    out = []
    for column in table.columns:
        named = column.name in names or (
            target_table == "users" and column.name.endswith("_user_id")
        )
        if _points_at(column, target_table) or named:
            out.append(column)
    if table.name == target_table:
        out.append(table.c.id)
    return list(dict.fromkeys(out))


def _is_text(column) -> bool:
    return isinstance(column.type, String | JSON) or column.type.__class__.__name__ in {
        "JSONB",
        "JSON",
    }


def _needles(value: str) -> tuple[str, ...]:
    lowered = value.lower()
    escaped = json.dumps(lowered)[1:-1]
    return (lowered,) if escaped == lowered else (lowered, escaped)


def _count(db: Session, table: Table, *where) -> int:
    return db.execute(select(func.count()).select_from(table).where(*where)).scalar_one()


def _content_findings(
    db: Session, tables: list[Table], identifiers: list[str], *, skip: set[tuple[str, str]]
) -> tuple[int, list[Finding]]:
    needles = tuple(n for value in identifiers for n in _needles(value))
    checked, findings = 0, []
    for table in tables:
        for column in table.columns:
            if not _is_text(column) or (table.name, column.name) in skip:
                continue
            checked += 1
            text = func.lower(cast(column, Text))
            rows = _count(db, table, or_(*(text.contains(n, autoescape=True) for n in needles)))
            if rows:
                findings.append(Finding(table.name, column.name, rows))
    return checked, findings


def _stamp(report: ErasureReport, tables: list[Table]) -> ErasureReport:
    report.tables_checked = len(tables)
    report.generated_at = datetime.now(UTC).isoformat()
    return report


def verify_subject_erased(
    db: Session, *, user_id: str | None = None, email: str | None = None
) -> ErasureReport:
    """Prove no row references the account ``user_id`` and no value contains it or ``email``.

    Either may be given alone: an invitee who never registered has only an email.
    """
    if not user_id and not email:
        raise ValueError("verify_subject_erased() needs a user id, an email, or both.")
    report = ErasureReport(
        subject_type="account" if user_id else "email",
        subject_id=user_id,
        email_sha256=hashlib.sha256(email.lower().encode()).hexdigest() if email else None,
    )
    tables = _reflect(db)
    if user_id:
        for table in tables:
            for column in _reference_columns(table, "users", ("user_id",)):
                report.reference_columns_checked += 1
                rows = _count(db, table, column == user_id)
                if rows:
                    report.reference_findings.append(Finding(table.name, column.name, rows))
    identifiers = [v for v in (user_id, email) if v]
    report.content_columns_checked, report.content_findings = _content_findings(
        db, tables, identifiers, skip=set()
    )
    return _stamp(report, tables)


#: The columns of an organization's retained audit records that must all be cleared.
_AUDIT_IDENTIFYING = ("actor_user_id", "entity_id", "reason", "previous_state", "new_state")


def verify_organization_erased(
    db: Session, *, organization_id: str, workspace_ids: tuple[str, ...] | list[str] = ()
) -> ErasureReport:
    """Prove nothing references the organization or its workspaces, beyond anonymised audit.

    ``workspace_ids`` are the organization's workspaces as they were before deletion (the
    deletion receipt lists them). Without them, tables that reach the organization only
    through a workspace are checked by content only.
    """
    report = ErasureReport(
        subject_type="organization", subject_id=organization_id, email_sha256=None
    )
    tables = _reflect(db)
    workspace_ids = list(workspace_ids)
    by_name = {t.name: t for t in tables}
    audit = by_name.get("audit_logs")
    skip: set[tuple[str, str]] = set()
    for table in tables:
        checks = [(c, [organization_id]) for c in _reference_columns(
            table, "organizations", ("organization_id",)
        )]
        if workspace_ids:
            checks += [(c, workspace_ids) for c in _reference_columns(
                table, "workspaces", ("workspace_id",)
            )]
        for column, values in checks:
            if table is audit and column.name in ("organization_id", "workspace_id"):
                continue  # the declared O-6 exception, checked below
            report.reference_columns_checked += 1
            rows = _count(db, table, column.in_(values))
            if rows:
                report.reference_findings.append(Finding(table.name, column.name, rows))
    if audit is not None:
        skip = {("audit_logs", "organization_id"), ("audit_logs", "workspace_id")}
        scope = audit.c.organization_id == organization_id
        retained = _count(db, audit, scope)
        if retained:
            report.retained_anonymised.append(Finding("audit_logs", "organization_id", retained))
        for name in _AUDIT_IDENTIFYING:
            column = audit.c[name]
            identifying = column.is_not(None)
            if isinstance(column.type, JSON) or column.type.__class__.__name__ == "JSON":
                identifying = cast(column, Text) != "null"
                identifying = column.is_not(None) & identifying
            rows = _count(db, audit, scope, identifying)
            if rows:
                report.reference_findings.append(Finding("audit_logs", name, rows))
        context = audit.c.context
        rows = _count(
            db, audit, scope, context.is_not(None), cast(context, Text).not_in(["{}", "null"])
        )
        if rows:
            report.reference_findings.append(Finding("audit_logs", "context", rows))
    report.content_columns_checked, report.content_findings = _content_findings(
        db, tables, [organization_id, *workspace_ids], skip=skip
    )
    return _stamp(report, tables)


__all__ = ["ErasureReport", "Finding", "verify_organization_erased", "verify_subject_erased"]
