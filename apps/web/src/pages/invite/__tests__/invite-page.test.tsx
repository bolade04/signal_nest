import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { act, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { useEffect, useLayoutEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '@/App';
import { ApiError, setAuthToken } from '@/api/client';
import { API_PREFIX } from '@/api/config';
import * as api from '@/api/endpoints';
import type { InvitationRole } from '@/api/types';
import { useAuth } from '@/auth/AuthContext';
import {
  apiErrorCode,
  buildInviteLink,
  fragmentHasInviteToken,
  invitationErrorCode,
  isPlausibleInviteToken,
  MAX_INVITE_TOKEN_LENGTH,
  parseInviteFragment,
} from '@/pages/invite/invite-token';
import { orgModel } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp, renderProductionApp } from '@/test/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';

/**
 * The invitee's side of P6-AUTH-1: the `/invite#token=…` page (§10, §11, §20–§24,
 * §40–§43, §48–§50, §74, §81).
 *
 * Everything renders in the main.tsx tree (StrictMode, BrowserRouter, the production
 * QueryClient) against the stateful backend model, so the address bar is jsdom's
 * real one and a StrictMode remount happens for real. Invitations are created
 * through the real API client as the organization's owner — never inserted — and
 * every token the model hands out is scanned for on every surface the page could
 * leak it to.
 */

const P = (path: string) => `*${API_PREFIX}${path}`;
const ORIGIN = 'http://localhost:3000';

// ---- fixtures ---------------------------------------------------------------

async function createInvitation(email: string, role: InvitationRole, asToken = 'test-token') {
  setAuthToken(asToken);
  try {
    return await api.createInvitation('org-1', { email, role });
  } finally {
    setAuthToken(null);
  }
}

function addOtherOrganization() {
  orgModel.addOrganization({ id: 'org-b', name: 'Bayside Bakery', slug: 'bayside' }, [
    { id: 'ws-b', name: 'Bayside Workspace', slug: 'bayside-ws', onboarding_completed: true, created_at: '2026-01-01T00:00:00Z' },
  ]);
}

function addExistingUser(id: string, email: string, name: string, password: string) {
  orgModel.addUser({ id, email, full_name: name, password });
}

// ---- probes and leak instruments -------------------------------------------

let cache: QueryClient | null = null;
function CacheProbe() {
  const qc = useQueryClient();
  useEffect(() => {
    cache = qc;
  }, [qc]);
  return null;
}
function RouterHashProbe() {
  return <span data-testid="m4-router-hash">{useLocation().hash}</span>;
}
// Layout effects commit depth-first, so this sibling of <App /> runs after every
// layout effect inside the invite page: it sees the address bar as the first paint will.
let hrefAtFirstLayout: string | null = null;
function LayoutSampler() {
  useLayoutEffect(() => {
    hrefAtFirstLayout ??= window.location.href;
  }, []);
  return null;
}
const PROBES = (
  <>
    <CacheProbe />
    <RouterHashProbe />
  </>
);

interface HistoryCall {
  kind: 'push' | 'replace';
  url: string;
  state: string;
}
let historyCalls: HistoryCall[] = [];
let consoleCalls: string[] = [];

function installInstruments() {
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

/**
 * The token appears on none of the surfaces a page could leak it to. The initial
 * address-bar write that opens the link (the test's own `replaceState` before
 * render) is the one history entry allowed to carry it.
 */
function expectNoLeak(token: string, where: string, openingWrite?: number) {
  // Liveness: the instruments are attached (the page's own scrub is a history write)
  // and the cache probe is mounted, so a clean result is a measurement, not a blind spot.
  expect(historyCalls.length, `${where}: history spy live`).toBeGreaterThan(1);
  expect(cache, `${where}: cache probe mounted`).not.toBeNull();
  const encoded = encodeURIComponent(token);
  const contains = (text: string) => text.includes(token) || text.includes(encoded);
  expect(contains(window.location.href), `${where}: address bar`).toBe(false);
  expect(contains(JSON.stringify(window.history.state ?? null)), `${where}: history.state`).toBe(false);
  // The single write that OPENED the link (renderProductionApp's own, state null) is
  // the visitor arriving; every later write is the page's, and none may carry it.
  // (Or, for an in-app navigation to the link, the write the test itself made.)
  const opening = openingWrite ?? historyCalls.findIndex((c) => c.url.endsWith(`#token=${token}`) && c.state === 'null');
  const leakedHistory = historyCalls.filter((c, i) => i !== opening && (contains(c.url) || contains(c.state)));
  expect(leakedHistory, `${where}: history writes`).toEqual([]);
  expect(contains(storageDump()), `${where}: storage/cookies`).toBe(false);
  expect(contains(document.documentElement.outerHTML), `${where}: DOM`).toBe(false);
  expect(consoleCalls.filter(contains), `${where}: console`).toEqual([]);
  expect(contains(cacheDump()), `${where}: React Query cache`).toBe(false);
}

const previewBodies = () =>
  orgModel
    .requests()
    .filter((r) => r.path.endsWith('/auth/invitations/preview'))
    .map((r) => r.body as Record<string, unknown>);
const count = (suffix: string) => orgModel.requests().filter((r) => r.path.endsWith(suffix)).length;

// Every model request and URL is checked by the model itself.
beforeEach(() => {
  orgModel.reset();
  cache = null;
  hrefAtFirstLayout = null;
  // A test that forgets installInstruments() then fails the leak scan's liveness check.
  historyCalls = [];
  consoleCalls = [];
});

afterEach(() => {
  vi.restoreAllMocks();
  expect(orgModel.violations()).toEqual([]);
  window.history.replaceState(null, '', '/');
});

// ---- §10 / §42: capture, then scrub before anything else --------------------

describe('the token leaves the address bar before any request (§10, §42)', () => {
  it('scrubs the fragment, yet previews with the captured value', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    let hrefWhenPreviewed: string | null = null;
    server.use(
      http.post(P('/auth/invitations/preview'), () => {
        hrefWhenPreviewed ??= window.location.href;
        // Fall through to the model's handler.
        return undefined;
      }),
    );
    installInstruments();

    const screen = renderProductionApp(<App />, `/invite#token=${token}`, {
      probe: (
        <>
          {PROBES}
          <LayoutSampler />
        </>
      ),
    });

    expect(await screen.findByText(/create your account to join demo org/i)).toBeInTheDocument();
    // Scrubbed before the first paint, not merely before the first request.
    expect(hrefAtFirstLayout).toBe(`${ORIGIN}/invite`);
    expect(window.location.href).toBe(`${ORIGIN}/invite`);
    expect(window.location.hash).toBe('');
    // The router's own copy of the URL is scrubbed too, not just the address bar.
    expect(screen.getByTestId('m4-router-hash')).toHaveTextContent(/^$/);
    // The scrub happened BEFORE the first request, not merely before the assertion.
    expect(hrefWhenPreviewed).toBe(`${ORIGIN}/invite`);
    // …and the in-memory value is what the preview used, in the JSON body only.
    expect(previewBodies().length).toBeGreaterThan(0);
    for (const body of previewBodies()) expect(body).toEqual({ token });
    expectNoLeak(token, 'after preview');
  });

  it('survives the StrictMode remount: one token, one valid preview, no "invalid link"', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    expect(await screen.findByText(/create your account to join demo org/i)).toBeInTheDocument();
    expect(screen.queryByText(/this invitation link is invalid/i)).not.toBeInTheDocument();
    expect(new Set(previewBodies().map((b) => b.token))).toEqual(new Set([token]));
  });

  it('never resurrects a scrubbed token: re-renders, navigating away and back', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    let nav: ReturnType<typeof useNavigate> | null = null;
    function NavProbe() {
      const navigate = useNavigate();
      useEffect(() => {
        nav = navigate;
      }, [navigate]);
      return null;
    }
    installInstruments();
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, {
      probe: (
        <>
          {PROBES}
          <NavProbe />
        </>
      ),
    });
    await screen.findByText(/create your account to join demo org/i);
    await waitFor(() => expect(screen.getByTestId('m4-router-hash')).toHaveTextContent(/^$/));
    const settledPreviews = count('/auth/invitations/preview');

    // Many re-renders (every keystroke) adopt nothing and preview nothing again.
    await screen.user.type(screen.getByLabelText(/full name/i), 'Nia New');
    expect(count('/auth/invitations/preview')).toBe(settledPreviews);
    expectNoLeak(token, 'after re-renders');

    // Away and back: the /invite history entry was scrubbed in place, so Back finds no token.
    act(() => nav!('/sign-in'));
    await screen.findByRole('button', { name: /^sign in$/i });
    act(() => nav!(-1));
    expect(await screen.findByRole('heading', { name: /this invitation link is invalid/i })).toBeInTheDocument();
    expect(window.location.pathname).toBe('/invite');
    expect(count('/auth/invitations/preview')).toBe(settledPreviews);
    expectNoLeak(token, 'after away-and-back');
  });

  it('adopts a second link opened in the same tab (fragment-only navigation) and scrubs it too', async () => {
    addOtherOrganization();
    const first = await createInvitation('nia.new@example.com', 'marketer');
    setAuthToken('test-token');
    orgModel.setMembership('org-b', 'user-1', 'owner');
    const second = await api.createInvitation('org-b', { email: 'nia.new@example.com', role: 'viewer' });
    setAuthToken(null);
    installInstruments();
    const screen = renderProductionApp(<App />, `/invite#token=${first.token}`, { probe: PROBES });
    await screen.findByText(/create your account to join demo org/i);

    // The visitor pastes the second link into the address bar of the same tab.
    act(() => {
      window.location.hash = `#token=${second.token}`;
    });
    expect(await screen.findByText(/create your account to join bayside bakery/i)).toBeInTheDocument();
    await waitFor(() => expect(window.location.hash).toBe(''));
    await waitFor(() => expect(screen.getByTestId('m4-router-hash')).toHaveTextContent(/^$/));
    const tokensAfterSwitch = previewBodies().slice(-1).map((b) => b.token);
    expect(tokensAfterSwitch).toEqual([second.token]);
    // The first token is not adopted back by any later render.
    await screen.user.type(screen.getByLabelText(/full name/i), 'Nia');
    expect(previewBodies().slice(-1).map((b) => b.token)).toEqual([second.token]);
    expectNoLeak(first.token, 'after the second link');
    expectNoLeak(second.token, 'after the second link');
  });

  it('treats a link without a token as invalid without asking the server', async () => {
    const screen = renderProductionApp(<App />, '/invite');
    expect(await screen.findByRole('heading', { name: /this invitation link is invalid/i })).toBeInTheDocument();
    expect(count('/auth/invitations/preview')).toBe(0);
  });
});

// ---- §20 / §74 / §41 / §43: new user, full lifetime ---------------------------

