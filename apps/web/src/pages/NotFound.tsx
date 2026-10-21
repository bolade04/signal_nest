import { Compass } from 'lucide-react';
import { Link, Outlet } from 'react-router-dom';
import { useAuth } from '@/auth/AuthContext';
import { AppShell } from '@/components/layout/app-shell';
import { Button } from '@/components/ui/button';
import { EmptyState } from '@/components/common/states';
import { Spinner } from '@/components/ui/spinner';

export function NotFoundPage() {
  const { status } = useAuth();
  const signedIn = status === 'authenticated';

  return (
    <div className="py-16">
      <EmptyState
        icon={Compass}
        title="Page not found"
        description="That route doesn't exist. It may have moved, or it's a capability arriving in a later phase."
        action={
          <Button asChild>
            {signedIn ? <Link to="/">Back to overview</Link> : <Link to="/sign-in">Sign in</Link>}
          </Button>
        }
      />
    </div>
  );
}

/**
 * The catch-all route's layout (P6-UI-024): an unknown path is a 404 for everyone.
 * Signed in, it renders inside the app shell as before; signed out, on its own — no
 * redirect to sign-in, which hid the 404 and then sent the user on to the bad path.
 * Only paths no route declares reach it, so a real protected route still redirects.
 */
export function NotFoundLayout() {
  const { status } = useAuth();

  if (status === 'loading') {
    return (
      <div className="flex h-full min-h-screen items-center justify-center">
        <Spinner className="size-6" />
      </div>
    );
  }
  if (status === 'authenticated') return <AppShell />;
  return (
    <main className="mx-auto flex min-h-screen w-full max-w-lg items-center px-4">
      <div className="w-full">
        <Outlet />
      </div>
    </main>
  );
}
