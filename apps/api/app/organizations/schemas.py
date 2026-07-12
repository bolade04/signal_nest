from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class OrganizationOut(BaseModel):
    id: str
    name: str
    slug: str

    class Config:
        from_attributes = True


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class WorkspaceOut(BaseModel):
    id: str
    organization_id: str
    name: str
    slug: str
    onboarding_completed: bool
    created_at: datetime

    class Config:
        from_attributes = True
