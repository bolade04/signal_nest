import { QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState } from 'react';
import { ApiError } from '@/api/client';
import { TooltipProvider } from '@/components/ui/tooltip';
import { ToastProvider } from '@/components/ui/toast';
import { AuthProvider } from '@/auth/AuthContext';
import { WorkspaceProvider } from '@/workspace/WorkspaceContext';
import { ThemeProvider } from './theme';

function createQueryClient(): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache(),
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: false,
        // Only retry safe, transient failures — never auth/validation errors.
        retry: (failureCount, error) => {
          if (error instanceof ApiError) {
            if (error.status === 0) return failureCount < 2; // network blip
            if (error.status >= 500) return failureCount < 2;
            return false;
          }
          return false;
        },
      },
      mutations: {
        // Mutations are never retried automatically to avoid duplicate writes.
        retry: false,
      },
    },
  });
}

export function AppProviders({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(createQueryClient);

  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <ToastProvider>
          <TooltipProvider delayDuration={200}>
            <AuthProvider>
              <WorkspaceProvider>{children}</WorkspaceProvider>
            </AuthProvider>
          </TooltipProvider>
        </ToastProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
