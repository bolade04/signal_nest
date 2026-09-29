#!/usr/bin/env node
/**
 * P6-AUTH-5 — fail the build if the production SPA's Content-Security-Policy is missing,
 * weakened, misplaced, or no longer matches what the bundle needs.
 *
 * vite.config.ts injects the policy as a <meta> tag; this reads the bytes that ship and
 * checks them independently. The expected policy is written out here on purpose — it is
 * NOT imported from vite.config.ts, so a weakened plugin cannot also weaken this check.
 *
 * Checks, all on dist/:
 *   1. no inline <script> (a <script> without `src`) in index.html — the policy's
 *      `script-src 'self'` carries no 'unsafe-inline';
 *   2. no direct `eval(` or `new Function(` in any emitted .js — the policy carries no
 *      'unsafe-eval'. LIMITATION: only those two literal token sequences are detected.
 *      Other code-evaluation forms (e.g. `new window.Function(`, `(0,eval)(`, a bare
 *      `Function(`, string-argument timers) are not; a real-browser run and the live
 *      header check are the backstop;
 *   3. exactly one CSP <meta>, placed before every <script>, <link> (the favicon link
 *      included) and <style> in <head> — a <meta> policy governs only what follows it —
 *      whose content is exactly the expected policy, with `connect-src` equal to `'self'`
 *      plus the API origin the bundle was actually compiled with (if any), plus exactly one
 *      `<meta name="referrer" content="no-referrer">`, and `<meta charset>` inside the first
 *      1024 bytes. The policy must also be LIVE, not merely present: the build fails unless
 *      index.html opens with exactly the prefix Vite emits from our template (DOCUMENT_PREFIX),
 *      and the policy compared is the one in that prefix. Anything else before or around the
 *      policy (a comment, <noscript>, <template>, an unclosed <title>, <textarea> or <xmp>, a
 *      decoy <head>, a character HTML does not treat as whitespace) can leave the browser
 *      ignoring it.
 *
 * The API origin is read from the bundle, not from the environment: every absolute
 * http(s) origin literal in the emitted .js, minus NEVER_FETCHED_ORIGINS, must be that one
 * origin. Extend NEVER_FETCHED_ORIGINS only with evidence that the literal is never
 * fetched (a namespace URI, an error-message link, a placeholder) — never add such an origin
 * to connect-src just to make a build pass.
 *
 * Chained into `build` (like assert-no-demo-credentials.mjs) because CI runs `npm run test`
 * BEFORE `npm run build` and dist/ is gitignored. The checks are exported as pure functions
 * so scripts/assert-csp-build.test.mjs can exercise them on synthetic fixtures; the dist/
 * scan runs only when this file is executed directly. There is no way to point it at
 * another directory.
 *
 * Node builtins only — this must not add a dependency.
 */
import { readdirSync, readFileSync, realpathSync, statSync } from 'node:fs';
import { dirname, extname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/** Absolute URLs measured in the production bundle that the browser never fetches. */
export const NEVER_FETCHED_ORIGINS = new Set([
  'http://www.w3.org', // SVG / XLink / XML namespace URIs passed to createElementNS
  'https://reactjs.org', // React's error-decoder link in production error messages
  'http://fb.me', // a dead PropTypes warning string
  'https://example.com', // a form placeholder
]);

/** The SPA policy, exactly. `frame-ancestors` is absent: a <meta> policy ignores it. */
export function expectedPolicy(apiOrigin) {
  const connect = apiOrigin ? `'self' ${apiOrigin}` : "'self'";
  return (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; " +
    `connect-src ${connect}; frame-src 'none'; worker-src 'none'; media-src 'none'; ` +
    "object-src 'none'; base-uri 'none'; form-action 'self'; manifest-src 'none'"
  );
}

const ORIGIN_LITERAL = /https?:\/\/[A-Za-z0-9.-]+(?::\d+)?/gi;
const EVAL_CALL = /\beval\s*\(/g;
const NEW_FUNCTION_CALL = /\bnew\s+Function\s*\(/g;
const CSP_META = /<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]*)"\s*\/?>/gi;
const REFERRER_META = /<meta\s+name="referrer"\s+content="([^"]*)"\s*\/?>/gi;
const CHARSET_META = /<meta\s+charset="[^"]*"\s*\/?>/i;
const RESOURCE_ELEMENT = /<(script|link|style)\b/i;
/** HTML's whitespace. JavaScript's `\s` also matches NBSP and other Unicode spaces, which HTML
 * treats as text — inside a tag they rename it or an attribute, between tags they end <head>. */
