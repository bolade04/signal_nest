import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

export function titleCase(value: string): string {
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .trim();
}

// An ISO 8601 date-time as the API serializes it: date, `T` (or a space), time with optional
// fraction, then an optional offset (`Z`, `±hh:mm`, `±hhmm` or `±hh`).
const API_DATE_TIME = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)(\.\d+)?(Z|[+-]\d{2}(?::?\d{2})?)?$/i;

/**
 * Parse a timestamp from the API, with explicit time-zone handling (UI-AUDIT F6).
 *
 * Every timestamp the API writes is UTC (`app.db.base.utcnow`). On PostgreSQL the columns
 * are `timestamptz` and come back with an offset, which is honoured as given. On SQLite
 * (local and demo mode) they come back naive and are serialized WITHOUT an offset, e.g.
 * "2026-10-29T14:05:34.066648"; `new Date()` would read that as the viewer's local time, so
 * a run minutes old read "in 5 hours" in Chicago. An offset-less date-time is therefore read
 * as UTC. No fixed adjustment is applied: the instant is exact, and the viewer's zone and its
 * daylight-saving rules are applied only when it is formatted. Returns null when unparseable.
 */
export function parseApiDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const text = value.trim();
  const m = API_DATE_TIME.exec(text);
  let date: Date;
  if (m) {
    const [, day, time, fraction = '', offset] = m;
    // Millisecond precision: the API sends microseconds, which Date cannot hold anyway.
    const ms = fraction ? fraction.slice(0, 4).padEnd(4, '0') : '';
    let zone = 'Z';
    if (offset && offset.toUpperCase() !== 'Z') {
      const digits = offset.slice(1).replace(':', '');
      zone = `${offset[0]}${digits.slice(0, 2)}:${digits.slice(2, 4) || '00'}`;
    }
    date = new Date(`${day}T${time}${ms}${zone}`);
  } else {
    date = new Date(text);
  }
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDateTime(iso: string | null | undefined): string {
  const date = parseApiDate(iso);
  if (!date) return '—';
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date);
}

export function formatRelative(iso: string | null | undefined): string {
  const date = parseApiDate(iso);
  if (!date) return '—';
  const diffMs = date.getTime() - Date.now();
  const abs = Math.abs(diffMs);
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ['day', 86_400_000],
    ['hour', 3_600_000],
    ['minute', 60_000],
    ['second', 1000],
  ];
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
  for (const [unit, ms] of units) {
    if (abs >= ms || unit === 'second') {
      return rtf.format(Math.round(diffMs / ms), unit);
    }
  }
  return '—';
}
