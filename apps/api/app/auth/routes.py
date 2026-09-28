from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.audit.service import record_audit
from app.auth import account_tokens, mail_dispatch, service
from app.auth.dependencies import (
    AuthenticatedSession,
    get_authenticated_session,
    get_current_user,
)
from app.auth.models import AuthSession
from app.auth.schemas import (
    EmailVerificationConfirmRequest,
    EmailVerificationRequest,
    InvitationPreviewOut,
    InvitationRegisterRequest,
    InvitationTokenRequest,
    LoginRequest,
    MembershipOut,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    RegisterRequest,
    SessionOut,
    UserOut,
)
from app.core.logging import get_logger, log_event
from app.db.clock import database_now
from app.db.session import get_db
from app.organizations import invitations as invitation_service
from app.organizations.models import Organization, OrganizationMember, User

router = APIRouter(prefix="/auth", tags=["auth"])
logger = get_logger(__name__)


def _session(db: Session, user: User, auth_session: AuthSession) -> SessionOut:
    """The ``SessionOut`` for ``user``, with a token minted inside ``auth_session``."""
    rows = db.execute(
        select(OrganizationMember, Organization)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .where(OrganizationMember.user_id == user.id)
    ).all()
    memberships = [
        MembershipOut(organization_id=o.id, organization_name=o.name, role=m.role) for m, o in rows
    ]
    return SessionOut(
        access_token=service.issue_token(user, auth_session),
        user=UserOut(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            is_operator=user.is_operator,
            email_verified=user.email_verified_at is not None,
        ),
        memberships=memberships,
    )


def _fresh_session(db: Session, user: User) -> SessionOut:
    """A fresh-credential boundary (P6-AUTH-4): open a new session, COMMIT it, then mint.

    Only login, register and invitation register reach here. The commit happens before
    the token exists, so a returned token always belongs to a committed session, and a
    failed commit returns no token.
    """
    auth_session = service.create_session(db, user)
    db.commit()
    return _session(db, user, auth_session)


@router.post("/register", response_model=SessionOut, status_code=201)
def register(body: RegisterRequest, db: Session = Depends(get_db)) -> SessionOut:
    user = service.register(
        db,
        email=body.email,
        full_name=body.full_name,
        password=body.password,
        org_name=body.organization_name,
    )
    membership = db.scalar(select(OrganizationMember).where(OrganizationMember.user_id == user.id))
    record_audit(
        db,
        organization_id=membership.organization_id,
        actor_user_id=user.id,
        action="auth.register",
        entity_type="user",
        entity_id=user.id,
    )
    return _fresh_session(db, user)


@router.post("/login", response_model=SessionOut)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> SessionOut:
    user = service.authenticate(db, email=body.email, password=body.password)
    return _fresh_session(db, user)


@router.get("/me", response_model=SessionOut)
def me(
    db: Session = Depends(get_db),
    authenticated: AuthenticatedSession = Depends(get_authenticated_session),
) -> SessionOut:
    """Re-read the signed-in session. RE-ISSUE ONLY (P6-AUTH-4): the token returned is
    minted inside the presented session and capped at its fixed absolute expiry; no
    session is created or extended, however often this is called."""
    return _session(db, authenticated.user, authenticated.session)


@router.post("/logout", status_code=204)
def logout(
    db: Session = Depends(get_db),
    authenticated: AuthenticatedSession = Depends(get_authenticated_session),
) -> Response:
    """Sign out: revoke the session this request's token belongs to (P6-AUTH-4).

    Every token issued or re-issued inside that session stops authenticating; the
    account's other sessions stay signed in. ``auth_epoch`` is not moved. The revocation
    is stamped on the database clock and committed before the empty 204.
    """
    db.execute(
        update(AuthSession)
        .where(AuthSession.id == authenticated.session.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=database_now(db))
    )
    db.commit()
    log_event(logger, "security.session.revoked", outcome="success", user_id=authenticated.user.id)
    return Response(status_code=204)


@router.post("/logout-all", status_code=204)
def logout_all(
    db: Session = Depends(get_db),
    authenticated: AuthenticatedSession = Depends(get_authenticated_session),
) -> Response:
    """Sign out everywhere: revoke every session of the signed-in account (P6-AUTH-4).

    The caller's own session is included; other accounts are untouched and
    ``auth_epoch`` is not moved. A sign-in that commits after this creates a new,
    legitimate session.
    """
    db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == authenticated.user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=database_now(db))
    )
    db.commit()
    log_event(
        logger, "security.session.revoked_all", outcome="success", user_id=authenticated.user.id
    )
    return Response(status_code=204)


