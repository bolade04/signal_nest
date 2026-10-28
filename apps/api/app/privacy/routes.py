"""Privacy routes: export and deletion for an account and an organization (``P6-PRIV-1``).

    GET  /account/export                         the signed-in account's data
    GET  /account/deletion-preview               what deleting the account would do
    POST /account/delete                         delete the account (password + "DELETE")
    GET  /organizations/{id}/export              exact OWNER: the export index (first pages)
    GET  /organizations/{id}/export/{table}      exact OWNER: one keyset page of one table
    POST /organizations/{id}/delete              exact OWNER: delete it (password + name)

Every deletion commits before it answers, then runs the independent verifier's reference
check (:mod:`app.privacy.verification`, ``content=False``) on the committed state and
returns it in the receipt: only rows that point at the deleted subject itself. A check that
finds any is logged as an error for an operator; the deletion itself is not undone. The
verifier's content scan reads every text column of every table, so it is not run here: an
operator runs it with ``python -m app.privacy.cli``. Exports and the preview write nothing.
Security events carry pseudonymous ids and counts only — never an email address, name or
password.

Data found only by the account's email address is the account's only when that address is
verified (:class:`~app.privacy.erasure.EmailScope`); the preview, the receipt and the
account export say which applies.
"""

from __future__ import annotations

from logging import ERROR

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.dependencies import (
    OrganizationContext,
    get_current_user,
    require_exact_organization_roles,
)
from app.core.enums import Role
from app.core.logging import get_logger, log_event
from app.db.session import get_db
from app.organizations.models import User
from app.privacy import erasure, export
from app.privacy.erasure import EmailScope, ErasureCounts, MembershipOutcome, email_scope
from app.privacy.schemas import (
    AccountDeletionOut,
    AccountDeletionPreviewOut,
    AccountDeletionRequest,
    AccountExportOut,
    DeletedOrganizationOut,
    EmailKeyedDataOut,
    ErasureCountsOut,
    ErasureFindingOut,
    ErasureVerificationOut,
    MembershipImpactOut,
    OrganizationDeletionOut,
    OrganizationDeletionRequest,
    OrganizationExportOut,
    OrganizationExportPageOut,
)
from app.privacy.verification import (
    ErasureReport,
    verify_organization_erased,
    verify_subject_erased,
)

router = APIRouter(tags=["privacy"])
logger = get_logger(__name__)

#: Organization export and deletion: exactly OWNER, by name (no rank floor).
ORGANIZATION_OWNER = require_exact_organization_roles(Role.OWNER)


def _counts(counts: ErasureCounts) -> ErasureCountsOut:
    return ErasureCountsOut(**counts.as_dict())


_PREVIEW_NOTE = {
    EmailScope.INCLUDED: (
        "Your email address is verified, so every invitation addressed to it and the audit "
        "records that name it will be erased too."
    ),
    EmailScope.EXCLUDED_UNVERIFIED: (
        "Your email address is not verified. The invitations you accepted, and your address in "
        "the records about them, will be erased; other invitations addressed to it and other "
        "records that name it will not, because nothing proves the address is yours. Verify "
        "it first to include them."
    ),
}
_RECEIPT_NOTE = {
    EmailScope.INCLUDED: (
        "Your email address was verified, so every invitation addressed to it and the audit "
        "records that named it were erased too."
    ),
    EmailScope.EXCLUDED_UNVERIFIED: (
        "Your email address was not verified. The invitations you accepted, and your address "
        "in the records about them, were erased; other invitations addressed to it and other "
        "records that name it were not, because nothing proved the address was yours."
    ),
}


def _check(report: ErasureReport) -> ErasureVerificationOut:
    """The receipt's view of a reference-only report (the subject's own rows only)."""
    if not report.references_cleared:
        log_event(
            logger,
            "privacy.erasure.verification_failed",
            level=ERROR,
            outcome="failure",
            subject_type=report.subject_type,
            subject_id=report.subject_id,
            reference_findings=len(report.reference_findings),
        )
    return ErasureVerificationOut(
        references_cleared=report.references_cleared,
        subject_type=report.subject_type,
        subject_id=report.subject_id,
        tables_checked=report.tables_checked,
        reference_columns_checked=report.reference_columns_checked,
        reference_findings=[ErasureFindingOut(**vars(f)) for f in report.reference_findings],
        retained_anonymised=[ErasureFindingOut(**vars(f)) for f in report.retained_anonymised],
        content_scan="operator",
        generated_at=report.generated_at,
    )


