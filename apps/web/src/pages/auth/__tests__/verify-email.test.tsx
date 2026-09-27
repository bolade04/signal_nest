import { act, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { useAuth } from '@/auth/AuthContext';
import { orgModel } from '@/test/handlers';
import { server } from '@/test/server';
import {
  expectNoTokenLeak,
  hrefAtFirstPaint,
  installLeakInstruments,
  leakProbes,
  resetLeakInstruments,
} from '@/test/token-leak';
import { renderProductionApp } from '@/test/utils';

/**
 * `/verify-email#token=…` (6B-4B, P6-AUTH-2). Rendered in the main.tsx tree
 * (StrictMode, BrowserRouter, the production QueryClient) against the stateful
 * backend model: the token is minted in the model and reaches the page only
 * through the fragment of the link the test opens.
 */

const P = (path: string) => `*${API_PREFIX}${path}`;
const ORIGIN = 'http://localhost:3000';
const TOKEN = 'verify-tok_xyz-Q';
const VERA = { id: 'user-vera', email: 'vera@example.com', password: 'vera password 1' };
const OTTO = { id: 'user-otto', email: 'otto@example.com', password: 'otto password 1' };

function SessionProbe() {
  const { status, user } = useAuth();
  return (
    <span data-testid="session">{`${status}:${user?.id ?? ''}:${String(user?.email_verified ?? '')}`}</span>
  );
}

let navigateTo: ReturnType<typeof useNavigate> | null = null;
function NavigateProbe() {
  const navigate = useNavigate();
  useEffect(() => {
    navigateTo = navigate;
  }, [navigate]);
  return null;
}

const confirmBodies = () =>
  orgModel
    .requests()
    .filter((r) => r.path.endsWith('/auth/email-verification/confirm'))
    .map((r) => r.body as Record<string, unknown>);

function addVera() {
  orgModel.addUser({ id: VERA.id, email: VERA.email, full_name: 'Vera Verify', password: VERA.password, email_verified: false });
  orgModel.setMembership('org-1', VERA.id, 'marketer');
  return orgModel.issueEmailVerificationToken(VERA.id, TOKEN);
}

function addOtto() {
  orgModel.addUser({ id: OTTO.id, email: OTTO.email, full_name: 'Otto Other', password: OTTO.password, email_verified: false });
  orgModel.setMembership('org-1', OTTO.id, 'viewer');
}

const linkTo = (token: string) => `/verify-email#token=${encodeURIComponent(token)}`;

/** Open the mailed link with the leak instruments attached (once per test: they wrap history). */
function openLink(token: string, session?: string) {
  installLeakInstruments();
  return renderProductionApp(<App />, linkTo(token), {
    token: session,
    probe: (
      <>
        {leakProbes()}
        <SessionProbe />
        <NavigateProbe />
      </>
    ),
  });
}

async function signInInline(screen: ReturnType<typeof renderProductionApp>, who: { email: string; password: string }) {
  await screen.findByRole('heading', { name: /sign in to verify your email address/i });
  await screen.user.type(screen.getByLabelText(/^email/i), who.email);
  await screen.user.type(screen.getByLabelText(/^password/i), who.password);
  await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
}

beforeEach(() => {
  orgModel.reset();
  resetLeakInstruments();
  navigateTo = null;
});

afterEach(() => {
  vi.restoreAllMocks();
  expect(orgModel.violations()).toEqual([]);
  window.history.replaceState(null, '', '/');
});

describe('the verification token is read from the fragment and spent only by a click', () => {
  it('scrubs the fragment before the first paint and confirms nothing on mount (StrictMode)', async () => {
    const token = addVera();
    const screen = openLink(token, orgModel.signIn(VERA.id));

    expect(await screen.findByRole('button', { name: /^verify email$/i })).toBeInTheDocument();
    expect(hrefAtFirstPaint()).toBe(`${ORIGIN}/verify-email`);
    expect(window.location.href).toBe(`${ORIGIN}/verify-email`);
    await waitFor(() => expect(screen.getByTestId('leak-router-hash')).toHaveTextContent(/^$/));
    // Give any stray effect time to fire: renders and StrictMode's replay spend nothing.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(confirmBodies()).toEqual([]);
    expect(orgModel.user(VERA.id)?.email_verified).toBe(false);
    expectNoTokenLeak(token, 'before the click');
    const text = document.body.textContent ?? '';
    expect(text).not.toMatch(/Phase \d/);
    expect(text).not.toMatch(/creative generation/i);
    expect(text).not.toMatch(/scored opportunities/i);
  });

  it('signed in: the click verifies, the session is re-read, and the banner is gone in the app', async () => {
    const token = addVera();
    const screen = openLink(token, orgModel.signIn(VERA.id));
    await waitFor(() => expect(screen.getByTestId('session')).toHaveTextContent(`authenticated:${VERA.id}:false`));
    const sessionReads = orgModel.count('GET', /\/auth\/me$/);

    await screen.user.click(await screen.findByRole('button', { name: /^verify email$/i }));

    expect(await screen.findByRole('heading', { name: /your email address is verified\./i })).toBeInTheDocument();
    expect(confirmBodies()).toEqual([{ token }]);
    expect(orgModel.count('GET', /\/auth\/me$/)).toBeGreaterThan(sessionReads);
    expect(screen.getByTestId('session')).toHaveTextContent(`authenticated:${VERA.id}:true`);
    expectNoTokenLeak(token, 'after verifying');

    // Into the app: no verification banner for a verified address.
    await screen.user.click(screen.getByRole('link', { name: /go to signalnest/i }));
    expect(await screen.findByRole('heading', { name: /^overview$/i })).toBeInTheDocument();
    expect(screen.queryByText(/your email address isn't verified yet/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /send verification email/i })).not.toBeInTheDocument();
  });

  it('signed out: signs in on the page, keeps the SAME token in memory, and confirms only on the click', async () => {
    const token = addVera();
    const screen = openLink(token);

    await signInInline(screen, VERA);
    const verify = await screen.findByRole('button', { name: /^verify email$/i });
    // Signed in, on the same page, with nothing spent yet.
    expect(window.location.pathname).toBe('/verify-email');
    expect(screen.getByTestId('session')).toHaveTextContent(`authenticated:${VERA.id}:false`);
    expect(confirmBodies()).toEqual([]);
    expectNoTokenLeak(token, 'after the inline sign-in');

    await screen.user.click(verify);
    expect(await screen.findByRole('heading', { name: /your email address is verified\./i })).toBeInTheDocument();
    expect(confirmBodies()).toEqual([{ token }]);
    expect(orgModel.user(VERA.id)?.email_verified).toBe(true);
    // The page never went through /sign-in.
    expect(window.location.pathname).toBe('/verify-email');
    expectNoTokenLeak(token, 'after verifying');
  });

  it('a session that ended mid-way returns to the inline sign-in with a notice, token kept', async () => {
    const token = addVera();
    const session = orgModel.signIn(VERA.id);
    const screen = openLink(token, session);
    const verify = await screen.findByRole('button', { name: /^verify email$/i });
    await waitFor(() => expect(screen.getByTestId('session')).toHaveTextContent(/^authenticated:/));
    orgModel.expireSession(session);

    await screen.user.click(verify);
    expect(await screen.findByText('Your session has ended. Sign in again to verify your email address.')).toBeInTheDocument();
    expect(localStorage.getItem('signalnest-token')).toBeNull();

    await signInInline(screen, VERA);
    await screen.user.click(await screen.findByRole('button', { name: /^verify email$/i }));
    expect(await screen.findByRole('heading', { name: /your email address is verified\./i })).toBeInTheDocument();
    // Both attempts carried the one token from the link.
    expect(confirmBodies()).toEqual([{ token }, { token }]);
    expectNoTokenLeak(token, 'after the second sign-in');
  });
});

describe('a second link, and a scrubbed one', () => {
  it('adopts a newer link opened in the same tab, scrubs it, and starts the page over', async () => {
    const first = addVera();
    const screen = openLink(first, orgModel.signIn(VERA.id));
    await screen.findByRole('button', { name: /^verify email$/i });
    // A newer mail replaced the first link, which the page still holds: it is refused.
    const second = orgModel.issueEmailVerificationToken(VERA.id, 'verify-tok_second-Q');
    await screen.user.click(screen.getByRole('button', { name: /^verify email$/i }));
    await screen.findByRole('heading', { name: /this verification link is invalid or has expired/i });

    // The visitor opens the newer link in the same tab (a fragment-only navigation).
    act(() => {
      window.location.hash = `#token=${second}`;
    });
    await screen.findByRole('button', { name: /^verify email$/i });
    await waitFor(() => expect(window.location.hash).toBe(''));
    await waitFor(() => expect(screen.getByTestId('leak-router-hash')).toHaveTextContent(/^$/));
    // Nothing is spent by the adoption itself.
    expect(confirmBodies()).toEqual([{ token: first }]);

    await screen.user.click(screen.getByRole('button', { name: /^verify email$/i }));
    expect(await screen.findByRole('heading', { name: /your email address is verified\./i })).toBeInTheDocument();
    expect(confirmBodies()).toEqual([{ token: first }, { token: second }]);
    expectNoTokenLeak(first, 'after the second link');
    expectNoTokenLeak(second, 'after the second link');
  });

  it('adopts a second link announced only by popstate (Back / Forward) from the address bar', async () => {
    const first = addVera();
    const screen = openLink(first, orgModel.signIn(VERA.id));
    await screen.findByRole('button', { name: /^verify email$/i });
    const second = orgModel.issueEmailVerificationToken(VERA.id, 'verify-tok_second-Q');

    act(() => {
      // History traversal to an entry with a tokened fragment: popstate, and no hashchange.
      window.history.pushState(null, '', linkTo(second));
      window.dispatchEvent(new PopStateEvent('popstate', { state: null }));
    });
    await waitFor(() => expect(window.location.hash).toBe(''));
    await waitFor(() => expect(screen.getByTestId('leak-router-hash')).toHaveTextContent(/^$/));
    await screen.user.click(await screen.findByRole('button', { name: /^verify email$/i }));
    expect(await screen.findByRole('heading', { name: /your email address is verified\./i })).toBeInTheDocument();
    expect(confirmBodies()).toEqual([{ token: second }]);
  });

  it('a hash-only navigation without a token leaves the link as it is', async () => {
    const token = addVera();
    const screen = openLink(token, orgModel.signIn(VERA.id));
    await screen.findByRole('button', { name: /^verify email$/i });
    act(() => {
      window.location.hash = '#details';
    });
    await waitFor(() => expect(window.location.hash).toBe('#details'));
    await screen.user.click(screen.getByRole('button', { name: /^verify email$/i }));
    expect(await screen.findByRole('heading', { name: /your email address is verified\./i })).toBeInTheDocument();
    expect(confirmBodies()).toEqual([{ token }]);
  });

  it('never brings a scrubbed token back: away and Back again finds no link', async () => {
    const token = addVera();
    const screen = openLink(token, orgModel.signIn(VERA.id));
    await screen.findByRole('button', { name: /^verify email$/i });
    await waitFor(() => expect(screen.getByTestId('leak-router-hash')).toHaveTextContent(/^$/));

    act(() => navigateTo!('/forgot-password'));
    await screen.findByRole('heading', { name: /reset your password/i });
    act(() => navigateTo!(-1));
    expect(
      await screen.findByRole('heading', { name: /this verification link is unavailable or has expired/i }),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe('/verify-email');
    expect(confirmBodies()).toEqual([]);
    expectNoTokenLeak(token, 'after away-and-back');
  });
});

describe('a link for another account', () => {
  it('says so without naming either address, spends nothing, and lets the right account in', async () => {
    const token = addVera();
    addOtto();
    const screen = openLink(token, orgModel.signIn(OTTO.id));
    await screen.user.click(await screen.findByRole('button', { name: /^verify email$/i }));

    expect(await screen.findByRole('heading', { name: /this link is for a different account/i })).toBeInTheDocument();
    expect(
      screen.getByText('This verification link cannot be used with the currently signed-in account.'),
    ).toBeInTheDocument();
    const text = document.body.textContent ?? '';
    expect(text).not.toContain(VERA.email);
    expect(text).not.toContain(OTTO.email);
    // The server's own sentence is never shown.
    expect(text).not.toContain('This verification link was issued to a different account.');
    // Nothing was spent: neither account is verified.
    expect(orgModel.user(VERA.id)?.email_verified).toBe(false);
    expect(orgModel.user(OTTO.id)?.email_verified).toBe(false);

    // Sign out here; the token stays in memory for the account it was sent to.
    await screen.user.click(screen.getByRole('button', { name: /^sign out$/i }));
    expect(screen.getByTestId('session')).toHaveTextContent(/^unauthenticated:/);
    await signInInline(screen, VERA);
    await screen.user.click(await screen.findByRole('button', { name: /^verify email$/i }));
    expect(await screen.findByRole('heading', { name: /your email address is verified\./i })).toBeInTheDocument();
    expect(confirmBodies()).toEqual([{ token }, { token }]);
    expect(orgModel.user(VERA.id)?.email_verified).toBe(true);
    expect(orgModel.user(OTTO.id)?.email_verified).toBe(false);
    expectNoTokenLeak(token, 'after the right account verified');
  });
});

describe('an unusable link', () => {
  it('without a token: the unavailable panel and no request', async () => {
    const screen = renderProductionApp(<App />, '/verify-email');
    expect(
      await screen.findByRole('heading', { name: /this verification link is unavailable or has expired/i }),
    ).toBeInTheDocument();
    expect(screen.getByText('Sign in and request a new one from the banner.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /verify email/i })).not.toBeInTheDocument();
    expect(confirmBodies()).toEqual([]);
  });

  it('after a reload the scrubbed link is gone: unavailable, and no request', async () => {
    const token = addVera();
    const session = orgModel.signIn(VERA.id);
    const first = openLink(token, session);
    await first.findByRole('button', { name: /^verify email$/i });
    first.unmount();
    // The address bar as a reload finds it.
    const again = renderProductionApp(
      <App />,
      `${window.location.pathname}${window.location.search}${window.location.hash}`,
      { token: session },
    );
    expect(
      await again.findByRole('heading', { name: /this verification link is unavailable or has expired/i }),
    ).toBeInTheDocument();
    expect(again.getByText('Request a new one from the banner at the top of the app.')).toBeInTheDocument();
    expect(confirmBodies()).toEqual([]);
  });

  it.each([
    ['unknown', () => 'verify-tok_unknown-Q'],
    [
      'expired',
      () => {
        const token = addVera();
        orgModel.expireAccountToken(token);
        return token;
      },
    ],
    [
      'replaced by a newer link',
      () => {
        const token = addVera();
        orgModel.issueEmailVerificationToken(VERA.id, 'verify-tok_newer-Q');
        return token;
      },
    ],
  ])('an %s token: the invalid panel, and the token is dropped', async (_case, makeToken) => {
    const token = makeToken();
    if (!orgModel.user(VERA.id)) addVera();
    const screen = openLink(token, orgModel.signIn(VERA.id));
    await screen.user.click(await screen.findByRole('button', { name: /^verify email$/i }));
    expect(
      await screen.findByRole('heading', { name: /this verification link is invalid or has expired/i }),
    ).toBeInTheDocument();
    expect(screen.queryByText('This verification link is invalid or has expired.')).not.toBeInTheDocument();
    expect(orgModel.user(VERA.id)?.email_verified).toBe(false);
    expectNoTokenLeak(token, 'after a refused link');
  });

  it('an already used link: the invalid panel', async () => {
    const token = addVera();
    const session = orgModel.signIn(VERA.id);
    const first = openLink(token, session);
    await first.user.click(await first.findByRole('button', { name: /^verify email$/i }));
    await first.findByRole('heading', { name: /your email address is verified\./i });
    first.unmount();

    const again = renderProductionApp(<App />, linkTo(token), { token: localStorage.getItem('signalnest-token') ?? session });
    await again.user.click(await again.findByRole('button', { name: /^verify email$/i }));
    expect(
      await again.findByRole('heading', { name: /this verification link is invalid or has expired/i }),
    ).toBeInTheDocument();
  });
});

describe('a failed confirm leaves the page usable', () => {
  it('the rate limit gets calm copy, the button comes back, and a retry uses the same token', async () => {
    const token = addVera();
    let limited = 1;
    server.use(
      http.post(P('/auth/email-verification/confirm'), () => {
        if (limited <= 0) return undefined;
        limited -= 1;
        return HttpResponse.json({ error: { code: 'rate_limited', message: 'Too many requests' } }, { status: 429 });
      }),
    );
    const screen = openLink(token, orgModel.signIn(VERA.id));
    await screen.user.click(await screen.findByRole('button', { name: /^verify email$/i }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('There have been several attempts in a short time. Wait a few minutes, then try again.');
    expect(alert).not.toHaveTextContent(/too many requests/i);

    await screen.user.click(screen.getByRole('button', { name: /^verify email$/i }));
    expect(await screen.findByRole('heading', { name: /your email address is verified\./i })).toBeInTheDocument();
    expect(confirmBodies()).toEqual([{ token }]);
  });

  it('a click while the confirm is in flight sends nothing more', async () => {
    const token = addVera();
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post(P('/auth/email-verification/confirm'), async () => {
        await held;
        return undefined;
      }),
    );
    const screen = openLink(token, orgModel.signIn(VERA.id));
    await screen.user.dblClick(await screen.findByRole('button', { name: /^verify email$/i }));
    expect(await screen.findByRole('button', { name: /verifying/i })).toBeDisabled();
    release();
    expect(await screen.findByRole('heading', { name: /your email address is verified\./i })).toBeInTheDocument();
    expect(confirmBodies()).toEqual([{ token }]);
  });
});
