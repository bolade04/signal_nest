import { useCallback, useEffect, useLayoutEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { ApiError } from '@/api/client';
import {
  apiErrorCode,
  fragmentHasInviteToken,
  isPlausibleInviteToken,
  parseInviteFragment,
  scrubInviteFragment,
} from '@/pages/invite/invite-token';

// Password-reset and email-verification links in the browser (P6-AUTH-2).
//
// The backend mails `<origin>/reset-password#token=<raw>` and
// `<origin>/verify-email#token=<raw>`: the same `#token=` fragment key and the same
// 128-character bound as an invitation link, so the invitation helpers are reused as
// they are. The token is handled exactly as on the invite page: read from the address
// bar into component memory, the address bar scrubbed before the first paint and before
// any request, and from there only into the JSON body of the confirm call — never into
// localStorage, sessionStorage, a cookie, a React Query key, cache entry or mutation,
// the console, the page, its attributes or its title.

export interface FragmentToken {
  /** The captured token when it could be an issued one; null when missing, malformed or discarded. */
  token: string | null;
  /** Counts the links adopted after the first, so a page can start over for a new one. */
  generation: number;
  /** Forget the token: it was spent, or the server refused it for good. */
  discard: () => void;
}

/**
 * The `#token=` of the link that opened this page, held in component memory only.
 * Same capture, scrub and adoption rules as `InvitePage`.
 */
export function useFragmentToken(): FragmentToken {
  const location = useLocation();
  const navigate = useNavigate();

  // Captured once, from the address bar (falling back to the router's copy of the
  // same URL). The initializer only reads: StrictMode renders twice before anything
  // is scrubbed, and its simulated remount keeps this state, so the token survives
  // both while the address bar is still scrubbed before the first paint.
  const [token, setToken] = useState<string | null>(
    () => parseInviteFragment(window.location.hash) ?? parseInviteFragment(location.hash),
  );
  const [generation, setGeneration] = useState(0);
  // The router location the token was read from, or the latest one seen since.
  const [seenLocation, setSeenLocation] = useState(location);

  const adoptToken = useCallback((next: string | null) => {
    setToken(next);
    setGeneration((n) => n + 1);
  }, []);

  // A second link opened in this tab is a fragment-only navigation: no reload and
  // no remount. Its token is adopted only from a NEW router location — never from
  // the one the current token was read from, which the router can still hold until
  // the sync below replaces it — so a scrubbed token cannot come back.
  if (location !== seenLocation) {
    setSeenLocation(location);
    const hash = fragmentHasInviteToken(window.location.hash) ? window.location.hash : location.hash;
    if (fragmentHasInviteToken(hash)) adoptToken(parseInviteFragment(hash));
  }

  // The router only follows popstate; the address bar's own hashchange is the
  // signal when nothing else re-renders the page.
  useEffect(() => {
    const onHashChange = () => {
      if (!fragmentHasInviteToken(window.location.hash)) return;
      adoptToken(parseInviteFragment(window.location.hash));
      scrubInviteFragment();
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, [adoptToken]);

  // A layout effect runs before the paint and before any request the page can make,
  // so the token is out of the address bar first. It re-runs after every capture
  // opportunity; idempotent, so StrictMode's replay is harmless.
  useLayoutEffect(() => {
    scrubInviteFragment();
  }, [token, location]);

  // React Router still holds the URL it was created with; replace it too, so that
  // useLocation().hash is '' as well. A passive effect on purpose: during the page's
  // layout effects the Router has not yet subscribed to its history. Nothing is
  // carried over: no hash, no state.
  useEffect(() => {
    if (!fragmentHasInviteToken(location.hash)) return;
    navigate(
      { pathname: location.pathname, search: location.search, hash: '' },
      { replace: true, state: null },
    );
  }, [location.hash, location.pathname, location.search, navigate]);

  const discard = useCallback(() => setToken(null), []);

  return { token: isPlausibleInviteToken(token) ? token : null, generation, discard };
}

/**
 * The `/sign-in` router state (`{ notice }`) a completed reset hands over, so the
 * sign-in page can say the new password is set. Router state only: never the URL.
 */
export const PASSWORD_RESET_NOTICE = 'password-reset';

// --- Error copy ----------------------------------------------------------------

/** Every error code the 6B-4A reset and verification routes can return. */
export type AccountTokenErrorCode =
  | 'password_reset_invalid'
  | 'email_verification_invalid'
  | 'email_verification_wrong_account'
  | 'email_already_verified';

export interface AccountTokenErrorCopy {
  title: string;
  description: string;
}

/** User-facing copy for each code, so no screen shows a raw backend error. */
export const ACCOUNT_TOKEN_ERROR_COPY: Readonly<Record<AccountTokenErrorCode, AccountTokenErrorCopy>> = {
  password_reset_invalid: {
    title: 'This reset link is invalid or has expired',
    description:
      'It may already have been used, or a newer link replaced it. Request a new link to reset your password.',
  },
  email_verification_invalid: {
    title: 'This verification link is invalid or has expired',
    description:
      'It may already have been used, or a newer link replaced it. Request a new one from the banner in the app.',
  },
  email_verification_wrong_account: {
    title: 'This link is for a different account',
    description: 'This verification link cannot be used with the currently signed-in account.',
  },
  email_already_verified: {
    title: 'Your email address is already verified',
    description: 'There is nothing more to do.',
  },
};

/** The account-token code carried by an API error, when it is one this module knows. */
export function accountTokenErrorCode(error: unknown): AccountTokenErrorCode | null {
  const code = apiErrorCode(error);
  return code !== null && Object.prototype.hasOwnProperty.call(ACCOUNT_TOKEN_ERROR_COPY, code)
    ? (code as AccountTokenErrorCode)
    : null;
}

/** The global rate limiter's 429, in this app's words (the server's own text is not shown). */
export const RATE_LIMITED_MESSAGE = 'There have been several attempts in a short time. Wait a few minutes, then try again.';

/**
 * Words this app wrote for a failure no code above explains: the rate limit's own
 * copy, the client's network message, else `fallback` — never the server's text.
 */
export function calmFailureMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    if (error.status === 429) return RATE_LIMITED_MESSAGE;
    // Status 0 is the client's own "could not reach the server" message.
    if (error.status === 0) return error.message;
  }
  return fallback;
}
