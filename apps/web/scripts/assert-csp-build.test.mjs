/**
 * P6-AUTH-5 — the build guard (assert-csp-build.mjs), the meta-CSP plugin in vite.config.ts
 * and the `build` hook that ties them together.
 *
 * Synthetic fixtures only: dist/ is gitignored and CI runs the tests before the build, so
 * nothing here reads dist/. The expected policy is written out below on purpose — it is
 * the third, independent copy (plugin, guard, test), so weakening one of them fails here.
 * Runs in the `build-scripts` Vitest project (Node environment; see vite.config.ts).
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { checkBuild, evalCalls } from './assert-csp-build.mjs';
import { apiOriginFromBase, contentSecurityPolicy, injectSecurityMeta } from '../vite.config.ts';

const WEB_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const API = 'http://127.0.0.1:8000';

function policy(apiOrigin) {
  return (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; " +
    `connect-src ${apiOrigin ? `'self' ${apiOrigin}` : "'self'"}; frame-src 'none'; ` +
    "worker-src 'none'; media-src 'none'; object-src 'none'; base-uri 'none'; " +
    "form-action 'self'; manifest-src 'none'"
  );
}

/** A built index.html shaped like Vite's output, with the two metas after <meta charset>. */
function html({ csp = policy(null), referrer = 'no-referrer', afterCharset, beforeHeadEnd = '', body = '' } = {}) {
  const metas =
    afterCharset ??
    `\n    <meta http-equiv="Content-Security-Policy" content="${csp}" />` +
      `\n    <meta name="referrer" content="${referrer}" />`;
  return (
    '<!doctype html>\n<html lang="en" class="h-full">\n  <head>\n    <meta charset="UTF-8" />' +
    metas +
    '\n    <link rel="icon" type="image/svg+xml" href="/favicon.svg" />' +
    '\n    <meta name="viewport" content="width=device-width, initial-scale=1.0" />' +
    '\n    <title>SignalNest — AI Scout</title>' +
    '\n    <script type="module" crossorigin src="/assets/index-abc.js"></script>' +
    '\n    <link rel="stylesheet" crossorigin href="/assets/index-abc.css">' +
    beforeHeadEnd +
    `\n  </head>\n  <body class="h-full">\n    <div id="root" class="h-full"></div>${body}\n  </body>\n</html>\n`
  );
}

/** A bundle shaped like the real one: never-fetched literals, plus the API base if any. */
function bundle(extra = '') {
  return {
    path: 'assets/index-abc.js',
    text:
      'var n="http://www.w3.org/1999/xlink",r="https://reactjs.org/docs/error-decoder.html?invariant=",' +
      'p="http://fb.me/use-check-prop-types",q={placeholder:"https://example.com"};' +
      'var g=e||t||Function("return this")();MSApp.execUnsafeLocalFunction(function(){});' +
      extra,
  };
}

const withApi = (origin = API) => bundle(`var base=${JSON.stringify(origin)};`);

describe('checkBuild — clean builds pass', () => {
  it('passes a same-origin build (empty VITE_API_BASE_URL)', () => {
    expect(checkBuild({ indexHtml: html(), scripts: [bundle()] })).toEqual([]);
  });

  it('passes a cross-origin build whose policy names the compiled API origin', () => {
    expect(checkBuild({ indexHtml: html({ csp: policy(API) }), scripts: [withApi()] })).toEqual([]);
  });

  it('normalizes origins as WHATWG does (case, default port, path)', () => {
    const built = { indexHtml: html({ csp: policy('https://api.example.test') }), scripts: [withApi('HTTPS://API.Example.test:443/v1')] };
    expect(checkBuild(built)).toEqual([]);
  });
});