const HTML_SPACE = String.raw`[\t\n\f\r ]`;
/**
 * The exact opening Vite emits from our template: <!doctype html>, <html> with plain
 * name="value" attributes (no `<`, `>` or `"` in a value), a bare <head>, then the literal
 * charset, policy and referrer <meta> tags, separated only by HTML whitespace. Anchored at the
 * first byte, so nothing can precede or enclose the policy: not a comment, <noscript> or
 * <template>, not an unclosed <title>, <textarea> or <xmp> (whose content is text), not a decoy
 * <head> inside such an element or an attribute value. Group 1 is the policy the page opens with.
 */
const DOCUMENT_PREFIX = new RegExp(
  `^${HTML_SPACE}*<!doctype html>${HTML_SPACE}*` +
    `<html(?:${HTML_SPACE}+[A-Za-z][A-Za-z0-9-]*="[^"<>]*")*>${HTML_SPACE}*` +
    `<head>${HTML_SPACE}*` +
    `<meta charset="UTF-8" />${HTML_SPACE}*` +
    `<meta http-equiv="Content-Security-Policy" content="([^"<>]*)" />${HTML_SPACE}*` +
    '<meta name="referrer" content="no-referrer" />',
);

/** WHATWG origin serialization (lowercase host, default port dropped). */
function normalizeOrigin(literal) {
  try {
    return new URL(literal).origin;
  } catch {
    return literal.toLowerCase();
  }
}

/** Every absolute http(s) origin literal in the given bundle texts. */
export function bundleOrigins(scriptTexts) {
  const origins = new Set();
  for (const text of scriptTexts) {
    for (const match of text.matchAll(ORIGIN_LITERAL)) origins.add(normalizeOrigin(match[0]));
  }
  return origins;
}

/** Classic inline scripts: a <script> element without a `src` attribute. */
export function inlineScripts(indexHtml) {
  const found = [];
  for (const match of indexHtml.matchAll(/<script\b([^>]*)>/gi)) {
    // `src` as an attribute of its own, after HTML whitespace — `data-src=` is not one.
    if (!/(?:^|[\t\n\f\r ])src[\t\n\f\r ]*=/i.test(match[1])) found.push(match[0]);
  }
  return found;
}

/** Direct eval-family calls that would need 'unsafe-eval'. */
export function evalCalls(scriptText) {
  return [...scriptText.matchAll(EVAL_CALL), ...scriptText.matchAll(NEW_FUNCTION_CALL)].map(
    (match) => match[0],
  );
}

/**
 * All checks over one build. `scripts` is `[{ path, text }]` for every emitted .js file.
 * Returns a list of problems; an empty list is a pass.
 */