# --- Invitations -------------------------------------------------------------------------
# The token travels only in the request body, never in a path or query string. The
# organization, role and email are the invitation's own; no request here accepts them.


@router.post("/invitations/preview", response_model=InvitationPreviewOut)
def preview_invitation(
    body: InvitationTokenRequest, db: Session = Depends(get_db)
) -> InvitationPreviewOut:
    # Read-only: previewing does not spend the token.
    preview = invitation_service.preview_invitation(db, token=body.token)
    return InvitationPreviewOut.model_validate(preview)


@router.post("/invitations/register", response_model=SessionOut, status_code=201)
def register_with_invitation(
    body: InvitationRegisterRequest, db: Session = Depends(get_db)
) -> SessionOut:
    user = service.register_invited(
        db, token=body.token, full_name=body.full_name, password=body.password
    )
    # The new account's first session is committed together with the claimed invitation.
    return _fresh_session(db, user)


@router.post("/invitations/accept", response_model=SessionOut)
def accept_invitation(
    body: InvitationTokenRequest,
    db: Session = Depends(get_db),
    authenticated: AuthenticatedSession = Depends(get_authenticated_session),
) -> SessionOut:
    """Accept an invitation as the signed-in user. RE-ISSUE ONLY (P6-AUTH-4): the token
    returned is minted inside the caller's own session and capped at its fixed absolute
    expiry; a revoked or expired session is refused (401) before any membership effect."""
    invitation_service.accept_invitation_existing_user(
        db, user=authenticated.user, token=body.token
    )
    db.commit()
    # Every membership the user now holds, the accepted one included.
    return _session(db, authenticated.user, authenticated.session)


# --- Password reset and email verification ------------------------------------------------
# The token travels only in the request body, never in a path or query string. Each route
# commits before it schedules mail and before it answers, so a message is only sent for a
# committed token and a 204 only reports a committed change. Security events carry no
# email address, token, digest or link.


@router.post("/password-reset/request", status_code=204)
def request_password_reset(
    body: PasswordResetRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> Response:
    """Email a password-reset link if the address belongs to an active account.

    Always 204 with an empty body, whatever the address: unknown, inactive, cooling down
    and capped accounts are indistinguishable from one that was sent mail.
    """
    issued = account_tokens.request_password_reset(db, email=body.email)
    db.commit()
    if issued is None:
        log_event(logger, "security.password_reset.requested", outcome="suppressed")
    else:
        background_tasks.add_task(mail_dispatch.send_password_reset_email, issued)
        log_event(
            logger, "security.password_reset.requested", outcome="issued", user_id=issued.user_id
        )
    return Response(status_code=204)


@router.post("/password-reset/confirm", status_code=204)
def confirm_password_reset(
    body: PasswordResetConfirmRequest, db: Session = Depends(get_db)
) -> Response:
    """Set a new password with a reset token.

    Spends the token and invalidates every session already issued for the account; no
    new session is created. A token that is unknown, expired, used, revoked or superseded,
    or whose account is inactive, is a 404.
    """
    account_tokens.confirm_password_reset(db, token=body.token, new_password=body.new_password)
    db.commit()
    log_event(logger, "security.password_reset.completed", outcome="success")
    return Response(status_code=204)


@router.post("/email-verification/request", status_code=204)
def request_email_verification(
    body: EmailVerificationRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Email a verification link to the signed-in account's stored address.

    A 409 if the address is already verified; otherwise 204, whether or not a cooldown
    or daily cap held the message back.
    """
    # The body is an empty object: the address is the account's own, never the caller's.
    issued = account_tokens.request_email_verification(db, user=user)
    db.commit()
    if issued is None:
        log_event(logger, "security.email_verification.requested", outcome="suppressed")
    else:
        background_tasks.add_task(mail_dispatch.send_email_verification_email, issued)
        log_event(
            logger,
            "security.email_verification.requested",
            outcome="issued",
            user_id=issued.user_id,
        )
    return Response(status_code=204)


@router.post("/email-verification/confirm", status_code=204)
def confirm_email_verification(
    body: EmailVerificationConfirmRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Mark the signed-in account's stored address verified with a verification token.

    The token must be this account's (403 otherwise, and nothing is spent); an unknown,
    expired, used or superseded token, or one for an address the account no longer has,
    is a 404.
    """
    account_tokens.confirm_email_verification(db, user=user, token=body.token)
    db.commit()
    log_event(logger, "security.email_verification.completed", outcome="success", user_id=user.id)
    return Response(status_code=204)
