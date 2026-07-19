import { describe, expect, it } from 'vitest';
import { queryKeys } from '@/api/queryKeys';

describe('queryKeys.opportunityFeedback', () => {
  it('embeds workspace, opportunity and intelligence record id for full scoping', () => {
    expect(queryKeys.opportunityFeedback('ws-1', 'opp-1', 'rec-1')).toEqual([
      'workspaces',
      'ws-1',
      'opportunities',
      'detail',
      'opp-1',
      'feedback',
      'rec-1',
    ]);
  });

  it('produces distinct keys per record so caches never collide across records', () => {
    const a = queryKeys.opportunityFeedback('ws-1', 'opp-1', 'rec-a');
    const b = queryKeys.opportunityFeedback('ws-1', 'opp-1', 'rec-b');
    expect(a).not.toEqual(b);
  });

  it('is nested under the same opportunity-detail prefix as the intelligence key', () => {
    const feedback = queryKeys.opportunityFeedback('ws-1', 'opp-1', 'rec-1');
    const intelligence = queryKeys.opportunityIntelligence('ws-1', 'opp-1');
    // Both share the [workspaces, ws, opportunities, detail, opp] prefix.
    expect(feedback.slice(0, 5)).toEqual(intelligence.slice(0, 5));
  });
});
