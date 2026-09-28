import { type QueryClient, useQueryClient } from '@tanstack/react-query';
import { act, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '@/App';
import { setAuthToken } from '@/api/client';
import { API_PREFIX } from '@/api/config';
import * as api from '@/api/endpoints';
import {
  SIGN_OUT_NOTICE_COPY,
  SIGN_OUT_TIMEOUT_MS,
  SIGN_OUT_UNCONFIRMED_NOTICE,
  SIGNED_OUT_EVERYWHERE_NOTICE,
  SIGN_OUT_EVERYWHERE_UNCONFIRMED_NOTICE,
} from '@/auth/sign-out';
import { orgModel } from '@/test/handlers';
import { server } from '@/test/server';
import { renderProductionApp } from '@/test/utils';

/**
 * P6-AUTH-4 sign-out from the account menu. Every access token belongs to a server-side
 * session: "Sign out" revokes this session on the server BEFORE clearing this browser;
 * "Sign out everywhere" revokes every session of the account and clears only once the
 * server has answered 204 or 401. The MSW model records how each sign-out request
 * arrived (its bearer, and what this browser still stored at that moment) and answers
 * later requests from its own session table.
 */

const P = (path: string) => `*${API_PREFIX}${path}`;
const USER = 'user-sam';

let cache: QueryClient | null = null;
function CacheProbe() {
  const queryClient = useQueryClient();
  useEffect(() => {
    cache = queryClient;
  }, [queryClient]);
  return null;
}

beforeEach(() => {
  orgModel.reset();
  orgModel.addUser({ id: USER, email: 'sam@example.com', full_name: 'Sam Signout' });
  orgModel.setMembership('org-1', USER, 'owner');
  cache = null;
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function signedIn(signedInWith = orgModel.signIn(USER)) {
  const screen = renderProductionApp(<App />, '/settings', { token: signedInWith, probe: <CacheProbe /> });
  await screen.findByRole('button', { name: /account menu/i });
  await waitFor(() => expect(cache?.getQueryData(['organizations'])).toBeTruthy());
  // The boot session read re-issued the token inside the same session: this is the one stored.
  const token = localStorage.getItem('signalnest-token')!;
  expect(token).not.toBe(signedInWith);
  return { screen, token };
}

type Screen = Awaited<ReturnType<typeof signedIn>>['screen'];

async function openMenu(screen: Screen) {
  const [account] = screen.getAllByRole('button', { name: /account menu/i });
  await screen.user.click(account!);
  return screen.findByRole('menu');
}

async function clickSignOut(screen: Screen) {
  await openMenu(screen);
  await screen.user.click(await screen.findByRole('menuitem', { name: /^sign out$/i }));
}

async function confirmEverywhere(screen: Screen) {
  await openMenu(screen);
  await screen.user.click(await screen.findByRole('menuitem', { name: /^sign out everywhere$/i }));
  const dialog = await screen.findByRole('dialog', { name: /sign out everywhere\?/i });
  await screen.user.click(within(dialog).getByRole('button', { name: /^sign out everywhere$/i }));
}

function cachedData(): unknown[] {
  return cache!
    .getQueryCache()
    .getAll()
    .filter((q) => q.state.data !== undefined)
    .map((q) => q.queryKey);
}

function expectSignedOutHere() {
  expect(localStorage.getItem('signalnest-token')).toBeNull();
  expect(cachedData()).toEqual([]);
}

describe('Sign out (the account menu)', () => {
  it('revokes the session on the server with the stored token, then clears this browser (F-01)', async () => {
    const { screen, token } = await signedIn();
    expect(orgModel.accepts(token)).toBe(true);

    await clickSignOut(screen);
    await screen.findByRole('button', { name: /^sign in$/i });

    // The request carried the stored token and arrived while it was still stored.
    expect(orgModel.signOuts()).toEqual([
      { path: '/auth/logout', authorization: `Bearer ${token}`, storedToken: token },
    ]);
    // Only after the answer: storage, auth state and query cache are gone ...
    expectSignedOutHere();
    // ... and the server no longer accepts the captured token ('server-revoked').
    expect(orgModel.accepts(token)).toBe(false);
    // A confirmed sign-out needs no notice.
    expect(screen.queryByText(SIGN_OUT_NOTICE_COPY[SIGN_OUT_UNCONFIRMED_NOTICE])).not.toBeInTheDocument();
  });

  it('clears locally with no notice when the server says the session had already ended (F-04: 401)', async () => {
    const { screen, token } = await signedIn();
    orgModel.expireSession(token);

    await clickSignOut(screen);
    await screen.findByRole('button', { name: /^sign in$/i });

    expect(orgModel.signOuts()).toHaveLength(1);
    expectSignedOutHere();
    expect(screen.queryByText(SIGN_OUT_NOTICE_COPY[SIGN_OUT_UNCONFIRMED_NOTICE])).not.toBeInTheDocument();
  });

  it.each([
    ['404', () => new HttpResponse(null, { status: 404 })],
    ['500', () => HttpResponse.json({ error: { code: 'internal_error', message: 'boom' } }, { status: 500 })],
    ['network error', () => HttpResponse.error()],
  ])(
    'clears this browser but says the server did not confirm on a %s (F-04: local-only)',
    async (_label, answer) => {
      const { screen, token } = await signedIn();
      let arrived = 0;
      server.use(
        http.post(P('/auth/logout'), () => {
          arrived += 1;
          return answer();
        }),
      );

      await clickSignOut(screen);

      expect(await screen.findByText(SIGN_OUT_NOTICE_COPY[SIGN_OUT_UNCONFIRMED_NOTICE])).toBeInTheDocument();
      expect(arrived).toBe(1);
      expectSignedOutHere();
      // Nothing was revoked: the server still accepts the token until its 12-hour limit.
      expect(orgModel.accepts(token)).toBe(true);
    },
  );

  it('gives up on the server after 10 seconds and clears this browser (F-04: timeout)', async () => {
    expect(SIGN_OUT_TIMEOUT_MS).toBe(10_000);
    const { screen, token } = await signedIn();
    server.use(http.post(P('/auth/logout'), () => new Promise<never>(() => {})));
    const timers = vi.spyOn(globalThis, 'setTimeout');

    await clickSignOut(screen);
    await waitFor(() => expect(timers.mock.calls.some(([, ms]) => ms === SIGN_OUT_TIMEOUT_MS)).toBe(true));
    // Still waiting on the server: nothing is cleared yet.
    expect(localStorage.getItem('signalnest-token')).toBe(token);

    const [expire] = timers.mock.calls.find(([, ms]) => ms === SIGN_OUT_TIMEOUT_MS)!;
    act(() => (expire as () => void)());

    expect(await screen.findByText(SIGN_OUT_NOTICE_COPY[SIGN_OUT_UNCONFIRMED_NOTICE])).toBeInTheDocument();
    expectSignedOutHere();
    expect(orgModel.accepts(token)).toBe(true);
  });

  it('sends nothing when this browser no longer stores the session, and says it is unconfirmed (F-04: no stored token)', async () => {
    const { screen, token } = await signedIn();
    // Another tab cleared the shared stored session LOCALLY (e.g. the reset page, FD-AUTH4-8):
    // this tab's session was never revoked on the server.
    localStorage.removeItem('signalnest-token');

    await clickSignOut(screen);

    expect(await screen.findByText(SIGN_OUT_NOTICE_COPY[SIGN_OUT_UNCONFIRMED_NOTICE])).toBeInTheDocument();
    expect(orgModel.signOuts()).toEqual([]);
    expect(orgModel.count('POST', /\/auth\/logout(-all)?$/)).toBe(0);
    expectSignedOutHere();
    expect(orgModel.accepts(token)).toBe(true);
  });
});

describe('the model keeps a re-issued token in its session (F-08)', () => {
  afterEach(() => setAuthToken(null));

  function inviteSam() {
    orgModel.addOrganization({ id: 'org-2', name: 'Second Org', slug: 'second' });
    orgModel.addUser({ id: 'user-olga', email: 'olga@example.com', full_name: 'Olga Owner' });
    orgModel.setMembership('org-2', 'user-olga', 'owner');
    orgModel.invitationCreatedBehindTheUi('org-2', 'sam@example.com', 'viewer', 'user-olga');
    return orgModel.invitations().find((i) => i.email === 'sam@example.com')!.token;
  }

  it.each(['presented', 'accepted'])(
    'an accepted invitation re-issues inside the presented session: signing out with the %s token ends both',
    async (endWith) => {
      const invitation = inviteSam();
      const presented = orgModel.signIn(USER);
      setAuthToken(presented);
      const accepted = (await api.acceptInvitation({ token: invitation })).access_token;
      expect(accepted).not.toBe(presented);
      expect([orgModel.accepts(presented), orgModel.accepts(accepted)]).toEqual([true, true]);

      setAuthToken(endWith === 'presented' ? presented : accepted);
      await api.logout();
      expect([orgModel.accepts(presented), orgModel.accepts(accepted)]).toEqual([false, false]);
    },
  );

  it.each(['presented', 'reissued'])(
    'a session re-read re-issues inside the presented session: signing out with the %s token ends both',
    async (endWith) => {
      const presented = orgModel.signIn(USER);
      setAuthToken(presented);
      const reread = (await api.getSession()).access_token;
      expect(reread).not.toBe(presented);
      expect([orgModel.accepts(presented), orgModel.accepts(reread)]).toEqual([true, true]);

      setAuthToken(endWith === 'presented' ? presented : reread);
      await api.logout();
      expect([orgModel.accepts(presented), orgModel.accepts(reread)]).toEqual([false, false]);
    },
  );
});

describe('Sign out everywhere (the account menu)', () => {
  it('sits below Sign out, apart from the settings entries, with no session list (F-12)', async () => {
    const { screen } = await signedIn();
    const menu = await openMenu(screen);
    const items = within(menu).getAllByRole('menuitem').map((item) => item.textContent?.trim());
    expect(items).toEqual(['Profile & account', 'Workspace settings', 'Sign out', 'Sign out everywhere']);
    // A separator stands between the settings entries and the two sign-out entries.
    const children = Array.from(menu.querySelectorAll('[role="menuitem"], [role="separator"]'));
    const roles = children.map((el) => (el.getAttribute('role') === 'separator' ? '|' : el.textContent?.trim()));
    expect(roles.slice(-3)).toEqual(['|', 'Sign out', 'Sign out everywhere']);
    expect(within(menu).queryByText(/device|active session|session list/i)).not.toBeInTheDocument();
  });

  it('asks first, and sends nothing when cancelled (F-06)', async () => {
    const { screen, token } = await signedIn();
    await openMenu(screen);
    await screen.user.click(await screen.findByRole('menuitem', { name: /^sign out everywhere$/i }));
    const dialog = await screen.findByRole('dialog', { name: /sign out everywhere\?/i });
    await screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(orgModel.signOuts()).toEqual([]);
    expect(localStorage.getItem('signalnest-token')).toBe(token);
  });

  it('revokes every session of the account, then clears and says so (F-06)', async () => {
    const other = orgModel.signIn(USER); // the same account, signed in elsewhere
    const { screen, token } = await signedIn();
    expect([orgModel.accepts(token), orgModel.accepts(other)]).toEqual([true, true]);

    await confirmEverywhere(screen);

    expect(await screen.findByText(SIGN_OUT_NOTICE_COPY[SIGNED_OUT_EVERYWHERE_NOTICE])).toBeInTheDocument();
    expect(orgModel.signOuts()).toEqual([
      { path: '/auth/logout-all', authorization: `Bearer ${token}`, storedToken: token },
    ]);
    expectSignedOutHere();
    // The other session ended too; the demo account's session was never touched.
    expect([orgModel.accepts(token), orgModel.accepts(other), orgModel.accepts('test-token')]).toEqual([
      false,
      false,
      true,
    ]);
  });

  it('sends nothing when this browser no longer stores the session, and says it is unconfirmed (no stored token)', async () => {
    const other = orgModel.signIn(USER);
    const { screen, token } = await signedIn();
    localStorage.removeItem('signalnest-token');

    await confirmEverywhere(screen);

    expect(await screen.findByText(SIGN_OUT_NOTICE_COPY[SIGN_OUT_UNCONFIRMED_NOTICE])).toBeInTheDocument();
    expect(screen.queryByText(SIGN_OUT_NOTICE_COPY[SIGNED_OUT_EVERYWHERE_NOTICE])).not.toBeInTheDocument();
    expect(orgModel.signOuts()).toEqual([]);
    expectSignedOutHere();
    // Nothing was revoked anywhere.
    expect([orgModel.accepts(token), orgModel.accepts(other)]).toEqual([true, true]);
  });

  it('clears and says nothing else was signed out when this session had already ended (F-07: 401)', async () => {
    const other = orgModel.signIn(USER);
    const { screen, token } = await signedIn();
    orgModel.expireSession(token);

    await confirmEverywhere(screen);

    expect(await screen.findByText(SIGN_OUT_NOTICE_COPY[SIGN_OUT_EVERYWHERE_UNCONFIRMED_NOTICE])).toBeInTheDocument();
    expectSignedOutHere();
    expect(orgModel.accepts(other)).toBe(true);
  });

  it.each([
    ['404', () => new HttpResponse(null, { status: 404 })],
    ['500', () => HttpResponse.json({ error: { code: 'internal_error', message: 'boom' } }, { status: 500 })],
    ['network error', () => HttpResponse.error()],
  ])('changes nothing on a %s, says so, and lets the user retry (F-07)', async (_label, answer) => {
    const { screen, token } = await signedIn();
    let arrived = 0;
    server.use(
      http.post(P('/auth/logout-all'), () => {
        arrived += 1;
        return answer();
      }),
    );

    await confirmEverywhere(screen);

    expect(await screen.findByText(/could not sign out everywhere/i)).toBeInTheDocument();
    expect(arrived).toBe(1);
    // Still signed in here, exactly as before.
    expect(localStorage.getItem('signalnest-token')).toBe(token);
    expect(orgModel.accepts(token)).toBe(true);
    expect(screen.getAllByRole('button', { name: /account menu/i }).length).toBeGreaterThan(0);

    // A retry sends a second request; this one succeeds.
    server.resetHandlers();
    await confirmEverywhere(screen);
    expect(await screen.findByText(SIGN_OUT_NOTICE_COPY[SIGNED_OUT_EVERYWHERE_NOTICE])).toBeInTheDocument();
    expect(orgModel.signOuts()).toHaveLength(1); // the model saw only the retry
    expect(arrived).toBe(1);
    expectSignedOutHere();
  });

  it('ends every session of the account in the model, whichever token of it asks (F-08)', async () => {
    const first = orgModel.signIn(USER);
    const second = orgModel.signIn(USER);
    setAuthToken(first);
    await api.logoutAll();
    expect([orgModel.accepts(first), orgModel.accepts(second)]).toEqual([false, false]);
    setAuthToken(null);
  });

  it('changes nothing when the server does not answer within 10 seconds (F-07: timeout)', async () => {
    const { screen, token } = await signedIn();
    server.use(http.post(P('/auth/logout-all'), () => new Promise<never>(() => {})));
    const timers = vi.spyOn(globalThis, 'setTimeout');

    await confirmEverywhere(screen);
    await waitFor(() => expect(timers.mock.calls.some(([, ms]) => ms === SIGN_OUT_TIMEOUT_MS)).toBe(true));
    const [expire] = timers.mock.calls.find(([, ms]) => ms === SIGN_OUT_TIMEOUT_MS)!;
    act(() => (expire as () => void)());

    expect(await screen.findByText(/could not sign out everywhere/i)).toBeInTheDocument();
    expect(localStorage.getItem('signalnest-token')).toBe(token);
    expect(orgModel.accepts(token)).toBe(true);
  });
});
