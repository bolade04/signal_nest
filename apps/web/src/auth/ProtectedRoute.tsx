import { useEffect } from 'react';
import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { Spinner } from '@/components/ui/spinner';
import { useAuth } from './AuthContext';
import { SESSION_ENDED_NOTICE } from './session-ended';

export function ProtectedRoute() {
  const { status, sessionEnded, setIntendedPath } = useAuth();
  const location = useLocation();

  useEffect(() => {
    if (status === 'unauthenticated') {
      setIntendedPath(location.pathname + location.search);
    }
  }, [status, location.pathname, location.search, setIntendedPath]);

  if (status === 'loading') {
    return (
      <div className="flex h-full min-h-screen items-center justify-center">
        <Spinner className="size-6" />
      </div>
    );
  }

  if (status === 'unauthenticated') {
    // A session the server refused is announced on sign-in (P6-UI-013).
    return <Navigate to="/sign-in" replace state={sessionEnded ? { notice: SESSION_ENDED_NOTICE } : null} />;
  }

  return <Outlet />;
}
