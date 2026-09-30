import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { createElement, Fragment, useEffect, useLayoutEffect, type ReactElement } from 'react';
import { useLocation } from 'react-router-dom';
import { expect, vi } from 'vitest';
import { ApiError } from '@/api/client';

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
 * `expectNoTokenLeak(token, where)`. A secret TYPED into a form (P6-UI-017's passwords)
 * uses `expectNoSecretLeak(secret, where)`, which also walks React state, and
 * `expectNoApiErrorInReactState(where)`.
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

/**
 * A cached error as the scan reads it: its text AND, for an ApiError, the whole payload
 * it carries in `detail` — a 422's `details[].input` can echo the submitted body
 * (P6-UI-017).
 */
function errorDump(error: unknown): unknown {
  if (error === null || error === undefined) return null;
  return error instanceof ApiError ? { message: error.message, detail: error.detail } : String(error);
}

function cacheDump(): string {
  if (!cache) return '';
  const queries = cache.getQueryCache().getAll().map((q) => ({
    key: q.queryKey,
    data: q.state.data,
    error: errorDump(q.state.error),
    meta: q.meta ?? null,
  }));
  const mutations = cache.getMutationCache().getAll().map((m) => ({
    key: m.options.mutationKey ?? null,
    variables: m.state.variables,
    data: m.state.data,
    context: m.state.context,
    error: errorDump(m.state.error),
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
  expectCleanSurfaces(contains, where);
}

/** The surfaces every scan shares: storage, the DOM and form values, the title, console, caches. */
function expectCleanSurfaces(contains: (text: string) => boolean, where: string, { inputs = true } = {}): void {
  expect(contains(storageDump()), `${where}: storage/cookies`).toBe(false);
  expect(contains(document.documentElement.outerHTML), `${where}: DOM`).toBe(false);
  if (inputs) expect(contains(formValues()), `${where}: form values`).toBe(false);
  expect(contains(document.title), `${where}: document.title`).toBe(false);
  expect(consoleCalls.filter(contains), `${where}: console`).toEqual([]);
  expect(contains(cacheDump()), `${where}: React Query cache`).toBe(false);
}

// ---- Secrets typed into a form (P6-UI-017: the change-password dialog) --------------
//
// A typed password never arrives in a link, so there is no opening history write to
// exempt: EVERY history write is scanned. React state is scanned as well (see
// `scanReactState`).

/**
 * `secret` (raw or URL-encoded) appears on none of the surfaces: the address bar, history
 * state and every history write, storage and cookies, the DOM (text, attributes, input
 * values, title), the console, React Query's query and mutation caches (errors with
 * their payload included) and any mounted component's state. With `formOpen`, the form
 * the secret was typed into is still mounted: its inputs and its own field state hold
 * the secret by design, so those two surfaces are left out.
 */
export function expectNoSecretLeak(secret: string, where: string, { formOpen = false } = {}): void {
  // Liveness: the history spy saw renderProductionApp's own write (it was installed before
  // the render) and the cache probe is mounted.
  expect(historyCalls.length, `${where}: history spy live`).toBeGreaterThan(0);
  expect(cache, `${where}: cache probe mounted`).not.toBeNull();
  const encoded = encodeURIComponent(secret);
  const contains = (text: string) => text.includes(secret) || text.includes(encoded);
  expect(contains(window.location.href), `${where}: address bar`).toBe(false);
  expect(contains(JSON.stringify(window.history.state ?? null)), `${where}: history.state`).toBe(false);
  expect(historyCalls.filter((c) => contains(c.url) || contains(c.state)), `${where}: history writes`).toEqual([]);
  expectCleanSurfaces(contains, where, { inputs: !formOpen });
  if (!formOpen) {
    const scan = scanReactState(secret);
    expect(scan.hooks, `${where}: React state walk reached the components`).toBeGreaterThan(0);
    expect(scan.secretHits, `${where}: React state`).toBe(0);
  }
}

/** No mounted component holds an ApiError (whose `detail` is the whole response payload). */
export function expectNoApiErrorInReactState(where: string): void {
  const scan = scanReactState();
  expect(scan.hooks, `${where}: React state walk reached the components`).toBeGreaterThan(0);
  expect(scan.apiErrors, `${where}: ApiError held in React state`).toBe(0);
}

interface FiberLike {
  tag: number;
  memoizedState: unknown;
  stateNode: unknown;
  child: FiberLike | null;
  sibling: FiberLike | null;
}

// React work tags whose memoizedState is a hook list (function components) or the state
// object (class components).
const FUNCTION_COMPONENT = 0;
const CLASS_COMPONENT = 1;
const FORWARD_REF = 11;
const SIMPLE_MEMO_COMPONENT = 15;
/** Upper bound on values visited; exceeding it fails loudly rather than scanning partially. */
const SCAN_BUDGET = 500_000;

/** The current tree of every React root rendered into the page (RTL renders under body). */
function mountedFibers(): FiberLike[] {
  const out: FiberLike[] = [];
  for (const element of Array.from(document.body.children)) {
    for (const key of Object.keys(element)) {
      if (!key.startsWith('__reactContainer$')) continue;
      const hostRoot = (element as unknown as Record<string, FiberLike>)[key]!;
      // The container keeps the root fiber it was created with; the FiberRoot knows which
      // of the two alternates is current.
      const current = (hostRoot.stateNode as { current?: FiberLike } | null)?.current ?? hostRoot;
      const stack: FiberLike[] = current.child ? [current.child] : [];
      while (stack.length) {
        const fiber = stack.pop()!;
        out.push(fiber);
        if (fiber.sibling) stack.push(fiber.sibling);
        if (fiber.child) stack.push(fiber.child);
      }
    }
  }
  return out;
}

function isFiber(value: object): boolean {
  return 'stateNode' in value && 'return' in value && 'tag' in value && 'memoizedProps' in value;
}

/**
 * Walk the state of every mounted component — each hook's value (state, reducer, ref,
 * memo) and class state — and everything reachable from it except functions, DOM nodes
 * and fibers. Counts the ApiError instances held and the strings containing `secret`.
 */
export function scanReactState(secret?: string): { hooks: number; apiErrors: number; secretHits: number } {
  let hooks = 0;
  let apiErrors = 0;
  let secretHits = 0;
  const stack: unknown[] = [];
  for (const fiber of mountedFibers()) {
    if (fiber.tag === CLASS_COMPONENT) {
      hooks += 1;
      stack.push(fiber.memoizedState);
    } else if (fiber.tag === FUNCTION_COMPONENT || fiber.tag === FORWARD_REF || fiber.tag === SIMPLE_MEMO_COMPONENT) {
      let hook = fiber.memoizedState as { memoizedState?: unknown; next?: unknown } | null;
      while (hook && typeof hook === 'object' && 'next' in hook) {
        hooks += 1;
        stack.push(hook.memoizedState);
        hook = hook.next as typeof hook;
      }
    }
  }
  const seen = new WeakSet<object>();
  let budget = SCAN_BUDGET;
  while (stack.length) {
    const value = stack.pop();
    budget -= 1;
    if (budget < 0) throw new Error('scanReactState: budget exhausted; the scan would be partial');
    if (typeof value === 'string') {
      if (secret && (value.includes(secret) || value.includes(encodeURIComponent(secret)))) secretHits += 1;
      continue;
    }
    if (!value || typeof value !== 'object' || seen.has(value)) continue;
    seen.add(value);
    if (value instanceof ApiError) apiErrors += 1;
    if (value instanceof Node || isFiber(value) || ArrayBuffer.isView(value)) continue;
    // An Error's own message is not enumerable.
    if (value instanceof Error) stack.push(value.message);
    if (value instanceof Map) {
      for (const [k, v] of value) stack.push(k, v);
      continue;
    }
    if (value instanceof Set) {
      for (const v of value) stack.push(v);
      continue;
    }
    for (const key of Object.keys(value)) {
      try {
        stack.push((value as Record<string, unknown>)[key]);
      } catch {
        // An own getter that throws holds nothing to scan.
      }
    }
  }
  return { hooks, apiErrors, secretHits };
}
