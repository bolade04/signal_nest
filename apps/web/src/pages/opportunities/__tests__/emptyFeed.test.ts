import { describe, expect, it } from 'vitest';
import type { ScoutRequestOut } from '@/api/types';
import { describeEmptyFeed } from '../emptyFeed';

const scout = (over: Partial<ScoutRequestOut>): ScoutRequestOut => ({
  id: 's',
  organization_id: 'org-1',
  workspace_id: 'ws-1',
  brand_id: 'brand-1',
  location_id: null,
  campaign_id: null,
  name: 'Scout',
  status: 'draft',
  source_types: [],
  keywords: [],
  product_profile_id: null,
  resolved_market: null,
  notes: null,
  last_run_at: null,
  stats: {},
  created_at: '2026-05-01T00:00:00Z',
  updated_at: '2026-05-01T00:00:00Z',
  ...over,
});

describe('describeEmptyFeed (P6-UI-004)', () => {
  it('no scouts', () => {
    expect(describeEmptyFeed([])).toEqual({ kind: 'no-scouts' });
  });

  it('never ran', () => {
    expect(describeEmptyFeed([scout({})]).kind).toBe('never-ran');
  });

  it('in flight wins over everything else', () => {
    const ran = scout({ id: 'a', status: 'completed', last_run_at: '2026-06-01T00:00:00Z' });
    const queued = scout({ id: 'b', status: 'queued' });
    expect(describeEmptyFeed([ran, queued])).toMatchObject({ kind: 'in-flight', scout: { id: 'b' } });
  });

  it('a failure is read from status, since it records no last_run_at', () => {
    const ok = scout({ id: 'a', status: 'completed', last_run_at: '2026-06-01T00:00:00Z', updated_at: '2026-06-01T00:00:00Z' });
    const failed = scout({ id: 'b', status: 'failed', updated_at: '2026-06-02T00:00:00Z' });
    expect(describeEmptyFeed([ok, failed])).toMatchObject({ kind: 'last-run-failed', scout: { id: 'b' } });
    // An older failure does not hide a newer completed run.
    const older = scout({ id: 'c', status: 'failed', updated_at: '2026-05-20T00:00:00Z' });
    expect(describeEmptyFeed([ok, older]).kind).toBe('ran-found-nothing');
  });

  it('ran and found nothing: the newest run, its signal count, and no invented reason', () => {
    const older = scout({ id: 'a', status: 'completed', last_run_at: '2026-06-01T00:00:00Z', stats: { signals_analyzed: 3 } });
    const newer = scout({ id: 'b', status: 'completed', last_run_at: '2026-06-03T00:00:00Z', stats: { signals_analyzed: 0 } });
    expect(describeEmptyFeed([older, newer])).toEqual({
      kind: 'ran-found-nothing',
      scout: newer,
      signalsAnalyzed: 0,
      reason: null,
    });
  });

  it('a missing count is "not reported" (null), never zero', () => {
    const ran = scout({ status: 'completed', last_run_at: '2026-06-01T00:00:00Z', stats: {} });
    expect(describeEmptyFeed([ran])).toMatchObject({ signalsAnalyzed: null });
  });
});