@router.get("/account/export", response_model=AccountExportOut)
def export_my_account(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> AccountExportOut:
    """Download everything stored about the signed-in account (portability)."""
    return AccountExportOut.model_validate(export.export_account(db, user=user))


@router.get("/account/deletion-preview", response_model=AccountDeletionPreviewOut)
def preview_account_deletion(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> AccountDeletionPreviewOut:
    """What deleting the signed-in account would do to each of its organizations."""
    impacts = erasure.plan_account_deletion(db, user_id=user.id)
    scope = email_scope(user)
    return AccountDeletionPreviewOut(
        can_delete=not any(i.outcome is MembershipOutcome.BLOCKED_SOLE_OWNER for i in impacts),
        organizations=[MembershipImpactOut.model_validate(i) for i in impacts],
        email_keyed_data=EmailKeyedDataOut(status=scope.value, note=_PREVIEW_NOTE[scope]),
    )


@router.post("/account/delete", response_model=AccountDeletionOut)
def delete_my_account(
    body: AccountDeletionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AccountDeletionOut:
    """Delete the signed-in account and everything that is only its own.

    A wrong current password is 422 ``current_password_incorrect`` and changes nothing.
    The only OWNER of an organization that has other members is refused with 409
    ``account_deletion_sole_owner``. Organizations in which the account is the only
    member are deleted with it. On success every token of the account stops working,
    because the account no longer exists. The invitations the account accepted are erased
    with it; other invitations addressed to its email and other audit values naming it are
    erased only when the address is verified (``email_keyed_data``).
    """
    result = erasure.delete_account(db, user=user, current_password=body.current_password)
    db.commit()
    log_event(
        logger,
        "privacy.account.deleted",
        outcome="success",
        user_id=result.user_id,
        email_scope=result.email_scope.value,
        organizations_deleted=[o.organization_id for o in result.deleted_organizations],
        rows_deleted=sum(result.counts.deleted.values()),
    )
    deleted = [
        DeletedOrganizationOut(
            organization_id=o.organization_id,
            workspace_ids=list(o.workspace_ids),
            verification=_check(
                verify_organization_erased(
                    db,
                    organization_id=o.organization_id,
                    workspace_ids=o.workspace_ids,
                    content=False,
                )
            ),
        )
        for o in result.deleted_organizations
    ]
    scope = result.email_scope
    return AccountDeletionOut(
        user_id=result.user_id,
        organizations=[MembershipImpactOut.model_validate(i) for i in result.organizations],
        deleted_organizations=deleted,
        counts=_counts(result.counts),
        verification=_check(verify_subject_erased(db, user_id=result.user_id, content=False)),
        email_keyed_data=EmailKeyedDataOut(status=scope.value, note=_RECEIPT_NOTE[scope]),
    )


@router.get("/organizations/{organization_id}/export", response_model=OrganizationExportOut)
def export_organization(
    db: Session = Depends(get_db),
    ctx: OrganizationContext = Depends(ORGANIZATION_OWNER),
) -> OrganizationExportOut:
    """The organization export index (portability): the exact row count of every exported
    table, its first page and the cursor of the next. Exactly OWNER."""
    return OrganizationExportOut.model_validate(
        export.export_organization(db, organization_id=ctx.organization.id)
    )


@router.get(
    "/organizations/{organization_id}/export/{table}", response_model=OrganizationExportPageOut
)
def export_organization_table(
    table: str,
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(
        default=export.ORGANIZATION_EXPORT_DEFAULT_PAGE,
        ge=1,
        le=export.ORGANIZATION_EXPORT_PAGE_CAP,
    ),
    db: Session = Depends(get_db),
    ctx: OrganizationContext = Depends(ORGANIZATION_OWNER),
) -> OrganizationExportPageOut:
    """One keyset page of one exported table, in ``(created_at, id)`` order after ``cursor``.
    Exactly OWNER. An unknown or not-exported table is 404; a cursor that is not valid for
    the table is 422 ``invalid_cursor``; ``limit`` is 1..5000."""
    return OrganizationExportPageOut.model_validate(
        export.export_organization_page(
            db, organization_id=ctx.organization.id, table=table, cursor=cursor, limit=limit
        )
    )


@router.post("/organizations/{organization_id}/delete", response_model=OrganizationDeletionOut)
def delete_organization(
    body: OrganizationDeletionRequest,
    db: Session = Depends(get_db),
    ctx: OrganizationContext = Depends(ORGANIZATION_OWNER),
) -> OrganizationDeletionOut:
    """Delete the organization, every workspace and everything in them. Exactly OWNER.

    The current password and the organization's exact name are required. Every member
    loses access; members' accounts are not deleted. Refused with 409
    ``organization_jobs_active`` while one of its jobs is claimed or running.
    """
    organization_id = ctx.organization.id
    result = erasure.delete_organization(
        db,
        user=ctx.user,
        organization_id=organization_id,
        current_password=body.current_password,
        confirm_organization_name=body.confirm_organization_name,
    )
    db.commit()
    log_event(
        logger,
        "privacy.organization.deleted",
        outcome="success",
        organization_id=organization_id,
        workspace_ids=list(result.workspace_ids),
        user_id=ctx.user.id,
        rows_deleted=sum(result.counts.deleted.values()),
    )
    return OrganizationDeletionOut(
        organization_id=organization_id,
        workspace_ids=list(result.workspace_ids),
        counts=_counts(result.counts),
        verification=_check(
            verify_organization_erased(
                db,
                organization_id=organization_id,
                workspace_ids=result.workspace_ids,
                content=False,
            )
        ),
    )
