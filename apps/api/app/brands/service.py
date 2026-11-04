"""Onboarding + brand/business-profile services.

**Onboarding's claims input is a claims-library write** (Project Phase 5C, W21 coordinator
ruling R1 and its extension after review W21-REV-CF2-CONTROLS r1 C-4). The claims library is
a protected safety control (CF-2), and the ``generate_creative`` prompt trusts its
``approved`` entries, so onboarding's ``claims`` are held to exactly the ``/claims`` create
rules. Onboarding used to create library entries for any editor, of any kind, at any time,
unvalidated and unaudited. Now, checked in this order before anything is written (each
refusal writes nothing — the whole onboarding, not just its claims, so nothing is half
created and no claim is silently dropped):

1. **Stewards only.** A non-steward (in practice a ``MARKETER``, the one editor that is not a
   steward; :data:`app.claims.library.CLAIMS_STEWARD_ROLES` is ``OWNER``, ``ADMIN``,
   ``COMPLIANCE_REVIEWER``, exact membership) whose body carries ``claims`` is refused with
   **403** ``permission_denied``, the answer ``/claims`` gives a marketer.
2. **First onboarding only.** Once the workspace is onboarded, claims change through
   ``/claims`` only: a repeated onboarding that carries ``claims`` is refused with **409**
   ``onboarding_claims_closed``. (A repeated onboarding without claims behaves as before.)
3. **The ``/claims`` validation.** Each claim is validated as a ``/claims`` create body
   (:class:`~app.campaign_context.schemas.ClaimIn`: an empty ``text`` is refused); an
   invalid one is refused with **422** ``validation_error`` naming the claim's position and
   field, never its value.

A steward's first onboarding then creates the entries from the validated bodies, as
``/claims`` would, and writes one ``claims_library_entry.created`` audit record per entry in
the ``/claims`` shape (:func:`app.claims.library.audit_claim_created`). The same body without
``claims`` onboards exactly as before for every editor.

These are disclosed live behaviour changes (the 5C behaviour-change ledger).
"""

from __future__ import annotations

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import TenantContext
from app.brands.models import Brand, BusinessProfile
from app.brands.schemas import BusinessProfileBase, OnboardingRequest
from app.campaign_context.models import (
    AudienceProfile,
    ClaimsLibraryEntry,
    CompetitorProfile,
    ProductProfile,
    SourcePreference,
)
from app.campaign_context.schemas import ClaimIn
from app.claims.library import audit_claim_created, is_claims_steward
from app.core.errors import ConflictError, PermissionDeniedError, ValidationDomainError

#: The refusal a non-steward's onboarding with claims receives (no claim text echoed).
CLAIMS_INPUT_REFUSED = (
    "Only an owner, admin or compliance reviewer can add claims to the claims library. "
    "Complete onboarding without claims; a claims steward can add them afterwards."
)
#: The refusal of claims on an onboarding repeated after the workspace was onboarded.
CLAIMS_INPUT_CLOSED = (
    "This workspace is already onboarded, so claims can no longer be added here. "
    "Use the claims library instead."
)
CLAIMS_INPUT_CLOSED_CODE = "onboarding_claims_closed"


def _validated_claims(raw: list[dict]) -> list[ClaimIn]:
    """Each onboarding claim validated as a ``/claims`` create body, or a 422 that names
    the claim's position and field and the error type, never the submitted value."""
    claims: list[ClaimIn] = []
    for index, item in enumerate(raw):
        try:
            claims.append(ClaimIn.model_validate(item))
        except ValidationError as exc:
            problems = "; ".join(
                f"claims[{index}].{'.'.join(str(p) for p in err['loc']) or '<item>'}: "
                f"{err['type']}"
                for err in exc.errors(include_url=False, include_context=False, include_input=False)
            )
            raise ValidationDomainError(f"Invalid claim: {problems}") from None
    return claims


def _profile_columns(profile: BusinessProfileBase) -> dict:
    return profile.model_dump()


def run_onboarding(
    db: Session, ctx: TenantContext, body: OnboardingRequest
) -> tuple[Brand, BusinessProfile]:
    org_id = ctx.organization.id
    ws_id = ctx.workspace.id

    # R1: the claims input is a claims-library write under the /claims rules. Every check
    # runs before anything is written, so the onboarding is all or nothing.
    claim_bodies: list[ClaimIn] = []
    if body.claims:
        if not is_claims_steward(ctx.role):
            raise PermissionDeniedError(CLAIMS_INPUT_REFUSED)
        if ctx.workspace.onboarding_completed:
            raise ConflictError(CLAIMS_INPUT_CLOSED, code=CLAIMS_INPUT_CLOSED_CODE)
        claim_bodies = _validated_claims(body.claims)

    brand = Brand(
        organization_id=org_id,
        workspace_id=ws_id,
        name=body.brand_name,
        industry=body.profile.industry,
        business_type=body.profile.business_type,
    )
    db.add(brand)
    db.flush()

    profile = BusinessProfile(
        organization_id=org_id,
        workspace_id=ws_id,
        brand_id=brand.id,
        **_profile_columns(body.profile),
    )
    db.add(profile)
    db.flush()

    for p in body.products:
        db.add(
            ProductProfile(
                organization_id=org_id, workspace_id=ws_id, brand_id=brand.id,
                name=p.get("name", "Product"),
                description=p.get("description"),
                audience=p.get("audience"),
                pain_points=p.get("pain_points", []),
                use_cases=p.get("use_cases", []),
                competitors=p.get("competitors", []),
                keywords=p.get("keywords", []),
                exclusion_rules=p.get("exclusion_rules", []),
                claims_rules=p.get("claims_rules", {}),
            )
        )
    for c in body.competitors:
        db.add(
            CompetitorProfile(
                organization_id=org_id, workspace_id=ws_id, brand_id=brand.id,
                name=c.get("name", "Competitor"),
                website=c.get("website"),
                known_weaknesses=c.get("known_weaknesses", []),
                notes=c.get("notes"),
            )
        )
    for a in body.audiences:
        db.add(
            AudienceProfile(
                organization_id=org_id, workspace_id=ws_id, brand_id=brand.id,
                label=a.get("label", "Target audience"),
                description=a.get("description"),
                keywords=a.get("keywords", []),
                objections=a.get("objections", []),
            )
        )
    claims = [
        ClaimsLibraryEntry(
            organization_id=org_id, workspace_id=ws_id, brand_id=brand.id, **claim.model_dump()
        )
        for claim in claim_bodies
    ]
    if claims:
        db.add_all(claims)
        db.flush()
        for entry in claims:
            audit_claim_created(
                db,
                organization_id=org_id,
                workspace_id=ws_id,
                actor_user_id=ctx.user.id,
                entry_id=entry.id,
            )
    for st in body.preferred_source_types:
        db.add(
            SourcePreference(
                organization_id=org_id, workspace_id=ws_id, brand_id=brand.id,
                source_type=st, enabled=True,
            )
        )

    ctx.workspace.onboarding_completed = True
    db.add(ctx.workspace)
    db.flush()
    return brand, profile


def get_primary_brand(db: Session, workspace_id: str) -> Brand | None:
    return db.scalar(
        select(Brand).where(Brand.workspace_id == workspace_id).order_by(Brand.created_at)
    )
