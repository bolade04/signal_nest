import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StrictMode, type ReactElement, type ReactNode } from 'react';
import { BrowserRouter, MemoryRouter } from 'react-router-dom';
import { vi } from 'vitest';
import { setAuthToken } from '@/api/client';
import { AppProviders } from '@/app/providers';
import { ThemeProvider } from '@/app/theme';
import { AuthProvider } from '@/auth/AuthContext';
import { ToastProvider } from '@/components/ui/toast';
import { TooltipProvider } from '@/components/ui/tooltip';
import { WorkspaceProvider } from '@/workspace/WorkspaceContext';

interface RenderOptions {
  route?: string;
  /** Seed an authenticated session (default true). */
  authed?: boolean;
  /** Pre-select an active location (persisted key the WorkspaceContext reads). */
  activeLocation?: string;
  /** Pre-select an active organization, as a returning browser has it persisted. */
  activeOrganization?: string;
}

export function renderApp(
  ui: ReactElement,
  { route = '/', authed = true, activeLocation, activeOrganization }: RenderOptions = {},
): RenderResult & { user: ReturnType<typeof userEvent.setup> } {
  localStorage.clear();
  setAuthToken(null);
  if (authed) {
    localStorage.setItem('signalnest-token', 'test-token');
    setAuthToken('test-token');
  }
  if (activeLocation) localStorage.setItem('signalnest-active-location', activeLocation);
  if (activeOrganization) localStorage.setItem('signalnest-active-org', activeOrganization);

  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });

  const result = render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <ToastProvider>
          <TooltipProvider delayDuration={0}>
            <AuthProvider>
              <WorkspaceProvider>
                <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
              </WorkspaceProvider>
            </AuthProvider>
          </TooltipProvider>
        </ToastProvider>
      </ThemeProvider>
    </QueryClientProvider>,
  );

  return { ...result, user: userEvent.setup() };
}

interface ProductionRenderOptions {
  /** A session token already held by this browser (none = signed out). */
  token?: string;
  /** Extra test-only children rendered inside the providers, e.g. a cache probe. */
  probe?: ReactNode;
}

/**
 * Render `ui` (normally `<App />`) exactly as main.tsx does — StrictMode,
 * BrowserRouter and the production AppProviders (the real QueryClient: 30 s
 * staleTime, 5 min gcTime) — at `url`, which is written to the jsdom address bar
 * first. Each call is a fresh
 * browser: storage and cookies are cleared, so a test can play a second user by
 * unmounting and rendering again while the MSW model keeps the backend state.
 */
export function renderProductionApp(
  ui: ReactElement,
  url: string,
  { token, probe }: ProductionRenderOptions = {},
): RenderResult & { user: ReturnType<typeof userEvent.setup> } {
  localStorage.clear();
  sessionStorage.clear();
  for (const cookie of document.cookie.split(';')) {
    const name = cookie.split('=')[0]?.trim();
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  }
  setAuthToken(null);
  if (token) localStorage.setItem('signalnest-token', token);
  window.history.replaceState(null, '', url);

  const result = render(
    <StrictMode>
      <BrowserRouter>
        <AppProviders>
          {ui}
          {probe}
        </AppProviders>
      </BrowserRouter>
    </StrictMode>,
  );

  return { ...result, user: userEvent.setup() };
}

/**
 * A browser keeps a closing Radix dialog on screen for its exit animation (the
 * `data-[state=closed]:animate-out` classes); jsdom runs no CSS, so Radix Presence
 * unmounts it at once. This gives every element that carries `data-state` a LIVE
 * computed `animation-name` ("enter" while open, "exit" once closed), which is what
 * Presence reads to decide to wait for `animationend`. `finish()` ends the animations.
 */
export function emulateExitAnimations() {
  const realGetComputedStyle = window.getComputedStyle.bind(window);
  const spy = vi.spyOn(window, 'getComputedStyle').mockImplementation((element, pseudo) => {
    const styles = realGetComputedStyle(element, pseudo);
    if (!(element instanceof HTMLElement) || !element.hasAttribute('data-state')) return styles;
    return new Proxy(styles, {
      get(target, property) {
        if (property === 'animationName') return element.getAttribute('data-state') === 'open' ? 'enter' : 'exit';
        const value = Reflect.get(target, property, target) as unknown;
        return typeof value === 'function' ? (value as (...args: unknown[]) => unknown).bind(target) : value;
      },
    });
  });
  const hadCss = 'CSS' in globalThis;
  if (!hadCss) (globalThis as { CSS?: unknown }).CSS = { escape: (value: string) => value };
  return {
    finish() {
      for (const element of Array.from(document.querySelectorAll('[data-state="closed"]'))) {
        const end = new Event('animationend', { bubbles: true });
        Object.defineProperty(end, 'animationName', { value: 'exit' });
        act(() => {
          element.dispatchEvent(end);
        });
      }
    },
    restore() {
      spy.mockRestore();
      if (!hadCss) delete (globalThis as { CSS?: unknown }).CSS;
    },
  };
}