describe('a new user registers through the invitation (§20, §43, §74)', () => {
  it('shows the preview, sends only token / full_name / password, and never leaks the token', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    // Hold the first preview so the "previewing" state is observable.
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post(P('/auth/invitations/preview'), async () => {
        await held;
        return undefined;
      }),
    );
    installInstruments();
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, { probe: PROBES });

    expect(await screen.findByText(/checking your invitation/i)).toBeInTheDocument();
    act(() => release());

    // Preview: organization, invited email, role, expiry (§20).
    await screen.findByText(/create your account to join demo org/i);
    const summary = screen.getByText('Invited email').closest('dl')!;
    expect(within(summary).getByText('Demo Org')).toBeInTheDocument();
    expect(within(summary).getByText('nia.new@example.com')).toBeInTheDocument();
    expect(within(summary).getByText('Marketer')).toBeInTheDocument();
    expect(within(summary).getByText('Expires')).toBeInTheDocument();
    // The email is shown locked; it is not an input the user could change.
    const email = screen.getByLabelText(/^email\b/i);
    expect(email).toHaveValue('nia.new@example.com');
    expect(email).toHaveAttribute('readonly');
    expectNoLeak(token, 'register form');

    await screen.user.type(screen.getByLabelText(/full name/i), 'Nia New');
    await screen.user.type(screen.getByLabelText(/^password\b/i), 'long enough password');
    // A signed-out visitor's tab holds no organization data at this point: the organization
    // queries exist (the app's disabled observers) but have never fetched. So nothing of any
    // organization can be served, reset or lost by the registration.
    const orgScoped = cache!
      .getQueryCache()
      .getAll()
      .filter((q) => q.queryKey[0] === 'organizations' || q.queryKey[0] === 'workspaces');
    // Positive instance of the scan: it does see the app's organization-list entry.
    expect(orgScoped.map((q) => JSON.stringify(q.queryKey))).toContain('["organizations"]');
    expect(
      orgScoped.map((q) => [JSON.stringify(q.queryKey), q.state.status, q.state.data ?? null, q.state.dataUpdatedAt]),
    ).toEqual(orgScoped.map((q) => [JSON.stringify(q.queryKey), 'pending', null, 0]));
    await screen.user.click(screen.getByRole('button', { name: /create account and join/i }));

    // SessionOut applied, joined organization entered.
    await waitFor(() => expect(window.location.pathname).toBe('/'));
    await waitFor(() => expect(localStorage.getItem('signalnest-active-org')).toBe('org-1'));
    const registered = orgModel.requests().filter((r) => r.path.endsWith('/auth/invitations/register'));
    expect(registered).toHaveLength(1);
    expect(Object.keys(registered[0]!.body as object).sort()).toEqual(['full_name', 'password', 'token']);
    expect((registered[0]!.body as { token: string }).token).toBe(token);
    const joined = orgModel.memberships().find((m) => m.organization_id === 'org-1' && m.role === 'marketer');
    expect(joined?.source).toBe('invitation');
    const session = localStorage.getItem('signalnest-token');
    expect(session).toMatch(/^access-/);

    // After the page is gone, nothing retained the token (§11 "only as long as needed").
    expectNoLeak(token, 'after registration');
  });

  it('does not resume the spent token when /invite is opened again', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    const first = renderProductionApp(<App />, `/invite#token=${token}`);
    await first.findByText(/create your account to join demo org/i);
    await first.user.type(first.getByLabelText(/full name/i), 'Nia New');
    await first.user.type(first.getByLabelText(/^password\b/i), 'long enough password');
    await first.user.click(first.getByRole('button', { name: /create account and join/i }));
    await waitFor(() => expect(window.location.pathname).toBe('/'));
    const previews = count('/auth/invitations/preview');
    first.unmount();

    const again = renderProductionApp(<App />, '/invite', { token: localStorage.getItem('signalnest-token') ?? undefined });
    expect(await again.findByRole('heading', { name: /this invitation link is invalid/i })).toBeInTheDocument();
    expect(count('/auth/invitations/preview')).toBe(previews);
  });
});

// ---- §21 / §40: existing, signed-in user --------------------------------------

describe('a signed-in invitee accepts explicitly (§21, §40, §74)', () => {
  it('shows Join / Role, accepts only on the click, and enters the joined organization', async () => {
    addOtherOrganization();
    addExistingUser('user-erin', 'erin@example.com', 'Erin Existing', 'erin password 1');
    orgModel.setMembership('org-b', 'user-erin', 'owner');
    const { token } = await createInvitation('erin@example.com', 'admin');
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post(P('/auth/invitations/accept'), async () => {
        await held;
        return undefined;
      }),
    );

    const screen = renderProductionApp(<App />, `/invite#token=${token}`, { token: orgModel.signIn('user-erin') });

    // Usable while signed in: not redirected away from /invite.
    expect(await screen.findByRole('heading', { name: /join demo org/i })).toBeInTheDocument();
    expect(window.location.pathname).toBe('/invite');
    expect(screen.getByText(/role:/i)).toHaveTextContent('Role: Admin');
    expect(count('/auth/invitations/accept')).toBe(0);

    await screen.user.click(screen.getByRole('button', { name: /accept invitation/i }));
    // "accepting" is a visible, disabled state — never a double submit.
    expect(await screen.findByRole('button', { name: /accepting/i })).toBeDisabled();
    act(() => release());

    await waitFor(() => expect(window.location.pathname).toBe('/'));
    expect(count('/auth/invitations/accept')).toBe(1);
    await waitFor(() => expect(localStorage.getItem('signalnest-active-org')).toBe('org-1'));
    expect(orgModel.memberships().find((m) => m.user_id === 'user-erin' && m.organization_id === 'org-1')?.source).toBe(
      'invitation',
    );
  });
});

// ---- §74 / §82: after acceptance ------------------------------------------------

describe('after acceptance the joined organization is entered, or the page says why not (§74, §82)', () => {
  function erinInvitedToDemoOrg() {
    addOtherOrganization();
    addExistingUser('user-erin', 'erin@example.com', 'Erin Existing', 'erin password 1');
    orgModel.setMembership('org-b', 'user-erin', 'owner');
    return createInvitation('erin@example.com', 'marketer');
  }

  it('shows "Invitation accepted" while it opens the organization, then goes in', async () => {
    const { token } = await erinInvitedToDemoOrg();
    let accepted = false;
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post(P('/auth/invitations/accept'), () => {
        accepted = true;
        return undefined;
      }),
      // Hold the organization-list refresh that follows acceptance.
      http.get(P('/organizations'), async () => {
        if (accepted) await held;
        return undefined;
      }),
    );
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, { token: orgModel.signIn('user-erin') });
    await screen.user.click(await screen.findByRole('button', { name: /accept invitation/i }));

    expect(await screen.findByRole('heading', { name: /invitation accepted/i })).toBeInTheDocument();
    expect(screen.getByText(/opening demo org/i)).toBeInTheDocument();
    act(() => release());
    await waitFor(() => expect(window.location.pathname).toBe('/'));
    await waitFor(() => expect(localStorage.getItem('signalnest-active-org')).toBe('org-1'));
  });

  it('never leaves the user in an unrelated organization under a success message', async () => {
    const { token } = await erinInvitedToDemoOrg();
    let accepted = false;
    let omitJoined = true;
    server.use(
      http.post(P('/auth/invitations/accept'), () => {
        accepted = true;
        return undefined;
      }),
      // A list that (for now) lacks the joined organization, as a lagging replica might.
      http.get(P('/organizations'), () =>
        accepted && omitJoined ? HttpResponse.json([{ id: 'org-b', name: 'Bayside Bakery', slug: 'bayside' }]) : undefined,
      ),
    );
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, { token: orgModel.signIn('user-erin') });
    await screen.user.click(await screen.findByRole('button', { name: /accept invitation/i }));

    expect(await screen.findByText(/demo org could not be opened automatically/i)).toBeInTheDocument();
    expect(window.location.pathname).toBe('/invite');
    expect(localStorage.getItem('signalnest-active-org')).not.toBe('org-1');
    expect(screen.queryByText(/welcome to demo org/i)).not.toBeInTheDocument();

    omitJoined = false;
    await screen.user.click(screen.getByRole('button', { name: /open demo org/i }));
    await waitFor(() => expect(window.location.pathname).toBe('/'));
    await waitFor(() => expect(localStorage.getItem('signalnest-active-org')).toBe('org-1'));
  });
});

// ---- §82 / §80: WHICH organization is entered, and what acceptance may touch ----------

function ActiveProbe() {
  const { memberships } = useAuth();
  const { organizationId } = useWorkspace();
  const role = memberships.find((m) => m.organization_id === organizationId)?.role ?? '';
  return <span data-testid="m4-active">{`${organizationId ?? ''}:${role}`}</span>;
}
let navigateTo: ReturnType<typeof useNavigate> | null = null;
function NavigateProbe() {
  const navigate = useNavigate();
  useEffect(() => {
    navigateTo = navigate;
  }, [navigate]);
  return null;
}

