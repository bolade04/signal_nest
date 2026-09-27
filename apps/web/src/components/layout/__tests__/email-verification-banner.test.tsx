import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { orgModel } from '@/test/handlers';
import { server } from '@/test/server';
import { renderProductionApp } from '@/test/utils';

/**
 * The email-verification banner in the app shell (6B-4B, P6-AUTH-2): shown to a
 * signed-in account whose session says `email_verified: false`, next to the page,
 * never in place of it.
 */

const P = (path: string) => `*${API_PREFIX}${path}`;
const BANNER_TEXT = /your email address isn't verified yet/i;
const REQUESTED =
  "Verification email requested. If it doesn't arrive, you can request another in a few minutes.";

const requestBodies = () =>
  orgModel
    .requests()
    .filter((r) => r.path.endsWith('/auth/email-verification/request'))
    .map((r) => r.body);

function openAppAsUna(path = '/') {
  return renderProductionApp(<App />, path, { token: orgModel.signIn('user-una') });
}

async function findBanner(screen: ReturnType<typeof renderProductionApp>) {
  const text = await screen.findByText(BANNER_TEXT);
  return text.closest<HTMLElement>('[role="status"]')!;
}

beforeEach(() => {
  orgModel.reset();
  orgModel.addUser({ id: 'user-una', email: 'una@example.com', full_name: 'Una Unverified', email_verified: false });
  orgModel.setMembership('org-1', 'user-una', 'marketer');
});

afterEach(() => {
  expect(orgModel.violations()).toEqual([]);
  window.history.replaceState(null, '', '/');
});

describe('the email-verification banner', () => {
  it('shows an unverified account a status banner above the page, which still renders', async () => {
    const screen = openAppAsUna();
    const banner = await findBanner(screen);
    expect(banner).toHaveAttribute('role', 'status');
    // Inside <main>, before the page's own content, and not an alert.
    const main = screen.getByRole('main');
    expect(main).toContainElement(banner);
    const heading = await screen.findByRole('heading', { name: /^overview$/i });
    expect(banner.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(main).queryAllByRole('alert')).toEqual([]);
    const button = within(banner).getByRole('button', { name: /send verification email/i });
    expect(button).toHaveAccessibleName('Send verification email');
    expect(button).not.toHaveAccessibleName(/account menu/i);
    // No dismiss control: the only action is the request.
    expect(within(banner).getAllByRole('button')).toEqual([button]);
    // Nothing is requested until the click.
    expect(requestBodies()).toEqual([]);
  });

  it('is not shown to a verified account', async () => {
    const screen = renderProductionApp(<App />, '/', { token: 'test-token' });
    // Liveness: the page and the session have loaded.
    expect(await screen.findByRole('heading', { name: /^overview$/i })).toBeInTheDocument();
    await screen.findAllByRole('button', { name: /account menu/i });
    expect(screen.queryByText(BANNER_TEXT)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /send verification email/i })).not.toBeInTheDocument();
  });

  it('posts exactly {} and says the request was made, without claiming delivery', async () => {
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post(P('/auth/email-verification/request'), async () => {
        await held;
        return undefined;
      }),
    );
    const screen = openAppAsUna();
    const banner = await findBanner(screen);
    await screen.user.click(within(banner).getByRole('button', { name: /send verification email/i }));
    // Pending: disabled, so a second click cannot send again.
    expect(await within(banner).findByRole('button', { name: /requesting/i })).toBeDisabled();
    release();

    expect(await within(banner).findByText(REQUESTED)).toBeInTheDocument();
    expect(banner).not.toHaveTextContent(/has been sent|delivered|check your inbox/i);
    // The empty object, as the body (the model flags any other body, and no body at all).
    expect(requestBodies()).toEqual([{}]);
    expect(orgModel.outbox().map((m) => [m.kind, m.to])).toEqual([['email_verification', 'una@example.com']]);
    // Still unverified: the banner stays, and can be used again.
    expect(within(banner).getByRole('button', { name: /send verification email/i })).toBeEnabled();
  });

  it('a request held back by the cooldown looks the same: a 204 never proves a message went out', async () => {
    const screen = openAppAsUna();
    const banner = await findBanner(screen);
    const button = within(banner).getByRole('button', { name: /send verification email/i });
    await screen.user.click(button);
    await within(banner).findByText(REQUESTED);
    await screen.user.click(within(banner).getByRole('button', { name: /send verification email/i }));
    await waitFor(() => expect(requestBodies()).toHaveLength(2));
    expect(await within(banner).findByText(REQUESTED)).toBeInTheDocument();
    // The model mailed once; the second 204 was the cooldown.
    expect(orgModel.outbox()).toHaveLength(1);
  });

  it('an address verified elsewhere (409) re-reads the session and the banner goes away', async () => {
    const screen = openAppAsUna();
    const banner = await findBanner(screen);
    orgModel.emailVerifiedBehindTheUi('user-una');
    const sessionReads = orgModel.count('GET', /\/auth\/me$/);
    await screen.user.click(within(banner).getByRole('button', { name: /send verification email/i }));

    await waitFor(() => expect(screen.queryByText(BANNER_TEXT)).not.toBeInTheDocument());
    expect(orgModel.count('GET', /\/auth\/me$/)).toBeGreaterThan(sessionReads);
    expect(screen.getByRole('heading', { name: /^overview$/i })).toBeInTheDocument();
    expect(screen.queryByText(/already verified/i)).not.toBeInTheDocument();
  });

  it.each([
    [
      'the rate limit',
      () => HttpResponse.json({ error: { code: 'rate_limited', message: 'Too many requests' } }, { status: 429 }),
      'There have been several attempts in a short time. Wait a few minutes, then try again.',
    ],
    [
      'a server failure',
      () => HttpResponse.json({ error: { code: 'internal_error', message: 'The server stumbled.' } }, { status: 500 }),
      'The verification email could not be requested. Please try again.',
    ],
  ])('%s gets calm copy of its own, never the server text', async (_case, answer, copy) => {
    server.use(http.post(P('/auth/email-verification/request'), answer));
    const screen = openAppAsUna();
    const banner = await findBanner(screen);
    await screen.user.click(within(banner).getByRole('button', { name: /send verification email/i }));
    expect(await within(banner).findByText(copy, { exact: false })).toBeInTheDocument();
    expect(banner).not.toHaveTextContent(/too many requests|stumbled/i);
    expect(within(banner).getByRole('button', { name: /send verification email/i })).toBeEnabled();
  });

  it('never blocks the app: navigation works and the banner follows', async () => {
    const screen = openAppAsUna();
    await findBanner(screen);
    const [settingsLink] = screen.getAllByRole('link', { name: /^settings$/i });
    await screen.user.click(settingsLink!);
    await waitFor(() => expect(window.location.pathname).toBe('/settings'));
    expect(await screen.findByRole('heading', { name: /^settings$/i })).toBeInTheDocument();
    expect(screen.getByText(BANNER_TEXT)).toBeInTheDocument();
  });
});
