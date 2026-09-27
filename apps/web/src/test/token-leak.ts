import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { createElement, Fragment, useEffect, useLayoutEffect, type ReactElement } from 'react';
import { useLocation } from 'react-router-dom';
import { expect, vi } from 'vitest';

/**
 * Leak instruments for a page that holds a `#token=` link token in memory
 * (6B-4B: `/reset-password`, `/verify-email`). Modelled on the invite page's own
 * instruments (pages/invite/__tests__/invite-page.test.tsx), which stay where they
 * are. Every surface a page could leak a token to is scanned for the raw and the
 * URL-encoded token: the address bar, history state and every history write, local
 * and session storage, cookies, the DOM (text, attributes, form values, the
 * document title), the console, and React Query's query AND mutation caches.
 *
 * Use: `resetLeakInstruments()` in `beforeEach`, `installLeakInstruments()` before
 * rendering, `leakProbes()` as `renderProductionApp`'s `probe`, then
 * `expectNoTokenLeak(token, where)`.
 */

interface HistoryCall {
  kind: 'push' | 'replace';
  url: string;
  state: string;
}

let cache: QueryClient | null = null;
let historyCalls: HistoryCall[] = [];
let consoleCalls: string[] = [];
let hrefAtFirstLayout: string | null = null;

function CacheProbe() {
  const qc = useQueryClient();
  useEffect(() => {
    cache = qc;
  }, [qc]);
  return null;
}

function RouterHashProbe() {
  return createElement('span', { 'data-testid': 'leak-router-hash' }, useLocation().hash);
}

// Layout effects commit depth-first, so this sibling of <App /> runs after every
// layout effect inside the page: it sees the address bar as the first paint will.
function LayoutSampler() {
  useLayoutEffect(() => {
    hrefAtFirstLayout ??= window.location.href;
  }, []);
  return null;
}

/** The probes, for `renderProductionApp(…, { probe: leakProbes() })`. */
export function leakProbes(): ReactElement {
  return createElement(
    Fragment,
    null,
    createElement(CacheProbe),
    createElement(RouterHashProbe),
    createElement(LayoutSampler),
  );
}

/** Forget everything a previous test measured (call in `beforeEach`). */
export function resetLeakInstruments(): void {
  cache = null;
  historyCalls = [];
  consoleCalls = [];
  hrefAtFirstLayout = null;
}

/** Spy on history writes and on every console level; both pass through. */
export function installLeakInstruments(): void {
  historyCalls = [];
  consoleCalls = [];
  const push = window.history.pushState.bind(window.history);
  const replace = window.history.replaceState.bind(window.history);
  vi.spyOn(window.history, 'pushState').mockImplementation((state, unused, url) => {
    historyCalls.push({ kind: 'push', url: String(url ?? ''), state: JSON.stringify(state ?? null) });
    push(state, unused, url);
  });
  vi.spyOn(window.history, 'replaceState').mockImplementation((state, unused, url) => {
    historyCalls.push({ kind: 'replace', url: String(url ?? ''), state: JSON.stringify(state ?? null) });
    replace(state, unused, url);
  });
  for (const level of ['log', 'info', 'warn', 'error', 'debug'] as const) {
    vi.spyOn(console, level).mockImplementation((...args: unknown[]) => {
      consoleCalls.push(args.map((a) => (a instanceof Error ? `${a.message} ${a.stack}` : safeJson(a))).join(' '));
    });
  }
}

/** The address bar as the first paint saw it (null before the probe mounted). */
export const hrefAtFirstPaint = (): string | null => hrefAtFirstLayout;

/** The QueryClient the page ran under, once the probe has mounted. */
export const probedQueryClient = (): QueryClient | null => cache;

/** Every history write the instruments saw, oldest first. */
export const historyWrites = (): readonly HistoryCall[] => historyCalls;

function safeJson(value: unknown): string {
  try {
    return typeof value === 'string' ? value : JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function storageDump(): string {
  const out: string[] = [];
  for (const store of [localStorage, sessionStorage]) {
    for (let i = 0; i < store.length; i += 1) {
      const key = store.key(i)!;
      out.push(`${key}=${store.getItem(key)}`);
    }
  }
  return `${out.join('\n')}\ncookie=${document.cookie}`;
}

function cacheDump(): string {
  if (!cache) return '';
  const queries = cache.getQueryCache().getAll().map((q) => ({
    key: q.queryKey,
    data: q.state.data,
    error: q.state.error ? String(q.state.error) : null,
    meta: q.meta ?? null,
  }));
  const mutations = cache.getMutationCache().getAll().map((m) => ({
    key: m.options.mutationKey ?? null,
    variables: m.state.variables,
    data: m.state.data,
    context: m.state.context,
    meta: m.meta ?? null,
  }));
  return JSON.stringify({ queries, mutations });
}

function formValues(): string {
  return Array.from(document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>('input, textarea'))
    .map((el) => el.value)
    .join('\n');
}

/**
 * The token appears on none of the surfaces a page could leak it to. The single
 * write that OPENED the link (renderProductionApp's own, state null) is the visitor
 * arriving; every later history write is the page's, and none may carry it.
 */
export function expectNoTokenLeak(token: string, where: string): void {
  // Liveness: the instruments are attached (the page's own scrub is a history write)
  // and the cache probe is mounted, so a clean result is a measurement, not a blind spot.
  expect(historyCalls.length, `${where}: history spy live`).toBeGreaterThan(1);
  expect(cache, `${where}: cache probe mounted`).not.toBeNull();
  const encoded = encodeURIComponent(token);
  const contains = (text: string) => text.includes(token) || text.includes(encoded);
  expect(contains(window.location.href), `${where}: address bar`).toBe(false);
  expect(contains(JSON.stringify(window.history.state ?? null)), `${where}: history.state`).toBe(false);
  const opening = historyCalls.findIndex((c) => c.url.endsWith(`#token=${encoded}`) && c.state === 'null');
  const leakedHistory = historyCalls.filter((c, i) => i !== opening && (contains(c.url) || contains(c.state)));
  expect(leakedHistory, `${where}: history writes`).toEqual([]);
  expect(contains(storageDump()), `${where}: storage/cookies`).toBe(false);
  expect(contains(document.documentElement.outerHTML), `${where}: DOM`).toBe(false);
  expect(contains(formValues()), `${where}: form values`).toBe(false);
  expect(contains(document.title), `${where}: document.title`).toBe(false);
  expect(consoleCalls.filter(contains), `${where}: console`).toEqual([]);
  expect(contains(cacheDump()), `${where}: React Query cache`).toBe(false);
}