describe('acceptance enters the JOINED organization and touches only its caches (§82, §80)', () => {
  it('selects the joined organization even when it is not first in the list or in the session', async () => {
    // Erin already belongs to Demo Org, which precedes the organization she joins
    // everywhere: GET /organizations and the session's memberships both list it first.
    orgModel.addOrganization({ id: 'org-z', name: 'Zeta Org', slug: 'zeta' }, [
      { id: 'ws-z', name: 'Zeta Workspace', slug: 'zeta-ws', onboarding_completed: true, created_at: '2026-01-01T00:00:00Z' },
    ]);
    addExistingUser('user-zoe', 'zoe@example.com', 'Zoe Zeta', 'zoe password 12');
    addExistingUser('user-erin', 'erin@example.com', 'Erin Existing', 'erin password 1');
    orgModel.setMembership('org-z', 'user-zoe', 'owner');
    orgModel.setMembership('org-1', 'user-erin', 'viewer');
    setAuthToken(orgModel.signIn('user-zoe'));
    let token: string;
    try {
      token = (await api.createInvitation('org-z', { email: 'erin@example.com', role: 'marketer' })).token;
    } finally {
      setAuthToken(null);
    }

    const screen = renderProductionApp(<App />, `/invite#token=${token}`, {
      token: orgModel.signIn('user-erin'),
      probe: (
        <>
          {PROBES}
          <ActiveProbe />
        </>
      ),
    });
    await screen.user.click(await screen.findByRole('button', { name: /accept invitation/i }));
    await waitFor(() => expect(window.location.pathname).toBe('/'));

    // Precondition, measured rather than assumed: the joined organization is NOT first.
    const listed = (cache!.getQueryData(['organizations']) as { id: string }[]).map((o) => o.id);
    expect(listed).toEqual(['org-1', 'org-z']);
    expect(orgModel.memberships().filter((m) => m.user_id === 'user-erin').map((m) => m.organization_id)).toEqual([
      'org-1',
      'org-z',
    ]);
    // The joined organization is the active one, stays so, and its workspace loads.
    await waitFor(() => expect(screen.getByTestId('m4-active')).toHaveTextContent('org-z:marketer'));
    await screen.findAllByText('Zeta Workspace');
    expect(localStorage.getItem('signalnest-active-org')).toBe('org-z');
    expect(screen.getByTestId('m4-active')).toHaveTextContent('org-z:marketer');
  });

  it('never cancels another organization\'s in-flight read when it refreshes the organization list', async () => {
    addOtherOrganization();
    addExistingUser('user-erin', 'erin@example.com', 'Erin Existing', 'erin password 1');
    orgModel.setMembership('org-b', 'user-erin', 'owner');
    const { token } = await createInvitation('erin@example.com', 'marketer');
    // Erin's own organization's workspaces are still loading when she accepts…
    let releaseWorkspaces!: () => void;
    const slow = new Promise<void>((resolve) => {
      releaseWorkspaces = resolve;
    });
    let workspaceReads = 0;
    // …and (as a lagging replica might) the list read after accepting lacks the joined
    // organization, so she stays in org-b on the "could not be opened" state.
    let accepted = false;
    server.use(
      http.get(P('/organizations/org-b/workspaces'), async () => {
        workspaceReads += 1;
        await slow;
        return undefined;
      }),
      http.post(P('/auth/invitations/accept'), () => {
        accepted = true;
        return undefined;
      }),
      http.get(P('/organizations'), () =>
        accepted ? HttpResponse.json([{ id: 'org-b', name: 'Bayside Bakery', slug: 'bayside' }]) : undefined,
      ),
    );
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, {
      token: orgModel.signIn('user-erin'),
      probe: PROBES,
    });
    await waitFor(() => expect(workspaceReads).toBeGreaterThan(0));
    await screen.user.click(await screen.findByRole('button', { name: /accept invitation/i }));
    expect(await screen.findByText(/demo org could not be opened automatically/i)).toBeInTheDocument();

    // The page cancelled only the organization list (exact), so org-b's own read lands.
    act(() => releaseWorkspaces());
    await waitFor(() => expect(cache!.getQueryState(['organizations', 'org-b', 'workspaces'])?.status).toBe('success'));
    expect(cache!.getQueryData(['organizations', 'org-b', 'workspaces'])).toEqual([
      expect.objectContaining({ id: 'ws-b', name: 'Bayside Workspace' }),
    ]);
  });

  it('never lets an organization list requested before the join answer for after it', async () => {
    addOtherOrganization();
    addExistingUser('user-erin', 'erin@example.com', 'Erin Existing', 'erin password 1');
    orgModel.setMembership('org-b', 'user-erin', 'owner');
    const { token } = await createInvitation('erin@example.com', 'marketer');
    // The list this tab asked for BEFORE accepting is slow; its answer predates the join.
    let releaseFirst!: () => void;
    const firstHeld = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    let listReads = 0;
    server.use(
      http.get(P('/organizations'), async () => {
        listReads += 1;
        if (listReads > 1) return undefined;
        const beforeJoin = [{ id: 'org-b', name: 'Bayside Bakery', slug: 'bayside' }];
        await firstHeld;
        return HttpResponse.json(beforeJoin);
      }),
    );
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, {
      token: orgModel.signIn('user-erin'),
      probe: PROBES,
    });
    await waitFor(() => expect(listReads).toBe(1));
    await screen.user.click(await screen.findByRole('button', { name: /accept invitation/i }));
    try {
      // The pre-join request was cancelled and a fresh list read: she enters Demo Org
      // while the old request is still unanswered.
      await waitFor(() => expect(window.location.pathname).toBe('/'), { timeout: 2000 });
      await waitFor(() => expect(localStorage.getItem('signalnest-active-org')).toBe('org-1'));
      expect(listReads).toBeGreaterThan(1);
    } finally {
      act(() => releaseFirst());
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(screen.queryByText(/could not be opened automatically/i)).not.toBeInTheDocument();
    expect(localStorage.getItem('signalnest-active-org')).toBe('org-1');
  });

  it('a returning member re-invited in the same tab sees the joined organization fresh, not from cache', async () => {
    // Erin administers her own organization and USED to be a viewer in Demo Org.
    orgModel.addOrganization({ id: 'org-a', name: 'Erin Agency', slug: 'erin-agency' }, [
      { id: 'ws-a', name: 'Agency Workspace', slug: 'agency-ws', onboarding_completed: true, created_at: '2026-01-01T00:00:00Z' },
    ]);
    addExistingUser('user-erin', 'erin@example.com', 'Erin Existing', 'erin password 1');
    orgModel.setMembership('org-a', 'user-erin', 'admin');
    orgModel.setMembership('org-1', 'user-erin', 'viewer');
    const screen = renderProductionApp(<App />, '/settings', {
      token: orgModel.signIn('user-erin'),
      probe: (
        <>
          {PROBES}
          <ActiveProbe />
          <NavigateProbe />
        </>
      ),
    });
    await waitFor(() => expect(screen.getByTestId('m4-active')).toHaveTextContent('org-a:admin'));
    // She looks at Demo Org: its members (her as Viewer) and workspaces enter this tab's cache.
    const [orgSwitch] = screen.getAllByRole('combobox', { name: /^organization$/i });
    await screen.user.click(orgSwitch!);
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Demo Org' }));
    await waitFor(() => expect(screen.getByTestId('m4-active')).toHaveTextContent('org-1:viewer'));
    const table = await screen.findByRole('table', { name: /members of demo org/i });
    expect(within(within(table).getByText('Erin Existing').closest('tr')!).getByText('Viewer')).toBeInTheDocument();

    // Meanwhile, as Demo Org's owner (outside this tab): Erin is removed, a workspace is
    // added, and Erin is invited back as a Marketer.
    const asOwner = (path: string, method: string, body?: unknown) =>
      fetch(`${ORIGIN}${API_PREFIX}${path}`, {
        method,
        headers: { Authorization: 'Bearer test-token', 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    expect((await asOwner('/organizations/org-1/members/user-erin', 'DELETE')).status).toBe(204);
    expect((await asOwner('/organizations/org-1/workspaces', 'POST', { name: 'Growth Workspace' })).status).toBe(201);
    const invited = await asOwner('/organizations/org-1/invitations', 'POST', { email: 'erin@example.com', role: 'marketer' });
    const { token } = (await invited.json()) as { token: string };

    // Back in her own organization she hits a stale refusal (she was demoted there too),
    // which re-reads her session: Demo Org is no longer in it.
    await screen.user.click(screen.getAllByRole('combobox', { name: /^organization$/i })[0]!);
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Erin Agency' }));
    await waitFor(() => expect(screen.getByTestId('m4-active')).toHaveTextContent('org-a:admin'));
    orgModel.changeRoleBehindTheUi('org-a', 'user-erin', 'viewer');
    await screen.user.click(await screen.findByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'someone@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    await waitFor(() => expect(screen.getByTestId('m4-active')).toHaveTextContent('org-a:viewer'));

    // Precondition: Demo Org's entries from before are still cached and still fresh.
    expect(cache!.getQueryState(['organizations', 'org-1', 'members'])?.status).toBe('success');
    expect(cache!.getQueryState(['organizations', 'org-1', 'workspaces'])?.status).toBe('success');

    // She follows the new link in this tab and accepts.
    act(() => navigateTo!(`/invite#token=${token}`));
    await screen.user.click(await screen.findByRole('button', { name: /accept invitation/i }));
    await waitFor(() => expect(screen.getByTestId('m4-active')).toHaveTextContent('org-1:marketer'));
    await waitFor(() => expect(window.location.pathname).toBe('/'));

    // Demo Org's workspaces were re-read: the one added while she was away is there.
    await waitFor(() =>
      expect((cache!.getQueryData(['organizations', 'org-1', 'workspaces']) as { name: string }[]).map((w) => w.name)).toContain(
        'Growth Workspace',
      ),
    );
    // Demo Org's members were re-read: she is listed as what she holds now.
    const [settingsLink] = screen.getAllByRole('link', { name: /^settings$/i });
    await screen.user.click(settingsLink!);
    const members = await screen.findByRole('table', { name: /members of demo org/i });
    await waitFor(() =>
      expect(within(within(members).getByText('Erin Existing').closest('tr')!).getByText('Marketer')).toBeInTheDocument(),
    );
  });

  it('invalidates only the joined organization and the exact organization list, never the invitee\'s other organization', async () => {
    orgModel.addOrganization({ id: 'org-a', name: 'Erin Agency', slug: 'erin-agency' }, [
      { id: 'ws-a', name: 'Agency Workspace', slug: 'agency-ws', onboarding_completed: true, created_at: '2026-01-01T00:00:00Z' },
    ]);
    addExistingUser('user-erin', 'erin@example.com', 'Erin Existing', 'erin password 1');
    addExistingUser('user-abe', 'abe.agency@example.com', 'Abe Agency', 'abe password 12');
    orgModel.setMembership('org-a', 'user-erin', 'owner');
    orgModel.setMembership('org-a', 'user-abe', 'viewer');
    const { token } = await createInvitation('erin@example.com', 'marketer');

    // Erin works in her own organization first, so this tab caches org-a's members,
    // pending invitations and workspaces.
    installInstruments();
    const screen = renderProductionApp(<App />, '/settings', {
      token: orgModel.signIn('user-erin'),
      probe: (
        <>
          {PROBES}
          <ActiveProbe />
          <NavigateProbe />
        </>
      ),
    });
    await waitFor(() => expect(screen.getByTestId('m4-active')).toHaveTextContent('org-a:owner'));
    await screen.findByText('abe.agency@example.com');
    await waitFor(() => expect(orgModel.count('GET', /\/organizations\/org-a\/invitations$/)).toBeGreaterThan(0));
    for (const part of ['members', 'invitations', 'workspaces']) {
      expect(cache!.getQueryState(['organizations', 'org-a', part])?.status, part).toBe('success');
    }
    const before = {
      members: orgModel.count('GET', /\/organizations\/org-a\/members$/),
      invitations: orgModel.count('GET', /\/organizations\/org-a\/invitations$/),
      workspaces: orgModel.count('GET', /\/organizations\/org-a\/workspaces$/),
    };
    const snapshot = Object.fromEntries(
      ['members', 'invitations', 'workspaces'].map((part) => {
        const state = cache!.getQueryState(['organizations', 'org-a', part])!;
        return [part, { dataUpdatedAt: state.dataUpdatedAt, data: JSON.stringify(state.data) }];
      }),
    );

    // She follows the link inside the same tab (an in-app navigation keeps this cache).
    const openingWrite = historyCalls.length;
    act(() => navigateTo!(`/invite#token=${token}`));
    expect(historyCalls[openingWrite]?.url).toBe(`/invite#token=${token}`);
    await screen.user.click(await screen.findByRole('button', { name: /accept invitation/i }));
    await waitFor(() => expect(screen.getByTestId('m4-active')).toHaveTextContent('org-1:marketer'));
    await waitFor(() => expect(window.location.pathname).toBe('/'));

    // The other organization's caches were neither invalidated, reset, removed nor refetched:
    // the same data, from the same moment, still there after everything settled.
    await new Promise((resolve) => setTimeout(resolve, 100));
    for (const part of ['members', 'invitations', 'workspaces']) {
      const state = cache!.getQueryState(['organizations', 'org-a', part]);
      expect(state?.isInvalidated, `org-a ${part} invalidated`).toBe(false);
      expect(state?.status, `org-a ${part} reset or removed`).toBe('success');
      expect(state?.dataUpdatedAt, `org-a ${part} replaced`).toBe(snapshot[part]!.dataUpdatedAt);
      expect(JSON.stringify(state?.data), `org-a ${part} data`).toBe(snapshot[part]!.data);
    }
    // The page never added a history entry of its own: the scrub and the hand-over replace.
    expect(historyCalls.slice(openingWrite + 1).filter((c) => c.kind === 'push')).toEqual([]);
    expect(orgModel.count('GET', /\/organizations\/org-a\/members$/)).toBe(before.members);
    expect(orgModel.count('GET', /\/organizations\/org-a\/invitations$/)).toBe(before.invitations);
    expect(orgModel.count('GET', /\/organizations\/org-a\/workspaces$/)).toBe(before.workspaces);
    // The joined organization's workspaces were read fresh for the new membership.
    expect(orgModel.count('GET', /\/organizations\/org-1\/workspaces$/)).toBeGreaterThan(0);
    expectNoLeak(token, 'after an in-tab acceptance', openingWrite);
  });
});

// ---- failures leave the page usable (InvitePage catch / 401 / notice / pending) -------

describe('a failed step leaves the invite page usable, never stuck (§23, §24, §74)', () => {
  const STUMBLE = () =>
    HttpResponse.json({ error: { code: 'internal_error', message: 'The server stumbled.' } }, { status: 500 });

  function erinSignedInWithInvite() {
    addOtherOrganization();
    addExistingUser('user-erin', 'erin@example.com', 'Erin Existing', 'erin password 1');
    orgModel.setMembership('org-b', 'user-erin', 'owner');
    return createInvitation('erin@example.com', 'marketer');
  }

  it('a failed accept shows why and hands the button back (not stuck on "Accepting…")', async () => {
    const { token } = await erinSignedInWithInvite();
    let failures = 0;
    server.use(
      http.post(P('/auth/invitations/accept'), () => {
        if (failures > 0) return undefined;
        failures += 1;
        return STUMBLE();
      }),
    );
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, { token: orgModel.signIn('user-erin') });
    await screen.user.click(await screen.findByRole('button', { name: /accept invitation/i }));
    expect(await screen.findByText('The server stumbled.')).toBeInTheDocument();
    const again = screen.getByRole('button', { name: /^accept invitation$/i });
    expect(again).toBeEnabled();
    // …and it really works the second time.
    await screen.user.click(again);
    await waitFor(() => expect(window.location.pathname).toBe('/'));
  });

  it('a failed registration shows why and hands the form back', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    server.use(http.post(P('/auth/invitations/register'), STUMBLE));
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    await screen.findByText(/create your account to join demo org/i);
    await screen.user.type(screen.getByLabelText(/full name/i), 'Nia New');
    await screen.user.type(screen.getByLabelText(/^password\b/i), 'long enough password');
    await screen.user.click(screen.getByRole('button', { name: /create account and join/i }));
    expect(await screen.findByText('The server stumbled.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /create account and join/i })).toBeEnabled();
    expect(screen.getByLabelText(/full name/i)).toHaveValue('Nia New');
  });

  it('an accept refused because the session ended (401) returns to sign-in with an explanation', async () => {
    const { token } = await erinSignedInWithInvite();
    const erinToken = orgModel.signIn('user-erin');
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, { token: erinToken });
    await screen.findByRole('button', { name: /accept invitation/i });
    // Let the page's own background reads (organizations, workspaces) finish first, so the
    // accept itself is what meets the ended session.
    await waitFor(() => expect(orgModel.count('GET', /\/organizations\/org-b\/workspaces$/)).toBeGreaterThan(0));
    await new Promise((resolve) => setTimeout(resolve, 100));
    orgModel.expireSession(erinToken);
    await screen.user.click(screen.getByRole('button', { name: /accept invitation/i }));
    await waitFor(() => expect(orgModel.count('POST', /\/auth\/invitations\/accept$/)).toBe(1));
    expect(await screen.findByText(/your session has ended\. sign in again to accept the invitation\./i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^email\b/i)).toHaveValue('erin@example.com');
    expect(screen.getByRole('button', { name: /^sign in$/i })).toBeInTheDocument();
    expect(localStorage.getItem('signalnest-token')).toBeNull();
    expect(orgModel.memberships().some((m) => m.user_id === 'user-erin' && m.organization_id === 'org-1')).toBe(false);
  });

  it('a successful inline sign-in drops the "account already exists" notice for good', async () => {
    addOtherOrganization();
    addExistingUser('user-ola', 'ola@example.com', 'Ola Returning', 'ola password 12');
    orgModel.setMembership('org-b', 'user-ola', 'viewer');
    const { token } = await createInvitation('ola@example.com', 'reviewer');
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    await screen.findByText(/create your account to join demo org/i);
    await screen.user.type(screen.getByLabelText(/full name/i), 'Ola Returning');
    await screen.user.type(screen.getByLabelText(/^password\b/i), 'some new password');
    await screen.user.click(screen.getByRole('button', { name: /create account and join/i }));
    expect(await screen.findByText(/an account with this email address already exists/i)).toBeInTheDocument();

    // She signs in; right after, her new session is refused once (revoked meanwhile), which
    // signs her out and brings the sign-in form back — WITHOUT the old notice.
    let refusals = 0;
    server.use(
      http.get(P('/organizations'), ({ request }) => {
        if (refusals > 0 || !request.headers.get('authorization')?.includes('access-user-ola')) return undefined;
        refusals += 1;
        return HttpResponse.json({ error: { code: 'unauthorized', message: 'Session expired.' } }, { status: 401 });
      }),
    );
    await screen.user.type(screen.getByLabelText(/^password\b/i), 'ola password 12');
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
    await waitFor(() => expect(refusals).toBe(1));
    await waitFor(() => expect(localStorage.getItem('signalnest-token')).toBeNull());
    expect(await screen.findByRole('button', { name: /^sign in$/i })).toBeInTheDocument();
    expect(screen.queryByText(/an account with this email address already exists/i)).not.toBeInTheDocument();
  });

  it('after a joined-but-not-entered acceptance, a second link is offered fresh (not "Accepting…")', async () => {
    const { token } = await erinSignedInWithInvite();
    orgModel.addOrganization({ id: 'org-c', name: 'Cedar Collective', slug: 'cedar' });
    addExistingUser('user-cy', 'cy@example.com', 'Cy Cedar', 'cy password 123');
    orgModel.setMembership('org-c', 'user-cy', 'owner');
    setAuthToken(orgModel.signIn('user-cy'));
    let second: string;
    try {
      second = (await api.createInvitation('org-c', { email: 'erin@example.com', role: 'viewer' })).token;
    } finally {
      setAuthToken(null);
    }
    let accepted = false;
    server.use(
      http.post(P('/auth/invitations/accept'), () => {
        accepted = true;
        return undefined;
      }),
      // The list read after the first acceptance lags and lacks Demo Org once.
      http.get(P('/organizations'), () =>
        accepted ? HttpResponse.json([{ id: 'org-b', name: 'Bayside Bakery', slug: 'bayside' }]) : undefined,
      ),
    );
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, { token: orgModel.signIn('user-erin') });
    await screen.user.click(await screen.findByRole('button', { name: /accept invitation/i }));
    expect(await screen.findByText(/demo org could not be opened automatically/i)).toBeInTheDocument();

    act(() => {
      window.location.hash = `#token=${second}`;
    });
    expect(await screen.findByRole('heading', { name: /join cedar collective/i })).toBeInTheDocument();
    const button = screen.getByRole('button', { name: /^accept invitation$/i });
    expect(button).toBeEnabled();
  });
});

// ---- §22 / §48 / §81: wrong account ------------------------------------------

describe('a different signed-in account never accepts (§22, §48, §81)', () => {
  it('shows the mismatch, never calls accept, and signs the right account in to the joined organization', async () => {
    addOtherOrganization();
    addExistingUser('user-bob', 'bob@example.com', 'Bob Other', 'bob password 1');
    addExistingUser('user-alice', 'alice@example.com', 'Alice Invited', 'alice password 1');
    // Both belong to org-b, so the organization Bob leaves selected is a VALID one for
    // Alice too: only an explicit switch to the joined organization gets her there.
    orgModel.setMembership('org-b', 'user-bob', 'owner');
    orgModel.setMembership('org-b', 'user-alice', 'viewer');
    const { token } = await createInvitation('alice@example.com', 'marketer');
    installInstruments();

    const screen = renderProductionApp(<App />, `/invite#token=${token}`, {
      token: orgModel.signIn('user-bob'),
      probe: PROBES,
    });

    expect(
      await screen.findByText('This invitation is for alice@example.com, but you are signed in as bob@example.com.'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /accept invitation/i })).not.toBeInTheDocument();
    expect(count('/auth/invitations/accept')).toBe(0);
    // Bob's session resolved his organization and persisted it.
    await waitFor(() => expect(localStorage.getItem('signalnest-active-org')).toBe('org-b'));

    // The existing sign-out path, then the invited account signs in right here.
    await screen.user.click(screen.getByRole('button', { name: /^sign out$/i }));
    expect(count('/auth/invitations/accept')).toBe(0);
    const password = await screen.findByLabelText(/^password\b/i);
    expect(screen.getByLabelText(/^email\b/i)).toHaveValue('alice@example.com');
    await screen.user.type(password, 'alice password 1');
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));

    await screen.findByRole('heading', { name: /join demo org/i });
    expect(count('/auth/invitations/accept')).toBe(0);
    await screen.user.click(screen.getByRole('button', { name: /accept invitation/i }));

    await waitFor(() => expect(window.location.pathname).toBe('/'));
    await waitFor(() => expect(localStorage.getItem('signalnest-active-org')).toBe('org-1'));
    // It stays there once everything settles (no render-time fallback back to org-b).
    await screen.findAllByText('Demo Workspace');
    expect(localStorage.getItem('signalnest-active-org')).toBe('org-1');
    expect(orgModel.requests().filter((r) => r.path.endsWith('/auth/invitations/accept')).map((r) => r.userId)).toEqual([
      'user-alice',
    ]);
    expectNoLeak(token, 'after the account switch');
  });
});

