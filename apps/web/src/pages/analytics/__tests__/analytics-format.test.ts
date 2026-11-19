import { describe, expect, it } from 'vitest';
import {
  familySummaries,
  formatCount,
  formatDuration,
  formatRate,
  NOT_ENOUGH_DATA,
  NULL_FIGURE,
} from '@/pages/analytics/analyticsFormat';
import { analyticsFixture } from '@/test/handlers';

// Project Phase 5E (lane 5E-W4): the display rules of status analytics. `null` (a figure the
// API could not compute) is the dash, never a zero; a real zero is a zero.

describe('analytics display rules', () => {
  it('null is the dash for rates and durations, never 0, 0% or 0 s', () => {
    expect(NULL_FIGURE).toBe('—');
    expect(NOT_ENOUGH_DATA).toBe('Not enough data yet');
    expect(formatRate(null)).toBe('—');
    expect(formatDuration(null)).toBe('—');
  });

  it('a real zero is a zero', () => {
    expect(formatRate(0)).toBe('0%');
    expect(formatDuration(0)).toBe('0 s');
    expect(formatCount(0)).toBe('0');
  });

  it.each([
    [0.5, '50%'],
    [1, '100%'],
    [0.3333, '33%'],
  ])('a rate of %s is %s', (value, shown) => {
    expect(formatRate(value)).toBe(shown);
  });

  it.each([
    [59, '59 s'],
    [60, '1 min'],
    [3600, '1 h'],
    [5400, '1 h 30 min'],
    [90000, '1 d 1 h'],
    [-30, '-30 s'],
  ])('a duration of %s seconds is %s', (seconds, shown) => {
    expect(formatDuration(seconds)).toBe(shown);
  });

  it('the six families are in the plan order with every state', () => {
    const families = familySummaries(analyticsFixture('ws-1'));
    expect(families.map((f) => f.key)).toEqual([
      'opportunities',
      'briefs',
      'approvals',
      'drafts',
      'reviews',
      'exports',
    ]);
    expect(families.map((f) => f.total)).toEqual([7, 5, 4, 2, 2, 1]);
    // No family row is an attribution or advertising-performance figure.
    const keys = families.flatMap((f) => f.rows.map((r) => r.key));
    expect(keys.filter((k) => /spend|cost|click|impression|conversion|revenue|attribut/.test(k))).toEqual([]);
  });
});
