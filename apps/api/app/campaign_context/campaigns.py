"""The dedicated campaigns router (Project Phase 5D §2, lane 5D-W1).

Campaigns are served here, no longer by the generic context-entity factory
(:mod:`app.campaign_context.routes`):

* ``GET /workspaces/{workspace_id}/campaigns`` (list), ``POST`` (create),
  ``GET .../campaigns/{item_id}`` (read one, new), ``PATCH .../campaigns/{item_id}`` (update;
  coordinator ruling R-c keeps the P6-PLAT-9 PATCH in place of the plan's ``PUT``) and
  ``DELETE .../campaigns/{item_id}``. Paths, route names, roles (any member reads; ``OWNER`` /
  ``ADMIN`` / ``MARKETER`` write), the list's paging and cursor scope, and the request bodies are
  the factory's, unchanged.
* **Projected answers** (:class:`~app.campaign_context.schemas.CampaignOut`): a campaign no
  longer echoes ``organization_id`` / ``workspace_id`` (exit criterion 2; live change 8(c)).
  The other eight context collections keep the factory's full-row answer, deliberately (§2).
* **Scoped**: a campaign of another workspace, in this organization or another, answers the
  same ``404`` as one that does not exist.
* **Every write commits before it answers** (O9: ``db: Session = WriteDb``).

**Membership is the ``campaign_locations`` join table** (operator decision O-8; CF-3's
:class:`~app.campaign_context.models.CampaignLocation`; integration item I-CF3-2). Create and
update validate every requested location against the campaign's workspace, fail closed
(:func:`~app.campaign_context.location_refs.validate_location_ids`: ``404`` for an id of no
location of this workspace, ``422`` for newly adding a deactivated one), then **reconcile** the
campaign's memberships to exactly that set in the request's transaction: the missing pairs are
inserted and the absent ones deleted. The join table's composite keys refuse a cross-workspace
pair in the database as well. Answers read ``location_ids`` from the join table only.

The JSON ``campaigns.location_ids`` array is retained read-only for one release (§2, O-8): no
request writes it directly any more. Each reconcile overwrites it with the reconciled
membership, so it stays a faithful mirror of the join table, which keeps the CF-3 downgrade and
a revert of this change lossless. Nothing here reads it.

**Not done here (needs a data migration; reported):** a one-time reconcile of every existing
campaign's memberships to its validated JSON array when this change is deployed. A campaign
whose array was edited after the CF-3 back-fill ran and before this change, and that is not
written again, answers its back-filled membership until its next update.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth.dependencies import TenantContext, get_tenant_context, require_role
from app.brands.service import get_primary_brand
from app.briefs.models import RecommendationBrief
from app.campaign_context.location_refs import validate_location_ids
from app.campaign_context.models import Campaign, CampaignLocation
from app.campaign_context.schemas import CampaignIn, CampaignOut, CampaignPatch
from app.core.enums import Role
from app.core.errors import ConflictError, NotFoundError, ValidationDomainError
from app.core.pagination import MAX_PAGE_SIZE, PAGINATION_RESPONSES, paginate
from app.creative.models import CreativeDraft
from app.db.request_tx import WriteDb
from app.db.session import get_db

router = APIRouter()

EDITORS = require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)

_PATH = "/workspaces/{workspace_id}/campaigns"
_ITEM = "/workspaces/{workspace_id}/campaigns/{item_id}"
#: ``campaign-context`` is prepended by the including router (:mod:`app.campaign_context.routes`).
_TAGS = ["campaigns"]
#: The factory's cursor scope for this collection, kept so a cursor stays valid.
_CURSOR_SCOPE = "context:campaigns"
_NOT_FOUND = "Item not found in this workspace."
#: The refusal of a campaign deletion that would change an append-only record (D-N4 item 37,
#: coordinator ruling 37-R-1): fixed code and message, no customer text.
CAMPAIGN_IN_USE_CODE = "campaign_referenced_by_brief_or_draft"
CAMPAIGN_IN_USE = (
    "This campaign cannot be deleted because a recommendation brief or creative draft refers "
    "to it. Briefs and drafts are kept as they were written."
)


def _scoped(db: Session, workspace_id: str, item_id: str) -> Campaign:
    campaign = db.get(Campaign, item_id)
    if campaign is None or campaign.workspace_id != workspace_id:
        raise NotFoundError(_NOT_FOUND)
    return campaign


def memberships(
    db: Session, workspace_id: str, campaign_ids: Sequence[str]
) -> dict[str, list[str]]:
    """Each campaign's location ids from the join table, in location-id order."""
    found: dict[str, list[str]] = {campaign_id: [] for campaign_id in campaign_ids}
    if not found:
        return found
    rows = db.execute(
        select(CampaignLocation.campaign_id, CampaignLocation.location_id)
        .where(
            CampaignLocation.workspace_id == workspace_id,
            CampaignLocation.campaign_id.in_(list(found)),
        )
        .order_by(CampaignLocation.campaign_id, CampaignLocation.location_id)
    )
    for campaign_id, location_id in rows:
        found[campaign_id].append(location_id)
    return found


