"""Independent erasure verification — the E10 proof path (``P6-PRIV-1``).

Phase-6 demonstration E10 ("A data-deletion request has been serviced end to end by an
implemented mechanism") must be independently verifiable and may not rest on audit
records, because audit coverage (``P6-PRIV-5``) is P1. So this module does not read the
audit trail, does not read :mod:`app.privacy.inventory` and does not call
:mod:`app.privacy.erasure`. It shares one thing with the deletion code, by design: the
matcher (:mod:`app.privacy.matching`) that decides whether a text names the subject, so a
record the deletion should have cleared can never pass as clean here. It **reflects the live
database schema** and asks the database two questions about the subject:

1. **References.** Every column that can point at the subject — any column with a foreign
   key to ``users.id`` / ``organizations.id`` / ``workspaces.id``, any column *named*
   ``user_id``, ``*_user_id``, ``organization_id`` or ``workspace_id`` (foreign key or
   not), and the subject table's own ``id`` — is counted for the subject's ids.
2. **Content.** Every text or JSON column of every table is scanned, case-insensitively,
   for the subject's identifiers (the account id and email address; or the organization
   and workspace ids), so a copy left in free text or a JSON payload is found too. A JSON
   value is decoded and every string in it, object keys included, is judged as the deletion
   judges it. A ``WHOLE`` mention is a finding. An ``AMBIGUOUS`` one is a text where some
   registrable address could contain the identifier. That may be another person's address
   (``xcarol@…``, ``o'carol@…``) or the subject's own in a context such as ``email=`` or
   ``users/``. It is not a finding, and it is not passed over in silence either: it is
   reported in ``content_ambiguous``, and :attr:`ErasureReport.needs_review` asks an
   operator to look.

A report is ``verified`` only when the content was scanned and both come back empty. The
single declared exception is the organization's own audit records, which ``O-6`` keeps after
the organization is gone: for an organization subject, ``audit_logs`` rows whose
``organization_id`` is the subject are reported as *retained* — and each must have every
identifying column cleared, or it is a finding. Nothing here writes.

Cost: the content scan reads every text column of every table (359 at this writing) once
per identifier. That is acceptable for an operator tool and for the pilot's data volume; it
is not an online path. The deletion routes therefore run only the reference check
(``content=False``: one equality count per reference column, bounded by the schema, not by
the data's text) and leave the content scan to the operator's command
(:mod:`app.privacy.cli`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sqlalchemy import JSON, MetaData, String, Table, Text, cast, func, inspect, or_, select
from sqlalchemy.orm import Session

from app.privacy.matching import Mention, Subject, mention, mention_in_json

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
    #: False when only the reference check ran (``content=False``).
    content_scanned: bool = True
    content_columns_checked: int = 0
    reference_findings: list[Finding] = field(default_factory=list)
    content_findings: list[Finding] = field(default_factory=list)
    #: Rows holding an identifier only where a registrable address could contain it.
    content_ambiguous: list[Finding] = field(default_factory=list)
    retained_anonymised: list[Finding] = field(default_factory=list)
    generated_at: str = ""

    @property
    def references_cleared(self) -> bool:
        return not self.reference_findings

    @property
    def verified(self) -> bool:
        """Both questions answered and empty. A report without the content scan never is."""
        return self.content_scanned and self.references_cleared and not self.content_findings

    @property
    def needs_review(self) -> bool:
        """Ambiguous mentions remain: an operator decides whose they are."""
        return bool(self.content_ambiguous)

    def as_dict(self) -> dict:
        out = asdict(self)
        out["references_cleared"] = self.references_cleared
        out["verified"] = self.verified
        out["needs_review"] = self.needs_review
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


def _is_json(column) -> bool:
    return isinstance(column.type, JSON) or column.type.__class__.__name__ in {"JSONB", "JSON"}


def _is_text(column) -> bool:
    return isinstance(column.type, String) or _is_json(column)


def _mention(value: str, subject: Subject, *, as_json: bool) -> Mention:
    """A JSON value is decoded first, so escapes cannot hide a boundary or forge one."""
    if as_json:
        try:
            return mention_in_json(json.loads(value), subject)
        except ValueError:
            pass
    return mention(value, subject)


def _count(db: Session, table: Table, *where) -> int:
    return db.execute(select(func.count()).select_from(table).where(*where)).scalar_one()


def _content_findings(
    db: Session, tables: list[Table], subject: Subject, *, skip: set[tuple[str, str]]
) -> tuple[int, list[Finding], list[Finding]]:
    """Columns checked, ``WHOLE`` findings and ``AMBIGUOUS`` rows, per column."""
    needles = subject.sql_needles()
    checked, findings, ambiguous = 0, [], []
    for table in tables:
        for column in table.columns:
            if not _is_text(column) or (table.name, column.name) in skip:
                continue
            checked += 1
            # The database narrows to values that contain a needle; the shared matcher then
            # decides, for each, whether it names the subject.
            text = func.lower(cast(column, Text))
            values = db.execute(
                select(cast(column, Text))
                .select_from(table)
                .where(or_(*(text.contains(n, autoescape=True) for n in needles)))
            ).scalars()
            mentions = [_mention(v, subject, as_json=_is_json(column)) for v in values if v]
            whole = sum(1 for m in mentions if m is Mention.WHOLE)
            unsure = sum(1 for m in mentions if m is Mention.AMBIGUOUS)
            if whole:
                findings.append(Finding(table.name, column.name, whole))
            if unsure:
                ambiguous.append(Finding(table.name, column.name, unsure))
    return checked, findings, ambiguous


def _stamp(report: ErasureReport, tables: list[Table]) -> ErasureReport:
    report.tables_checked = len(tables)
    report.generated_at = datetime.now(UTC).isoformat()
    return report


def verify_subject_erased(
    db: Session, *, user_id: str | None = None, email: str | None = None, content: bool = True
) -> ErasureReport:
    """Prove no row references the account ``user_id`` and no value contains it or ``email``.

    Either may be given alone: an invitee who never registered has only an email.
    ``content=False`` runs the reference check only (the deletion route's in-request check).
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
    if content:
        subject = Subject(ids=(user_id,) if user_id else (), addresses=(email,) if email else ())
        (
            report.content_columns_checked,
            report.content_findings,
            report.content_ambiguous,
        ) = _content_findings(db, tables, subject, skip=set())
    else:
        report.content_scanned = False
    return _stamp(report, tables)


#: The columns of an organization's retained audit records that must all be cleared.
_AUDIT_IDENTIFYING = ("actor_user_id", "entity_id", "reason", "previous_state", "new_state")


def verify_organization_erased(
    db: Session,
    *,
    organization_id: str,
    workspace_ids: tuple[str, ...] | list[str] = (),
    content: bool = True,
) -> ErasureReport:
    """Prove nothing references the organization or its workspaces, beyond anonymised audit.

    ``workspace_ids`` are the organization's workspaces as they were before deletion (the
    deletion receipt lists them). Without them, tables that reach the organization only
    through a workspace are checked by content only. ``content=False`` runs the reference
    and retained-audit checks only (the deletion route's in-request check).
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
    if content:
        subject = Subject(ids=(organization_id, *workspace_ids))
        (
            report.content_columns_checked,
            report.content_findings,
            report.content_ambiguous,
        ) = _content_findings(db, tables, subject, skip=skip)
    else:
        report.content_scanned = False
    return _stamp(report, tables)


__all__ = ["ErasureReport", "Finding", "verify_organization_erased", "verify_subject_erased"]
