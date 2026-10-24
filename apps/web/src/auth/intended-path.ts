/**
 * The path a sign-in returns to, reduced to a path inside this app (P6-CI-7, PR #234 F1).
 *
 * It comes from the address bar (`location.pathname + location.search`), so it can be
 * `//evil.example`, `/\evil.example` or carry tabs and newlines the URL parser drops.
 * React Router 7 refuses to navigate to anything that resolves to another origin and throws
 * "External navigation is not allowed" — from `<Navigate>` during render, which blanks the
 * page. Every leading run of slashes, backslashes, whitespace and control characters
 * collapses to one `/`, and anything that still does not resolve to the page's own origin
 * becomes `/` (the check React Router makes). The query stays: it is data for the page,
 * never a navigation target. No origin literal: the CSP build guard reads every absolute
 * origin in the bundle as a fetch target.
 */
export function toAppPath(path: string | null): string | null {
  if (path === null) return null;
  // eslint-disable-next-line no-control-regex -- control characters are exactly what is stripped
  const candidate = `/${path.replace(/^[\s\u0000-\u001f\\/]+/, '')}`;
  try {
    if (new URL(candidate, window.location.href).origin === window.location.origin) return candidate;
  } catch {
    // An unparsable path is not an app path.
  }
  return '/';
}
