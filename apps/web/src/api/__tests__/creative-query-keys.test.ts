import { describe, expect, it } from 'vitest';
import { queryKeys } from '@/api/queryKeys';

// Project Phase 5 CF-2 (W21): the creative-generation keys are a frozen contract the 5C
// build lanes consume and may not edit, so their exact shape is pinned here. They are the
// cache-isolation boundary for these surfaces: the workspace is always embedded, a brief's
// drafts carry the brief, and the workspace-effective generation availability never shares
// the raw-global runtimeSummary entry or another capability's entry.

describe('queryKeys: creative drafts and generation availability (CF-2)', () => {
  it('has the exact tuple for each key', () => {
    expect(queryKeys.briefDrafts('ws-1', 'brief-1')).toEqual([
      'workspaces',
      'ws-1',
      'briefs',
      'brief-1',
      'drafts',
    ]);
    expect(queryKeys.creativeDraft('ws-1', 'draft-1')).toEqual([
      'workspaces',
      'ws-1',
      'drafts',
      'draft-1',
    ]);
    expect(queryKeys.creativeCapability('ws-1')).toEqual([
      'workspaces',
      'ws-1',
      'creative-capability',
    ]);
  });

  it('never shares an entry across workspaces or briefs', () => {
    expect(queryKeys.briefDrafts('ws-1', 'brief-1')).not.toEqual(
      queryKeys.briefDrafts('ws-2', 'brief-1'),
    );
    expect(queryKeys.briefDrafts('ws-1', 'brief-1')).not.toEqual(
      queryKeys.briefDrafts('ws-1', 'brief-2'),
    );
    expect(queryKeys.creativeDraft('ws-1', 'draft-1')).not.toEqual(
      queryKeys.creativeDraft('ws-2', 'draft-1'),
    );
  });

  it('keeps the workspace-effective generation capability apart from every other entry', () => {
    const key = queryKeys.creativeCapability('ws-1');
    expect(key).not.toEqual(queryKeys.creativeCapability('ws-2'));
    expect(key).not.toEqual([...queryKeys.runtimeSummary]);
    expect(key).not.toEqual(queryKeys.briefCapability('ws-1'));
    expect(key).not.toEqual(queryKeys.feedbackCapability('ws-1'));
  });

  it('keys a draft apart from the brief keys of CF-1', () => {
    expect(queryKeys.creativeDraft('ws-1', 'brief-1')).not.toEqual(
      queryKeys.briefDrafts('ws-1', 'brief-1'),
    );
    const workspace = ['workspaces', 'ws-1'];
    for (const key of [
      queryKeys.briefDrafts('ws-1', 'brief-1'),
      queryKeys.creativeDraft('ws-1', 'draft-1'),
      queryKeys.creativeCapability('ws-1'),
    ]) {
      expect(key.slice(0, 2)).toEqual(workspace);
    }
  });
});
