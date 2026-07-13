"""Aggregate router mounting every domain router under the API prefix.

Importing ``app.jobs.pipeline`` here guarantees the ``run_scout_request`` job handler
is registered in the queue before any request tries to enqueue it.
"""

from __future__ import annotations

from fastapi import APIRouter

import app.jobs.pipeline  # noqa: F401  (registers the run_scout_request job)
from app.audit.routes import router as audit_router
from app.auth.routes import router as auth_router
from app.brands.routes import router as brands_router
from app.campaign_context.routes import router as campaign_context_router
from app.locations.routes import router as locations_router
from app.opportunities.routes import router as opportunities_router
from app.organizations.routes import router as organizations_router
from app.scouting_requests.routes import router as scout_requests_router
from app.system.routes import router as system_router

api_router = APIRouter()

for _r in (
    system_router,
    auth_router,
    organizations_router,
    brands_router,
    campaign_context_router,
    locations_router,
    scout_requests_router,
    opportunities_router,
    audit_router,
):
    api_router.include_router(_r)