// ---- §23 / §49 / §74: the invitation cannot be used ----------------------------

describe('unusable invitations get a specific, calm state (§23, §49)', () => {
  const noRawError = () => {
    const text = document.body.textContent ?? '';
    expect(text).not.toMatch(/checking your invitation/i); // no infinite spinner
    expect(text).not.toMatch(/\{"|\[object Object\]|Request failed \(|invitation_[a-z_]+|stack/);
  };

  it('invalid: an unknown token', async () => {
    const screen = renderProductionApp(<App />, '/invite#token=not-a-real-token-1234');
    expect(await screen.findByRole('heading', { name: 'This invitation link is invalid' })).toBeInTheDocument();
    noRawError();
  });

  it('expired: past its 72 hours', async () => {
    const created = await createInvitation('late@example.com', 'viewer');
    orgModel.expireInvitation(created.id);
    const screen = renderProductionApp(<App />, `/invite#token=${created.token}`);
    expect(await screen.findByRole('heading', { name: 'This invitation has expired' })).toBeInTheDocument();
    expect(screen.getByText(/ask an administrator of the organization for a new invitation/i)).toBeInTheDocument();
    noRawError();
  });

  it('revoked: withdrawn by an administrator', async () => {
    const created = await createInvitation('withdrawn@example.com', 'viewer');
    setAuthToken('test-token');
    await api.revokeInvitation('org-1', created.id);
    setAuthToken(null);
    const screen = renderProductionApp(<App />, `/invite#token=${created.token}`);
    expect(await screen.findByRole('heading', { name: 'This invitation was revoked' })).toBeInTheDocument();
    noRawError();
  });

  it('already used: the link was spent by an earlier registration', async () => {
    const created = await createInvitation('once@example.com', 'viewer');
    await api.registerWithInvitation({ token: created.token, full_name: 'Once Only', password: 'long enough password' });
    setAuthToken(null);
    const screen = renderProductionApp(<App />, `/invite#token=${created.token}`);
    expect(await screen.findByRole('heading', { name: 'This invitation has already been used' })).toBeInTheDocument();
    noRawError();
  });

  it('inviter no longer authorized: the admin who invited was demoted before acceptance', async () => {
    orgModel.seedRoster('org-1');
    const created = await createInvitation('late-join@example.com', 'admin', orgModel.signIn('user-admin'));
    orgModel.changeRoleBehindTheUi('org-1', 'user-admin', 'viewer');
    const screen = renderProductionApp(<App />, `/invite#token=${created.token}`);
    await screen.findByText(/create your account to join demo org/i);
    await screen.user.type(screen.getByLabelText(/full name/i), 'Late Joiner');
    await screen.user.type(screen.getByLabelText(/^password\b/i), 'long enough password');
    await screen.user.click(screen.getByRole('button', { name: /create account and join/i }));
    expect(await screen.findByRole('heading', { name: 'This invitation can no longer be accepted' })).toBeInTheDocument();
    expect(localStorage.getItem('signalnest-token')).toBeNull();
    noRawError();
  });

  it('already a member: the server says so on accept (stubbed: unreachable through a consistent model)', async () => {
    addOtherOrganization();
    addExistingUser('user-erin', 'erin@example.com', 'Erin Existing', 'erin password 1');
    orgModel.setMembership('org-b', 'user-erin', 'owner');
    const { token } = await createInvitation('erin@example.com', 'viewer');
    server.use(
      http.post(P('/auth/invitations/accept'), () =>
        HttpResponse.json(
          { error: { code: 'invitation_already_member', message: 'You are already a member of this organization.' } },
          { status: 409 },
        ),
      ),
    );
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, { token: orgModel.signIn('user-erin') });
    await screen.user.click(await screen.findByRole('button', { name: /accept invitation/i }));
    expect(await screen.findByRole('heading', { name: 'You already belong to this organization' })).toBeInTheDocument();
    noRawError();
  });
});

// ---- §24 / §50: the account already exists -----------------------------------

describe('registering an address that already has an account moves to sign-in (§24, §50)', () => {
  it('keeps the token in memory only, signs in on /invite, and accepts with the same token', async () => {
    addOtherOrganization();
    addExistingUser('user-ola', 'ola@example.com', 'Ola Returning', 'ola password 12');
    orgModel.setMembership('org-b', 'user-ola', 'viewer');
    const { token } = await createInvitation('ola@example.com', 'reviewer');
    installInstruments();
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, { probe: PROBES });

    await screen.findByText(/create your account to join demo org/i);
    await screen.user.type(screen.getByLabelText(/full name/i), 'Ola Returning');
    await screen.user.type(screen.getByLabelText(/^password\b/i), 'some new password');
    await screen.user.click(screen.getByRole('button', { name: /create account and join/i }));

    // Directed to sign in — on the same page, with the token nowhere but memory.
    expect(await screen.findByText(/an account with this email address already exists/i)).toBeInTheDocument();
    expect(window.location.pathname).toBe('/invite');
    expect(window.location.search).toBe('');
    expectNoLeak(token, 'after account_exists');

    await screen.user.type(screen.getByLabelText(/^password\b/i), 'ola password 12');
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
    await screen.user.click(await screen.findByRole('button', { name: /accept invitation/i }));

    await waitFor(() => expect(window.location.pathname).toBe('/'));
    const accepts = orgModel.requests().filter((r) => r.path.endsWith('/auth/invitations/accept'));
    expect(accepts.map((r) => r.body)).toEqual([{ token }]);
    expect(orgModel.memberships().find((m) => m.user_id === 'user-ola' && m.organization_id === 'org-1')?.role).toBe(
      'reviewer',
    );
    expectNoLeak(token, 'after acceptance');
  });
});

