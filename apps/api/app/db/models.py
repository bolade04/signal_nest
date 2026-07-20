"""Import all ORM models so ``Base.metadata`` is fully populated.

Import this module before calling ``Base.metadata.create_all`` or running Alembic
autogenerate.
"""

from __future__ import annotations

from app.audit.models import AuditLog  # noqa: F401
from app.brands.models import Brand, BusinessProfile  # noqa: F401
from app.capabilities.models import WorkspaceCapabilityOverride  # noqa: F401
from app.campaign_context.models import (  # noqa: F401
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
from app.db.base import Base  # noqa: F401
from app.feedback.models import OpportunityFeedback  # noqa: F401
from app.intelligence.records import SignalIntelligenceRecord  # noqa: F401
from app.jobs.models import Job, JobEvent  # noqa: F401
from app.jobs.worker_models import WorkerRegistration  # noqa: F401
from app.locations.models import BusinessLocation, GeoCoverageRule  # noqa: F401
from app.opportunities.models import (  # noqa: F401
    Opportunity,
    OpportunityScore,
    ValidationEvidence,
)
from app.organizations.models import (  # noqa: F401
    Organization,
    OrganizationMember,
    User,
    Workspace,
)
from app.scouting_requests.models import ScoutRequest, ScoutSchedule  # noqa: F401
from app.signals.models import (  # noqa: F401
    NormalizedSignal,
    RawSignal,
    SignalCluster,
    SignalLocationEvidence,
)

__all__ = ["Base", "SignalIntelligenceRecord"]
