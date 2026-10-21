import { waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { orgModel } from '@/test/handlers';
import { renderProductionApp } from '@/test/utils';

/**
 * An unknown path is a 404 for everyone (P6-UI-024). The catch-all used to sit inside
 * ProtectedRoute, so a signed-out visitor was redirected to sign-in, never saw the 404,
 * and was then sent on to the bad path after signing in. A real route still redirects.
 */

beforeEach(() => {
  orgModel.reset();
});

describe('an unknown path when signed out (P6-UI-024)', () => {
  it('shows the 404 at that address instead of redirecting to sign-in', async () => {
    const screen = renderProductionApp(<App />, '/no-such-page');

    expect(await screen.findByText(/page not found/i)).toBeInTheDocument();
    expect(window.location.pathname).toBe('/no-such-page');
    expect(screen.queryByRole('button', { name: /^sign in$/i })).not.toBeInTheDocument();
    // No app shell without a session.
    expect(screen.queryByRole('banner')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /account menu/i })).not.toBeInTheDocument();
  });

  it('offers sign-in, and signing in lands on the overview rather than the unknown path', async () => {
    const screen = renderProductionApp(<App />, '/scout-requests/no-such/deeper');
    await screen.findByText(/page not found/i);
    expect(screen.queryByRole('link', { name: /back to overview/i })).not.toBeInTheDocument();

    await screen.user.click(screen.getByRole('link', { name: /^sign in$/i }));
    await waitFor(() => expect(window.location.pathname).toBe('/sign-in'));
    await screen.user.type(screen.getByLabelText(/^email/i), 'demo@signalnest.dev');
    await screen.user.type(screen.getByLabelText(/^password/i), 'any password');
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));

    await waitFor(() => expect(window.location.pathname).toBe('/'));
    expect(await screen.findByRole('heading', { name: /^overview$/i })).toBeInTheDocument();
  });

  it('still redirects a real protected route to sign-in, and returns there after signing in', async () => {
    const screen = renderProductionApp(<App />, '/opportunities');
    expect(await screen.findByRole('button', { name: /^sign in$/i })).toBeInTheDocument();
    expect(window.location.pathname).toBe('/sign-in');
    expect(screen.queryByText(/page not found/i)).not.toBeInTheDocument();
  });
});

describe('an unknown path when signed in (P6-UI-024)', () => {
  it('shows the 404 inside the app shell, as before, with a way back to the overview', async () => {
    const screen = renderProductionApp(<App />, '/no-such-page', { token: 'test-token' });

    expect(await screen.findByText(/page not found/i)).toBeInTheDocument();
    const header = await screen.findByRole('banner');
    expect(within(header).getAllByRole('button', { name: /account menu/i }).length).toBeGreaterThan(0);
    expect(within(screen.getByRole('main')).getByText(/page not found/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /back to overview/i })).toHaveAttribute('href', '/');
    expect(window.location.pathname).toBe('/no-such-page');
  });
});
