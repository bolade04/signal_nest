import { useLocation } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { App } from '@/App';
import { renderApp, renderProductionApp } from '@/test/utils';

/**
 * The router configuration (P6-CI-7, react-router 6 → 7): one router setting for the app
 * and its test harness, mounted the way main.tsx mounts it, and no router warnings.
 */

// Every non-test source module, as text.
const sources = import.meta.glob(['/src/**/*.{ts,tsx}', '!/src/**/__tests__/**'], {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

const ROUTER_ELEMENT = /<(?:Browser|Memory|Hash|Static|Unstable_History|History)?Router\b|\bcreate(?:Browser|Memory|Hash|Static)Router\s*\(|<RouterProvider\b/;

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="router-location">{location.pathname + location.search}</output>;
}

describe('router configuration', () => {
  // React Router 6 warns once per module instance about every v7 behaviour the app has not
  // opted into ("Future Flag Warning"); vitest gives every test file its own module
  // instances and does not print the console of passing tests, so only a spy installed
  // before this file's first router render sees them. Keep this test first in the file.
  it('emits no React Router warning from the production or the test router', async () => {
    const warn = vi.spyOn(console, 'warn');
    const error = vi.spyOn(console, 'error');

    const production = renderProductionApp(<App />, '/sign-in');
    expect(await production.findByRole('heading', { level: 1, name: 'Sign in' })).toBeInTheDocument();
    production.unmount();

    const harness = renderApp(<App />, { route: '/sign-in', authed: false });
    expect(await harness.findByRole('heading', { level: 1, name: 'Sign in' })).toBeInTheDocument();

    const routerMessages = [...warn.mock.calls, ...error.mock.calls]
      .map((call) => call.map(String).join(' '))
      .filter((message) => /react router|react-router|future flag/i.test(message));
    expect(routerMessages).toEqual([]);
  });

  it('finds the router elements it looks for (positive instances)', () => {
    for (const planted of ['<BrowserRouter>', '<MemoryRouter initialEntries={[route]}>', '<Router location={l}>', 'createBrowserRouter(routes)', '<RouterProvider router={r} />']) {
      expect(planted).toMatch(ROUTER_ELEMENT);
    }
    expect('<Routes>').not.toMatch(ROUTER_ELEMENT);
  });

  it('mounts a router only in app/router.tsx (the app) and test/utils.tsx (the harness)', () => {
    const mounting = Object.keys(sources).filter((path) => ROUTER_ELEMENT.test(sources[path]!)).sort();
    expect(mounting).toEqual(['/src/app/router.tsx', '/src/test/utils.tsx']);
    expect(sources['/src/main.tsx']).toMatch(/<AppRouter>\s*<AppProviders>\s*<App \/>/);
    expect(sources['/src/app/router.tsx']).toMatch(/<BrowserRouter useTransitions=\{ROUTER_USE_TRANSITIONS\}>/);
    expect(sources['/src/app/router.tsx']).toMatch(/^export const ROUTER_USE_TRANSITIONS = false;$/m);
  });

  it('gives the test harness the app router and its transition setting', () => {
    const harness = sources['/src/test/utils.tsx']!;
    expect(harness).toMatch(/<MemoryRouter initialEntries=\{\[route\]\} useTransitions=\{ROUTER_USE_TRANSITIONS\}>/);
    expect(harness).toMatch(/<AppRouter>\s*<AppProviders>/);
    expect(harness.match(/<MemoryRouter\b/g)).toHaveLength(1);
  });

  it('returns a signed-out deep link to its path after sign-in under the production router', async () => {
    const screen = renderProductionApp(
      <>
        <App />
        <LocationProbe />
      </>,
      '/settings?from=deep-link',
    );
    expect(await screen.findByRole('heading', { level: 1, name: 'Sign in' })).toBeInTheDocument();
    expect(window.location.pathname).toBe('/sign-in');

    await screen.user.click(screen.getByRole('button', { name: /use demo account/i }));
    expect(await screen.findByRole('heading', { level: 1, name: 'Settings' })).toBeInTheDocument();
    expect(screen.getByTestId('router-location').textContent).toBe('/settings?from=deep-link');
    expect(window.location.pathname + window.location.search).toBe('/settings?from=deep-link');
  });
});
