import { describe, expect, it } from 'vitest';
import { queryKeys } from '@/api/queryKeys';

// Project Phase 5 CF-3 (W21): the campaign, export, analytics and notification keys are a
// frozen contract the 5D/5E build lanes consume and may not edit, so their exact shape is
// pinned here. They are the cache-isolation boundary for these surfaces; the notification
// keys add the first PER-USER dimension to the factory (an isolation control, plan §6.3).

const prefix = (key: readonly unknown[], of: readonly unknown[]) =>
  expect(key.slice(0, of.length)).toEqual([...of]);

describe('queryKeys: 5D campaign, 5E export and analytics (CF-3)', () => {
  it('has the exact tuple for each key', () => {
    expect(queryKeys.campaign('ws-1', 'camp-1')).toEqual([
      'workspaces',
      'ws-1',
      'context',
      'campaigns',
      'camp-1',
    ]);
    expect(queryKeys.draftExports('ws-1', 'draft-1')).toEqual([
      'workspaces',
      'ws-1',
      'drafts',
      'draft-1',
      'exports',
    ]);
    expect(queryKeys.exportCapability('ws-1')).toEqual(['workspaces', 'ws-1', 'export-capability']);
    expect(queryKeys.analytics('ws-1', null)).toEqual([
      'workspaces',
      'ws-1',
      'analytics',
      { locationId: null },
    ]);
    expect(queryKeys.analytics('ws-1', 'loc-1')).toEqual([
      'workspaces',
      'ws-1',
      'analytics',
      { locationId: 'loc-1' },
    ]);
  });

  it('nests each detail key under the key its invalidation must reach', () => {
    prefix(queryKeys.campaign('ws-1', 'camp-1'), queryKeys.context('ws-1', 'campaigns'));
    prefix(queryKeys.draftExports('ws-1', 'draft-1'), queryKeys.creativeDraft('ws-1', 'draft-1'));
  });

  it('never shares an entry across workspaces, campaigns, drafts or locations', () => {
    expect(queryKeys.campaign('ws-1', 'camp-1')).not.toEqual(queryKeys.campaign('ws-2', 'camp-1'));
    expect(queryKeys.campaign('ws-1', 'camp-1')).not.toEqual(queryKeys.campaign('ws-1', 'camp-2'));
    expect(queryKeys.draftExports('ws-1', 'd-1')).not.toEqual(queryKeys.draftExports('ws-2', 'd-1'));
    expect(queryKeys.draftExports('ws-1', 'd-1')).not.toEqual(queryKeys.draftExports('ws-1', 'd-2'));
    expect(queryKeys.analytics('ws-1', null)).not.toEqual(queryKeys.analytics('ws-2', null));
    expect(queryKeys.analytics('ws-1', 'loc-1')).not.toEqual(queryKeys.analytics('ws-1', 'loc-2'));
    expect(queryKeys.analytics('ws-1', 'loc-1')).not.toEqual(queryKeys.analytics('ws-1', null));
  });

  it('keeps the workspace-effective export capability apart from every other entry', () => {
    const key = queryKeys.exportCapability('ws-1');
    expect(key).not.toEqual(queryKeys.exportCapability('ws-2'));
    expect(key).not.toEqual([...queryKeys.runtimeSummary]);
    expect(key).not.toEqual(queryKeys.creativeCapability('ws-1'));
    expect(key).not.toEqual(queryKeys.briefCapability('ws-1'));
    expect(key).not.toEqual(queryKeys.feedbackCapability('ws-1'));
  });
});

describe('queryKeys: 5E notifications carry a per-user scope dimension (CF-3)', () => {
  it('has the exact tuple for each key', () => {
    expect(queryKeys.userScope('ws-1', 'user-1')).toEqual(['workspaces', 'ws-1', 'users', 'user-1']);
    expect(queryKeys.notifications('ws-1', 'user-1')).toEqual([
      'workspaces',
      'ws-1',
      'users',
      'user-1',
      'notifications',
    ]);
    expect(queryKeys.notificationPreferences('ws-1', 'user-1')).toEqual([
      'workspaces',
      'ws-1',
      'users',
      'user-1',
      'notification-preferences',
    ]);
  });

  it('embeds the workspace and then the user, under the user scope', () => {
    for (const key of [
      queryKeys.notifications('ws-1', 'user-1'),
      queryKeys.notificationPreferences('ws-1', 'user-1'),
    ]) {
      prefix(key, queryKeys.userScope('ws-1', 'user-1'));
      prefix(key, queryKeys.workspace('ws-1'));
    }
  });

  it('never serves one user the entry of another, in the same workspace or another', () => {
    for (const make of [queryKeys.notifications, queryKeys.notificationPreferences]) {
      const mine = make('ws-1', 'user-1');
      expect(mine).not.toEqual(make('ws-1', 'user-2'));
      expect(mine).not.toEqual(make('ws-2', 'user-1'));
      // One user's scope is never a prefix of another user's keys.
      expect(make('ws-1', 'user-2').slice(0, 4)).not.toEqual(
        [...queryKeys.userScope('ws-1', 'user-1')],
      );
    }
    expect(queryKeys.notifications('ws-1', 'user-1')).not.toEqual(
      queryKeys.notificationPreferences('ws-1', 'user-1'),
    );
  });
});