def _members(db: Session, campaign_id: str) -> set[str]:
    return set(
        db.scalars(
            select(CampaignLocation.location_id).where(CampaignLocation.campaign_id == campaign_id)
        )
    )


def reconcile_memberships(
    db: Session, campaign: Campaign, location_ids: Collection[str], *, current: set[str]
) -> list[str]:
    """Make ``campaign``'s memberships exactly ``location_ids`` (already validated): insert the
    missing pairs, delete the absent ones (I-CF3-2), and mirror the result into the retained
    JSON array. ``current`` is the membership read under the campaign's row lock. Returns the
    membership in location-id order."""
    desired = set(location_ids)
    stale = current - desired
    if stale:
        db.execute(
            delete(CampaignLocation).where(
                CampaignLocation.campaign_id == campaign.id,
                CampaignLocation.location_id.in_(sorted(stale)),
            )
        )
    for location_id in sorted(desired - current):
        db.add(
            CampaignLocation(
                organization_id=campaign.organization_id,
                workspace_id=campaign.workspace_id,
                campaign_id=campaign.id,
                location_id=location_id,
            )
        )
    members = sorted(desired)
    campaign.location_ids = members
    db.add(campaign)
    db.flush()
    return members


def _refuse_if_referenced(db: Session, campaign: Campaign) -> None:
    """Refuse (409, :data:`CAMPAIGN_IN_USE`) to delete a campaign that a recommendation brief or
    a creative draft names (D-N4 item 37, coordinator ruling 37-R-1).

    Both are append-only records, and their ``campaign_id`` is ``ON DELETE SET NULL``: deleting
    the campaign would change them, which the PostgreSQL backstop refuses (an unhandled error)
    and which SQLite would let through. The campaign row is first locked ``FOR UPDATE``, which
    the ``KEY SHARE`` check of a concurrent brief or draft insert naming it waits on, so no such
    row can appear between this check and the delete; a campaign deleted meanwhile is not
    found. This holds under READ COMMITTED, the application's isolation level (none is set in
    ``app/``): after the lock is granted, the check's own statement sees a brief or draft that
    committed while it waited (pinned by a PostgreSQL race test in
    ``test_append_only_backstop_postgres``)."""
    locked = db.scalar(
        select(Campaign.id)
        .where(Campaign.id == campaign.id, Campaign.workspace_id == campaign.workspace_id)
        .with_for_update()
    )
    if locked is None:
        raise NotFoundError(_NOT_FOUND)
    for model in (RecommendationBrief, CreativeDraft):
        named = db.scalar(select(model.id).where(model.campaign_id == campaign.id).limit(1))
        if named is not None:
            raise ConflictError(CAMPAIGN_IN_USE, code=CAMPAIGN_IN_USE_CODE)


def _lock(db: Session, campaign: Campaign) -> None:
    """Serialize membership reconciles of one campaign (PostgreSQL ``FOR NO KEY UPDATE``, which
    the join table's foreign-key ``KEY SHARE`` checks do not wait on); a campaign deleted
    meanwhile is not found."""
    locked = db.scalar(
        select(Campaign.id)
        .where(Campaign.id == campaign.id, Campaign.workspace_id == campaign.workspace_id)
        .with_for_update(key_share=True)
    )
    if locked is None:
        raise NotFoundError(_NOT_FOUND)


def _out(campaign: Campaign, location_ids: list[str]) -> CampaignOut:
    return CampaignOut(
        id=campaign.id,
        brand_id=campaign.brand_id,
        name=campaign.name,
        goal=campaign.goal,
        mode=campaign.mode,
        location_ids=location_ids,
        status=campaign.status,
        created_at=campaign.created_at,
        updated_at=campaign.updated_at,
    )


