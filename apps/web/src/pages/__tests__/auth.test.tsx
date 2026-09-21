import { waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { SignInPage } from '@/pages/auth/SignIn';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

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
