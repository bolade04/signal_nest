import { describe, expect, it } from 'vitest';
import { toAppPath } from '@/auth/intended-path';

// PR #234 F1: the intended path is the address bar; it must only ever name a path in this app.
describe('toAppPath', () => {
  it.each([
    ['/settings?from=deep-link', '/settings?from=deep-link'],
    ['/opportunities/opp-loc-dallas-0', '/opportunities/opp-loc-dallas-0'],
    ['/', '/'],
    ['/settings?next=//evil.example', '/settings?next=//evil.example'],
    ['/%5Cevil.example', '/%5Cevil.example'],
    ['/%2F%2Fevil.example', '/%2F%2Fevil.example'],
  ])('keeps the app path %s', (raw, expected) => {
    expect(toAppPath(raw)).toBe(expected);
  });

  it.each([
    ['//evil.example', '/evil.example'],
    ['///evil.example/x?y=1', '/evil.example/x?y=1'],
    ['/\\evil.example', '/evil.example'],
    ['\\\\evil.example', '/evil.example'],
    ['/\t/evil.example', '/evil.example'],
    ['/\n/evil.example', '/evil.example'],
    [' //evil.example', '/evil.example'],
    ['settings', '/settings'],
  ])('collapses the leading run of %j into one slash', (raw, expected) => {
    expect(toAppPath(raw)).toBe(expected);
  });

  it.each([
    ['/a\t/\\evil.example'],
    ['https://evil.example/'],
  ])('never yields a path that resolves to another origin: %j', (raw) => {
    const result = toAppPath(raw)!;
    expect(new URL(result, 'https://app.example.test').origin).toBe('https://app.example.test');
  });

  it('keeps no intended path as none', () => {
    expect(toAppPath(null)).toBeNull();
  });
});
