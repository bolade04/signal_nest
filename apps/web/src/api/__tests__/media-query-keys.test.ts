import { describe, expect, it } from 'vitest';
import { queryKeys } from '@/api/queryKeys';

// Phase 7A CF-7A-0: the media asset-library keys are a frozen contract the M0-B UI lane
// consumes and may not edit (queryKeys.ts is freeze-owned), so their exact shape is pinned
// here. They are the cache-isolation boundary of the media surface: every key embeds the
// workspace id, and an asset's key sits under the library key its invalidation must reach.

const prefix = (key: readonly unknown[], of: readonly unknown[]) =>
  expect(key.slice(0, of.length)).toEqual([...of]);

describe('queryKeys: media assets (CF-7A-0)', () => {
  it('has the exact tuple for each key', () => {
    expect(queryKeys.mediaAssets('ws-1')).toEqual(['workspaces', 'ws-1', 'media', 'assets']);
    expect(queryKeys.mediaAsset('ws-1', 'asset-1')).toEqual([
      'workspaces',
      'ws-1',
      'media',
      'assets',
      'asset-1',
    ]);
    expect(queryKeys.mediaAssetsCapability('ws-1')).toEqual([
      'workspaces',
      'ws-1',
      'media-assets-capability',
    ]);
  });

  it('nests an asset under the library key and both under the workspace', () => {
    prefix(queryKeys.mediaAsset('ws-1', 'asset-1'), queryKeys.mediaAssets('ws-1'));
    prefix(queryKeys.mediaAssets('ws-1'), queryKeys.workspace('ws-1'));
    prefix(queryKeys.mediaAssetsCapability('ws-1'), queryKeys.workspace('ws-1'));
  });

  it('never shares an entry across workspaces or assets', () => {
    expect(queryKeys.mediaAssets('ws-1')).not.toEqual(queryKeys.mediaAssets('ws-2'));
    expect(queryKeys.mediaAsset('ws-1', 'a-1')).not.toEqual(queryKeys.mediaAsset('ws-2', 'a-1'));
    expect(queryKeys.mediaAsset('ws-1', 'a-1')).not.toEqual(queryKeys.mediaAsset('ws-1', 'a-2'));
    expect(queryKeys.mediaAssetsCapability('ws-1')).not.toEqual(
      queryKeys.mediaAssetsCapability('ws-2'),
    );
  });

  it('keeps the capability key out of the library prefix', () => {
    // Invalidating the library must not drop the availability answer, and vice versa.
    const library = queryKeys.mediaAssets('ws-1');
    const capability = queryKeys.mediaAssetsCapability('ws-1');
    expect(capability.slice(0, library.length)).not.toEqual([...library]);
  });
});
