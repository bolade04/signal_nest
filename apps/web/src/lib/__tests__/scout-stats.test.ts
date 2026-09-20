import { describe, expect, it } from 'vitest';
import { RUN_STAT_KEYS, formatStat, statValue, sumStat } from '@/lib/scout-stats';

// P6-UI-019. The pipeline writes exactly four keys (apps/api/app/jobs/pipeline.py);
// `signals_processed` has never existed in the backend. The UI read it anyway and
// rendered a confident 0, because the old helper coerced a missing key to zero and
// `ScoutRequestOut.stats` is an open index signature that type-checks any string.
//
// These tests pin the two properties that make that class of defect impossible to
// repeat quietly: a missing key is `null` (rendered as an em dash), never 0; and the
// key set is derived from the generated contract rather than hand-written.

// The four-key shape the pipeline actually persists.
const RAN = { scanned: 9, noise_filtered: 1, signals_analyzed: 7, opportunities: 3 };
// A scout that has never completed a run: the column defaults to `{}`.
const NEVER_RAN: Record<string, unknown> = {};

describe('RUN_STAT_KEYS', () => {
  it('is exactly the set the backend persists', () => {
    // Mirrors the backend assertion in test_scout_run_history_api.py.
    expect([...RUN_STAT_KEYS].sort()).toEqual([
      'noise_filtered',
      'opportunities',
      'scanned',
      'signals_analyzed',
    ]);
  });

  it('does not contain the invented key this defect was built on', () => {
    expect(RUN_STAT_KEYS).not.toContain('signals_processed' as never);
  });
});

describe('statValue', () => {
  it('reads the analyzed-signal count the pipeline actually writes', () => {
    expect(statValue(RAN, 'signals_analyzed')).toBe(7);
  });

  it('is NOT the difference between scanned and noise filtered', () => {
    // 9 - 1 = 8, but the true value is 7: one signal was a near-duplicate and was
    // dropped without being counted as noise. The dedupe count is emitted nowhere,
    // so the value cannot be reconstructed client-side — it must be read.
    expect(RAN.scanned - RAN.noise_filtered).toBe(8);
    expect(statValue(RAN, 'signals_analyzed')).not.toBe(8);
  });

  it('returns null — not 0 — when the key is absent', () => {
    expect(statValue(NEVER_RAN, 'signals_analyzed')).toBeNull();
  });

  it('returns a genuine zero as 0, distinct from absent', () => {
    // A run that legitimately found nothing is not the same as a run that never happened.
    const ranAndFoundNothing = { scanned: 0, noise_filtered: 0, signals_analyzed: 0, opportunities: 0 };
    expect(statValue(ranAndFoundNothing, 'signals_analyzed')).toBe(0);
    expect(statValue(NEVER_RAN, 'signals_analyzed')).toBeNull();
  });

  it('returns null for a non-numeric value rather than coercing', () => {
    expect(statValue({ signals_analyzed: '7' }, 'signals_analyzed')).toBeNull();
    expect(statValue({ signals_analyzed: null }, 'signals_analyzed')).toBeNull();
  });

  it('rejects non-finite numbers, which would otherwise render as text', () => {
    expect(statValue({ signals_analyzed: NaN }, 'signals_analyzed')).toBeNull();
    expect(statValue({ signals_analyzed: Infinity }, 'signals_analyzed')).toBeNull();
    expect(statValue({ signals_analyzed: -Infinity }, 'signals_analyzed')).toBeNull();
  });

  it('reads own properties only, never the prototype chain', () => {
    const inherited = Object.create({ signals_analyzed: 42 }) as Record<string, unknown>;
    expect(statValue(inherited, 'signals_analyzed')).toBeNull();
  });

  it('tolerates a null or undefined stats object', () => {
    // `RunStats` is nullable on the run-history contract, so the obvious next
    // caller would otherwise crash the render rather than show "not reported".
    expect(statValue(null, 'signals_analyzed')).toBeNull();
    expect(statValue(undefined, 'signals_analyzed')).toBeNull();
  });
});

describe('sumStat', () => {
  it('sums the scouts that have run', () => {
    expect(sumStat([RAN, RAN], 'signals_analyzed')).toBe(14);
  });

  it('ignores scouts that have never run', () => {
    expect(sumStat([RAN, NEVER_RAN], 'signals_analyzed')).toBe(7);
  });

  it('is null only when no scout yields a number', () => {
    expect(sumStat([NEVER_RAN, NEVER_RAN], 'signals_analyzed')).toBeNull();
    expect(sumStat([], 'signals_analyzed')).toBeNull();
  });

  it('tolerates null entries rather than throwing mid-render', () => {
    expect(sumStat([null, RAN, undefined], 'signals_analyzed')).toBe(7);
    expect(sumStat([null, undefined], 'signals_analyzed')).toBeNull();
  });

  it('is not poisoned by a single non-finite member', () => {
    expect(sumStat([RAN, { signals_analyzed: NaN }], 'signals_analyzed')).toBe(7);
  });
});

describe('formatStat', () => {
  it('renders an em dash for unknown, never a fabricated zero', () => {
    expect(formatStat(null)).toBe('—');
  });

  it('renders a real zero as 0', () => {
    expect(formatStat(0)).toBe('0');
  });

  it('renders a number as itself', () => {
    expect(formatStat(7)).toBe('7');
  });
});