export function checkBuild({ indexHtml, scripts }) {
  const problems = [];
  if (scripts.length === 0) {
    problems.push('no .js file was emitted; refusing to report a vacuous pass');
  }

  // 1. Inline scripts.
  for (const tag of inlineScripts(indexHtml)) problems.push(`inline script in index.html: ${tag}`);

  // 2. Eval-family calls.
  for (const { path, text } of scripts) {
    for (const hit of evalCalls(text)) problems.push(`${path}: ${JSON.stringify(hit)}`);
  }

  // 3a. The API origin the bundle was compiled with (at most one).
  const apiOrigins = [...bundleOrigins(scripts.map((s) => s.text))].filter(
    (origin) => !NEVER_FETCHED_ORIGINS.has(origin),
  );
  if (apiOrigins.length > 1) {
    problems.push(`more than one fetchable origin in the bundle: ${apiOrigins.join(', ')}`);
  }
  const expected = expectedPolicy(apiOrigins.length === 1 ? apiOrigins[0] : null);

  // 3b. Exactly one policy, exactly the expected one. The policy compared is the one the page
  //     opens with (3d) — the tag the browser enforces — when there is one.
  const opening = DOCUMENT_PREFIX.exec(indexHtml);
  const policies = [...indexHtml.matchAll(CSP_META)];
  if (policies.length !== 1) {
    problems.push(`expected exactly one Content-Security-Policy <meta>, found ${policies.length}`);
  } else {
    const found = opening ? opening[1] : policies[0][1];
    if (found !== expected) {
      problems.push(
        `Content-Security-Policy differs from the expected policy:\n` +
          `      found:    ${found}\n      expected: ${expected}`,
      );
    }
  }

  // 3c. Placement: before every resource-loading element in <head>.
  if (policies.length >= 1) {
    const headEnd = indexHtml.search(/<\/head>/i);
    const head = headEnd === -1 ? indexHtml : indexHtml.slice(0, headEnd);
    const firstResource = head.search(RESOURCE_ELEMENT);
    if (firstResource !== -1 && policies[0].index > firstResource) {
      problems.push(
        'the Content-Security-Policy <meta> comes after a <script>, <link> or <style> in <head>',
      );
    }
  }

  // 3d. The policy must be live: the page opens with it (DOCUMENT_PREFIX).
  if (policies.length >= 1 && !opening) {
    problems.push(
      'index.html must open with exactly <!doctype html>, <html …>, <head>, ' +
        '<meta charset="UTF-8" />, the Content-Security-Policy <meta> and ' +
        '<meta name="referrer" content="no-referrer" />, separated only by HTML whitespace; ' +
        'anything else before or around the policy (a comment, <noscript>, <template>, an ' +
        'unclosed <title>/<textarea>/<xmp>, a decoy <head>, a non-HTML space) can make the ' +
        'browser ignore it',
    );
  }

  // 3e. Referrer policy.
  const referrers = [...indexHtml.matchAll(REFERRER_META)];
  if (referrers.length !== 1 || referrers[0][1] !== 'no-referrer') {
    problems.push('expected exactly one <meta name="referrer" content="no-referrer">');
  }

  // 3f. The charset declaration must be serialized within the first 1024 bytes.
  const charset = CHARSET_META.exec(indexHtml);
  if (!charset) {
    problems.push('no <meta charset> in index.html');
  } else {
    const end = Buffer.byteLength(indexHtml.slice(0, charset.index + charset[0].length), 'utf8');
    if (end > 1024) problems.push(`<meta charset> ends at byte ${end}, past the first 1024 bytes`);
  }

  return problems;
}

/** Every emitted file, enumerated rather than globbed, so nothing is skipped. */
function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else out.push(full);
  }
  return out;
}

function main() {
  const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
  const dist = join(webRoot, 'dist');
  let indexHtml;
  let files;
  try {
    indexHtml = readFileSync(join(dist, 'index.html'), 'utf8');
    files = walk(dist);
  } catch {
    console.error(`[csp-guard] ${relative(webRoot, dist)}/index.html is missing — run the build first.`);
    process.exit(1);
  }
  const scripts = files
    .filter((file) => extname(file) === '.js')
    .map((file) => ({ path: relative(dist, file), text: readFileSync(file, 'latin1') }));
  const problems = checkBuild({ indexHtml, scripts });
  if (problems.length > 0) {
    console.error('[csp-guard] the production build breaks its Content-Security-Policy:');
    for (const problem of problems) console.error(`  - ${problem}`);
    process.exit(1);
  }
  console.log(
    `[csp-guard] clean — index.html policy matches; scanned ${scripts.length} .js file(s): ` +
      '0 inline scripts, 0 eval( / new Function( calls.',
  );
}

/** True when this file is the entry point — compared by real path, so a symlinked
 * `scripts/` or file cannot make `npm run build` skip the check silently. */
function invokedDirectly() {
  if (!process.argv[1]) return false;
  try {
    return realpathSync(resolve(process.argv[1])) === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
}

if (invokedDirectly()) main();
