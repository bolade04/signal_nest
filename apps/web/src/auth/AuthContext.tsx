import { useQueryClient } from '@tanstack/react-query';
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { setAuthToken, setUnauthorizedHandler } from '@/api/client';
import * as api from '@/api/endpoints';
import type { LoginRequest, MembershipOut, RegisterRequest, SessionOut, UserOut } from '@/api/types';

const TOKEN_KEY = 'signalnest-token';

interface AuthContextValue {
  status: 'loading' | 'authenticated' | 'unauthenticated';
  user: UserOut | null;
  memberships: MembershipOut[];
  token: string | null;
  login: (body: LoginRequest) => Promise<void>;
  register: (body: RegisterRequest) => Promise<void>;
  logout: () => void;
  /** Path the user was trying to reach before being bounced to sign-in. */
  intendedPath: string | null;
  setIntendedPath: (path: string | null) => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const [token, setToken] = useState<string | null>(() => localStorage.getItem(TOKEN_KEY));
  const [user, setUser] = useState<UserOut | null>(null);
  const [memberships, setMemberships] = useState<MembershipOut[]>([]);
  const [status, setStatus] = useState<AuthContextValue['status']>(token ? 'loading' : 'unauthenticated');
  const intendedPathRef = useRef<string | null>(null);
  const [intendedPath, setIntendedPathState] = useState<string | null>(null);

  const applySession = useCallback((session: SessionOut) => {
    localStorage.setItem(TOKEN_KEY, session.access_token);
    setAuthToken(session.access_token);
    setToken(session.access_token);
    setUser(session.user);
    setMemberships(session.memberships);
    setStatus('authenticated');
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY);
    setAuthToken(null);
    setToken(null);
    setUser(null);
    setMemberships([]);
    setStatus('unauthenticated');
    queryClient.clear();
  }, [queryClient]);

  const setIntendedPath = useCallback((path: string | null) => {
    intendedPathRef.current = path;
    setIntendedPathState(path);
  }, []);

  // Register the 401 handler once so any expired-token response logs the user out.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (localStorage.getItem(TOKEN_KEY)) logout();
    });
    return () => setUnauthorizedHandler(null);
  }, [logout]);

  // Validate a persisted token on first load.
  useEffect(() => {
    const existing = localStorage.getItem(TOKEN_KEY);
    // status is already seeded to 'unauthenticated' when there is no token
    // (see useState initializer), so no setState is needed on this path.
    if (!existing) return;
    setAuthToken(existing);
    const controller = new AbortController();
    api
      .getSession(controller.signal)
      .then((session) => applySession(session))
      .catch((err) => {
        if (controller.signal.aborted) return;
        localStorage.removeItem(TOKEN_KEY);
        setAuthToken(null);
        setToken(null);
        setStatus('unauthenticated');
        void err;
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const login = useCallback(
    async (body: LoginRequest) => {
      const session = await api.login(body);
      applySession(session);
    },
    [applySession],
  );

  const register = useCallback(
    async (body: RegisterRequest) => {
      const session = await api.register(body);
      applySession(session);
    },
    [applySession],
  );

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      memberships,
      token,
      login,
      register,
      logout,
      intendedPath,
      setIntendedPath,
    }),
    [status, user, memberships, token, login, register, logout, intendedPath, setIntendedPath],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}
