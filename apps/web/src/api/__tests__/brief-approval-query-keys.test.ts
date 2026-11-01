import { describe, expect, it } from 'vitest';
import { queryKeys } from '@/api/queryKeys';

// Project Phase 5 CF-1 (W21): the brief and approval keys are a frozen contract the 5A/5B
// build lanes consume and may not edit, so their exact shape is pinned here (review r1
// AUDIT-MINOR-2). They are the cache-isolation boundary for these surfaces: workspace,
// opportunity and brief are all embedded, and the brief keys nest under the opportunity's
// detail prefix so invalidating an opportunity reaches its briefs and their approvals.

describe('queryKeys: recommendation briefs and approvals (CF-1)', () => {
  it('has the exact tuple for each key', () => {
    expect(queryKeys.opportunityBriefs('ws-1', 'opp-1')).toEqual([
      'workspaces',
      'ws-1',
      'opportunities',
      'detail',
      'opp-1',
      'briefs',
    ]);
    expect(queryKeys.opportunityBrief('ws-1', 'opp-1', 'brief-1')).toEqual([
      'workspaces',
      'ws-1',
      'opportunities',
      'detail',
      'opp-1',
      'briefs',
      'brief-1',
    ]);
    expect(queryKeys.briefApprovals('ws-1', 'opp-1', 'brief-1')).toEqual([
      'workspaces',
      'ws-1',
      'opportunities',
      'detail',
      'opp-1',
      'briefs',
      'brief-1',
      'approvals',
    ]);
    expect(queryKeys.approvalQueue('ws-1')).toEqual(['workspaces', 'ws-1', 'approvals']);
    expect(queryKeys.briefCapability('ws-1')).toEqual(['workspaces', 'ws-1', 'brief-capability']);
  });

  it('keeps the workspace-effective brief capability apart from the raw-global summary', () => {
    expect(queryKeys.briefCapability('ws-1')).not.toEqual(queryKeys.briefCapability('ws-2'));
    expect(queryKeys.briefCapability('ws-1')).not.toEqual([...queryKeys.runtimeSummary]);
    expect(queryKeys.briefCapability('ws-1')).not.toEqual(queryKeys.feedbackCapability('ws-1'));
  });

  it('nests every brief key under its opportunity-detail key', () => {
    const opportunity = queryKeys.opportunity('ws-1', 'opp-1');
    for (const key of [
      queryKeys.opportunityBriefs('ws-1', 'opp-1'),
      queryKeys.opportunityBrief('ws-1', 'opp-1', 'brief-1'),
      queryKeys.briefApprovals('ws-1', 'opp-1', 'brief-1'),
    ]) {
      expect(key.slice(0, opportunity.length)).toEqual([...opportunity]);
    }
  });

  it('nests a brief under its list and its approvals under the brief', () => {
    const list = queryKeys.opportunityBriefs('ws-1', 'opp-1');
    const brief = queryKeys.opportunityBrief('ws-1', 'opp-1', 'brief-1');
    const approvals = queryKeys.briefApprovals('ws-1', 'opp-1', 'brief-1');
    expect(brief.slice(0, list.length)).toEqual([...list]);
    expect(approvals.slice(0, brief.length)).toEqual([...brief]);
  });

  it('differs per workspace, opportunity and brief', () => {
    const base = queryKeys.briefApprovals('ws-1', 'opp-1', 'brief-1');
    expect(queryKeys.briefApprovals('ws-2', 'opp-1', 'brief-1')).not.toEqual(base);
    expect(queryKeys.briefApprovals('ws-1', 'opp-2', 'brief-1')).not.toEqual(base);
    expect(queryKeys.briefApprovals('ws-1', 'opp-1', 'brief-2')).not.toEqual(base);
    expect(queryKeys.opportunityBrief('ws-1', 'opp-1', 'brief-1')).not.toEqual(
      queryKeys.opportunityBrief('ws-1', 'opp-2', 'brief-1'),
    );
    expect(queryKeys.approvalQueue('ws-1')).not.toEqual(queryKeys.approvalQueue('ws-2'));
  });
});
