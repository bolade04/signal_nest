import { afterEach, describe, expect, it, vi } from 'vitest';
import { formatDate } from '@/pages/settings/member-admin';
import { formatDateTime, formatRelative, parseApiDate } from '@/lib/utils';

/**
 * UI-AUDIT F6 / P7: timestamps read "in 5 hours" for events minutes old, and absolute
 * times were 5 h ahead, for a viewer in America/Chicago.
 *
 * Every API timestamp is UTC. PostgreSQL (`timestamptz`) serializes it with an offset; SQLite
 * (local/demo mode) hands it back naive and the API serializes it WITHOUT one, e.g.
 * "2026-10-29T14:05:34.066648" (P6-W10-UIFIX-01 raw/010, raw/030). `new Date()` reads an
 * offset-less date-time as LOCAL time. The parser now reads it as UTC and honours `Z` and
 * `±hh:mm` as given; the viewer's zone and its daylight-saving rules apply only when
 * formatting. These tests switch the process time zone (Node re-reads `TZ` at run time) so
 * they do not depend on the machine's zone, and they cross both 2026 US transitions.
 */

const env = (globalThis as unknown as { process: { env: Record<string, string | undefined> } }).process.env;
const ORIGINAL_TZ = env.TZ;

function useZone(zone: string) {
  env.TZ = zone;
  // Fail loudly rather than pass vacuously if this runner cannot switch zones. (ICU may
  // report a canonical alias, e.g. Asia/Calcutta for Asia/Kolkata.)
  const canonical = new Intl.DateTimeFormat(undefined, { timeZone: zone }).resolvedOptions().timeZone;
  expect(Intl.DateTimeFormat().resolvedOptions().timeZone).toBe(canonical);
}

/** The same instant formatted by Intl in an explicitly named zone: the reference. */
function reference(utcMs: number, zone: string, options: Intl.DateTimeFormatOptions = { dateStyle: 'medium', timeStyle: 'short' }) {
  return new Intl.DateTimeFormat(undefined, { ...options, timeZone: zone }).format(utcMs);
}

afterEach(() => {
  env.TZ = ORIGINAL_TZ;
  vi.useRealTimers();
});

// America/Chicago twice (US rules), UTC, and three non-US zones: Europe/London (other DST
// dates), Asia/Kolkata (a half-hour offset, no DST), Asia/Tokyo (no DST).
const ZONES = ['America/Chicago', 'UTC', 'Europe/London', 'Asia/Kolkata', 'Asia/Tokyo'];

describe('parseApiDate: one instant whatever the serialization (UI-AUDIT P7)', () => {
  it.each(ZONES)('in %s: naive = UTC, and Z / ±hh:mm / ±hhmm are honoured', (zone) => {
    useZone(zone);
    const instant = '2026-10-29T14:05:34.066Z';
    for (const text of [
      '2026-10-29T14:05:34.066648', // SQLite: naive, microseconds
      '2026-10-29T14:05:34.066648Z', // PostgreSQL, session TimeZone UTC
      '2026-10-29T09:05:34.066648-05:00', // PostgreSQL, session TimeZone America/Chicago (CDT)
      '2026-10-29T19:35:34.066+05:30',
      '2026-10-29T14:05:34.066+0000',
      '2026-10-29 14:05:34.066', // space separator
    ]) {
      expect(parseApiDate(text)?.toISOString(), text).toBe(instant);
    }
    expect(parseApiDate('2026-10-29T14:05')?.toISOString()).toBe('2026-10-29T14:05:00.000Z');
    expect(parseApiDate('2026-10-29T14:05:34+05')?.toISOString()).toBe('2026-10-29T09:05:34.000Z');
    for (const bad of ['', null, undefined, 'not a date', '2026-13-45T99:00:00']) {
      expect(parseApiDate(bad), String(bad)).toBeNull();
    }
  });
});

