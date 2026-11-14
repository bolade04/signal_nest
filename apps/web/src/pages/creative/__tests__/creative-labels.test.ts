import { describe, expect, it } from 'vitest';
import type { ContentReviewOut, CreativeChannel } from '@/api/types';
import {
  CHANNEL_LABELS,
  CHANNELS,
  generationFailureMessage,
  isGenerationInFlight,
  reviewState,
} from '@/pages/creative/creativeLabels';

const PASSED: ContentReviewOut = {
  id: 'r',
  draft_id: 'd',
  draft_version: 1,
  risk_level: 'low',
  is_blocked: false,
  findings: [],
  warnings: [],
  blocked_claim_ids: [],
  required_disclaimers: [],
  disclaimers_resolved: true,
  passed: true,
  created_at: '2026-11-01T10:00:00Z',
};

describe('creative labels', () => {
  it('offers every contract channel exactly once', () => {
    const contract: CreativeChannel[] = ['social_post', 'email', 'search_ad', 'sms', 'landing_page'];
    expect([...CHANNELS].sort()).toEqual([...contract].sort());
    expect(Object.keys(CHANNEL_LABELS).sort()).toEqual([...contract].sort());
  });

  it('reads the review state from the server decision only', () => {
    expect(reviewState(null)).toBe('pending');
    expect(reviewState(undefined)).toBe('pending');
    expect(reviewState(PASSED)).toBe('passed');
    expect(reviewState({ ...PASSED, is_blocked: true, risk_level: 'blocked', passed: false })).toBe('blocked');
    // A named blocked claim is blocked even if a stale flag said otherwise.
    expect(reviewState({ ...PASSED, blocked_claim_ids: ['c'] })).toBe('blocked');
    expect(reviewState({ ...PASSED, disclaimers_resolved: false, passed: false })).toBe('missing_disclaimers');
    expect(reviewState({ ...PASSED, passed: false })).toBe('missing_disclaimers');
    // An unresolved disclaimer is never shown as passed, even on an inconsistent answer
    // (the database CHECK makes passed imply resolved; the UI does not rely on it).
    expect(reviewState({ ...PASSED, disclaimers_resolved: false })).toBe('missing_disclaimers');
    // High risk that is not blocked is still approvable (a reviewer decides).
    expect(reviewState({ ...PASSED, risk_level: 'high' })).toBe('passed');
  });

  it('knows which job states are still in flight', () => {
    for (const s of ['pending', 'scheduled', 'claimed', 'running', 'retry_wait', 'cancel_requested']) {
      expect(isGenerationInFlight(s)).toBe(true);
    }
    for (const s of ['succeeded', 'failed', 'dead_lettered', 'cancelled']) {
      expect(isGenerationInFlight(s)).toBe(false);
    }
  });

  it('maps every job failure class to customer copy', () => {
    expect(generationFailureMessage('policy')).toMatch(/unavailable or its daily limit/);
    expect(generationFailureMessage('validation')).toMatch(/as it stands now\. Refresh the page to see why/);
    expect(generationFailureMessage('authorization')).toMatch(/no longer have permission/);
    expect(generationFailureMessage('cancelled')).toMatch(/cancelled/);
    expect(generationFailureMessage('non_retryable')).toMatch(/safety screen/);
    expect(generationFailureMessage(null)).toMatch(/could not be generated/);
  });
});
