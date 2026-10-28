import { describe, expect, it } from 'vitest';
import { safeExternalHref } from '@/lib/links';

describe('safeExternalHref', () => {
  it('keeps absolute http(s) URLs as given', () => {
    expect(safeExternalHref('https://example.com/article')).toBe('https://example.com/article');
    expect(safeExternalHref('http://example.com/a?b=1#c')).toBe('http://example.com/a?b=1#c');
    expect(safeExternalHref('  https://example.com/x  ')).toBe('https://example.com/x');
  });

  it('refuses every other scheme', () => {
    for (const value of [
      'javascript:alert(1)',
      'JavaScript:alert(1)',
      ' javascript:alert(1)',
      'data:text/html,<script>alert(1)</script>',
      'blob:https://example.com/uuid',
      'file:///etc/passwd',
      'vbscript:msgbox',
      'mailto:someone@example.com',
      'ftp://example.com/feed',
    ]) {
      expect(safeExternalHref(value), value).toBeNull();
    }
  });

  it('refuses relative, empty, credentialed and non-string values', () => {
    for (const value of ['/relative/path', 'example.com/no-scheme', '//example.com/protocol-relative', '', '   ', 'https://user:pw@example.com/', 'https://']) {
      expect(safeExternalHref(value), value).toBeNull();
    }
    expect(safeExternalHref(null)).toBeNull();
    expect(safeExternalHref(undefined)).toBeNull();
    expect(safeExternalHref(42)).toBeNull();
    expect(safeExternalHref({ href: 'https://example.com' })).toBeNull();
  });
});
