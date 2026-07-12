import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { setAuthToken } from '@/api/client';
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
}

export function renderApp(
  ui: ReactElement,
  { route = '/', authed = true, activeLocation }: RenderOptions = {},
): RenderResult & { user: ReturnType<typeof userEvent.setup> } {
  localStorage.clear();
  setAuthToken(null);
  if (authed) {
    localStorage.setItem('signalnest-token', 'test-token');
    setAuthToken('test-token');
  }
  if (activeLocation) localStorage.setItem('signalnest-active-location', activeLocation);

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
