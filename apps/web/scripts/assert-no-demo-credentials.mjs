#!/usr/bin/env node
/**
 * P6-UI-008 — fail the build if demo credentials reach the production bundle.
 *
 * The `import.meta.env.DEV` guard in SignIn.tsx is the mechanism; this is the
 * control. The guard is only closed by default, not pinned: setting
 * `NODE_ENV=development` — directly, or via an `apps/web/.env` or `.env.production`
 * file — makes Vite emit `DEV: true` from a plain `vite build`, silently restoring
 * the vulnerable bundle. Nothing in the repository pins it, so the only durable
 * check is reading the bytes that actually ship.
 *
 * Chained into `build` rather than written as a test on purpose: CI runs
 * `npm run test` BEFORE `npm run build`, and `dist/` is gitignored, so a vitest
 * assertion on `dist/` would read an absent directory on a fresh checkout and pass
 * vacuously. Chained explicitly rather than via a `postbuild` hook so it is visible
 * in the diff and cannot be skipped by `--ignore-scripts`.
 *
 * Node builtins only — this must not add a dependency.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, extname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const WEB_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const DIST = join(WEB_ROOT, 'dist');

/**
 * Fixed strings, never regexes: `signalnest.dev` contains a metacharacter and the
 * separator in the banner copy is a non-ASCII `·`.
 */
const FORBIDDEN = [
  'demo@signalnest.dev',
  'demo1234',
  'Use demo account',
  'Demo login:',
];

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

function countOccurrences(haystack, needle) {
  let count = 0;
  let index = haystack.indexOf(needle);
  while (index !== -1) {
    count += 1;
    index = haystack.indexOf(needle, index + needle.length);
  }
  return count;
}

let files;
try {
  files = walk(DIST);
} catch {
  console.error(
    `[demo-credential-guard] ${relative(WEB_ROOT, DIST)} is missing — run the build first.`,
  );
  process.exit(1);
}

if (files.length === 0) {
  console.error('[demo-credential-guard] dist/ is empty; refusing to report a vacuous pass.');
  process.exit(1);
}

// Two distinct failure classes, reported separately: a credential compiled into a
// chunk and a sourcemap republishing the guarded source are different defects with
// different fixes, and one shared headline misdescribes whichever did not occur.
const mapFailures = [];
const hitFailures = [];

// A sourcemap embeds `sourcesContent`, i.e. the original source including any
// guarded branch — so a map file would republish exactly what the guard removed.
for (const file of files.filter((f) => extname(f) === '.map')) {
  mapFailures.push(relative(DIST, file));
}

for (const file of files) {
  const rel = relative(DIST, file);
  // Read as latin1 so a minified chunk is never mistaken for binary and skipped;
  // the needles are ASCII, so byte-wise matching is exact.
  const contents = readFileSync(file, 'latin1');
  for (const needle of FORBIDDEN) {
    // The path is checked as well as the body: a filename is published exactly like
    // file contents, and scanning only contents let `demo1234-demo@signalnest.dev.txt`
    // ship while this scanner reported clean.
    const inName = countOccurrences(rel, needle);
    if (inName > 0) {
      hitFailures.push(`${rel}: ${inName}x ${JSON.stringify(needle)} in the filename`);
    }
    const hits = countOccurrences(contents, needle);
    if (hits > 0) hitFailures.push(`${rel}: ${hits}x ${JSON.stringify(needle)}`);
  }
}

if (mapFailures.length > 0) {
  console.error(
    `[demo-credential-guard] ${mapFailures.length} sourcemap(s) emitted alongside the bundle:`,
  );
  for (const failure of mapFailures) console.error(`  - ${failure}`);
  console.error(
    '\nA .map embeds `sourcesContent`, i.e. the original source including the guarded\n' +
      'demo branch, so it republishes exactly what the guard removed. Production\n' +
      'builds must not emit sourcemaps; check `build.sourcemap` in vite.config.ts.',
  );
}

if (hitFailures.length > 0) {
  console.error('[demo-credential-guard] demo credentials reached the production bundle:');
  for (const failure of hitFailures) console.error(`  - ${failure}`);
  console.error(
    '\nThe demo shortcut must stay behind the `import.meta.env.DEV` guard in\n' +
      'src/pages/auth/DemoSignInShortcut.tsx, imported statically and side-effect free.\n' +
      'Check for NODE_ENV=development leaking into the build environment.',
  );
}

if (mapFailures.length > 0 || hitFailures.length > 0) process.exit(1);

console.log(
  `[demo-credential-guard] clean — scanned ${files.length} emitted file(s), ` +
    `0 hits for ${FORBIDDEN.length} forbidden strings, 0 sourcemaps.`,
);