describe('checkBuild — script and eval regressions fail', () => {
  it('fails on a classic inline <script>', () => {
    const built = { indexHtml: html({ body: '\n    <script>window.x = 1</script>' }), scripts: [bundle()] };
    expect(checkBuild(built).join('\n')).toMatch(/inline script/);
  });

  it('treats data-src as no src (still an inline script)', () => {
    const built = { indexHtml: html({ body: '\n    <script data-src="/x.js">window.x = 1</script>' }), scripts: [bundle()] };
    expect(checkBuild(built).join('\n')).toMatch(/inline script/);
    // After a non-HTML space, "src" is part of another attribute's name, not an attribute.
    const renamed = { indexHtml: html({ body: '\n    <script data-x src="/x.js">window.x = 1</script>' }), scripts: [bundle()] };
    expect(checkBuild(renamed).join('\n')).toMatch(/inline script/);
  });

  it('fails on direct eval( and new Function( in any emitted script', () => {
    expect(checkBuild({ indexHtml: html(), scripts: [bundle('eval(code);')] }).join('\n')).toMatch(/eval\(/);
    expect(checkBuild({ indexHtml: html(), scripts: [bundle('var f=new Function("a","return a");')] }).join('\n')).toMatch(
      /new Function\(/,
    );
  });

  it('matches only the two literal token sequences (documented limitation)', () => {
    expect(evalCalls('retrieval(x); Function("return this")(); MSApp.execUnsafeLocalFunction(f);')).toEqual([]);
    expect(evalCalls('eval (x); new  Function (y)')).toHaveLength(2);
  });

  it('refuses a vacuous pass when no script was emitted', () => {
    expect(checkBuild({ indexHtml: html(), scripts: [] }).join('\n')).toMatch(/vacuous/);
  });
});

describe('checkBuild — a weakened or misplaced policy fails', () => {
  const weakenings = {
    "'unsafe-eval' added": policy(null).replace("script-src 'self'", "script-src 'self' 'unsafe-eval'"),
    "script 'unsafe-inline' added": policy(null).replace("script-src 'self'", "script-src 'self' 'unsafe-inline'"),
    'wildcard image source': policy(null).replace("img-src 'self'", 'img-src *'),
    'directive removed': policy(null).replace("object-src 'none'; ", ''),
    'directive added': `${policy(null)}; font-src https:`,
    'style-src relaxed': policy(null).replace("style-src 'self' 'unsafe-inline'", "style-src * 'unsafe-inline'"),
  };
  for (const [name, csp] of Object.entries(weakenings)) {
    it(`fails when ${name}`, () => {
      expect(checkBuild({ indexHtml: html({ csp }), scripts: [bundle()] }).join('\n')).toMatch(/differs/);
    });
  }

  it('fails when the policy meta is missing or duplicated', () => {
    const missing = html({ afterCharset: '\n    <meta name="referrer" content="no-referrer" />' });
    expect(checkBuild({ indexHtml: missing, scripts: [bundle()] }).join('\n')).toMatch(/found 0/);
    const twice = html({ beforeHeadEnd: `\n    <meta http-equiv="Content-Security-Policy" content="${policy(null)}" />` });
    expect(checkBuild({ indexHtml: twice, scripts: [bundle()] }).join('\n')).toMatch(/found 2/);
  });

  it('fails when the policy comes after the favicon link or the entry script', () => {
    const late = html({
      afterCharset: '\n    <meta name="referrer" content="no-referrer" />',
      beforeHeadEnd: `\n    <meta http-equiv="Content-Security-Policy" content="${policy(null)}" />`,
    });
    expect(checkBuild({ indexHtml: late, scripts: [bundle()] }).join('\n')).toMatch(/comes after/);
  });

  it('fails without exactly one no-referrer meta', () => {
    expect(checkBuild({ indexHtml: html({ referrer: 'origin' }), scripts: [bundle()] }).join('\n')).toMatch(/referrer/);
  });

  it('fails when the policy is present but inert (comment, <noscript>, <template>)', () => {
    const metas =
      `<meta http-equiv="Content-Security-Policy" content="${policy(null)}" />` +
      '<meta name="referrer" content="no-referrer" />';
    for (const [open, close] of [['<!--', '-->'], ['<noscript>', '</noscript>'], ['<template>', '</template>']]) {
      const inert = html({ afterCharset: `\n    ${open}${metas}${close}` });
      const problems = checkBuild({ indexHtml: inert, scripts: [bundle()] }).join('\n');
      expect(problems, open).toMatch(/ignore the policy|must open with/);
    }
  });

  it('fails when anything but whitespace separates <meta charset> and the policy', () => {
    const shifted = html({
      afterCharset:
        '\n    <meta name="viewport" content="width=device-width" />' +
        `\n    <meta http-equiv="Content-Security-Policy" content="${policy(null)}" />` +
        '\n    <meta name="referrer" content="no-referrer" />',
    });
    expect(checkBuild({ indexHtml: shifted, scripts: [bundle()] }).join('\n')).toMatch(/must open with/);
  });

  // Constructions that leave the browser without a live policy; each must fail the build.
  const METAS =
    `<meta http-equiv="Content-Security-Policy" content="${policy(null)}" />` +
    '<meta name="referrer" content="no-referrer" />';
  const WRAPPERS = [
    ['<title>', '</title>'],
    ['<textarea>', '</textarea>'],
    ['<xmp>', '</xmp>'],
    ['<noembed>', '</noembed>'],
    ['<noframes>', '</noframes>'],
    ['<iframe>', '</iframe>'],
    ['<svg><desc>', '</desc></svg>'],
    ['<!--', '-->'],
    ['<noscript>', '</noscript>'],
    ['<template>', '</template>'],
  ];

  it('fails when an element before <head> swallows the head, policy included', () => {
    for (const [open] of WRAPPERS) {
      const swallowed = html().replace('<head>', `${open}<head>`);
      expect(checkBuild({ indexHtml: swallowed, scripts: [bundle()] }).join('\n'), open).toMatch(/must open with/);
    }
  });

  it('fails on a decoy policy inside an element while the entry script stays live', () => {
    for (const [open, close] of WRAPPERS) {
      // The only policy is text or foreign content; the entry <script> after it still runs.
      const decoy = html({ afterCharset: `\n    ${open}<head><meta charset="UTF-8" />${METAS}${close}` });
      expect(checkBuild({ indexHtml: decoy, scripts: [bundle()] }).join('\n'), open).toMatch(/must open with/);
    }
  });

  it('fails when the opening tags are smuggled into, or swallowed by, an attribute', () => {
    const cases = {
      'single-quoted value carrying the tags': html({ afterCharset: '' }).replace(
        '<html lang="en" class="h-full">',
        `<html lang="en" class="h-full" data-x='<head><meta charset="UTF-8" />${METAS}'>`,
      ),
      'unclosed single quote on <head>': html().replace('<head>', "<head x='>"),
      'unclosed single quote on <html>': html().replace('class="h-full">', "class='h-full>"),
      'attribute on <head>': html().replace('<head>', '<head class="x">'),
      'comment before the doctype': `<!-- x -->${html()}`,
    };
    for (const [name, page] of Object.entries(cases)) {
      expect(checkBuild({ indexHtml: page, scripts: [bundle()] }).join('\n'), name).toMatch(/must open with/);
    }
  });

  it('fails on a character JavaScript counts as whitespace but HTML does not', () => {
    // In a tag it renames the tag or an attribute; between tags it ends <head>. Either way
    // the policy <meta> is not a live head child, yet `\s` would match it.
    for (const space of ['\u000B', '\u00A0', '\u2028', '\u3000', '\uFEFF']) {
      const pages = [
        html().replace('<!doctype html>\n', `<!doctype html>${space}`),
        html().replace('<head>\n    <meta charset', `<head>${space}<meta charset`),
        html().replace('<meta http-equiv', `<meta${space}http-equiv`),
        html().replace('" content="default-src', `"${space}content="default-src`),
      ];
      for (const page of pages) {
        expect(checkBuild({ indexHtml: page, scripts: [bundle()] }).join('\n'), JSON.stringify(space)).toMatch(
          /must open with/,
        );
      }
    }
  });

  it('fails when <meta charset> ends past the first 1024 bytes', () => {
    const padded = html().replace('<head>', `<head>\n    <!--${'x'.repeat(1100)}-->`);
    expect(checkBuild({ indexHtml: padded, scripts: [bundle()] }).join('\n')).toMatch(/1024 bytes/);
  });
});

describe('checkBuild — connect-src equals the compiled API origin, both ways, exactly', () => {
  it('fails on a stray origin when the bundle calls none', () => {
    const built = { indexHtml: html({ csp: policy('https://stray.invalid') }), scripts: [bundle()] };
    expect(checkBuild(built).join('\n')).toMatch(/differs/);
  });

  it('fails when the bundle calls an origin the policy omits', () => {
    expect(checkBuild({ indexHtml: html(), scripts: [withApi()] }).join('\n')).toMatch(/differs/);
  });

  it('fails when the bundle calls two origins', () => {
    const built = { indexHtml: html({ csp: policy(API) }), scripts: [withApi(), bundle('var o="https://other.invalid";')] };
    expect(checkBuild(built).join('\n')).toMatch(/more than one/);
  });

  it('never accepts a scheme or wildcard source in place of the origin', () => {
    for (const source of ['https:', 'http:', '*']) {
      const csp = policy(null).replace("connect-src 'self'", `connect-src 'self' ${source}`);
      expect(checkBuild({ indexHtml: html({ csp }), scripts: [withApi()] }).join('\n')).toMatch(/differs/);
    }
  });
});

describe('vite.config.ts — the meta-CSP plugin', () => {
  it('reduces the base URL to an origin, or to none', () => {
    expect(apiOriginFromBase(undefined)).toBeNull();
    expect(apiOriginFromBase('')).toBeNull();
    expect(apiOriginFromBase('   ')).toBeNull();
    expect(apiOriginFromBase('/backend')).toBeNull();
    expect(apiOriginFromBase(API)).toBe(API);
    expect(apiOriginFromBase(' https://API.Example.test:443/v1/ ')).toBe('https://api.example.test');
    for (const bad of ['//evil.invalid', 'ftp://files.invalid', 'api.example.test', 'javascript:alert(1)']) {
      expect(() => apiOriginFromBase(bad)).toThrow(/VITE_API_BASE_URL/);
    }
  });

  it('writes exactly the expected policy', () => {
    expect(contentSecurityPolicy(null)).toBe(policy(null));
    expect(contentSecurityPolicy(API)).toBe(policy(API));
  });

  it('injects into the real index.html template output that the guard accepts', () => {
    const template = readFileSync(join(WEB_ROOT, 'index.html'), 'utf8');
    for (const origin of [null, API]) {
      const built = injectSecurityMeta(template, contentSecurityPolicy(origin));
      const scripts = [origin ? withApi(origin) : bundle()];
      expect(checkBuild({ indexHtml: built, scripts })).toEqual([]);
      expect(built.indexOf('Content-Security-Policy')).toBeLessThan(built.indexOf('<link rel="icon"'));
    }
  });

  it('refuses a template without exactly one <meta charset>', () => {
    expect(() => injectSecurityMeta('<head></head>', policy(null))).toThrow(/exactly one/);
    const twice = '<meta charset="UTF-8" /><meta charset="UTF-8" />';
    expect(() => injectSecurityMeta(twice, policy(null))).toThrow(/exactly one/);
  });
});

describe('assert-csp-build.mjs — runs when invoked, even through a symlink', () => {
  it('runs its dist/ check when started through a symlinked path (no silent skip)', () => {
    const dir = mkdtempSync(join(tmpdir(), 'csp-guard-'));
    try {
      const link = join(dir, 'guard.mjs');
      symlinkSync(join(WEB_ROOT, 'scripts', 'assert-csp-build.mjs'), link);
      const run = spawnSync(process.execPath, [link], { encoding: 'utf8' });
      // Whatever dist/ holds (absent in CI at test time), the check itself must have run.
      expect(`${run.stdout}${run.stderr}`).toMatch(/\[csp-guard\]/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('package.json — the guard is part of the production build', () => {
  it('chains the CSP guard after vite build and the demo-credential guard', () => {
    const pkg = JSON.parse(readFileSync(join(WEB_ROOT, 'package.json'), 'utf8'));
    expect(pkg.scripts.build).toBe(
      'tsc -b && vite build && node scripts/assert-no-demo-credentials.mjs && node scripts/assert-csp-build.mjs',
    );
  });
});