@router.get(
    _PATH,
    name="list_campaigns",
    tags=_TAGS,
    responses=PAGINATION_RESPONSES,
    response_model=list[CampaignOut],
)
def list_campaigns(
    workspace_id: str,
    response: Response,
    # No limit by default, as before P6-PLAT-12; a client that pages sends one.
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None, max_length=1024),
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> list[CampaignOut]:
    """The workspace's campaigns, oldest first, each with its location memberships."""
    page = paginate(
        db,
        select(Campaign).where(Campaign.workspace_id == workspace_id),
        order=(Campaign.created_at, Campaign.id),
        descending=False,
        scope=_CURSOR_SCOPE,
        workspace_id=workspace_id,
        filters={},
        cursor=cursor,
        limit=limit,
    )
    page.apply_headers(response)
    members = memberships(db, workspace_id, [c.id for c in page.rows])
    return [_out(c, members[c.id]) for c in page.rows]


@router.post(
    _PATH, status_code=201, name="create_campaigns", tags=_TAGS, response_model=CampaignOut
)
def create_campaign(
    workspace_id: str,
    body: CampaignIn,
    db: Session = WriteDb,
    ctx: TenantContext = Depends(EDITORS),
) -> CampaignOut:
    """Create a campaign. Every location in ``location_ids`` must be an active location of this
    workspace (else ``404``, or ``422`` for a deactivated one); the memberships are written in
    the same transaction."""
    brand = get_primary_brand(db, workspace_id)
    if not brand:
        raise ValidationDomainError("Complete onboarding before adding context.")
    values = body.model_dump()
    location_ids = values.pop("location_ids")
    validate_location_ids(db, workspace_id, location_ids)
    campaign = Campaign(
        organization_id=ctx.organization.id,
        workspace_id=workspace_id,
        brand_id=brand.id,
        location_ids=[],
        **values,
    )
    db.add(campaign)
    db.flush()
    members = reconcile_memberships(db, campaign, location_ids, current=set())
    return _out(campaign, members)


@router.get(_ITEM, name="get_campaign", tags=_TAGS, response_model=CampaignOut)
def get_campaign(
    workspace_id: str,
    item_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> CampaignOut:
    """One campaign of this workspace; a campaign of any other workspace is not found."""
    campaign = _scoped(db, workspace_id, item_id)
    return _out(campaign, memberships(db, workspace_id, [campaign.id])[campaign.id])


@router.patch(_ITEM, name="update_campaigns", tags=_TAGS, response_model=CampaignOut)
def update_campaign(
    workspace_id: str,
    item_id: str,
    body: CampaignPatch,
    db: Session = WriteDb,
    ctx: TenantContext = Depends(EDITORS),
) -> CampaignOut:
    """Change only the fields sent. A sent ``location_ids`` replaces the campaign's memberships
    whole: locations it adds are validated as on create (a deactivated location already a
    member may stay), and memberships it omits are removed, in the same transaction."""
    campaign = _scoped(db, workspace_id, item_id)
    changes = body.model_dump(exclude_unset=True)
    members: list[str] | None = None
    if "location_ids" in changes:
        location_ids = changes.pop("location_ids")
        _lock(db, campaign)
        current = _members(db, campaign.id)
        validate_location_ids(db, workspace_id, location_ids, already_stored=current)
        members = reconcile_memberships(db, campaign, location_ids, current=current)
    for key, value in changes.items():
        setattr(campaign, key, value)
    db.add(campaign)
    db.flush()
    if members is None:
        members = memberships(db, workspace_id, [campaign.id])[campaign.id]
    return _out(campaign, members)


@router.delete(_ITEM, status_code=204, name="delete_campaigns", tags=_TAGS)
def delete_campaign(
    workspace_id: str,
    item_id: str,
    db: Session = WriteDb,
    ctx: TenantContext = Depends(EDITORS),
) -> None:
    """Delete a campaign and its location memberships.

    Refused with 409 ``campaign_referenced_by_brief_or_draft`` while a recommendation brief or
    a creative draft names the campaign: those records are append-only, and deleting the
    campaign would clear their ``campaign_id``. Nothing is changed."""
    campaign = _scoped(db, workspace_id, item_id)
    _refuse_if_referenced(db, campaign)
    db.execute(delete(CampaignLocation).where(CampaignLocation.campaign_id == campaign.id))
    db.delete(campaign)