describe('formatDateTime across the 2026 US daylight-saving transitions (UI-AUDIT P7)', () => {
  it('America/Chicago, 2026-11-01 (CDT -> CST): two different UTC instants both read 1:30 AM; no fixed offset', () => {
    useZone('America/Chicago');
    const cdt = formatDateTime('2026-11-01T06:30:00'); // 01:30 CDT (UTC-5)
    const cst = formatDateTime('2026-11-01T07:30:00'); // 01:30 CST (UTC-6), the repeated hour
    expect(cdt).toBe(reference(Date.UTC(2026, 10, 1, 6, 30), 'America/Chicago'));
    expect(cst).toBe(reference(Date.UTC(2026, 10, 1, 7, 30), 'America/Chicago'));
    expect(cdt).toMatch(/\b0?1:30\b/);
    expect(cst).toBe(cdt);
    expect(formatDateTime('2026-11-01T08:30:00')).toMatch(/\b0?2:30\b/);
    // The same instants with explicit offsets read the same.
    expect(formatDateTime('2026-11-01T01:30:00-05:00')).toBe(cdt);
    expect(formatDateTime('2026-11-01T07:30:00Z')).toBe(cst);
  });

  it('America/Chicago, 2026-03-08 (CST -> CDT): 07:59Z reads 1:59 AM and 08:00Z reads 3:00 AM', () => {
    useZone('America/Chicago');
    expect(formatDateTime('2026-03-08T07:59:00')).toBe(reference(Date.UTC(2026, 2, 8, 7, 59), 'America/Chicago'));
    expect(formatDateTime('2026-03-08T07:59:00')).toMatch(/\b0?1:59\b/);
    expect(formatDateTime('2026-03-08T08:00:00')).toBe(reference(Date.UTC(2026, 2, 8, 8, 0), 'America/Chicago'));
    expect(formatDateTime('2026-03-08T08:00:00')).toMatch(/\b0?3:00\b/);
  });

  it.each(ZONES)('in %s: a naive API time formats as the UTC instant it is, on both transition days', (zone) => {
    useZone(zone);
    for (const [text, utc] of [
      ['2026-03-08T07:59:00', Date.UTC(2026, 2, 8, 7, 59)],
      ['2026-03-08T08:00:00', Date.UTC(2026, 2, 8, 8, 0)],
      ['2026-11-01T06:30:00', Date.UTC(2026, 10, 1, 6, 30)],
      ['2026-11-01T07:30:00', Date.UTC(2026, 10, 1, 7, 30)],
      ['2026-10-29T14:05:34.066648', Date.UTC(2026, 9, 29, 14, 5, 34)],
      // Summer: London is on BST (UTC+1) here, Chicago on CDT (UTC-5).
      ['2026-07-01T12:00:00', Date.UTC(2026, 6, 1, 12, 0)],
    ] as const) {
      expect(formatDateTime(text), `${zone} ${text}`).toBe(reference(utc, zone));
    }
  });

  it('a membership join date is the calendar date in the viewer zone (Chicago sees the 28th for 03:30Z on the 29th)', () => {
    useZone('America/Chicago');
    expect(formatDate('2026-10-29T03:30:00')).toBe(reference(Date.UTC(2026, 9, 29, 3, 30), 'America/Chicago', { dateStyle: 'medium' }));
    expect(formatDate('2026-10-29T03:30:00')).toMatch(/28/);
    useZone('UTC');
    expect(formatDate('2026-10-29T03:30:00')).toMatch(/29/);
  });
});

describe('formatRelative is the same in every zone (UI-AUDIT P7)', () => {
  const rtf = () => new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });

  it.each(ZONES)('in %s: a naive time five minutes old reads "5 minutes ago", not "in 5 hours"', (zone) => {
    useZone(zone);
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-29T14:10:34.066Z'));
    expect(formatRelative('2026-10-29T14:05:34.066648')).toBe(rtf().format(-5, 'minute'));
    expect(formatRelative('2026-10-29T14:05:34.066648Z')).toBe(rtf().format(-5, 'minute'));
    expect(formatRelative('2026-10-29T09:05:34.066-05:00')).toBe(rtf().format(-5, 'minute'));
  });

  it('across the 2026-11-01 fall-back hour in Chicago: 06:50Z seen at 07:10Z is 20 minutes ago', () => {
    useZone('America/Chicago');
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-01T07:10:00Z'));
    expect(formatRelative('2026-11-01T06:50:00')).toBe(rtf().format(-20, 'minute'));
  });

  it('across the 2026-03-08 spring-forward hour in Chicago: 07:50Z seen at 08:10Z is 20 minutes ago', () => {
    useZone('America/Chicago');
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-03-08T08:10:00Z'));
    expect(formatRelative('2026-03-08T07:50:00')).toBe(rtf().format(-20, 'minute'));
  });
});