// ---- coverage closure on the 6B-3B lines of the invite page (SR/m4/COVERAGE.md) ----

describe('the invite page on its less travelled paths', () => {
  const STUMBLE = () =>
    HttpResponse.json({ error: { code: 'internal_error', message: 'The server stumbled.' } }, { status: 500 });

  function hold() {
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    return { held, release: () => act(() => release()) };
  }

  it('a preview the server cannot answer says so, and "Try again" really checks again', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    let failing = true;
    const second = hold();
    server.use(
      http.post(P('/auth/invitations/preview'), async () => {
        if (failing) return STUMBLE();
        await second.held;
        return undefined;
      }),
    );
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    const failed = await screen.findByRole('alert');
    expect(within(failed).getByRole('heading', { name: 'We could not check this invitation' })).toBeInTheDocument();
    expect(failed).toHaveTextContent('The server stumbled.');
    const before = count('/auth/invitations/preview');

    failing = false;
    await screen.user.click(within(failed).getByRole('button', { name: /^try again$/i }));
    expect(await screen.findByText('Checking your invitation…')).toBeInTheDocument();
    expect(screen.queryByText('We could not check this invitation')).not.toBeInTheDocument();
    second.release();
    expect(await screen.findByText(/create your account to join demo org/i)).toBeInTheDocument();
    expect(count('/auth/invitations/preview')).toBe(before + 1);
    expect(previewBodies().slice(-1)[0]).toEqual({ token });
  });

  it('a preview whose answer breaks off mid-read says so in plain words, never the raw error', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    server.use(
      http.post(
        P('/auth/invitations/preview'),
        () =>
          new HttpResponse(
            new ReadableStream({
              start(controller) {
                controller.error(new TypeError('The connection was reset.'));
              },
            }),
            { status: 502, headers: { 'Content-Type': 'text/plain' } },
          ),
      ),
    );
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    const failed = await screen.findByRole('alert');
    expect(within(failed).getByRole('heading', { name: 'We could not check this invitation' })).toBeInTheDocument();
    expect(failed).toHaveTextContent('Something went wrong.');
    expect(document.body.textContent).not.toMatch(/connection was reset/i);
  });

  it('a second link announced only by popstate (Back / Forward) is adopted from the address bar', async () => {
    addOtherOrganization();
    const first = await createInvitation('nia.new@example.com', 'marketer');
    setAuthToken('test-token');
    orgModel.setMembership('org-b', 'user-1', 'owner');
    const second = await api.createInvitation('org-b', { email: 'nia.new@example.com', role: 'viewer' });
    setAuthToken(null);
    const screen = renderProductionApp(<App />, `/invite#token=${first.token}`, { probe: PROBES });
    await screen.findByText(/create your account to join demo org/i);

    act(() => {
      // History traversal to an entry with a tokened fragment: popstate, and no hashchange.
      window.history.pushState(null, '', `/invite#token=${second.token}`);
      window.dispatchEvent(new PopStateEvent('popstate', { state: null }));
    });
    expect(await screen.findByText(/create your account to join bayside bakery/i)).toBeInTheDocument();
    await waitFor(() => expect(window.location.hash).toBe(''));
    await waitFor(() => expect(screen.getByTestId('m4-router-hash')).toHaveTextContent(/^$/));
    expect(previewBodies().slice(-1)[0]).toEqual({ token: second.token });
  });

  it('a hash-only navigation that carries no token leaves the invitation as it is', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    await screen.findByText(/create your account to join demo org/i);
    await screen.user.type(screen.getByLabelText(/full name/i), 'Nia');
    const previews = count('/auth/invitations/preview');

    act(() => {
      window.location.hash = '#help';
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.getByText(/create your account to join demo org/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/full name/i)).toHaveValue('Nia');
    expect(screen.queryByRole('heading', { name: /this invitation link is invalid/i })).not.toBeInTheDocument();
    expect(count('/auth/invitations/preview')).toBe(previews);
    // Not an invitation fragment, so it is left in the address bar.
    expect(window.location.hash).toBe('#help');
  });

  it('an accept refused as forbidden (403) explains the address mismatch and hands the button back', async () => {
    addOtherOrganization();
    addExistingUser('user-erin', 'erin@example.com', 'Erin Existing', 'erin password 1');
    orgModel.setMembership('org-b', 'user-erin', 'owner');
    const { token } = await createInvitation('erin@example.com', 'marketer');
    server.use(
      http.post(P('/auth/invitations/accept'), () =>
        HttpResponse.json(
          { error: { code: 'permission_denied', message: 'This invitation was issued to a different email address.' } },
          { status: 403 },
        ),
      ),
    );
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, { token: orgModel.signIn('user-erin') });
    await screen.user.click(await screen.findByRole('button', { name: /accept invitation/i }));
    expect(
      await screen.findByText(
        'This invitation is for a different email address than the account you are signed in with.',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText('This invitation was issued to a different email address.')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^accept invitation$/i })).toBeEnabled();
    expect(localStorage.getItem('signalnest-token')).not.toBeNull();
  });

  it('a sign-in the server cannot answer shows the server\'s message, not "Incorrect email or password"', async () => {
    addOtherOrganization();
    addExistingUser('user-ola', 'ola@example.com', 'Ola Returning', 'ola password 12');
    orgModel.setMembership('org-b', 'user-ola', 'viewer');
    const { token } = await createInvitation('ola@example.com', 'reviewer');
    server.use(http.post(P('/auth/login'), STUMBLE));
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    await screen.findByText(/create your account to join demo org/i);
    await screen.user.click(screen.getByRole('button', { name: /sign in to accept/i }));
    await screen.user.type(await screen.findByLabelText(/^password\b/i), 'ola password 12');
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
    expect(await screen.findByText('The server stumbled.')).toBeInTheDocument();
    expect(screen.queryByText('Incorrect email or password.')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^sign in$/i })).toBeEnabled();
  });

  it('while a stored session is still being checked, the page says so instead of offering to register', async () => {
    addOtherOrganization();
    addExistingUser('user-erin', 'erin@example.com', 'Erin Existing', 'erin password 1');
    orgModel.setMembership('org-b', 'user-erin', 'owner');
    const { token } = await createInvitation('erin@example.com', 'marketer');
    const session = hold();
    server.use(
      http.get(P('/auth/me'), async () => {
        await session.held;
        return undefined;
      }),
    );
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, { token: orgModel.signIn('user-erin') });
    await waitFor(() => expect(count('/auth/invitations/preview')).toBeGreaterThan(0));
    expect(await screen.findByText('Checking your session…')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /create account and join/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^sign in$/i })).not.toBeInTheDocument();
    session.release();
    expect(await screen.findByRole('button', { name: /accept invitation/i })).toBeInTheDocument();
    expect(screen.queryByText('Checking your session…')).not.toBeInTheDocument();
  });

  it('a signed-in member of the invited organization is told so, with nothing to accept (stubbed preview)', async () => {
    server.use(
      http.post(P('/auth/invitations/preview'), () =>
        HttpResponse.json({
          organization_id: 'org-1',
          organization_name: 'Demo Org',
          email: 'demo@signalnest.dev',
          role: 'viewer',
          expires_at: '2099-01-01T00:00:00Z',
        }),
      ),
    );
    const screen = renderProductionApp(<App />, '/invite#token=member-already-token', { token: 'test-token' });
    expect(await screen.findByRole('heading', { name: 'You already belong to this organization' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /accept invitation/i })).not.toBeInTheDocument();
    expect(count('/auth/invitations/accept')).toBe(0);
  });

  it('from sign-in, "Create your account" returns to the registration form', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    await screen.findByText(/create your account to join demo org/i);
    await screen.user.click(screen.getByRole('button', { name: /sign in to accept/i }));
    expect(await screen.findByRole('heading', { name: /sign in to accept the invitation/i })).toBeInTheDocument();
    await screen.user.click(screen.getByRole('button', { name: /^create your account$/i }));
    expect(await screen.findByRole('button', { name: /create account and join/i })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /sign in to accept the invitation/i })).not.toBeInTheDocument();
    expect(screen.getByLabelText(/full name/i)).toBeInTheDocument();
  });
});

