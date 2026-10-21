"""Privacy routes: export and deletion for an account and an organization (``P6-PRIV-1``).

    GET  /account/export                         the signed-in account's data
    GET  /account/deletion-preview               what deleting the account would do
    POST /account/delete                         delete the account (password + "DELETE")
    GET  /organizations/{id}/export              exact OWNER: the organization's data
    POST /organizations/{id}/delete              exact OWNER: delete it (password + name)

Every deletion commits before it answers, then runs the independent verification
(:mod:`app.privacy.verification`) on the committed state and returns its report in the
receipt. A report that is not ``verified`` is logged as an error for an operator; the
deletion itself is not undone. Exports and the preview write nothing. Security events
carry pseudonymous ids and counts only — never an email address, name or password.
"""

from __future__ import annotations

from logging import ERROR

from fastapi import APIRouter, Depends
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
from app.privacy.erasure import ErasureCounts, MembershipOutcome
from app.privacy.schemas import (
    AccountDeletionOut,
    AccountDeletionPreviewOut,
    AccountDeletionRequest,
    AccountExportOut,
    DeletedOrganizationOut,
    ErasureCountsOut,
    ErasureVerificationOut,
    MembershipImpactOut,
    OrganizationDeletionOut,
    OrganizationDeletionRequest,
    OrganizationExportOut,
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


def _report(report: ErasureReport) -> ErasureVerificationOut:
    out = ErasureVerificationOut.model_validate(report.as_dict())
    if not out.verified:
        log_event(
            logger,
            "privacy.erasure.verification_failed",
            level=ERROR,
            outcome="failure",
            subject_type=report.subject_type,
            subject_id=report.subject_id,
            reference_findings=len(report.reference_findings),
            content_findings=len(report.content_findings),
        )
    return out


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
    return AccountDeletionPreviewOut(
        can_delete=not any(i.outcome is MembershipOutcome.BLOCKED_SOLE_OWNER for i in impacts),
        organizations=[MembershipImpactOut.model_validate(i) for i in impacts],
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
    because the account no longer exists.
    """
    email = user.email
    result = erasure.delete_account(db, user=user, current_password=body.current_password)
    db.commit()
    log_event(
        logger,
        "privacy.account.deleted",
        outcome="success",
        user_id=result.user_id,
        organizations_deleted=[o.organization_id for o in result.deleted_organizations],
        rows_deleted=sum(result.counts.deleted.values()),
    )
    deleted = [
        DeletedOrganizationOut(
            organization_id=o.organization_id,
            workspace_ids=list(o.workspace_ids),
            verification=_report(
                verify_organization_erased(
                    db, organization_id=o.organization_id, workspace_ids=o.workspace_ids
                )
            ),
        )
        for o in result.deleted_organizations
    ]
    return AccountDeletionOut(
        user_id=result.user_id,
        organizations=[MembershipImpactOut.model_validate(i) for i in result.organizations],
        deleted_organizations=deleted,
        counts=_counts(result.counts),
        verification=_report(verify_subject_erased(db, user_id=result.user_id, email=email)),
    )


@router.get("/organizations/{organization_id}/export", response_model=OrganizationExportOut)
def export_organization(
    db: Session = Depends(get_db),
    ctx: OrganizationContext = Depends(ORGANIZATION_OWNER),
) -> OrganizationExportOut:
    """Download every exported table of the organization (portability). Exactly OWNER."""
    return OrganizationExportOut.model_validate(
        export.export_organization(db, organization_id=ctx.organization.id)
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
        verification=_report(
            verify_organization_erased(
                db, organization_id=organization_id, workspace_ids=result.workspace_ids
            )
        ),
    )
