import type { ClaimRisk, ContentReviewOut, CreativeChannel } from '@/api/types';
import type { Intent } from '@/lib/labels';

/**
 * Display copy for the Phase 5C draft UI (W21 lane P5C). Every closed vocabulary is mapped
 * exhaustively (a `Record` over the generated union), so a value added to the contract
 * fails the type-check here rather than rendering a raw code.
 */
export const CHANNEL_LABELS: Record<CreativeChannel, string> = {
  social_post: 'Social post',
  email: 'Email',
  search_ad: 'Search ad',
  sms: 'SMS',
  landing_page: 'Landing page',
};

/** The channels in the order the generate control offers them. */
export const CHANNELS: CreativeChannel[] = ['social_post', 'email', 'search_ad', 'sms', 'landing_page'];

export const RISK_LABELS: Record<ClaimRisk, { label: string; intent: Intent }> = {
  low: { label: 'Low risk', intent: 'success' },
  medium: { label: 'Medium risk', intent: 'warning' },
  high: { label: 'High risk', intent: 'danger' },
  blocked: { label: 'Blocked', intent: 'danger' },
};

/**
 * One draft version's review state, as the server decided it (never re-derived from the
 * text): `blocked` when the review is blocked or names a blocked claim; `missing_disclaimers`
 * when a required disclaimer is not carried; `passed` when the review passed; `pending`
 * while no review is attached. Only `passed` can be approved (the 5B service refuses the
 * others).
 */
export type ReviewState = 'pending' | 'blocked' | 'missing_disclaimers' | 'passed';

export function reviewState(review: ContentReviewOut | null | undefined): ReviewState {
  if (!review) return 'pending';
  if (review.is_blocked || review.blocked_claim_ids.length > 0) return 'blocked';
  if (!review.disclaimers_resolved) return 'missing_disclaimers';
  return review.passed ? 'passed' : 'missing_disclaimers';
}

export const REVIEW_STATE_LABELS: Record<ReviewState, { label: string; intent: Intent }> = {
  pending: { label: 'Review pending', intent: 'muted' },
  blocked: { label: 'Blocked', intent: 'danger' },
  missing_disclaimers: { label: 'Missing disclaimer', intent: 'warning' },
  passed: { label: 'Review passed', intent: 'success' },
};

/** Job states in which a generation is still going to finish. */
const IN_FLIGHT = new Set(['pending', 'scheduled', 'claimed', 'running', 'retry_wait', 'cancel_requested']);

export function isGenerationInFlight(status: string): boolean {
  return IN_FLIGHT.has(status);
}

/**
 * Customer copy for a generation job that ended without a draft, from the job's stable
 * error class (`JobOut.last_error_code`; the customer job view carries no summary). The
 * job's failure classes (`app.creative.service.execute_generation`): `policy` — generation
 * unavailable, not configured, or a daily limit reached; `validation` — the request, the brief,
 * its workspace, its recorded jurisdiction (S1-5D item 31), its approval or the version revised is
 * no longer what the request needs (one wording true for each; FU-5DW3-4, W21-REV-5D-INTEG r1
 * §6(b)); `authorization` — the requester may no longer
 * generate; `non_retryable` — the model failed or its text did not pass the safety screen.
 */
export function generationFailureMessage(code: string | null | undefined): string {
  switch (code) {
    case 'policy':
      return 'Draft generation is unavailable or its daily limit has been reached. Try again later.';
    case 'validation':
      return 'The draft could not be generated from this brief version as it stands now. Refresh the page to see why.';
    case 'authorization':
      return 'You no longer have permission to generate drafts in this workspace.';
    case 'cancelled':
      return 'The draft generation was cancelled.';
    default:
      return 'The draft could not be generated, or its text did not pass the safety screen. Try again.';
  }
}