// ---- mutation closure: every state reset and signal of the page is pinned (SR/m4/CAMPAIGN-V2) ----

describe('the invite page resets, re-checks and signals exactly when it should', () => {
  const STUMBLE = () =>
    HttpResponse.json({ error: { code: 'internal_error', message: 'The server stumbled.' } }, { status: 500 });

  function hold() {
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    return { held, release: () => act(() => release()) };
  }

  async function twoLinks(firstEmail = 'nia.new@example.com') {
    addOtherOrganization();
    const first = await createInvitation(firstEmail, 'marketer');
    setAuthToken('test-token');
    orgModel.setMembership('org-b', 'user-1', 'owner');
    let second: string;
    try {
      second = (await api.createInvitation('org-b', { email: 'nia.new@example.com', role: 'viewer' })).token;
    } finally {
      setAuthToken(null);
    }
    return { first: first.token, second };
  }

  /** Every panel heading the page ever renders, in order (a flash counts). */
  function recordHeadings() {
    const seen: string[] = [];
    const scan = () => {
      for (const h of Array.from(document.querySelectorAll('h2'))) {
        const t = h.textContent?.trim() ?? '';
        if (t && seen[seen.length - 1] !== t) seen.push(t);
      }
    };
    const observer = new MutationObserver(scan);
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    return { seen, stop: () => observer.disconnect() };
  }

  it('under a memory router, the token is read from the router location when the address bar has none', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    expect(window.location.hash).toBe('');
    const screen = renderApp(<App />, { route: `/invite#token=${token}`, authed: false });
    expect(await screen.findByText(/create your account to join demo org/i)).toBeInTheDocument();
    expect(previewBodies()).toContainEqual({ token });
  });

  it('under a memory router, a new tokened router location is adopted', async () => {
    const { first, second } = await twoLinks();
    const screen = renderApp(
      <>
        <App />
        <NavigateProbe />
      </>,
      { route: `/invite#token=${first}`, authed: false },
    );
    await screen.findByText(/create your account to join demo org/i);
    expect(window.location.hash).toBe('');
    act(() => navigateTo!(`/invite#token=${second}`));
    expect(await screen.findByText(/create your account to join bayside bakery/i)).toBeInTheDocument();
    expect(previewBodies().slice(-1)[0]).toEqual({ token: second });
  });

  it('a second link that is malformed replaces the invitation at once with "invalid"', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    await screen.findByText(/create your account to join demo org/i);
    const previews = count('/auth/invitations/preview');
    act(() => {
      window.location.hash = '#token=%E0%A4%A';
    });
    expect(await screen.findByRole('heading', { name: /this invitation link is invalid/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /create account and join/i })).not.toBeInTheDocument();
    expect(count('/auth/invitations/preview')).toBe(previews);
  });

  it('the same link opened again is checked again, and the page comes back', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    await screen.findByText(/create your account to join demo org/i);
    const previews = count('/auth/invitations/preview');
    act(() => {
      window.location.hash = `#token=${token}`;
    });
    await waitFor(() => expect(count('/auth/invitations/preview')).toBeGreaterThan(previews));
    expect(await screen.findByRole('button', { name: /create account and join/i })).toBeInTheDocument();
    expect(screen.queryByText('Checking your invitation…')).not.toBeInTheDocument();
    await waitFor(() => expect(window.location.hash).toBe(''));
  });

  it('a second link starts over: registration mode, no leftover error', async () => {
    addExistingUser('user-ola', 'ola@example.com', 'Ola Returning', 'ola password 12');
    const { first, second } = await twoLinks('ola@example.com');
    const screen = renderProductionApp(<App />, `/invite#token=${first}`);
    await screen.findByText(/create your account to join demo org/i);
    await screen.user.type(screen.getByLabelText(/full name/i), 'Ola Returning');
    await screen.user.type(screen.getByLabelText(/^password\b/i), 'some new password');
    await screen.user.click(screen.getByRole('button', { name: /create account and join/i }));
    expect(await screen.findByText(/an account with this email address already exists/i)).toBeInTheDocument();
    await screen.user.type(screen.getByLabelText(/^password\b/i), 'not her password');
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
    expect(await screen.findByText('Incorrect email or password.')).toBeInTheDocument();

    act(() => {
      window.location.hash = `#token=${second}`;
    });
    expect(await screen.findByText(/create your account to join bayside bakery/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /create account and join/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^sign in$/i })).not.toBeInTheDocument();
    expect(screen.queryByText('Incorrect email or password.')).not.toBeInTheDocument();
    // …and no stale notice waits behind the sign-in switch either.
    await screen.user.click(screen.getByRole('button', { name: /sign in to accept/i }));
    expect(await screen.findByRole('heading', { name: /sign in to accept the invitation/i })).toBeInTheDocument();
    expect(screen.queryByText(/an account with this email address already exists/i)).not.toBeInTheDocument();
  });

  it('a hashchange on its own (no popstate) is adopted and scrubbed, including the same link again', async () => {
    const { first, second } = await twoLinks();
    const screen = renderProductionApp(<App />, `/invite#token=${first}`);
    await screen.findByText(/create your account to join demo org/i);

    act(() => {
      // replaceState is silent: only the hashchange below can tell the page.
      window.history.replaceState(window.history.state, '', `/invite#token=${second}`);
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    expect(await screen.findByText(/create your account to join bayside bakery/i)).toBeInTheDocument();
    await waitFor(() => expect(window.location.hash).toBe(''));
    expect(previewBodies().slice(-1)[0]).toEqual({ token: second });

    const previews = count('/auth/invitations/preview');
    act(() => {
      window.history.replaceState(window.history.state, '', `/invite#token=${second}`);
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    // The same token: nothing else re-renders the page, so the handler itself scrubs.
    expect(window.location.hash).toBe('');
    await waitFor(() => expect(count('/auth/invitations/preview')).toBe(previews + 1));
    expect(await screen.findByText(/create your account to join bayside bakery/i)).toBeInTheDocument();
  });

  it('switching between sign-in and registration drops the notice and the error', async () => {
    addOtherOrganization();
    addExistingUser('user-ola', 'ola@example.com', 'Ola Returning', 'ola password 12');
    orgModel.setMembership('org-b', 'user-ola', 'viewer');
    const { token } = await createInvitation('ola@example.com', 'reviewer');
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    await screen.findByText(/create your account to join demo org/i);
    await screen.user.type(screen.getByLabelText(/full name/i), 'Ola Returning');
    await screen.user.type(screen.getByLabelText(/^password\b/i), 'some new password');
    await screen.user.click(screen.getByRole('button', { name: /create account and join/i }));
    expect(await screen.findByText(/an account with this email address already exists/i)).toBeInTheDocument();
    await screen.user.type(screen.getByLabelText(/^password\b/i), 'not her password');
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
    expect(await screen.findByText('Incorrect email or password.')).toBeInTheDocument();

    await screen.user.click(screen.getByRole('button', { name: /^create your account$/i }));
    expect(await screen.findByRole('button', { name: /create account and join/i })).toBeInTheDocument();
    expect(screen.queryByText('Incorrect email or password.')).not.toBeInTheDocument();
    await screen.user.click(screen.getByRole('button', { name: /sign in to accept/i }));
    expect(await screen.findByRole('heading', { name: /sign in to accept the invitation/i })).toBeInTheDocument();
    expect(screen.queryByText(/an account with this email address already exists/i)).not.toBeInTheDocument();
    expect(screen.queryByText('Incorrect email or password.')).not.toBeInTheDocument();
  });

  it('a failed sign-in does not carry its error into the accept step', async () => {
    addOtherOrganization();
    addExistingUser('user-ola', 'ola@example.com', 'Ola Returning', 'ola password 12');
    orgModel.setMembership('org-b', 'user-ola', 'viewer');
    const { token } = await createInvitation('ola@example.com', 'reviewer');
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    await screen.findByText(/create your account to join demo org/i);
    await screen.user.click(screen.getByRole('button', { name: /sign in to accept/i }));
    await screen.user.type(await screen.findByLabelText(/^password\b/i), 'not her password');
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
    expect(await screen.findByText('Incorrect email or password.')).toBeInTheDocument();
    await screen.user.clear(screen.getByLabelText(/^password\b/i));
    await screen.user.type(screen.getByLabelText(/^password\b/i), 'ola password 12');
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
    expect(await screen.findByRole('button', { name: /accept invitation/i })).toBeEnabled();
    expect(screen.queryByText('Incorrect email or password.')).not.toBeInTheDocument();
  });

  it('a second registration attempt clears the first one\'s error, and shows its progress, while it runs', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    let attempts = 0;
    const second = hold();
    server.use(
      http.post(P('/auth/invitations/register'), async () => {
        attempts += 1;
        if (attempts === 1) return STUMBLE();
        await second.held;
        return undefined;
      }),
    );
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    await screen.findByText(/create your account to join demo org/i);
    const submit = screen.getByRole('button', { name: /create account and join/i });
    expect(within(submit).queryByLabelText('Loading')).not.toBeInTheDocument();
    await screen.user.type(screen.getByLabelText(/full name/i), 'Nia New');
    await screen.user.type(screen.getByLabelText(/^password\b/i), 'long enough password');
    await screen.user.click(submit);
    expect(await screen.findByText('The server stumbled.')).toBeInTheDocument();
    expect(within(submit).queryByLabelText('Loading')).not.toBeInTheDocument();

    await screen.user.click(submit);
    await waitFor(() => expect(attempts).toBe(2));
    expect(screen.queryByText('The server stumbled.')).not.toBeInTheDocument();
    expect(within(submit).getByLabelText('Loading')).toBeInTheDocument();
    second.release();
    await waitFor(() => expect(window.location.pathname).toBe('/'));
  });

  it('registering never flashes the "already a member" panel on the way in', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    await screen.findByText(/create your account to join demo org/i);
    const headings = recordHeadings();
    try {
      await screen.user.type(screen.getByLabelText(/full name/i), 'Nia New');
      await screen.user.type(screen.getByLabelText(/^password\b/i), 'long enough password');
      await screen.user.click(screen.getByRole('button', { name: /create account and join/i }));
      await waitFor(() => expect(window.location.pathname).toBe('/'));
    } finally {
      headings.stop();
    }
    expect(headings.seen).not.toContain('You already belong to this organization');
    expect(headings.seen).not.toContain('Checking your session…');
  });

  it('after registering, a failed organization entry and a second link offer the accept step', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    orgModel.addOrganization({ id: 'org-c', name: 'Cedar Collective', slug: 'cedar' });
    addExistingUser('user-cy', 'cy@example.com', 'Cy Cedar', 'cy password 123');
    orgModel.setMembership('org-c', 'user-cy', 'owner');
    setAuthToken(orgModel.signIn('user-cy'));
    let second: string;
    try {
      second = (await api.createInvitation('org-c', { email: 'nia.new@example.com', role: 'viewer' })).token;
    } finally {
      setAuthToken(null);
    }
    let registered = false;
    server.use(
      http.post(P('/auth/invitations/register'), () => {
        registered = true;
        return undefined;
      }),
      // The list read after registering lags and lacks Demo Org.
      http.get(P('/organizations'), () => (registered ? HttpResponse.json([]) : undefined)),
    );
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    await screen.findByText(/create your account to join demo org/i);
    await screen.user.type(screen.getByLabelText(/full name/i), 'Nia New');
    await screen.user.type(screen.getByLabelText(/^password\b/i), 'long enough password');
    await screen.user.click(screen.getByRole('button', { name: /create account and join/i }));
    expect(await screen.findByText(/demo org could not be opened automatically/i)).toBeInTheDocument();

    act(() => {
      window.location.hash = `#token=${second}`;
    });
    expect(await screen.findByRole('heading', { name: /join cedar collective/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^accept invitation$/i })).toBeEnabled();
    expect(screen.queryByRole('button', { name: /create account and join/i })).not.toBeInTheDocument();
  });

  it('a second accept attempt clears the first one\'s error while it runs, then welcomes the member', async () => {
    addOtherOrganization();
    addExistingUser('user-erin', 'erin@example.com', 'Erin Existing', 'erin password 1');
    orgModel.setMembership('org-b', 'user-erin', 'owner');
    const { token } = await createInvitation('erin@example.com', 'marketer');
    let attempts = 0;
    const second = hold();
    server.use(
      http.post(P('/auth/invitations/accept'), async () => {
        attempts += 1;
        if (attempts === 1) return STUMBLE();
        await second.held;
        return undefined;
      }),
    );
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, { token: orgModel.signIn('user-erin') });
    await screen.user.click(await screen.findByRole('button', { name: /accept invitation/i }));
    expect(await screen.findByText('The server stumbled.')).toBeInTheDocument();
    await screen.user.click(screen.getByRole('button', { name: /^accept invitation$/i }));
    await waitFor(() => expect(attempts).toBe(2));
    expect(screen.queryByText('The server stumbled.')).not.toBeInTheDocument();
    second.release();
    await waitFor(() => expect(window.location.pathname).toBe('/'));
    expect(await screen.findByText('Welcome to Demo Org')).toBeInTheDocument();
    expect(screen.getByText('You joined as Marketer.')).toBeInTheDocument();
  });

  it('an unusable invitation points a signed-out visitor to sign in, and a signed-in one into the app', async () => {
    let screen = renderProductionApp(<App />, '/invite');
    expect(await screen.findByRole('heading', { name: /this invitation link is invalid/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Go to sign in' })).toHaveAttribute('href', '/sign-in');
    expect(screen.queryByRole('link', { name: 'Go to SignalNest' })).not.toBeInTheDocument();
    screen.unmount();

    screen = renderProductionApp(<App />, '/invite', { token: 'test-token' });
    await waitFor(() => expect(screen.getByRole('link', { name: 'Go to SignalNest' })).toHaveAttribute('href', '/'));
    expect(screen.queryByRole('link', { name: 'Go to sign in' })).not.toBeInTheDocument();
  });
});

// ---- panel-4 closure: scrub timing, acceptance never flashes, history ----

let hrefsAtLayout: string[] = [];
/** Records the address bar at every commit that changes the router location (after the page's own layout effects). */
function HrefAtEveryLayout() {
  const location = useLocation();
  useLayoutEffect(() => {
    hrefsAtLayout.push(window.location.href);
  }, [location]);
  return null;
}

describe('the invite page keeps its promises on every commit', () => {
  beforeEach(() => {
    hrefsAtLayout = [];
  });

  it('a second link arriving by popstate is out of the address bar by the time that commit is painted', async () => {
    addOtherOrganization();
    const first = await createInvitation('nia.new@example.com', 'marketer');
    setAuthToken('test-token');
    orgModel.setMembership('org-b', 'user-1', 'owner');
    const second = await api.createInvitation('org-b', { email: 'nia.new@example.com', role: 'viewer' });
    setAuthToken(null);
    installInstruments();
    const screen = renderProductionApp(<App />, `/invite#token=${first.token}`, { probe: <HrefAtEveryLayout /> });
    await screen.findByText(/create your account to join demo org/i);
    // Positive instance of the instrument: it records commits.
    expect(hrefsAtLayout.length).toBeGreaterThan(0);
    expect(hrefsAtLayout.every((href) => !href.includes(first.token))).toBe(true);
    const seen = hrefsAtLayout.length;

    act(() => {
      window.history.pushState(null, '', `/invite#token=${second.token}`);
      window.dispatchEvent(new PopStateEvent('popstate', { state: null }));
    });
    expect(await screen.findByText(/create your account to join bayside bakery/i)).toBeInTheDocument();
    expect(hrefsAtLayout.length).toBeGreaterThan(seen);
    expect(hrefsAtLayout.filter((href) => href.includes(second.token))).toEqual([]);
    // The page itself added no history entry (the test's own pushState is the only push).
    expect(historyCalls.filter((c) => c.kind === 'push').map((c) => c.url)).toEqual([`/invite#token=${second.token}`]);
  });

  it('accepting never flashes the "already a member" panel, and hands over by replacing /invite', async () => {
    addOtherOrganization();
    addExistingUser('user-erin', 'erin@example.com', 'Erin Existing', 'erin password 1');
    orgModel.setMembership('org-b', 'user-erin', 'owner');
    const { token } = await createInvitation('erin@example.com', 'marketer');
    installInstruments();
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, { token: orgModel.signIn('user-erin') });
    const accept = await screen.findByRole('button', { name: /accept invitation/i });
    const headings = (() => {
      const seen: string[] = [];
      const scan = () => {
        for (const h of Array.from(document.querySelectorAll('h2'))) {
          const t = h.textContent?.trim() ?? '';
          if (t && seen[seen.length - 1] !== t) seen.push(t);
        }
      };
      const observer = new MutationObserver(scan);
      observer.observe(document.body, { childList: true, subtree: true, characterData: true });
      return { seen, stop: () => observer.disconnect() };
    })();
    try {
      await screen.user.click(accept);
      await waitFor(() => expect(window.location.pathname).toBe('/'));
    } finally {
      headings.stop();
    }
    expect(headings.seen).not.toContain('You already belong to this organization');
    expect(historyCalls.filter((c) => c.kind === 'push')).toEqual([]);
  });
});

