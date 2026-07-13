import { titleCase } from './utils';

// Human-readable labels + semantic intents for backend enum values. These mirror
// the API contract's string enums; the backend remains the source of truth for
// the values themselves — this module only handles presentation.

export type Intent = 'neutral' | 'info' | 'success' | 'warning' | 'danger' | 'muted';

export const classificationLabels: Record<string, string> = {
  noise: 'Noise',
  discussion_only: 'Discussion only',
  weak: 'Weak',
  early: 'Early',
  emerging: 'Emerging',
  validated: 'Validated',
  high_priority: 'High priority',
  dead: 'Dead',
};

export const classificationIntent: Record<string, Intent> = {
  noise: 'muted',
  discussion_only: 'muted',
  weak: 'neutral',
  early: 'info',
  emerging: 'info',
  validated: 'success',
  high_priority: 'success',
  dead: 'muted',
};

export const decisionLabels: Record<string, string> = {
  act_now: 'Act now',
  act_soon: 'Act soon',
  monitor: 'Monitor',
  archive: 'Archive',
  ignore: 'Ignore',
  stay_silent: 'Stay silent',
  block: 'Block',
};

export const decisionIntent: Record<string, Intent> = {
  act_now: 'success',
  act_soon: 'info',
  monitor: 'neutral',
  archive: 'muted',
  ignore: 'muted',
  stay_silent: 'muted',
  block: 'danger',
};

export const riskLabels: Record<string, string> = {
  low: 'Low risk',
  medium: 'Medium risk',
  high: 'High risk',
  blocked: 'Blocked',
};

export const riskIntent: Record<string, Intent> = {
  low: 'success',
  medium: 'warning',
  high: 'danger',
  blocked: 'danger',
};

export const confidenceIntent: Record<string, Intent> = {
  low: 'warning',
  medium: 'info',
  high: 'success',
};

export const statusLabels: Record<string, string> = {
  new: 'New',
  saved: 'Saved',
  monitoring: 'Monitoring',
  ignored: 'Ignored',
  actioned: 'Actioned',
};

export const statusIntent: Record<string, Intent> = {
  new: 'info',
  saved: 'neutral',
  monitoring: 'warning',
  ignored: 'muted',
  actioned: 'success',
};

export const scoutStatusLabels: Record<string, string> = {
  draft: 'Draft',
  queued: 'Queued',
  running: 'Running',
  paused: 'Paused',
  completed: 'Completed',
  failed: 'Failed',
};

export const scoutStatusIntent: Record<string, Intent> = {
  draft: 'muted',
  queued: 'info',
  running: 'info',
  paused: 'warning',
  completed: 'success',
  failed: 'danger',
};

export const jobStatusLabels: Record<string, string> = {
  pending: 'Pending',
  scheduled: 'Scheduled',
  claimed: 'Claimed',
  running: 'Running',
  retry_wait: 'Retry queued',
  succeeded: 'Succeeded',
  failed: 'Failed',
  dead_lettered: 'Dead-lettered',
  cancel_requested: 'Cancelling',
  cancelled: 'Cancelled',
};

export const jobStatusIntent: Record<string, Intent> = {
  pending: 'muted',
  scheduled: 'muted',
  claimed: 'info',
  running: 'info',
  retry_wait: 'warning',
  succeeded: 'success',
  failed: 'danger',
  dead_lettered: 'danger',
  cancel_requested: 'warning',
  cancelled: 'muted',
};

export const sourceTypeLabels: Record<string, string> = {
  manual: 'Manual input',
  website_scan: 'Website scan',
  competitor_scan: 'Competitor scan',
  rss_news: 'RSS / news',
  reddit: 'Reddit',
  reviews: 'Reviews',
  google_trends: 'Google Trends',
  meta_ad_library: 'Meta Ad Library',
  tiktok_creative_center: 'TikTok Creative Center',
};

export const coverageTypeLabels: Record<string, string> = {
  city: 'Single city',
  metro: 'Metro area',
  county: 'County',
  state: 'State / province',
  country: 'Whole country',
  multi_city: 'Multiple cities',
  multi_state: 'Multiple states',
  radius: 'Radius around address',
  online: 'Online / global',
};

export const campaignModeLabels: Record<string, string> = {
  same_for_all: 'Same for all locations',
  per_location: 'Independent per location',
  grouped: 'Grouped location sets',
  recommend: 'Let SignalNest recommend',
};

export const roleLabels: Record<string, string> = {
  owner: 'Owner',
  admin: 'Admin',
  marketer: 'Marketer',
  reviewer: 'Reviewer',
  viewer: 'Viewer',
  compliance_reviewer: 'Compliance Reviewer',
};

export function label(map: Record<string, string>, value: string | null | undefined): string {
  if (!value) return '—';
  return map[value] ?? titleCase(value);
}

// Descriptive tooltip copy for the various 0–100 scores.
export const scoreHelp = {
  opportunity_score:
    'How strong this opportunity is overall (0–100), combining relevance, trend, discussion, commercial value, engagement, source credibility and recency.',
  confidence_score:
    'How much the evidence can be trusted (0–100), based on evidence quantity, diversity, source reliability, clarity, consistency and recency.',
  relevance_score:
    'How closely this matches your business, product, audience and market (0–100). Below 40 is never recommended for action.',
  priority_score: 'Suggested ordering priority (0–100) derived from score, urgency and risk.',
} as const;

export function scoreBand(value: number): { label: string; intent: Intent } {
  if (value >= 75) return { label: 'Strong', intent: 'success' };
  if (value >= 50) return { label: 'Moderate', intent: 'info' };
  if (value >= 40) return { label: 'Fair', intent: 'warning' };
  return { label: 'Low', intent: 'muted' };
}
