import { act, waitFor } from '@testing-library/react';
import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { orgModel } from '@/test/handlers';
import { renderApp, renderProductionApp } from '@/test/utils';

/** The 6B-4B additions to the existing sign-in and registration pages (P6-AUTH-2, P6-UI-003). */

const NOTICE = 'Your password has been reset. Sign in with your new password.';

let navigateTo: ReturnType<typeof useNavigate> | null = null;
function NavigateProbe() {
  const navigate = useNavigate();
  useEffect(() => {
    navigateTo = navigate;
  }, [navigate]);
  return null;
}

beforeEach(() => {
  orgModel.reset();
  navigateTo = null;
});

afterEach(() => {
  expect(orgModel.violations()).toEqual([]);
  window.history.replaceState(null, '', '/');
});

describe('sign-in', () => {
  it('offers "Forgot password?" next to the password field', async () => {
    const screen = renderApp(<App />, { route: '/sign-in', authed: false });
    const link = await screen.findByRole('link', { name: /forgot password\?/i });
    expect(link).toHaveAttribute('href', '/forgot-password');
    // The existing form is intact.
    expect(screen.getByRole('button', { name: /^sign in$/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /create one/i })).toHaveAttribute('href', '/register');
  });

  it('shows the reset notice only when a completed reset hands it over in router state', async () => {
    const screen = renderProductionApp(<App />, '/sign-in', { probe: <NavigateProbe /> });
    await screen.findByRole('button', { name: /^sign in$/i });
    expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();

    act(() => navigateTo!('/sign-in', { replace: true, state: { notice: 'password-reset' } }));
    expect(await screen.findByText(NOTICE)).toBeInTheDocument();
    expect(screen.getByText(NOTICE)).toHaveAttribute('role', 'status');

    // Another notice value says nothing.
    act(() => navigateTo!('/sign-in', { replace: true, state: { notice: 'something-else' } }));
    await waitFor(() => expect(screen.queryByText(NOTICE)).not.toBeInTheDocument());
  });

  it('never takes the notice from the URL', async () => {
    const screen = renderProductionApp(<App />, '/sign-in?notice=password-reset#notice=password-reset');
    await screen.findByRole('button', { name: /^sign in$/i });
    expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
  });
});

describe('registration password bounds', () => {
  const registerRequests = () => orgModel.count('POST', /\/auth\/register$/);

  async function register(password: string) {
    const screen = renderApp(<App />, { route: '/register', authed: false });
    await screen.user.type(await screen.findByLabelText(/^full name/i), 'Nia New');
    await screen.user.type(screen.getByLabelText(/^organization name/i), 'Nia Co');
    await screen.user.type(screen.getByLabelText(/^work email/i), 'nia@example.com');
    await screen.user.type(screen.getByLabelText(/^password/i), password);
    await screen.user.click(screen.getByRole('button', { name: /^create account$/i }));
    return screen;
  }

  it.each([8, 128])('accepts %i characters', async (length) => {
    await register('x'.repeat(length));
    await waitFor(() => expect(registerRequests()).toBe(1));
    const body = orgModel.requests().find((r) => r.path.endsWith('/auth/register'))!.body as { password: string };
    expect(body.password).toHaveLength(length);
  });

  it('rejects 129 characters before sending anything', async () => {
    const screen = await register('x'.repeat(129));
    expect(await screen.findByText('Use at most 128 characters')).toBeInTheDocument();
    expect(registerRequests()).toBe(0);
  });

  it('still rejects 7 characters before sending anything', async () => {
    const screen = await register('x'.repeat(7));
    expect(await screen.findByText('Use at least 8 characters')).toBeInTheDocument();
    expect(registerRequests()).toBe(0);
  });
});
