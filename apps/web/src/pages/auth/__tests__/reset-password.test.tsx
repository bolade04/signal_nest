import { useMutation } from '@tanstack/react-query';
import { act, fireEvent, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { useEffect } from 'react';
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
  probedQueryClient,
  resetLeakInstruments,
} from '@/test/token-leak';
import { renderProductionApp } from '@/test/utils';

/**
 * `/reset-password#token=…` (6B-4B, P6-AUTH-2). Rendered in the main.tsx tree
 * (StrictMode, BrowserRouter, the production QueryClient) against the stateful
 * backend model: the token is minted in the model and reaches the page only
 * through the fragment of the link the test opens.
 */

const P = (path: string) => `*${API_PREFIX}${path}`;
const ORIGIN = 'http://localhost:3000';
const TOKEN = 'reset-tok_abc-DEF_ghi';
const OLD_PASSWORD = 'rita old password';
const NEW_PASSWORD = 'rita new password';

function AuthProbe() {
  const { status } = useAuth();
  return <span data-testid="auth-status">{status}</span>;
}

const confirmBodies = () =>
  orgModel
    .requests()
    .filter((r) => r.path.endsWith('/auth/password-reset/confirm'))
    .map((r) => r.body as Record<string, unknown>);

function addRita() {
  orgModel.addUser({ id: 'user-rita', email: 'rita@example.com', full_name: 'Rita Reset', password: OLD_PASSWORD });
  orgModel.setMembership('org-1', 'user-rita', 'marketer');
  return orgModel.issuePasswordResetToken('user-rita', TOKEN);
}

const linkTo = (token: string) => `/reset-password#token=${encodeURIComponent(token)}`;

/** Open the mailed link with the leak instruments attached (once per test: they wrap history). */
function openLink(token: string, { session, probe }: { session?: string; probe?: React.ReactNode } = {}) {
  installLeakInstruments();
  return renderProductionApp(<App />, linkTo(token), {
    token: session,
    probe: (
      <>
        {leakProbes()}
        <AuthProbe />
        {probe}
      </>
    ),
  });
}

async function fillPasswords(screen: ReturnType<typeof renderProductionApp>, password: string, confirm = password) {
  await screen.user.type(await screen.findByLabelText(/^new password/i), password);
  await screen.user.type(screen.getByLabelText(/^confirm new password/i), confirm);
}

beforeEach(() => {
  orgModel.reset();
  resetLeakInstruments();
});

afterEach(() => {
  vi.restoreAllMocks();
  expect(orgModel.violations()).toEqual([]);
  window.history.replaceState(null, '', '/');
});

