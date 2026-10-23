/**
 * Scheme allow-list for links that come from signal sources.
 *
 * Opportunity evidence carries a `source_url` written by whatever produced the signal.
 * With live feeds (P6-DATA-1) that value originates with a third party, so the SPA
 * never hands a raw value to `href`: only an absolute `http`/`https` URL without
 * embedded credentials becomes a clickable link; `javascript:`, `data:`, `blob:`,
 * `file:`, relative paths and malformed values render no link at all (the P6-AUTH-5
 * evidence-href residual). The API filters item links server-side as well; this is
 * the SPA's own last line.
 */
export function safeExternalHref(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const candidate = value.trim();
  if (!candidate) return null;
  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    return null;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
  if (parsed.username || parsed.password || !parsed.hostname) return null;
  return candidate;
}
