/// <reference types="vitest/config" />
import { fileURLToPath, URL } from 'node:url';
import react from '@vitejs/plugin-react';
import type { Plugin } from 'vite';
import { defineConfig } from 'vitest/config';

/**
 * The origin the SPA may call, from the base URL the bundle is built with (P6-AUTH-5).
 * Empty (dev proxy, CI) or a same-origin path such as "/backend" needs only 'self';
 * an absolute http(s) URL contributes its WHATWG origin (lowercase host, default port
 * dropped). Anything else — "//host", another scheme, a bare host — fails the build.
 */
export function apiOriginFromBase(raw: string | undefined): string | null {
  const base = (raw ?? '').trim();
  if (base === '') return null;
  if (base.startsWith('/') && !base.startsWith('//')) return null;
  if (/^https?:\/\//i.test(base)) return new URL(base).origin;
  throw new Error(
    `VITE_API_BASE_URL must be empty, a same-origin path such as /backend, or an ` +
      `absolute http(s) URL; got ${JSON.stringify(base)}`,
  );
}

/**
 * The SPA's Content-Security-Policy, delivered as a <meta> tag until the edge can send
 * response headers (P6-INF-19). A <meta> policy ignores frame-ancestors, so it is absent.
 * 'unsafe-inline' is for style elements only: Radix's scroll lock (react-style-singleton)
 * injects a <style> with runtime-computed text, and a static host cannot mint nonces.
 * scripts/assert-csp-build.mjs checks the built output against its own copy of this policy.
 */
export function contentSecurityPolicy(apiOrigin: string | null): string {
  const connect = apiOrigin ? `'self' ${apiOrigin}` : "'self'";
  return [
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self'",
    `connect-src ${connect}`,
    "frame-src 'none'",
    "worker-src 'none'",
    "media-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "manifest-src 'none'",
  ].join('; ');
}

/**
 * Splice the policy and referrer <meta> tags in right after <meta charset>: before the
 * favicon link and the entry script and stylesheet (a <meta> policy governs only what
 * follows it), while the charset declaration stays within the first 1024 bytes. Vite's
 * `injectTo` positions cannot express "after this tag", hence the string splice.
 */
export function injectSecurityMeta(html: string, policy: string): string {
  const charset = '<meta charset="UTF-8" />';
  const at = html.indexOf(charset);
  if (at === -1 || html.indexOf(charset, at + charset.length) !== -1) {
    throw new Error('index.html must contain exactly one <meta charset="UTF-8" />');
  }
  const end = at + charset.length;
  const tags =
    `\n    <meta http-equiv="Content-Security-Policy" content="${policy}" />` +
    '\n    <meta name="referrer" content="no-referrer" />';
  return html.slice(0, end) + tags + html.slice(end);
}

/** Production builds only: the dev server's React-refresh preamble is an inline script. */
function securityMeta(): Plugin {
  let policy = '';
  return {
    name: 'signalnest-security-meta',
    apply: 'build',
    configResolved(config) {
      // Vite's resolved env — the values import.meta.env is compiled with, including
      // .env files — never process.env, which does not carry .env values here.
      policy = contentSecurityPolicy(apiOriginFromBase(config.env.VITE_API_BASE_URL));
    },
    transformIndexHtml(html) {
      return injectSecurityMeta(html, policy);
    },
  };
}

// The API base URL is proxied in dev so the browser never needs the backend origin.
// In production the app reads VITE_API_BASE_URL (see src/api/config.ts).
export default defineConfig({
  plugins: [react(), securityMeta()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/health': {
        target: process.env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  test: {
    globals: true,
    css: false,
    restoreMocks: true,
    projects: [
      {
        extends: true,
        test: {
          name: 'web',
          environment: 'jsdom',
          setupFiles: ['./src/test/setup.ts'],
          include: ['src/**/*.{test,spec}.?(c|m)[jt]s?(x)'],
        },
      },
      {
        // Tests of the Node build scripts (scripts/*.mjs). Node, not jsdom: they import
        // this config, whose plugins load Vite/esbuild, which refuses jsdom's TextEncoder;
        // and the browser setup file (window, MSW) is irrelevant to them.
        extends: true,
        test: {
          name: 'build-scripts',
          environment: 'node',
          include: ['scripts/**/*.test.mjs'],
        },
      },
    ],
  },
});
