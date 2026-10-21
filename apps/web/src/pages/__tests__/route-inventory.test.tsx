import { within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { useLocation } from 'react-router-dom';
import { useAuth } from '@/auth/AuthContext';
import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { server } from '@/test/server';
import { renderApp, renderProductionApp } from '@/test/utils';

/**
 * The route inventory of docs/project-phase-6-plan.md §5A.3 — 16 declared routes and the
 * catch-all — as the router resolves them (P6-CI-7, react-router 6 → 7). Written and
 * passing on react-router 6.30.6 before the version change, and kept unchanged across it,
 * so a router upgrade that resolves, guards or redirects any route differently fails here.
 */

/** The router's current URL, as the app sees it. */
function LocationProbe() {
  const location = useLocation();
  return <output data-testid="router-location">{location.pathname + location.search}</output>;
}

const app = (
  <>
    <App />
    <LocationProbe />
  </>
);

type Screen = ReturnType<typeof renderApp>;

const routerLocation = (screen: Screen) => screen.getByTestId('router-location').textContent;

const P = (path: string) => `*${API_PREFIX}${path}`;

const nonOperatorSession = () =>
  server.use(
    http.get(P('/auth/me'), () =>
      HttpResponse.json({
        access_token: 'test-token',
        token_type: 'bearer',
        user: { id: 'user-2', email: 'customer@signalnest.dev', full_name: 'Customer', is_operator: false },
        memberships: [{ organization_id: 'org-1', role: 'owner' }],
      }),
    ),
  );

/** Public routes: reachable signed out, each with the heading its page renders. */
const PUBLIC_ROUTES: ReadonlyArray<readonly [path: string, heading: string]> = [
  ['/sign-in', 'Sign in'],
  ['/register', 'Create your workspace'],
  ['/invite', 'Your invitation'],
  ['/forgot-password', 'Reset your password'],
  ['/reset-password', 'Choose a new password'],
  ['/verify-email', 'Verify your email address'],
];

/** Public routes that stay usable while signed in (App.tsx: an invitee or a mailed link). */
const PUBLIC_WHILE_SIGNED_IN = PUBLIC_ROUTES.filter(([path]) => path !== '/sign-in' && path !== '/register');

/** Protected routes inside the shell: path, heading, and the sidebar entry that is active. */
const PROTECTED_ROUTES: ReadonlyArray<readonly [path: string, heading: string, activeNav: string]> = [
  ['/', 'Overview', 'Overview'],
  ['/onboarding', 'Guided onboarding', 'Onboarding'],
  ['/context', 'Campaign context', 'Campaign Context'],
  ['/locations', 'Locations', 'Locations'],
  ['/scout-requests', 'Scout requests', 'Scout Requests'],
  ['/scout-requests/scout-loc-dallas', 'Dallas demand scan', 'Scout Requests'],
  ['/opportunities', 'Opportunities', 'Opportunities'],
  ['/opportunities/opp-loc-dallas-0', 'Dallas customers want faster delivery 1', 'Opportunities'],
  ['/settings', 'Settings', 'Settings'],
  ['/operations', 'Operations', 'Operations'],
];

function activeNavLabels(screen: Screen): string[] {
  const nav = screen.getByRole('navigation', { name: 'Primary' });
  return within(nav)
    .getAllByRole('link')
    .filter((link) => link.getAttribute('aria-current') === 'page')
    .map((link) => link.textContent ?? '');
}

describe('route inventory (plan §5A.3)', () => {
  it('declares exactly the inventoried paths', () => {
    // 6 public + 10 protected = 16 declared routes; the catch-all is covered below.
    expect(PUBLIC_ROUTES.length + PROTECTED_ROUTES.length).toBe(16);
  });

  it.each(PUBLIC_ROUTES)('%s renders its public page signed out', async (path, heading) => {
    const screen = renderApp(app, { route: path, authed: false });
    expect(await screen.findByRole('heading', { level: 1, name: heading })).toBeInTheDocument();
    expect(routerLocation(screen)).toBe(path);
    expect(screen.queryByRole('navigation', { name: 'Primary' })).not.toBeInTheDocument();
  });

  it.each(PUBLIC_WHILE_SIGNED_IN)('%s stays usable while signed in', async (path, heading) => {
    const screen = renderApp(app, { route: path });
    expect(await screen.findByRole('heading', { level: 1, name: heading })).toBeInTheDocument();
    expect(routerLocation(screen)).toBe(path);
  });

  it.each([
    ['/sign-in'],
    ['/register'],
  ])('%s sends a signed-in visitor to the overview', async (path) => {
    const screen = renderApp(app, { route: path });
    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument();
    expect(routerLocation(screen)).toBe('/');
  });

  it.each(PROTECTED_ROUTES)('%s renders inside the shell for a signed-in operator', async (path, heading, activeNav) => {
    const screen = renderApp(app, { route: path });
    expect(await screen.findByRole('heading', { level: 1, name: heading })).toBeInTheDocument();
    expect(routerLocation(screen)).toBe(path);
    expect(activeNavLabels(screen)).toEqual([activeNav]);
  });

  it.each(PROTECTED_ROUTES)('%s sends a signed-out visitor to sign-in, then back after sign-in', async (path, heading) => {
    // The opportunities page rewrites its query to its filter state, so it gets a query it keeps.
    const target = `${path}?${path === '/opportunities' ? 'search=Dallas' : 'from=inventory'}`;
    const screen = renderApp(app, { route: target, authed: false });
    expect(await screen.findByRole('heading', { level: 1, name: 'Sign in' })).toBeInTheDocument();
    expect(routerLocation(screen)).toBe('/sign-in');
    expect(screen.queryByRole('heading', { level: 1, name: heading })).not.toBeInTheDocument();

    await screen.user.click(screen.getByRole('button', { name: /use demo account/i }));
    expect(await screen.findByRole('heading', { level: 1, name: heading })).toBeInTheDocument();
    expect(routerLocation(screen)).toBe(target);
  });

  it('renders the not-found page in place of /operations for a non-operator', async () => {
    nonOperatorSession();
    const screen = renderApp(app, { route: '/operations' });
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
    expect(routerLocation(screen)).toBe('/operations');
    expect(screen.queryByRole('heading', { level: 1, name: 'Operations' })).not.toBeInTheDocument();
    const nav = screen.getByRole('navigation', { name: 'Primary' });
    expect(within(nav).queryByRole('link', { name: 'Operations' })).not.toBeInTheDocument();
  });

  it('renders the catch-all not-found page inside the shell for an unknown path', async () => {
    const screen = renderApp(app, { route: '/no-such-page/deeper?x=1' });
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
    expect(routerLocation(screen)).toBe('/no-such-page/deeper?x=1');
    expect(activeNavLabels(screen)).toEqual([]);

    await screen.user.click(screen.getByRole('link', { name: 'Back to overview' }));
    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument();
    expect(routerLocation(screen)).toBe('/');
  });

  // Changed on purpose by P6-UI-024 (after the react-router 7 migration): this case used
  // to pin the redirect to sign-in that the row removes.
  it('renders the not-found page at an unknown path when signed out, without the shell', async () => {
    const screen = renderApp(app, { route: '/no-such-page', authed: false });
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
    expect(routerLocation(screen)).toBe('/no-such-page');
    expect(screen.queryByRole('heading', { level: 1, name: 'Sign in' })).not.toBeInTheDocument();
    expect(screen.queryByRole('navigation', { name: 'Primary' })).not.toBeInTheDocument();
  });

  it('navigates by sidebar link, card link and history back', async () => {
    const screen = renderApp(app, { route: '/' });
    await screen.findByRole('heading', { level: 1, name: 'Overview' });

    const nav = screen.getByRole('navigation', { name: 'Primary' });
    await screen.user.click(within(nav).getByRole('link', { name: 'Opportunities' }));
    expect(await screen.findByRole('heading', { level: 1, name: 'Opportunities' })).toBeInTheDocument();
    expect(routerLocation(screen)).toBe('/opportunities');
    expect(activeNavLabels(screen)).toEqual(['Opportunities']);

    const card = await screen.findByRole('link', { name: /Dallas customers want faster delivery 1\b/ });
    await screen.user.click(card);
    expect(
      await screen.findByRole('heading', { level: 1, name: 'Dallas customers want faster delivery 1' }),
    ).toBeInTheDocument();
    expect(routerLocation(screen)).toBe('/opportunities/opp-loc-dallas-0');

    await screen.user.click(screen.getByRole('button', { name: /back/i }));
    expect(await screen.findByRole('heading', { level: 1, name: 'Opportunities' })).toBeInTheDocument();
    expect(routerLocation(screen)).toBe('/opportunities');
  });
});

/**
 * PR #234 F1: the intended path is whatever the address bar held. React Router 7 throws
 * "External navigation is not allowed" from `<Navigate>` when a path resolves to another
 * origin, which blanked the page after sign-in on `//evil.example`. Every such link must
 * land inside the app after sign-in, and a normal deep link must still return to itself.
 */
describe('sign-in returns only to app paths (PR #234 F1)', () => {
  it.each([
    ['/settings?next=//evil.example', '/settings?next=//evil.example', 'Settings'],
    ['/settings?from=deep-link', '/settings?from=deep-link', 'Settings'],
  ])('%j signed out lands on %j after sign-in', async (path, landing, heading) => {
    const screen = renderApp(app, { route: path, authed: false });
    expect(await screen.findByRole('heading', { level: 1, name: 'Sign in' })).toBeInTheDocument();
    expect(routerLocation(screen)).toBe('/sign-in');

    await screen.user.click(screen.getByRole('button', { name: /use demo account/i }));
    expect(await screen.findByRole('heading', { name: heading })).toBeInTheDocument();
    expect(routerLocation(screen)).toBe(landing);
  });

  // Since P6-UI-024 an unknown path signed out is a 404 at that address rather than a
  // redirect to sign-in, so these paths are never stored as the intended path at all.
  // The F1 guarantee still holds and is what these cases pin: signing in from there
  // lands inside the app (on the overview), never on another origin.
  it.each([
    ['//evil.example'],
    ['/\\evil.example'],
    ['/\t/evil.example'],
    ['/%5Cevil.example'],
    ['/%2F%2Fevil.example'],
  ])('%j signed out shows the 404, and signing in from it lands on the overview', async (path) => {
    const screen = renderApp(app, { route: path, authed: false });
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { level: 1, name: 'Sign in' })).not.toBeInTheDocument();

    await screen.user.click(screen.getByRole('link', { name: /^sign in$/i }));
    expect(await screen.findByRole('heading', { level: 1, name: 'Sign in' })).toBeInTheDocument();
    await screen.user.click(screen.getByRole('button', { name: /use demo account/i }));
    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument();
    expect(routerLocation(screen)).toBe('/');
  });

  it.each([['//evil.example'], ['/%5Cevil.example'], ['/%2F%2Fevil.example']])(
    '%j under the production router shows the 404, and signing in from it lands on the overview',
    async (path) => {
      // The address bar holds the path on this origin, as a browser does for <app>//evil.example.
      const screen = renderProductionApp(app, `${window.location.origin}${path}`);
      expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument();

      await screen.user.click(screen.getByRole('link', { name: /^sign in$/i }));
      await screen.user.click(await screen.findByRole('button', { name: /use demo account/i }));
      expect(await screen.findByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument();
      expect(routerLocation(screen)).toBe('/');
      expect(window.location.origin + window.location.pathname).toBe(`${window.location.origin}/`);
    },
  );

  it('stores only an app path, whichever producer sets it (ProtectedRoute, sign-out)', async () => {
    function Producer() {
      const { intendedPath, setIntendedPath } = useAuth();
      return (
        <>
          <button type="button" onClick={() => setIntendedPath('//evil.example/x?y=//z')}>
            store
          </button>
          <output data-testid="stored">{String(intendedPath)}</output>
        </>
      );
    }
    const screen = renderApp(<Producer />, { authed: false });
    await screen.user.click(screen.getByRole('button', { name: 'store' }));
    expect(screen.getByTestId('stored').textContent).toBe('/evil.example/x?y=//z');
  });

  it.each([['//evil.example'], ['/%5Cevil.example'], ['/%2F%2Fevil.example']])(
    '%j signed in renders the not-found page inside the shell',
    async (path) => {
      const screen = renderApp(app, { route: path });
      expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
      expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument();
    },
  );
});
