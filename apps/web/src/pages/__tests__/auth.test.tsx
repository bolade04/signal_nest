import { type QueryClient, useQueryClient } from '@tanstack/react-query';
import { waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { SignInPage } from '@/pages/auth/SignIn';
import { orgModel } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp, renderProductionApp } from '@/test/utils';

describe('authentication & protected routes', () => {
  it('redirects an unauthenticated visitor to the sign-in screen', async () => {
    const screen = renderApp(<App />, { route: '/opportunities', authed: false });
    expect(await screen.findByRole('button', { name: /use demo account/i })).toBeInTheDocument();
    // The protected opportunities page must not render for an anonymous user.
    expect(screen.queryByText(/scored opportunities/i)).not.toBeInTheDocument();
  });

  it('signs in via the demo account shortcut and stores a session token', async () => {
    const screen = renderApp(<SignInPage />, { route: '/sign-in', authed: false });
    await screen.user.click(await screen.findByRole('button', { name: /use demo account/i }));
    await waitFor(() =>
      expect(localStorage.getItem('signalnest-token')).toBe('test-token'),
    );
  });

  it('validates required fields before submitting', async () => {
    const screen = renderApp(<SignInPage />, { route: '/sign-in', authed: false });
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
    expect(await screen.findByText(/email is required/i)).toBeInTheDocument();
    expect(screen.getByText(/password is required/i)).toBeInTheDocument();
  });
});

// P6-UI-008. Under vitest `import.meta.env.DEV` is true and MODE is "test", so the
// demo shortcut renders exactly as it does in development. Stubbing DEV is what
// lets us assert the production variant; the guard is read at render, so the stub
// takes effect without re-importing the module.
describe('demo shortcut is development-only (P6-UI-008)', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('is absent from a production build, credentials and all', async () => {
    vi.stubEnv('DEV', false);
    const screen = renderApp(<SignInPage />, { route: '/sign-in', authed: false });
    // The real form must still be there — this removes a shortcut, not a login.
    expect(await screen.findByRole('button', { name: /^sign in$/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /use demo account/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/demo login:/i)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain('demo@signalnest.dev');
    expect(document.body.textContent).not.toContain('demo1234');
  });

  it('keeps normal sign-in working in production', async () => {
    vi.stubEnv('DEV', false);
    const screen = renderApp(<SignInPage />, { route: '/sign-in', authed: false });
    await screen.user.type(await screen.findByLabelText(/email/i), 'someone@example.com');
    await screen.user.type(screen.getByLabelText(/password/i), 'hunter2');
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
    await waitFor(() => expect(localStorage.getItem('signalnest-token')).toBe('test-token'));
  });

  // The default handler returns 200 unconditionally, so a rejected sign-in has to
  // be staged explicitly. Asserting the rendered copy rather than merely "some
  // error appeared" is what gives this teeth: deleting the `status === 401` branch
  // in SignIn.tsx falls through to `err.message`, which is the envelope's
  // "Invalid credentials" — a different string, so this fails as it should.
  it('still shows the 401 message in production', async () => {
    vi.stubEnv('DEV', false);
    server.use(
      http.post(`*${API_PREFIX}/auth/login`, () =>
        HttpResponse.json(
          { error: { code: 'unauthorized', message: 'Invalid credentials' } },
          { status: 401 },
        ),
      ),
    );
    const screen = renderApp(<SignInPage />, { route: '/sign-in', authed: false });
    await screen.user.type(await screen.findByLabelText(/email/i), 'someone@example.com');
    await screen.user.type(screen.getByLabelText(/password/i), 'hunter2');
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
    expect(await screen.findByText('Incorrect email or password.')).toBeInTheDocument();
    expect(localStorage.getItem('signalnest-token')).toBeNull();
  });

  it('still offers the shortcut in development', async () => {
    const screen = renderApp(<SignInPage />, { route: '/sign-in', authed: false });
    expect(await screen.findByRole('button', { name: /use demo account/i })).toBeInTheDocument();
    expect(screen.getByText(/demo login:/i)).toBeInTheDocument();
  });
});

// P6-AUTH-4. A stored token the server refuses at boot is cleared here only: the
// automatic clears never send a server sign-out, and a fresh sign-in is not bounced.
describe('a stored session the server does not accept at boot (P6-AUTH-4)', () => {
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
    cache = null;
  });

  it('a pre-session token is cleared once, sends no sign-out, and a fresh sign-in holds (F-10, F-05 iii)', async () => {
    // Issued before sessions existed: no session in the model, so /auth/me answers 401.
    const screen = renderProductionApp(<App />, '/settings', { token: 'legacy-token-without-sid', probe: <CacheProbe /> });
    expect(await screen.findByRole('button', { name: /^sign in$/i })).toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe('/sign-in'));
    expect(localStorage.getItem('signalnest-token')).toBeNull();
    expect(cache!.getQueryCache().getAll().filter((q) => q.state.data !== undefined)).toEqual([]);
    expect(orgModel.signOuts()).toEqual([]);
    // Once: no further session reads after the clear.
    const reads = orgModel.count('GET', /\/auth\/me$/);
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(orgModel.count('GET', /\/auth\/me$/)).toBe(reads);

    // A fresh sign-in opens a new session, and the app stays signed in.
    orgModel.addUser({ id: 'user-lee', email: 'lee@example.com', full_name: 'Lee Later', password: 'lee password 1' });
    orgModel.setMembership('org-1', 'user-lee', 'owner');
    await screen.user.type(screen.getByLabelText(/^email/i), 'lee@example.com');
    await screen.user.type(screen.getByLabelText(/^password/i), 'lee password 1');
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
    await waitFor(() => expect(window.location.pathname).toBe('/settings'));
    const fresh = localStorage.getItem('signalnest-token')!;
    expect(fresh).toMatch(/^access-user-lee-/);
    expect(orgModel.accepts(fresh)).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(window.location.pathname).toBe('/settings');
    expect(localStorage.getItem('signalnest-token')).toBe(fresh);
    expect(orgModel.signOuts()).toEqual([]);
  });

  it('a boot session read that cannot reach the server clears locally and sends no sign-out (F-05 iv)', async () => {
    server.use(http.get(`*${API_PREFIX}/auth/me`, () => HttpResponse.error()));
    const screen = renderProductionApp(<App />, '/settings', { token: 'test-token' });
    expect(await screen.findByRole('button', { name: /^sign in$/i })).toBeInTheDocument();
    expect(localStorage.getItem('signalnest-token')).toBeNull();
    expect(orgModel.signOuts()).toEqual([]);
    // Disclosed residual (TC-3): that session was never revoked; it ends at its 12-hour limit.
    expect(orgModel.accepts('test-token')).toBe(true);
  });
});