describe('the reset token is read from the fragment and never leaks', () => {
  it('scrubs the fragment before the first paint and sends nothing until an explicit submit', async () => {
    const token = addRita();
    const screen = openLink(token);

    expect(await screen.findByLabelText(/^new password/i)).toBeInTheDocument();
    expect(hrefAtFirstPaint()).toBe(`${ORIGIN}/reset-password`);
    expect(window.location.href).toBe(`${ORIGIN}/reset-password`);
    await waitFor(() => expect(screen.getByTestId('leak-router-hash')).toHaveTextContent(/^$/));
    // Nothing is sent on mount (StrictMode included) or on keystrokes.
    expect(confirmBodies()).toEqual([]);
    await fillPasswords(screen, NEW_PASSWORD);
    expect(confirmBodies()).toEqual([]);
    expectNoTokenLeak(token, 'before submit');

    await screen.user.click(screen.getByRole('button', { name: /reset password/i }));
    await waitFor(() => expect(window.location.pathname).toBe('/sign-in'));
    // Exactly the two fields, the token only in the JSON body.
    expect(confirmBodies()).toEqual([{ token, new_password: NEW_PASSWORD }]);
    expectNoTokenLeak(token, 'after submit');
  });

  it('ignores a ?token= query parameter: no request, the unavailable panel', async () => {
    const token = addRita();
    const screen = renderProductionApp(<App />, `/reset-password?token=${token}`);
    expect(
      await screen.findByRole('heading', { name: /this reset link is unavailable or has expired/i }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(/^new password/i)).not.toBeInTheDocument();
    expect(confirmBodies()).toEqual([]);
  });

  it('shows the unavailable panel, with a way to ask for a new link, when the link has no token', async () => {
    const screen = renderProductionApp(<App />, '/reset-password');
    expect(
      await screen.findByRole('heading', { name: /this reset link is unavailable or has expired/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /request a new link/i })).toHaveAttribute('href', '/forgot-password');
    expect(confirmBodies()).toEqual([]);
    const text = document.body.textContent ?? '';
    expect(text).not.toMatch(/Phase \d/);
    expect(text).not.toMatch(/creative generation/i);
    expect(text).not.toMatch(/scored opportunities/i);
  });

  it('after a reload the scrubbed link is gone: unavailable, and no request', async () => {
    const token = addRita();
    const first = openLink(token);
    await first.findByLabelText(/^new password/i);
    first.unmount();
    // The address bar as the reload finds it.
    const again = renderProductionApp(<App />, `${window.location.pathname}${window.location.search}${window.location.hash}`);
    expect(
      await again.findByRole('heading', { name: /this reset link is unavailable or has expired/i }),
    ).toBeInTheDocument();
    expect(confirmBodies()).toEqual([]);
  });
});

describe('the leak scan is a measurement, not a blind spot (positive controls)', () => {
  let plantMutation: ((value: string) => void) | null = null;
  function MutationPlanter() {
    const { mutate } = useMutation({ mutationFn: async (_value: string) => undefined });
    useEffect(() => {
      plantMutation = mutate;
    }, [mutate]);
    return null;
  }

  it.each([
    ['sessionStorage', () => sessionStorage.setItem('planted', TOKEN)],
    ['localStorage', () => localStorage.setItem('planted', encodeURIComponent(TOKEN))],
    ['a cookie', () => void (document.cookie = `planted=${TOKEN}; path=/`)],
    ['the console', () => console.info('planted', TOKEN)],
    ['document.title', () => void (document.title = TOKEN)],
    ['a DOM attribute', () => document.body.setAttribute('data-planted', TOKEN)],
    ['a history write', () => window.history.replaceState({ planted: TOKEN }, '', '/reset-password')],
  ])('catches a token planted in %s', async (_surface, plant) => {
    const token = addRita();
    const screen = openLink(token);
    await screen.findByLabelText(/^new password/i);
    expectNoTokenLeak(token, 'clean baseline');
    plant();
    try {
      expect(() => expectNoTokenLeak(token, 'planted')).toThrow();
    } finally {
      document.title = '';
      document.body.removeAttribute('data-planted');
      document.cookie = 'planted=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/';
    }
  });

  it('catches a token planted in the React Query mutation cache', async () => {
    const token = addRita();
    const screen = openLink(token, { probe: <MutationPlanter /> });
    await screen.findByLabelText(/^new password/i);
    expectNoTokenLeak(token, 'clean baseline');
    act(() => plantMutation!(token));
    await waitFor(() =>
      expect(probedQueryClient()!.getMutationCache().getAll().map((m) => m.state.variables)).toContain(token),
    );
    expect(() => expectNoTokenLeak(token, 'planted')).toThrow(/React Query cache/);
  });
});

describe('client-side password rules send nothing', () => {
  it('rejects fewer than 8, more than 128 and a mismatch without a request', async () => {
    const token = addRita();
    const screen = openLink(token);

    await fillPasswords(screen, 'short1', 'short1');
    await screen.user.click(screen.getByRole('button', { name: /reset password/i }));
    expect(await screen.findByText('Use at least 8 characters')).toBeInTheDocument();

    const tooLong = 'p'.repeat(129);
    await screen.user.clear(screen.getByLabelText(/^new password/i));
    await screen.user.clear(screen.getByLabelText(/^confirm new password/i));
    await fillPasswords(screen, tooLong);
    await screen.user.click(screen.getByRole('button', { name: /reset password/i }));
    expect(await screen.findByText('Use at most 128 characters')).toBeInTheDocument();

    await screen.user.clear(screen.getByLabelText(/^new password/i));
    await screen.user.clear(screen.getByLabelText(/^confirm new password/i));
    await fillPasswords(screen, NEW_PASSWORD, `${NEW_PASSWORD}!`);
    await screen.user.click(screen.getByRole('button', { name: /reset password/i }));
    expect(await screen.findByText('The passwords do not match')).toBeInTheDocument();

    expect(confirmBodies()).toEqual([]);
    expectNoTokenLeak(token, 'after rejected submits');
  });

  it('accepts exactly 128 characters', async () => {
    const token = addRita();
    const screen = openLink(token);
    const longest = 'q'.repeat(128);
    await fillPasswords(screen, longest);
    await screen.user.click(screen.getByRole('button', { name: /reset password/i }));
    await waitFor(() => expect(window.location.pathname).toBe('/sign-in'));
    expect(confirmBodies()).toEqual([{ token, new_password: longest }]);
  });
});

describe('a completed reset', () => {
  it('signs this tab out, lands on sign-in with the notice, and never signs in by itself (F-11 c)', async () => {
    const token = addRita();
    const session = orgModel.signIn('user-rita');
    const screen = openLink(token, { session });
    // Usable while signed in: not redirected away.
    await waitFor(() => expect(screen.getByTestId('auth-status')).toHaveTextContent(/^authenticated$/));
    expect(window.location.pathname).toBe('/reset-password');
    // Signed in: the stored token is the boot re-read's re-issue of the same session.
    const stored = localStorage.getItem('signalnest-token')!;
    expect(stored).toMatch(/^access-user-rita-/);
    expect(orgModel.accepts(stored)).toBe(true);

    await fillPasswords(screen, NEW_PASSWORD);
    await screen.user.click(screen.getByRole('button', { name: /reset password/i }));

    await waitFor(() => expect(window.location.pathname).toBe('/sign-in'));
    expect(await screen.findByText('Your password has been reset. Sign in with your new password.')).toBeInTheDocument();
    // The notice came through router state: nothing in the URL.
    expect(window.location.href).toBe(`${ORIGIN}/sign-in`);
    // The prior session is cleared locally and no new one exists.
    expect(localStorage.getItem('signalnest-token')).toBeNull();
    expect(screen.getByTestId('auth-status')).toHaveTextContent(/^unauthenticated$/);
    expect(orgModel.count('POST', /\/auth\/login$/)).toBe(0);
    // …and the server no longer accepts it (auth_epoch).
    for (const token of [session, stored]) {
      const me = await fetch(`${ORIGIN}${API_PREFIX}/auth/me`, { headers: { Authorization: `Bearer ${token}` } });
      expect(me.status).toBe(401);
    }
    // The page cleared LOCALLY: no server sign-out was sent (P6-AUTH-4 FD-AUTH4-8).
    expect(orgModel.signOuts()).toEqual([]);
    // The new password is set, and the reset verified the address (FD-7).
    expect(orgModel.user('user-rita')).toMatchObject({ password: NEW_PASSWORD, email_verified: true });
    expectNoTokenLeak(token, 'after reset');

    // The new password signs in.
    await screen.user.type(screen.getByLabelText(/^email/i), 'rita@example.com');
    await screen.user.type(screen.getByLabelText(/^password/i), NEW_PASSWORD);
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
    await waitFor(() => expect(screen.getByTestId('auth-status')).toHaveTextContent(/^authenticated$/));
  });

  it('signed in as ANOTHER account: opening the page changes nothing, and the reset only clears this tab', async () => {
    // FD-AUTH4-8: the reset token is Rita's; the browser is signed in as the demo account.
    const token = addRita();
    const screen = openLink(token, { session: 'test-token' });
    await waitFor(() => expect(screen.getByTestId('auth-status')).toHaveTextContent(/^authenticated$/));
    await waitFor(() => expect(probedQueryClient()?.getQueryData(['organizations'])).toBeTruthy());
    // (a) Opening the page sends no sign-out and leaves the demo session in place.
    expect(orgModel.signOuts()).toEqual([]);
    expect(localStorage.getItem('signalnest-token')).toBe('test-token');

    await fillPasswords(screen, NEW_PASSWORD);
    await screen.user.click(screen.getByRole('button', { name: /reset password/i }));

    // (b) This tab is cleared -- stored token, auth state and cached data -- with the notice ...
    expect(await screen.findByText('Your password has been reset. Sign in with your new password.')).toBeInTheDocument();
    expect(localStorage.getItem('signalnest-token')).toBeNull();
    expect(screen.getByTestId('auth-status')).toHaveTextContent(/^unauthenticated$/);
    expect(
      probedQueryClient()!
        .getQueryCache()
        .getAll()
        .filter((q) => q.state.data !== undefined),
    ).toEqual([]);
    // ... and nothing was revoked on the server: the demo session is not Rita's to end.
    expect(orgModel.signOuts()).toEqual([]);
    expect(orgModel.count('POST', /\/auth\/logout(-all)?$/)).toBe(0);
    expect(orgModel.accepts('test-token')).toBe(true);
  });

  it('two submits that land before the button disables spend the token once', async () => {
    const token = addRita();
    const screen = openLink(token);
    await fillPasswords(screen, NEW_PASSWORD);
    const form = screen.getByRole('button', { name: /reset password/i }).closest('form')!;
    // Both in one synchronous burst: no render in between can disable anything.
    act(() => {
      fireEvent.submit(form);
      fireEvent.submit(form);
    });
    expect(await screen.findByText('Your password has been reset. Sign in with your new password.')).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(confirmBodies()).toEqual([{ token, new_password: NEW_PASSWORD }]);
  });

  it('a signed-out visitor lands on sign-in the same way', async () => {
    const token = addRita();
    const screen = openLink(token);
    await fillPasswords(screen, NEW_PASSWORD);
    await screen.user.click(screen.getByRole('button', { name: /reset password/i }));
    expect(await screen.findByText('Your password has been reset. Sign in with your new password.')).toBeInTheDocument();
    expect(screen.getByTestId('auth-status')).toHaveTextContent(/^unauthenticated$/);
  });
});

describe('a refused or failed reset', () => {
  it.each([
    ['unknown', () => 'reset-tok_unknown-Zz'],
    [
      'expired',
      () => {
        const token = addRita();
        orgModel.expireAccountToken(token);
        return token;
      },
    ],
    [
      'replaced by a newer link',
      () => {
        const token = addRita();
        orgModel.issuePasswordResetToken('user-rita', 'reset-tok_newer-Q');
        return token;
      },
    ],
  ])('an %s token shows the invalid panel with a link to ask again', async (_case, makeToken) => {
    const token = makeToken();
    const screen = openLink(token);
    await fillPasswords(screen, NEW_PASSWORD);
    await screen.user.click(screen.getByRole('button', { name: /reset password/i }));

    expect(
      await screen.findByRole('heading', { name: /this reset link is invalid or has expired/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /request a new link/i })).toHaveAttribute('href', '/forgot-password');
    expect(screen.queryByText(/this password reset link is invalid or has expired\./i)).not.toBeInTheDocument();
    expect(window.location.pathname).toBe('/reset-password');
    expectNoTokenLeak(token, 'after a refused reset');
  });

  it('an already used link shows the invalid panel', async () => {
    const token = addRita();
    const first = openLink(token);
    await fillPasswords(first, NEW_PASSWORD);
    await first.user.click(first.getByRole('button', { name: /reset password/i }));
    await waitFor(() => expect(window.location.pathname).toBe('/sign-in'));
    first.unmount();

    const again = renderProductionApp(<App />, linkTo(token));
    await fillPasswords(again, 'another new password');
    await again.user.click(again.getByRole('button', { name: /reset password/i }));
    expect(
      await again.findByRole('heading', { name: /this reset link is invalid or has expired/i }),
    ).toBeInTheDocument();
    expect(orgModel.user('user-rita')?.password).toBe(NEW_PASSWORD);
  });

  it('a server failure says so calmly and keeps the token for a retry', async () => {
    const token = addRita();
    let failures = 1;
    server.use(
      http.post(P('/auth/password-reset/confirm'), () => {
        if (failures <= 0) return undefined;
        failures -= 1;
        return HttpResponse.json({ error: { code: 'internal_error', message: 'The server stumbled.' } }, { status: 500 });
      }),
    );
    const screen = openLink(token);
    await fillPasswords(screen, NEW_PASSWORD);
    await screen.user.click(screen.getByRole('button', { name: /reset password/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Your password could not be changed. Please try again.');
    expect(screen.queryByText(/stumbled/i)).not.toBeInTheDocument();
    expect(window.location.pathname).toBe('/reset-password');

    await screen.user.click(screen.getByRole('button', { name: /reset password/i }));
    await waitFor(() => expect(window.location.pathname).toBe('/sign-in'));
    expect(confirmBodies()).toEqual([{ token, new_password: NEW_PASSWORD }]);
  });

  it('the rate limit gets calm copy, never the server text', async () => {
    const token = addRita();
    server.use(
      http.post(P('/auth/password-reset/confirm'), () =>
        HttpResponse.json({ error: { code: 'rate_limited', message: 'Too many requests' } }, { status: 429 }),
      ),
    );
    const screen = openLink(token);
    await fillPasswords(screen, NEW_PASSWORD);
    await screen.user.click(screen.getByRole('button', { name: /reset password/i }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('There have been several attempts in a short time. Wait a few minutes, then try again.');
    expect(alert).not.toHaveTextContent(/too many requests/i);
    expectNoTokenLeak(token, 'after a rate-limited reset');
  });
});
