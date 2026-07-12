from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ScoutRequestCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    location_id: str | None = None
    campaign_id: str | None = None
    source_types: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    product_profile_id: str | None = None
    resolved_market: str | None = None
    notes: str | None = None


class ScoutRequestUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    source_types: list[str] | None = None
    keywords: list[str] | None = None
    resolved_market: str | None = None
    notes: str | None = None


class ScoutRequestOut(BaseModel):
    id: str
    organization_id: str
    workspace_id: str
    brand_id: str
    location_id: str | None
    campaign_id: str | None
    name: str
    status: str
    source_types: list[str]
    keywords: list[str]
    product_profile_id: str | None
    resolved_market: str | None
    notes: str | None
    last_run_at: datetime | None
    stats: dict
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ScoutRunResult(BaseModel):
    scout_request_id: str
    status: str
    stats: dict
