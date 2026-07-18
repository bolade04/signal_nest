"""Canonical enums shared across domain modules.

These mirror the values exported to the frontend in ``packages/shared`` so the UI and
backend agree on status vocabularies without duplicating business logic.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MARKETER = "marketer"
    REVIEWER = "reviewer"
    VIEWER = "viewer"
    COMPLIANCE_REVIEWER = "compliance_reviewer"


class CoverageType(StrEnum):
    CITY = "city"
    METRO = "metro"
    COUNTY = "county"
    STATE = "state"
    COUNTRY = "country"
    MULTI_CITY = "multi_city"
    MULTI_STATE = "multi_state"
    RADIUS = "radius"
    ONLINE = "online"


class CampaignMode(StrEnum):
    SAME_FOR_ALL = "same_for_all"
    PER_LOCATION = "per_location"
    GROUPED = "grouped"
    RECOMMEND = "recommend"


class ScoutRequestStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class ScheduleInterval(StrEnum):
    """Bounded recurrence cadence for a scouting schedule (SB-B).

    Only two cadences exist and the minimum interval is 24h, so a schedule can
    never enqueue more than once per day. ``daily`` recurs every 24 hours and
    ``weekly`` every 7 days — pure fixed intervals with no clock-of-day, timezone
    or DST semantics.
    """

    DAILY = "daily"
    WEEKLY = "weekly"


class ScheduleState(StrEnum):
    """Derived lifecycle state of a scouting schedule surfaced to the customer (SB-C).

    Never persisted — always computed from the row plus the live job state so it can
    never drift from reality:

    * ``paused`` — disabled; drives no work.
    * ``active`` — enabled and a live tick chain is actually fanning out runs.
    * ``activation_required`` — enabled but not yet started. A schedule created while
      the feature was dark (or before the flag was turned on) is intentionally *not*
      auto-seeded; it stays here until an explicit resume/activate action starts it.
    """

    PAUSED = "paused"
    ACTIVE = "active"
    ACTIVATION_REQUIRED = "activation_required"


class SourceType(StrEnum):
    MANUAL = "manual"
    WEBSITE_SCAN = "website_scan"
    COMPETITOR_SCAN = "competitor_scan"
    RSS_NEWS = "rss_news"
    REDDIT = "reddit"
    REVIEWS = "reviews"
    GOOGLE_TRENDS = "google_trends"
    META_AD_LIBRARY = "meta_ad_library"
    TIKTOK_CREATIVE_CENTER = "tiktok_creative_center"


class SignalType(StrEnum):
    COMPLAINT = "complaint"
    PAIN_POINT = "pain_point"
    QUESTION = "question"
    BUYING_INTENT = "buying_intent"
    COMPETITOR_DISSATISFACTION = "competitor_dissatisfaction"
    FEATURE_REQUEST = "feature_request"
    PRODUCT_CONFUSION = "product_confusion"
    TREND_DISCUSSION = "trend_discussion"
    POSITIVE_TREND = "positive_trend"
    LOCAL_OPPORTUNITY = "local_opportunity"
    REVIEW_COMPLAINT = "review_complaint"
    SEARCH_DEMAND_CHANGE = "search_demand_change"
    SEO_GAP = "seo_gap"
    COMPETITOR_WEBSITE_GAP = "competitor_website_gap"
    OBJECTION = "objection"
    SEASONAL_OPPORTUNITY = "seasonal_opportunity"
    FAQ = "faq"
    NEWS_TRIGGER = "news_trigger"


class PainPointDNA(StrEnum):
    TRUST_ISSUE = "trust_issue"
    PRICE_FRUSTRATION = "price_frustration"
    SPEED_COMPLAINT = "speed_complaint"
    POOR_CUSTOMER_SERVICE = "poor_customer_service"
    PRODUCT_CONFUSION = "product_confusion"
    SAFETY_CONCERN = "safety_concern"
    CONVENIENCE_PROBLEM = "convenience_problem"
    QUALITY_CONCERN = "quality_concern"
    LACK_OF_TRANSPARENCY = "lack_of_transparency"
    HIDDEN_FEES = "hidden_fees"
    BAD_USER_EXPERIENCE = "bad_user_experience"
    SOCIAL_EMBARRASSMENT = "social_embarrassment"
    FEAR_OF_BEING_SCAMMED = "fear_of_being_scammed"
    NEED_FOR_STATUS = "need_for_status"
    NEED_FOR_PROOF = "need_for_proof"
    NEED_FOR_RELIABILITY = "need_for_reliability"


class OpportunityClassification(StrEnum):
    NOISE = "noise"
    DISCUSSION_ONLY = "discussion_only"
    WEAK = "weak"
    EARLY = "early"
    EMERGING = "emerging"
    VALIDATED = "validated"
    HIGH_PRIORITY = "high_priority"
    DEAD = "dead"


class DecisionAction(StrEnum):
    ACT_NOW = "act_now"
    ACT_SOON = "act_soon"
    MONITOR = "monitor"
    ARCHIVE = "archive"
    IGNORE = "ignore"
    STAY_SILENT = "stay_silent"
    BLOCK = "block"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKED = "blocked"


class ConfidenceLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class OpportunityStatus(StrEnum):
    NEW = "new"
    SAVED = "saved"
    MONITORING = "monitoring"
    IGNORED = "ignored"
    ACTIONED = "actioned"


class ClaimRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKED = "blocked"


class RejectionReason(StrEnum):
    """Structured, explainable reasons a signal is suppressed by the intelligence core.

    Ordered from cheapest/most-decisive to weakest. A suppressed signal always
    carries exactly one of these plus a human rationale, so a rejection is as
    auditable as an acceptance.
    """

    NOISE = "noise"
    OUT_OF_CONTEXT = "out_of_context"
    OUT_OF_MARKET = "out_of_market"
    DUPLICATE = "duplicate"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    POLICY_BLOCKED = "policy_blocked"
    WEAK_SIGNAL = "weak_signal"
