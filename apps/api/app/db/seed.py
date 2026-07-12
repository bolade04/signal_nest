"""Deterministic, idempotent demo seed.

Run via ``python -m app.db.seed`` (or ``npm run seed``). Safe to run repeatedly:
without ``--reset`` an already-seeded database is left untouched. With ``--reset``
all demo/seed rows are cleared and rebuilt from scratch (local development only).

Everything created here is clearly flagged as simulated (``is_simulated=True`` on
signals/opportunities). Root entities use stable, deterministic IDs derived from a
fixed namespace so re-seeding produces identical primary keys.

Scenario: one specialty-coffee brand ("Brew & Bean") operating four fully
independent locations — Dallas TX, London UK, Lagos NG, Nairobi KE. Each location
has its own geo-coverage rule, local competitors, a per-location promotion and a
dedicated scout request. Because the fixture signals are market-tagged and each
scout request is scoped to a single market, results never leak across locations:
Dallas opportunities are built only from Dallas signals, and so on.
"""

from __future__ import annotations

import argparse
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

# Importing the pipeline module registers the ``run_scout_request`` job and gives
# us direct access to the synchronous runner used below.
from app.brands.models import Brand, BusinessProfile
from app.campaign_context.models import (
    AudienceProfile,
    BrandVoiceProfile,
    Campaign,
    ChannelPreference,
    ClaimsLibraryEntry,
    CompetitorProfile,
    OfferCalendarEntry,
    ProductProfile,
    SourcePreference,
)
from app.core.enums import (
    CampaignMode,
    Role,
    ScoutRequestStatus,
    SourceType,
)
from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.models import Base
from app.db.session import SessionLocal, engine
from app.geography.geocoder import geocode
from app.jobs.pipeline import _run
from app.locations.models import BusinessLocation, GeoCoverageRule
from app.opportunities.models import Opportunity
from app.organizations.models import (
    Organization,
    OrganizationMember,
    User,
    Workspace,
)
from app.scouting_requests.models import ScoutRequest

logger = get_logger("signalnest.seed")

# Fixed namespace so deterministic IDs are stable across runs/machines.
_NS = uuid.UUID("6f1c2a10-5b3e-4d21-9f7a-0c9d8e7b6a54")

DEMO_ORG_SLUG = "brew-and-bean"
DEMO_EMAIL = "demo@signalnest.dev"
DEMO_PASSWORD = "demo1234"  # noqa: S105 - documented demo credential


def sid(*parts: str) -> str:
    """Stable 32-char hex primary key derived from the fixed namespace."""
    return uuid.uuid5(_NS, ":".join(parts)).hex


# --- Per-location scenarios (independent markets) ----------------------------
# ``market`` must match the fixture keys in scouting_requests/fixtures.py so the
# connector returns that market's signals only.
CITIES: list[dict] = [
    {
        "key": "dallas",
        "geocode": "Dallas",
        "market": "Dallas, TX",
        "radius_miles": 25,
        "audience": {
            "label": "Dallas commuters who want fast dairy-free coffee",
            "keywords": ["oat milk", "latte", "mobile order", "DFW"],
        },
        "competitors": ["Rise & Grind DFW", "Trinity Roasters"],
        "offer": {
            "name": "DFW Oat Milk Launch",
            "percentage_discount": 15.0,
            "promo_code": "OATDFW",
            "cta": "Order ahead in the app",
        },
    },
    {
        "key": "london",
        "geocode": "London",
        "market": "London, UK",
        "radius_miles": 15,
        "audience": {
            "label": "London office workers seeking single-origin flat whites",
            "keywords": ["flat white", "single-origin", "dairy-free", "London"],
        },
        "competitors": ["Thames Coffee Works", "Soho Bean Bar"],
        "offer": {
            "name": "London Single-Origin Week",
            "percentage_discount": 10.0,
            "promo_code": "SOHO10",
            "cta": "Reserve a tasting flight",
        },
    },
    {
        "key": "lagos",
        "geocode": "Lagos",
        "market": "Lagos, NG",
        "radius_miles": 20,
        "audience": {
            "label": "Lagos young professionals exploring specialty coffee",
            "keywords": ["cold brew", "specialty coffee", "delivery", "Lagos"],
        },
        "competitors": ["Island Roasters", "Victoria Brew Co"],
        "offer": {
            "name": "Lagos Cold Brew Delivery",
            "percentage_discount": 20.0,
            "promo_code": "LAGOSCB",
            "cta": "Get it delivered",
        },
    },
    {
        "key": "nairobi",
        "geocode": "Nairobi",
        "market": "Nairobi, KE",
        "radius_miles": 20,
        "audience": {
            "label": "Nairobi remote workers wanting locally roasted single-origin",
            "keywords": ["single-origin", "local roast", "wifi cafe", "Nairobi"],
        },
        "competitors": ["Rift Valley Roasters", "Karen Coffee House"],
        "offer": {
            "name": "Nairobi Local Roast Loyalty",
            "percentage_discount": 12.0,
            "promo_code": "NBOLOCAL",
            "cta": "Join the loyalty club",
        },
    },
]


