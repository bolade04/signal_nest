import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { orgModel } from '@/test/handlers';
import { server } from '@/test/server';
import { renderProductionApp } from '@/test/utils';

/**
 * A session the server stops accepting (P6-UI-013). The 12-hour limit is absolute per
 * sign-in (P6-AUTH-4), and "Sign out everywhere" or a password change on another device
 * ends this browser's session too. The next request answers 401, this browser forgets
 * the session, and the user lands on sign-in — which must say why, rather than look
 * like a blank start, and must not say it after a sign-out the user chose.
 */

const ENDED = /your session has ended/i;
const USER = 'user-sam';
const PASSWORD = 'sam password 1';

beforeEach(() => {
  orgModel.reset();
  orgModel.addUser({ id: USER, email: 'sam@example.com', full_name: 'Sam Session', password: PASSWORD });
  orgModel.setMembership('org-1', USER, 'owner');
});

async function signedIn(path: string) {
  const screen = renderProductionApp(<App />, path, { token: orgModel.signIn(USER) });
  await screen.findAllByRole('button', { name: /account menu/i });
  // Boot reads done (organizations, then the workspace and its locations), so an
  // expiry set after this is met by the first request the test itself causes.
  await screen.findAllByRole('combobox', { name: 'Location filter' });
  // The boot session read re-issued the token inside the same session.
  return { screen, token: localStorage.getItem('signalnest-token')! };
}

type Screen = Awaited<ReturnType<typeof signedIn>>['screen'];

async function signInAgain(screen: Screen) {
  await screen.user.type(screen.getByLabelText(/^email/i), 'sam@example.com');
  await screen.user.type(screen.getByLabelText(/^password/i), PASSWORD);
  await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
}

async function openSidebarLink(screen: Screen, name: RegExp) {
  const nav = screen.getAllByRole('navigation').find((n) => within(n).queryByRole('link', { name }));
  await screen.user.click(within(nav!).getByRole('link', { name }));
}

describe('a session that ends while the app is open (P6-UI-013)', () => {
  it('says the session ended, and signing in again returns to the page the user was opening', async () => {
    const { screen, token } = await signedIn('/');
    orgModel.expireSession(token);

    // The next request the user causes is refused: Settings reads the member list.
    await openSidebarLink(screen, /^settings$/i);

    const notice = await screen.findByText(ENDED);
    expect(notice.closest('[role="status"]')).not.toBeNull();
    expect(window.location.pathname).toBe('/sign-in');
    expect(localStorage.getItem('signalnest-token')).toBeNull();

    await signInAgain(screen);
    await waitFor(() => expect(window.location.pathname).toBe('/settings'));
    expect(screen.queryByText(ENDED)).not.toBeInTheDocument();
  });

  it('says so when the session stored in this browser is refused on load', async () => {
    const token = orgModel.signIn(USER);
    orgModel.expireSession(token);
    const screen = renderProductionApp(<App />, '/opportunities', { token });

    expect(await screen.findByText(ENDED)).toBeInTheDocument();
    expect(window.location.pathname).toBe('/sign-in');
    await signInAgain(screen);
    await waitFor(() => expect(window.location.pathname).toBe('/opportunities'));
  });

  it('does not say it after a fresh sign-in has replaced the ended session', async () => {
    const { screen, token } = await signedIn('/');
    orgModel.expireSession(token);
    await openSidebarLink(screen, /^settings$/i);
    await screen.findByText(ENDED);
    await signInAgain(screen);
    await waitFor(() => expect(window.location.pathname).toBe('/settings'));

    // A later sign-out the user chooses is not a session that ended on its own.
    await screen.user.click(screen.getAllByRole('button', { name: /account menu/i })[0]!);
    await screen.user.click(await screen.findByRole('menuitem', { name: /^sign out$/i }));
    await screen.findByRole('button', { name: /^sign in$/i });
    expect(screen.queryByText(ENDED)).not.toBeInTheDocument();
  });
});

describe('no session-ended notice where none ended (P6-UI-013)', () => {
  it('shows none on a first visit with no stored session', async () => {
    const screen = renderProductionApp(<App />, '/opportunities');
    expect(await screen.findByRole('button', { name: /^sign in$/i })).toBeInTheDocument();
    expect(screen.queryByText(ENDED)).not.toBeInTheDocument();
  });

  it('shows none after Sign out, including when the server says the session had already ended (401)', async () => {
    // On the Overview no request of its own answers 401, so the refusal is the sign-out's.
    const { screen, token } = await signedIn('/');
    orgModel.expireSession(token);
    await screen.user.click(screen.getAllByRole('button', { name: /account menu/i })[0]!);
    await screen.user.click(await screen.findByRole('menuitem', { name: /^sign out$/i }));
    await screen.findByRole('button', { name: /^sign in$/i });
    expect(orgModel.signOuts()).toHaveLength(1);
    expect(screen.queryByText(ENDED)).not.toBeInTheDocument();
  });

  it('shows only the sign-out-everywhere notice when that request finds the session already ended (401)', async () => {
    const { screen, token } = await signedIn('/');
    orgModel.expireSession(token);
    await screen.user.click(screen.getAllByRole('button', { name: /account menu/i })[0]!);
    await screen.user.click(await screen.findByRole('menuitem', { name: /^sign out everywhere$/i }));
    const dialog = await screen.findByRole('dialog', { name: /sign out everywhere\?/i });
    await screen.user.click(within(dialog).getByRole('button', { name: /^sign out everywhere$/i }));
    expect(await screen.findByText(/your session had already ended/i)).toBeInTheDocument();
    expect(screen.queryByText(ENDED)).not.toBeInTheDocument();
  });

  it('shows none when the session read at load could not reach the server (not a refusal)', async () => {
    server.use(http.get(`*${API_PREFIX}/auth/me`, () => HttpResponse.error()));
    const screen = renderProductionApp(<App />, '/settings', { token: 'test-token' });
    expect(await screen.findByRole('button', { name: /^sign in$/i })).toBeInTheDocument();
    expect(screen.queryByText(ENDED)).not.toBeInTheDocument();
  });
});
