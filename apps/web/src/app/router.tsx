import type { ReactNode } from 'react';
import { BrowserRouter } from 'react-router-dom';

/**
 * Whether router state updates run inside `React.startTransition` (P6-CI-7, react-router
 * 6 → 7). React Router 7 wraps them by default; 6 did not. The auth flows update their own
 * state synchronously next to a navigation — sign-in clears the intended path while it
 * navigates there, a password change or reset clears the session while it navigates to
 * /sign-in with a notice — and rely on both committing in one render. Inside a transition
 * the navigation commits after the urgent auth update, so sign-in lands on `/` instead of
 * the intended path and the sign-in notices are lost (route-inventory.test.tsx and the
 * password-change, invitation and session tests fail). `false` keeps the 6.x behaviour:
 * no transition on any navigation or router state update. Shared with the test harness
 * (src/test/utils.tsx) so the tests run the router the way the app does.
 */
export const ROUTER_USE_TRANSITIONS = false;

/** The SPA's router, as main.tsx mounts it. */
export function AppRouter({ children }: { children: ReactNode }) {
  return <BrowserRouter useTransitions={ROUTER_USE_TRANSITIONS}>{children}</BrowserRouter>;
}