def _reset(db: Session) -> None:
    """Clear every table (demo-only database)."""
    for table in reversed(Base.metadata.sorted_tables):
        db.execute(table.delete())
    db.flush()


def _already_seeded(db: Session) -> bool:
    return (
        db.scalar(select(Organization).where(Organization.slug == DEMO_ORG_SLUG))
        is not None
    )


def _counts(db: Session) -> dict[str, int]:
    def n(model) -> int:
        return db.scalar(select(func.count()).select_from(model)) or 0

    return {
        "organizations": n(Organization),
        "users": n(User),
        "workspaces": n(Workspace),
        "brands": n(Brand),
        "locations": n(BusinessLocation),
        "audiences": n(AudienceProfile),
        "competitors": n(CompetitorProfile),
        "offers": n(OfferCalendarEntry),
        "scout_requests": n(ScoutRequest),
        "opportunities": n(Opportunity),
    }


def seed(reset: bool = False) -> dict:
    db = SessionLocal()
    try:
        if reset:
            logger.info("seed.reset")
            _reset(db)
        elif _already_seeded(db):
            counts = _counts(db)
            logger.info("seed.skip_already_seeded", extra={"extra_fields": counts})
            print("Demo data already present; nothing to do (use --reset to rebuild).")
            _print_summary(db, counts)
            return counts

        now = datetime.now(UTC)

        # --- Tenancy root --------------------------------------------------
        org = Organization(
            id=sid("org"), name="Brew & Bean Coffee Co.", slug=DEMO_ORG_SLUG
        )
        db.add(org)
        db.flush()

        user = User(
            id=sid("user"),
            email=DEMO_EMAIL,
            full_name="Demo Owner",
            hashed_password=hash_password(DEMO_PASSWORD),
            is_active=True,
        )
        db.add(user)
        db.add(
            OrganizationMember(
                id=sid("member"),
                organization_id=org.id,
                user_id=user.id,
                role=Role.OWNER.value,
            )
        )

        ws = Workspace(
            id=sid("ws"),
            organization_id=org.id,
            name="Brew & Bean",
            slug="brew-and-bean",
            onboarding_completed=True,
        )
        db.add(ws)
        db.flush()

        brand = Brand(
            id=sid("brand"),
            organization_id=org.id,
            workspace_id=ws.id,
            name="Brew & Bean",
            industry="specialty coffee",
            business_type="multi-location cafe",
        )
        db.add(brand)
        db.flush()

        db.add(
            BusinessProfile(
                id=sid("profile"),
                organization_id=org.id,
                workspace_id=ws.id,
                brand_id=brand.id,
                company_name="Brew & Bean",
                industry="specialty coffee",
                business_type="multi-location cafe",
                website="https://brewandbean.example",
                description="Specialty-coffee chain serving dairy-free and single-origin drinks.",
                core_problem_solved="Fast, high-quality dairy-free coffee without long waits.",
                unique_value_proposition=(
                    "Freshly roasted single-origin beans with reliable mobile pickup."
                ),
                target_audience="Urban professionals who value speed and quality",
                markets_served=[c["market"] for c in CITIES],
                customer_pain_points=[
                    "long mobile-order waits",
                    "oat milk frequently out of stock",
                    "loyalty app logs users out",
                ],
                pricing_model="per-drink + subscriptions",
                campaign_goals=["grow local awareness", "increase mobile orders"],
                weekly_ad_volume=8,
                onboarding_path="website_only",
            )
        )

        # --- Workspace-level campaign context (global settings) ------------
        db.add(
            ProductProfile(
                id=sid("product"),
                organization_id=org.id,
                workspace_id=ws.id,
                brand_id=brand.id,
                name="Oat Milk Latte & Single-Origin Coffee",
                description="Dairy-free lattes and single-origin pour-overs with mobile pickup.",
                audience="Urban professionals seeking fast dairy-free coffee",
                pain_points=["oat milk stockouts", "slow mobile pickup"],
                use_cases=["morning commute", "remote work sessions"],
                keywords=[
                    "coffee",
                    "oat milk",
                    "latte",
                    "single-origin",
                    "cold brew",
                    "mobile order",
                    "dairy-free",
                    "specialty coffee",
                ],
                relevance_weight=1.0,
            )
        )
        db.add(
            BrandVoiceProfile(
                id=sid("voice"),
                organization_id=org.id,
                workspace_id=ws.id,
                brand_id=brand.id,
                tone=["warm", "helpful"],
                personality=["approachable", "quality-obsessed"],
                do_use=["fresh", "roasted in-house"],
                avoid=["cheapest", "best in the world"],
            )
        )
        db.add(
            SourcePreference(
                id=sid("source"),
                organization_id=org.id,
                workspace_id=ws.id,
                brand_id=brand.id,
                source_type=SourceType.REDDIT.value,
                enabled=True,
            )
        )
        db.add(
            ChannelPreference(
                id=sid("channel"),
                organization_id=org.id,
                workspace_id=ws.id,
                brand_id=brand.id,
                channel="instagram",
                enabled=True,
                weekly_volume=5,
            )
        )
        # Claims library — demonstrates approved / restricted / blocked entries so
        # claim-risk warnings and unsupported-comparison guards have context.
        db.add_all(
            [
                ClaimsLibraryEntry(
                    id=sid("claim", "approved"),
                    organization_id=org.id,
                    workspace_id=ws.id,
                    brand_id=brand.id,
                    text="Roasted fresh in-house every week.",
                    kind="approved",
                    category="quality",
                    risk_level="low",
                ),
                ClaimsLibraryEntry(
                    id=sid("claim", "restricted"),
                    organization_id=org.id,
                    workspace_id=ws.id,
                    brand_id=brand.id,
                    text="Healthiest coffee for you.",
                    kind="restricted",
                    category="health",
                    risk_level="high",
                    notes="Health claims require substantiation; avoid in ad copy.",
                ),
                ClaimsLibraryEntry(
                    id=sid("claim", "blocked"),
                    organization_id=org.id,
                    workspace_id=ws.id,
                    brand_id=brand.id,
                    text="Better than every other coffee shop in town.",
                    kind="blocked",
                    category="comparative",
                    risk_level="blocked",
                    notes="Unsupported comparative claim — never use.",
                ),
            ]
        )

        campaign = Campaign(
            id=sid("campaign"),
            organization_id=org.id,
            workspace_id=ws.id,
            brand_id=brand.id,
            name="Local Growth 2026",
            goal="Grow local awareness and mobile orders per market",
            mode=CampaignMode.PER_LOCATION.value,
            status="active",
        )
        db.add(campaign)

        # --- Per-location scenarios ---------------------------------------
        db.flush()  # ensure FKs resolvable before creating locations/requests
        requests: list[tuple[dict, ScoutRequest]] = []
        for c in CITIES:
            geo = geocode(c["geocode"])
            assert geo is not None, f"fixture geocoder missing {c['geocode']}"
            loc = BusinessLocation(
                id=sid("loc", c["key"]),
                organization_id=org.id,
                workspace_id=ws.id,
                brand_id=brand.id,
                name=f"Brew & Bean — {geo.city}",
                city=geo.city,
                state_province=geo.state_province,
                country=geo.country,
                latitude=geo.latitude,
                longitude=geo.longitude,
                timezone=geo.timezone,
                local_competitors=c["competitors"],
                is_active=True,
            )
            db.add(loc)

            db.add(
                GeoCoverageRule(
                    id=sid("geo", c["key"]),
                    organization_id=org.id,
                    workspace_id=ws.id,
                    location_id=loc.id,
                    coverage_type="radius",
                    business_address=f"{geo.city}, {geo.country}",
                    center_latitude=geo.latitude,
                    center_longitude=geo.longitude,
                    radius_miles=c["radius_miles"],
                    country=geo.country,
                    state=geo.state_province,
                    included_markets=[c["market"]],
                )
            )

            # Per-location audience (specific, not "consumers").
            db.add(
                AudienceProfile(
                    id=sid("aud", c["key"]),
                    organization_id=org.id,
                    workspace_id=ws.id,
                    brand_id=brand.id,
                    label=c["audience"]["label"],
                    keywords=c["audience"]["keywords"],
                )
            )
            # Per-location competitor profiles.
            for i, comp in enumerate(c["competitors"]):
                db.add(
                    CompetitorProfile(
                        id=sid("comp", c["key"], str(i)),
                        organization_id=org.id,
                        workspace_id=ws.id,
                        brand_id=brand.id,
                        name=comp,
                        known_weaknesses=["loyalty app logs users out"],
                    )
                )
            # Per-location promotion, eligible only for that location.
            offer = c["offer"]
            db.add(
                OfferCalendarEntry(
                    id=sid("offer", c["key"]),
                    organization_id=org.id,
                    workspace_id=ws.id,
                    brand_id=brand.id,
                    name=offer["name"],
                    product_service="Oat Milk Latte",
                    percentage_discount=offer["percentage_discount"],
                    promo_code=offer["promo_code"],
                    start_date=now,
                    end_date=now + timedelta(days=30),
                    eligible_location_ids=[loc.id],
                    cta=offer["cta"],
                    is_active=True,
                )
            )

            req = ScoutRequest(
                id=sid("scout", c["key"]),
                organization_id=org.id,
                workspace_id=ws.id,
                brand_id=brand.id,
                location_id=loc.id,
                campaign_id=campaign.id,
                name=f"{geo.city} specialty-coffee scout",
                status=ScoutRequestStatus.QUEUED.value,
                source_types=[],
                # Broad enough that every coffee fixture for this market is analyzed
                # (the market tag still keeps results isolated per location).
                keywords=[
                    "coffee",
                    "cold brew",
                    "loyalty",
                    *c["audience"]["keywords"],
                ],
                resolved_market=c["market"],
                notes="Simulated demo scout request.",
            )
            db.add(req)
            requests.append((c, req))

        db.flush()

        # --- Run the pipeline per request (isolated by market) -------------
        for c, req in requests:
            stats = _run(db, req.id)
            logger.info(
                "seed.scout_completed",
                extra={"extra_fields": {"market": c["market"], **stats}},
            )

        db.commit()
        counts = _counts(db)
        logger.info("seed.done", extra={"extra_fields": counts})
        _print_summary(db, counts)
        return counts
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _print_summary(db: Session, counts: dict[str, int]) -> None:
    print("\n=== SignalNest demo seed ===")
    print(f"  Login:    {DEMO_EMAIL} / {DEMO_PASSWORD}")
    for key, value in counts.items():
        print(f"  {key:16} {value}")
    # Per-market opportunity breakdown proves isolation.
    print("  opportunities by market:")
    rows = db.execute(
        select(Opportunity.resolved_market, func.count(Opportunity.id))
        .group_by(Opportunity.resolved_market)
        .order_by(Opportunity.resolved_market)
    ).all()
    for market, n in rows:
        print(f"    - {market or 'unknown':14} {n}")
    print("All seeded signals/opportunities are simulated (is_simulated=True).\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed SignalNest demo data.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete all existing data and reseed from scratch (local dev only).",
    )
    args = parser.parse_args()

    # Fail fast with a clear message if migrations haven't been applied.
    from sqlalchemy import inspect

    tables = inspect(engine).get_table_names()
    if "users" not in tables or "alembic_version" not in tables:
        raise SystemExit(
            "Database schema is not initialized. Run migrations first:\n"
            "  npm run migrate        # or: npm run demo:setup (migrate + seed)"
        )

    seed(reset=args.reset)


if __name__ == "__main__":
    main()
