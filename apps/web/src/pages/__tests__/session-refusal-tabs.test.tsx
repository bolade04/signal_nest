import { type QueryClient, useQueryClient } from '@tanstack/react-query';
import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { useEffect } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { ApiError, apiRequest } from '@/api/client';
import { API_PREFIX } from '@/api/config';
import { orgModel } from '@/test/handlers';
import { server } from '@/test/server';
import { renderProductionApp } from '@/test/utils';

/**
 * P6-AUTH-4 follow-up (a), (c) and the in-flight race (plan row `P6-AUTH-4`). The tabs
 * of one browser share the stored session token, and each also holds the token its own
 * requests carry. A 401 now ends only the tab whose token was refused: (a) the stored
 * token is removed only when it is that refused token, so another tab's newer session
 * survives; (c) the tab leaves the signed-in shell even when another tab already cleared
 * the stored token; and a late 401 for a session this tab has since replaced is ignored.
 *
 * jsdom has one window, so "another tab" is played by changing what is stored, and the
 * server's session table, behind this tab's back.
 */

const P = (path: string) => `*${API_PREFIX}${path}`;
const TOKEN_KEY = 'signalnest-token';
const ANN = 'user-ann';
const BO = 'user-bo';
const ENDED = /your session has ended/i;

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
  orgModel.addUser({ id: ANN, email: 'ann@example.com', full_name: 'Ann Able', password: 'ann password 1' });
  orgModel.addUser({ id: BO, email: 'bo@example.com', full_name: 'Bo Baker', password: 'bo password 1' });
  orgModel.setMembership('org-1', ANN, 'owner');
  orgModel.setMembership('org-1', BO, 'owner');
  cache = null;
});

async function signedInAs(userId: string) {
  const screen = renderProductionApp(<App />, '/', { token: orgModel.signIn(userId), probe: <CacheProbe /> });
  await screen.findAllByRole('button', { name: /account menu/i });
  // Boot reads done, so the next request is one the test causes.
  await screen.findAllByRole('combobox', { name: 'Location filter' });
  return { screen, token: localStorage.getItem(TOKEN_KEY)! };
}

type Screen = Awaited<ReturnType<typeof signedInAs>>['screen'];

async function openSettings(screen: Screen) {
  const nav = screen.getAllByRole('navigation').find((n) => within(n).queryByRole('link', { name: /^settings$/i }));
  await screen.user.click(within(nav!).getByRole('link', { name: /^settings$/i }));
}

function cachedData(): unknown[] {
  return cache!
    .getQueryCache()
    .getAll()
    .filter((q) => q.state.data !== undefined)
    .map((q) => q.queryKey);
}

describe('a refused session ends only the tab that used it (P6-AUTH-4 follow-up)', () => {
  it('(c) leaves the signed-in shell when another tab signed this session out', async () => {
    const { screen, token } = await signedInAs(ANN);
    // Another tab signs out: the server ends the session, that tab clears the stored token.
    orgModel.expireSession(token);
    localStorage.removeItem(TOKEN_KEY);

    // This tab's next request is refused.
    await openSettings(screen);

    expect(await screen.findByRole('button', { name: /^sign in$/i })).toBeInTheDocument();
    expect(screen.getByText(ENDED)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /account menu/i })).not.toBeInTheDocument();
    // Nothing of the ended session is left on screen or in the cache.
    expect(cachedData()).toEqual([]);
    expect(orgModel.signOuts()).toEqual([]);
  });

  it("(a) keeps the session another tab stored when this tab's own session is refused", async () => {
    const { screen, token: annToken } = await signedInAs(ANN);
    // Another tab: Ann's session ended there and Bo signed in, storing Bo's session.
    orgModel.expireSession(annToken);
    const boToken = orgModel.signIn(BO);
    localStorage.setItem(TOKEN_KEY, boToken);

    await openSettings(screen);

    // This tab leaves Ann's session ...
    expect(await screen.findByRole('button', { name: /^sign in$/i })).toBeInTheDocument();
    expect(cachedData()).toEqual([]);
    // ... and Bo's stays stored and live: nothing was removed or revoked.
    expect(localStorage.getItem(TOKEN_KEY)).toBe(boToken);
    expect(orgModel.accepts(boToken)).toBe(true);
    expect(orgModel.signOuts()).toEqual([]);
  });

  it('ignores a late refusal of a session this tab has since replaced (in-flight race)', async () => {
    const { screen, token: first } = await signedInAs(ANN);
    // A request sent with the first session, whose 401 arrives only later.
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get(P('/probe-late'), async () => {
        await held;
        return HttpResponse.json({ error: { code: 'unauthorized', message: 'Not authenticated.' } }, { status: 401 });
      }),
    );
    const late = apiRequest('/probe-late').catch((err: unknown) => err);

    // Meanwhile this tab signs out and signs in again.
    await screen.user.click(screen.getAllByRole('button', { name: /account menu/i })[0]!);
    await screen.user.click(await screen.findByRole('menuitem', { name: /^sign out$/i }));
    await screen.findByRole('button', { name: /^sign in$/i });
    await screen.user.type(screen.getByLabelText(/^email/i), 'ann@example.com');
    await screen.user.type(screen.getByLabelText(/^password/i), 'ann password 1');
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
    await screen.findAllByRole('button', { name: /account menu/i });
    const second = localStorage.getItem(TOKEN_KEY)!;
    expect(second).not.toBe(first);

    release();
    expect(await late).toBeInstanceOf(ApiError);

    // The new session is untouched.
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(localStorage.getItem(TOKEN_KEY)).toBe(second);
    expect(orgModel.accepts(second)).toBe(true);
    expect(screen.getAllByRole('button', { name: /account menu/i }).length).toBeGreaterThan(0);
    expect(screen.queryByText(ENDED)).not.toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe('/'));
  });

  it('ignores a 401 for a request sent with no session (a wrong password on sign-in)', async () => {
    // Another tab holds a live session; this tab is signed out and mistypes a password.
    const boToken = orgModel.signIn(BO);
    const screen = renderProductionApp(<App />, '/sign-in');
    localStorage.setItem(TOKEN_KEY, boToken);
    await screen.user.type(screen.getByLabelText(/^email/i), 'ann@example.com');
    await screen.user.type(screen.getByLabelText(/^password/i), 'not the password');
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));

    expect(await screen.findByText(/incorrect email or password/i)).toBeInTheDocument();
    expect(localStorage.getItem(TOKEN_KEY)).toBe(boToken);
    expect(orgModel.accepts(boToken)).toBe(true);
  });
});