// ---- panel-4 closure (u5 survivors) ----

describe('the invite page after the widened campaign', () => {
  beforeEach(() => {
    hrefsAtLayout = [];
  });

  it('the same link opened again by popstate is out of the address bar by the time that commit is painted', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    installInstruments();
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, { probe: <HrefAtEveryLayout /> });
    await screen.findByText(/create your account to join demo org/i);
    const seen = hrefsAtLayout.length;
    act(() => {
      window.history.pushState(null, '', `/invite#token=${token}`);
      window.dispatchEvent(new PopStateEvent('popstate', { state: null }));
    });
    await waitFor(() => expect(hrefsAtLayout.length).toBeGreaterThan(seen));
    expect(await screen.findByText(/create your account to join demo org/i)).toBeInTheDocument();
    expect(hrefsAtLayout.filter((href) => href.includes(token))).toEqual([]);
  });

  it('two unusable links in a row: the page shows the second one\'s reason', async () => {
    const created = await createInvitation('nia.new@example.com', 'marketer');
    orgModel.expireInvitation(created.id);
    const { token } = created;
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    expect(await screen.findByRole('heading', { name: 'This invitation has expired' })).toBeInTheDocument();
    act(() => {
      window.location.hash = '#token=%E0%A4%A';
    });
    expect(await screen.findByRole('heading', { name: /this invitation link is invalid/i })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'This invitation has expired' })).not.toBeInTheDocument();
  });

  it('accepting and signing in show their progress on the button', async () => {
    addOtherOrganization();
    addExistingUser('user-erin', 'erin@example.com', 'Erin Existing', 'erin password 1');
    orgModel.setMembership('org-b', 'user-erin', 'owner');
    const { token } = await createInvitation('erin@example.com', 'marketer');
    let releaseAccept!: () => void;
    const acceptHeld = new Promise<void>((resolve) => {
      releaseAccept = resolve;
    });
    server.use(
      http.post(P('/auth/invitations/accept'), async () => {
        await acceptHeld;
        return undefined;
      }),
    );
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, { token: orgModel.signIn('user-erin') });
    const accept = await screen.findByRole('button', { name: /accept invitation/i });
    expect(within(accept).queryByLabelText('Loading')).not.toBeInTheDocument();
    await screen.user.click(accept);
    const busy = await screen.findByRole('button', { name: /accepting/i });
    expect(within(busy).getByLabelText('Loading')).toBeInTheDocument();
    act(() => releaseAccept());
    await waitFor(() => expect(window.location.pathname).toBe('/'));
  });

  it('signing in on the invite page shows its progress on the button', async () => {
    addOtherOrganization();
    addExistingUser('user-ola', 'ola@example.com', 'Ola Returning', 'ola password 12');
    orgModel.setMembership('org-b', 'user-ola', 'viewer');
    const { token } = await createInvitation('ola@example.com', 'reviewer');
    let releaseLogin!: () => void;
    const loginHeld = new Promise<void>((resolve) => {
      releaseLogin = resolve;
    });
    server.use(
      http.post(P('/auth/login'), async () => {
        await loginHeld;
        return undefined;
      }),
    );
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    await screen.findByText(/create your account to join demo org/i);
    await screen.user.click(screen.getByRole('button', { name: /sign in to accept/i }));
    await screen.user.type(await screen.findByLabelText(/^password\b/i), 'ola password 12');
    const signIn = screen.getByRole('button', { name: /^sign in$/i });
    expect(within(signIn).queryByLabelText('Loading')).not.toBeInTheDocument();
    await screen.user.click(signIn);
    await waitFor(() => expect(within(signIn).getByLabelText('Loading')).toBeInTheDocument());
    act(() => releaseLogin());
    expect(await screen.findByRole('button', { name: /accept invitation/i })).toBeInTheDocument();
  });

  it('no empty alert or status region is ever shown on the invitation steps', async () => {
    addOtherOrganization();
    addExistingUser('user-erin', 'erin@example.com', 'Erin Existing', 'erin password 1');
    orgModel.setMembership('org-b', 'user-erin', 'owner');
    const erinInvite = await createInvitation('erin@example.com', 'marketer');
    const niaInvite = await createInvitation('nia.new@example.com', 'marketer');
    const inPage = (screen: { container: HTMLElement }) =>
      Array.from(screen.container.querySelectorAll('[role="alert"], [role="status"]')).map((e) => e.textContent ?? '');

    // Registration step, then the sign-in step (no notice, no error).
    let screen = renderProductionApp(<App />, `/invite#token=${niaInvite.token}`);
    await screen.findByText(/create your account to join demo org/i);
    expect(inPage(screen)).toEqual([]);
    await screen.user.click(screen.getByRole('button', { name: /sign in to accept/i }));
    await screen.findByRole('heading', { name: /sign in to accept the invitation/i });
    expect(inPage(screen)).toEqual([]);
    screen.unmount();

    // The accept step.
    screen = renderProductionApp(<App />, `/invite#token=${erinInvite.token}`, { token: orgModel.signIn('user-erin') });
    await screen.findByRole('button', { name: /accept invitation/i });
    expect(inPage(screen)).toEqual([]);
  });

  it('after re-joining, the joined organization\'s cached workspaces and members stay on screen while they are re-read', async () => {
    orgModel.addOrganization({ id: 'org-a', name: 'Erin Agency', slug: 'erin-agency' }, [
      { id: 'ws-a', name: 'Agency Workspace', slug: 'agency-ws', onboarding_completed: true, created_at: '2026-01-01T00:00:00Z' },
    ]);
    addExistingUser('user-erin', 'erin@example.com', 'Erin Existing', 'erin password 1');
    orgModel.setMembership('org-a', 'user-erin', 'admin');
    orgModel.setMembership('org-1', 'user-erin', 'viewer');
    const screen = renderProductionApp(<App />, '/settings', {
      token: orgModel.signIn('user-erin'),
      probe: (
        <>
          {PROBES}
          <ActiveProbe />
          <NavigateProbe />
        </>
      ),
    });
    await waitFor(() => expect(screen.getByTestId('m4-active')).toHaveTextContent('org-a:admin'));
    // She looks at Demo Org: its members and workspaces enter this tab's cache.
    await screen.user.click(screen.getAllByRole('combobox', { name: /^organization$/i })[0]!);
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Demo Org' }));
    await waitFor(() => expect(screen.getByTestId('m4-active')).toHaveTextContent('org-1:viewer'));
    await screen.findByRole('table', { name: /members of demo org/i });
    // Outside this tab: she is removed and invited back.
    const asOwner = (path: string, method: string, body?: unknown) =>
      fetch(`${ORIGIN}${API_PREFIX}${path}`, {
        method,
        headers: { Authorization: 'Bearer test-token', 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    expect((await asOwner('/organizations/org-1/members/user-erin', 'DELETE')).status).toBe(204);
    const invited = await asOwner('/organizations/org-1/invitations', 'POST', { email: 'erin@example.com', role: 'marketer' });
    const { token } = (await invited.json()) as { token: string };
    // Back in her own organization a stale refusal re-reads her session.
    await screen.user.click(screen.getAllByRole('combobox', { name: /^organization$/i })[0]!);
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Erin Agency' }));
    await waitFor(() => expect(screen.getByTestId('m4-active')).toHaveTextContent('org-a:admin'));
    orgModel.changeRoleBehindTheUi('org-a', 'user-erin', 'viewer');
    await screen.user.click(await screen.findByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'someone@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    await waitFor(() => expect(screen.getByTestId('m4-active')).toHaveTextContent('org-a:viewer'));
    expect(cache!.getQueryState(['organizations', 'org-1', 'workspaces'])?.status).toBe('success');
    expect(cache!.getQueryState(['organizations', 'org-1', 'members'])?.status).toBe('success');

    // Slow network from here on for Demo Org's workspaces and members.
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get(P('/organizations/org-1/:part'), async ({ params }) => {
        if (params.part === 'workspaces' || params.part === 'members') await held;
        return undefined;
      }),
    );
    act(() => navigateTo!(`/invite#token=${token}`));
    await screen.user.click(await screen.findByRole('button', { name: /accept invitation/i }));
    await waitFor(() => expect(window.location.pathname).toBe('/'));
    await waitFor(() => expect(screen.getByTestId('m4-active')).toHaveTextContent('org-1:marketer'));
    // The known workspace is shown at once, while the list is re-read.
    const [workspace] = screen.getAllByRole('combobox', { name: /^workspace$/i });
    await waitFor(() => expect(workspace).toHaveTextContent('Demo Workspace'));
    // The known member list, too.
    const [settingsLink] = screen.getAllByRole('link', { name: /^settings$/i });
    await screen.user.click(settingsLink!);
    const members = await screen.findByRole('table', { name: /members of demo org/i });
    expect(within(members).getByText('erin@example.com')).toBeInTheDocument();
    act(() => release());
    await waitFor(() =>
      expect(within(within(members).getByText('Erin Existing').closest('tr')!).getByText('Marketer')).toBeInTheDocument(),
    );
  });
});

// ---- final adjudication: a registration in flight keeps its form ----

describe('a registration in flight keeps its form (InvitePage.tsx:374)', () => {
  it('"Sign in to accept" cannot replace the form mid-flight, no second form appears, and one registration is sent', async () => {
    const { token } = await createInvitation('nia.new@example.com', 'marketer');
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    // Every registration request the page sends is counted here, as it is sent.
    let registers = 0;
    server.use(
      http.post(P('/auth/invitations/register'), async () => {
        registers += 1;
        await held;
        return undefined;
      }),
    );
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    await screen.findByText(/create your account to join demo org/i);
    await screen.user.type(screen.getByLabelText(/full name/i), 'Nia New');
    await screen.user.type(screen.getByLabelText(/^password\b/i), 'long enough password');
    await screen.user.click(screen.getByRole('button', { name: /create account and join/i }));
    await waitFor(() => expect(registers).toBe(1));

    // The link that asks for sign-in is still live while the registration runs…
    const signIn = screen.getByRole('button', { name: /sign in to accept/i });
    expect(signIn).toBeEnabled();
    await screen.user.click(signIn);
    await new Promise((resolve) => setTimeout(resolve, 30));

    // …but the registration keeps its form: no sign-in step, and no route to a second, fresh form.
    expect(screen.queryByRole('heading', { name: /sign in to accept the invitation/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^sign in$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^create your account$/i })).not.toBeInTheDocument();
    expect(screen.getByText(/create your account to join demo org/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/full name/i)).toHaveValue('Nia New');
    const submits = screen.getAllByRole('button', { name: /create account and join/i });
    expect(submits).toHaveLength(1);
    expect(submits[0]).toBeDisabled();
    expect(registers).toBe(1);

    act(() => release());
    await waitFor(() => expect(window.location.pathname).toBe('/'));
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(registers).toBe(1);
    expect(count('/auth/invitations/register')).toBe(1);
  });
});

describe('invite-token helpers', () => {
  it('parseInviteFragment reads only the token parameter, and refuses empty or malformed values', () => {
    expect(parseInviteFragment('#token=abc')).toBe('abc');
    expect(parseInviteFragment('token=abc')).toBe('abc');
    expect(parseInviteFragment('#ref=mail&token=abc')).toBe('abc');
    expect(parseInviteFragment('#token=a%2Bb%23c')).toBe('a+b#c');
    expect(parseInviteFragment('#token=')).toBeNull();
    expect(parseInviteFragment('#token=%E0%A4%A')).toBeNull();
    expect(parseInviteFragment('#ref=mail')).toBeNull();
    expect(parseInviteFragment('#')).toBeNull();
    expect(parseInviteFragment('')).toBeNull();
  });

  it('fragmentHasInviteToken sees a token parameter, well-formed or not, and nothing else', () => {
    expect(fragmentHasInviteToken('#token')).toBe(true);
    expect(fragmentHasInviteToken('#token=')).toBe(true);
    expect(fragmentHasInviteToken('#a=1&token=%E0')).toBe(true);
    expect(fragmentHasInviteToken('#tokens=1')).toBe(false);
    expect(fragmentHasInviteToken('#help')).toBe(false);
    expect(fragmentHasInviteToken('')).toBe(false);
  });

  it('isPlausibleInviteToken bounds the length like the backend', () => {
    expect(isPlausibleInviteToken('x'.repeat(MAX_INVITE_TOKEN_LENGTH))).toBe(true);
    expect(isPlausibleInviteToken('x'.repeat(MAX_INVITE_TOKEN_LENGTH + 1))).toBe(false);
    expect(isPlausibleInviteToken('')).toBe(false);
    expect(isPlausibleInviteToken(null)).toBe(false);
    expect(isPlausibleInviteToken(undefined)).toBe(false);
  });

  it('buildInviteLink puts the token in the fragment, once, after a normalised origin', () => {
    expect(buildInviteLink('https://app.example.com', 'abc_DEF-123')).toBe('https://app.example.com/invite#token=abc_DEF-123');
    expect(buildInviteLink('https://app.example.com//', 'abc')).toBe('https://app.example.com/invite#token=abc');
    expect(buildInviteLink('https://app.example.com', 'a&b#c')).toBe('https://app.example.com/invite#token=a%26b%23c');
  });

  it('apiErrorCode reads only error.code of an ApiError envelope', () => {
    const withDetail = (detail: unknown) => new ApiError('Refused.', 409, detail, null);
    expect(apiErrorCode(withDetail({ error: { code: 'invitation_expired', message: 'x' } }))).toBe('invitation_expired');
    expect(apiErrorCode(new Error('invitation_expired'))).toBeNull();
    // Only an ApiError is read: a lookalike carrying the same envelope is not.
    expect(apiErrorCode({ detail: { error: { code: 'invitation_expired' } } })).toBeNull();
    expect(apiErrorCode(Object.assign(new Error('x'), { detail: { error: { code: 'invitation_expired' } } }))).toBeNull();
    expect(apiErrorCode(withDetail(null))).toBeNull();
    expect(apiErrorCode(withDetail('invitation_expired'))).toBeNull();
    expect(apiErrorCode(withDetail([{ error: { code: 'invitation_expired' } }]))).toBeNull();
    expect(apiErrorCode(withDetail({ error: null }))).toBeNull();
    expect(apiErrorCode(withDetail({ error: 'invitation_expired' }))).toBeNull();
    expect(apiErrorCode(withDetail({ error: [{ code: 'invitation_expired' }] }))).toBeNull();
    expect(apiErrorCode(withDetail({ error: { code: 409 } }))).toBeNull();
    expect(apiErrorCode(withDetail({ detail: { code: 'invitation_expired' } }))).toBeNull();
  });

  it('invitationErrorCode knows only the invitation codes, never an inherited property name', () => {
    const coded = (code: string) => new ApiError('Refused.', 409, { error: { code, message: 'x' } }, null);
    expect(invitationErrorCode(coded('invitation_revoked'))).toBe('invitation_revoked');
    expect(invitationErrorCode(coded('member_not_found'))).toBeNull();
    expect(invitationErrorCode(coded('toString'))).toBeNull();
    expect(invitationErrorCode(coded('__proto__'))).toBeNull();
    expect(invitationErrorCode(new TypeError('Failed to fetch'))).toBeNull();
  });
});
