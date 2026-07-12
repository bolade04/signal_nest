import { waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { SignInPage } from '@/pages/auth/SignIn';
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
