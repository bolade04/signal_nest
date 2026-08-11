import { Outlet } from 'react-router-dom';
import { Spinner } from '@/components/ui/spinner';
import { NotFoundPage } from '@/pages/NotFound';
import { useAuth } from './AuthContext';

/**
 * Route guard for operator-only surfaces, composed inside ProtectedRoute.
 *
 * The operator signal is server-authoritative: it is `user.is_operator` from the
 * authenticated session (`GET /auth/me`), never derived or persisted on the
 * client. This guard is UX only — every operator endpoint is independently
 * enforced by the backend's `require_operator` dependency, so bypassing the
 * guard yields 403s, never data.
 *
 * Non-operators see the not-found page rendered in place rather than a
 * redirect, so the existence of operator routes is not enumerable from a
 * customer session — mirroring the backend's non-enumerating 404 discipline.
 */
export function RequireOperator() {
  const { status, user } = useAuth();

  // ProtectedRoute already blocks until the session resolves, but keep the
  // loading branch so this guard stays safe if composed elsewhere.
  if (status === 'loading') {
    return (
      <div className="flex h-full min-h-screen items-center justify-center">
        <Spinner className="size-6" />
      </div>
    );
  }

  if (!(user?.is_operator ?? false)) {
    return <NotFoundPage />;
  }

  return <Outlet />;
}
