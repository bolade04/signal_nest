import { render } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { ROUTER_USE_TRANSITIONS } from '@/app/router';
import { SignInPage } from '@/pages/auth/SignIn';

/**
 * PR #234 F1, consumer side: SignIn itself never navigates to a path off this app, even
 * when the stored intended path did not come through AuthContext's setter (which already
 * reduces it). The session is mocked as signed in with a raw address-bar value.
 */
const session = vi.hoisted(() => ({ intendedPath: null as string | null }));

vi.mock('@/auth/AuthContext', () => ({
  useAuth: () => ({
    status: 'authenticated',
    login: vi.fn(),
    intendedPath: session.intendedPath,
    setIntendedPath: vi.fn(),
  }),
}));

function Landing() {
  const location = useLocation();
  return <output data-testid="landing">{location.pathname + location.search}</output>;
}

describe('SignIn returns only to app paths (PR #234 F1)', () => {
  it.each([
    ['//evil.example', '/evil.example'],
    ['/\\evil.example', '/evil.example'],
    ['/\t/evil.example', '/evil.example'],
    ['/settings?from=deep-link', '/settings?from=deep-link'],
  ])('a stored %j lands on %j', async (stored, landing) => {
    session.intendedPath = stored;
    const screen = render(
      <MemoryRouter initialEntries={['/sign-in']} useTransitions={ROUTER_USE_TRANSITIONS}>
        <Routes>
          <Route path="/sign-in" element={<SignInPage />} />
          <Route path="*" element={<Landing />} />
        </Routes>
      </MemoryRouter>,
    );
    expect((await screen.findByTestId('landing')).textContent).toBe(landing);
  });
});
