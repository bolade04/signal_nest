from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


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


class GeoCoverageBase(BaseModel):
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


class GeoCoverageOut(GeoCoverageBase):
    id: str
    location_id: str | None

    class Config:
        from_attributes = True


class GeocodeRequest(BaseModel):
    query: str = Field(min_length=2)


class GeocodeResponse(BaseModel):
    latitude: float
    longitude: float
    city: str
    state_province: str
    country: str
    timezone: str
    confidence: float
