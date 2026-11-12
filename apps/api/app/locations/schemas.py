from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from app.db.clock import as_utc
from app.jurisdiction.resolver import country_code_of, subdivision_code_of
from app.jurisdiction.subdivisions import SUBDIVISION_JURISDICTIONS


class LocationBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str | None = None
    city: str | None = None
    state_province: str | None = None
    country: str | None = None
    postal_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    timezone: str | None = None
    currency: str | None = None
    languages: list[str] = Field(default_factory=list)
    local_phone: str | None = None
    local_website_page: str | None = None
    local_social_links: dict[str, str] = Field(default_factory=dict)
    google_business_profile: str | None = None
    local_competitors: list[str] = Field(default_factory=list)
    local_notes: str | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)


class LocationOut(LocationBase):
    id: str
    brand_id: str
    workspace_id: str
    is_active: bool

    class Config:
        from_attributes = True


#: The structured business address (W21 ADDR), in the order the request validates them: the
#: country before the state or province, which is read in it.
BUSINESS_ADDRESS_FIELDS: Final = (
    "business_address_street",
    "business_address_locality",
    "business_address_country",
    "business_address_region",
    "business_address_postcode",
)
_AS_ENTERED = "As entered by the customer; not verified against any external source."
#: The fixed 422 messages of the structured fields (they never repeat what was sent). Short
#: enough that the web client shows them whole beside the field's name (its 80-character bound
#: on one field message, "Value error, " included).
UNKNOWN_COUNTRY = "Enter a country by name or two-letter code, such as Nigeria or NG."
UNKNOWN_REGION = "Enter a state or province of that country, such as Texas or TX."


def unreadable_address_fields(
    country: str | None, region: str | None
) -> list[tuple[str, str]]:
    """The structured business-address fields whose values do not read, with their fixed
    messages: a country that is not a country, and a state or province that is not one of a
    US/CA/AU/NG country's subdivisions. Readability only: never compared with the location.
    The coverage PUT applies it to the values the rule will hold (review d1 C-4)."""
    found: list[tuple[str, str]] = []
    code = country_code_of(country) if country is not None else None
    if country is not None and code is None:
        found.append(("business_address_country", UNKNOWN_COUNTRY))
    if region is not None and code in SUBDIVISION_JURISDICTIONS:
        if subdivision_code_of(code, region) is None:
            found.append(("business_address_region", UNKNOWN_REGION))
    return found


class GeoCoverageFields(BaseModel):
    """The fields a coverage rule holds, as stored and answered."""

    coverage_type: str = "radius"
    business_address: str | None = None
    center_latitude: float | None = None
    center_longitude: float | None = None
    radius_miles: int | None = Field(default=None, ge=1, le=200)
    country: str | None = None
    state: str | None = None
    included_markets: list[str] = Field(default_factory=list)
    excluded_markets: list[str] = Field(default_factory=list)
    online_global: bool = False
    # The structured business address (W21 ADDR). What a jurisdiction-dependent action needs
    # of it is ``app.jurisdiction.business_address``; storing it needs only readable values.
    business_address_street: str | None = Field(
        default=None, max_length=400, description=f"Street. {_AS_ENTERED}"
    )
    business_address_locality: str | None = Field(
        default=None, max_length=160, description=f"Town or locality. {_AS_ENTERED}"
    )
    business_address_country: str | None = Field(
        default=None, max_length=120, description=f"Country. {_AS_ENTERED}"
    )
    business_address_region: str | None = Field(
        default=None,
        max_length=160,
        description=(
            "State or province (required for jurisdiction in the United States, Canada, "
            f"Australia and Nigeria). {_AS_ENTERED}"
        ),
    )
    business_address_postcode: str | None = Field(
        default=None, max_length=40, description=f"Postcode. {_AS_ENTERED}"
    )

    @field_validator("coverage_type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        allowed = {
            "city", "metro", "county", "state", "country",
            "multi_city", "multi_state", "radius", "online",
        }
        if v not in allowed:
            raise ValueError(f"coverage_type must be one of {sorted(allowed)}")
        return v


class GeoCoverageBase(GeoCoverageFields):
    """The coverage PUT's body. The structured business-address fields are written only when
    the request names them (an explicit ``null`` clears one); a value must be readable as what
    it is, and is never compared with the location here (the action does that). The readability
    validators belong to this request model only: ``GeoCoverageOut`` does not inherit them, so a
    stored value that no longer reads never turns a GET into a response-validation error."""

    @field_validator(*BUSINESS_ADDRESS_FIELDS, mode="before")
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @field_validator("business_address_country")
    @classmethod
    def _readable_country(cls, v: str | None) -> str | None:
        if any(f == "business_address_country" for f, _ in unreadable_address_fields(v, None)):
            raise ValueError(UNKNOWN_COUNTRY)
        return v

    @field_validator("business_address_region")
    @classmethod
    def _readable_region(cls, v: str | None, info: ValidationInfo) -> str | None:
        country = info.data.get("business_address_country")
        if any(f == "business_address_region" for f, _ in unreadable_address_fields(country, v)):
            raise ValueError(UNKNOWN_REGION)
        return v


class GeoCoverageOut(GeoCoverageFields):
    id: str
    location_id: str | None
    #: Computed from the stored town and the location's city; never stored.
    locality_matches_city: bool | None = Field(
        default=None,
        description=(
            "Whether the business town or locality and the location's city are the same name "
            "(ignoring case, accents and punctuation); null when either is empty."
        ),
    )
    locality_confirmed_at: datetime | None = Field(
        default=None,
        description=(
            "When a member of the workspace confirmed that the business town or locality lies "
            "within this location's area. Not verified by SignalNest."
        ),
    )
    locality_confirmation_applies: bool = Field(
        default=False,
        description=(
            "Whether that customer confirmation still applies: to the current business town, "
            "the location's current city and its current jurisdiction."
        ),
    )

    @field_validator("locality_confirmed_at")
    @classmethod
    def _as_utc(cls, value: datetime | None) -> datetime | None:
        # SQLite hands back a naive datetime; every value written here is UTC.
        return None if value is None else as_utc(value)

    class Config:
        from_attributes = True


class LocalityConfirmationIn(BaseModel):
    """Confirm (``true``) that the saved business town or locality lies within this location's
    area, or withdraw that confirmation (``false``). The confirmation is the customer's; it is
    not verified."""

    confirmed: bool


class GeocodeRequest(BaseModel):
    query: str = Field(min_length=2, max_length=200)


class GeocodeResponse(BaseModel):
    """One resolved place. A provider may not know every component (a region-level
    answer has no city), so the text components are nullable."""

    latitude: float
    longitude: float
    city: str | None
    state_province: str | None
    country: str | None
    timezone: str | None
    confidence: float
    #: ISO 3166-1 alpha-2 country code, e.g. "CA".
    country_code: str | None = None
    #: Normalised region, e.g. "CA-ON" (a known subdivision) or "NG:lagos".
    region_code: str | None = None
    #: The provider's one-line description of the place.
    label: str | None = None
    #: Which geocoder answered: "fixture" (development/test) or "aws_location".
    provider: str
