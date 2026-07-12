import { useEffect } from 'react';
import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { Spinner } from '@/components/ui/spinner';
import { useAuth } from './AuthContext';

export function ProtectedRoute() {
  const { status, setIntendedPath } = useAuth();
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
    return <Navigate to="/sign-in" replace />;
  }

  return <Outlet />;
}
