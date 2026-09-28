/**
 * Sign-out outcomes and the `/sign-in` notices they hand over (P6-AUTH-4).
 *
 * Every access token is bound to a server-side session, so a sign-out is a server
 * request first (`POST /auth/logout`, or `POST /auth/logout-all` for "Sign out
 * everywhere") and a local clear second. The outcome says what the server confirmed;
 * a local clear is never reported as a confirmed server revocation.
 */

/**
 * What `signOut()` achieved. The local session is cleared in every case.
 * - `server-revoked`: the server revoked this session (204).
 * - `already-ended`: the server no longer accepted this session (401) — it had ended —
 *   or this tab held no session at all (no request is sent).
 * - `local-only`: the server did not confirm (404, 5xx, network error or timeout), or no
 *   request could be sent because another tab had already cleared the stored session this
 *   tab was using; this browser is signed out, but the session may still be live until its
 *   12-hour limit.
 */
export type SignOutOutcome = 'server-revoked' | 'already-ended' | 'local-only';

/**
 * What `signOutEverywhere()` achieved.
 * - `server-revoked`: every session of the account was revoked (204); cleared locally.
 * - `already-ended`: this session had already ended (401), so the server revoked nothing
 *   on this request; cleared locally.
 * - `local-only`: another tab had already cleared the stored session this tab was using, so
 *   no request could be sent; cleared here, and nothing was confirmed by the server.
 * - `failed`: the server did not answer (404, 5xx, network error or timeout); NOTHING was
 *   cleared, so the user can retry.
 */
export type SignOutEverywhereOutcome = 'server-revoked' | 'already-ended' | 'local-only' | 'failed';

/** Bound on each server sign-out request, in milliseconds. */
export const SIGN_OUT_TIMEOUT_MS = 10_000;

/**
 * `/sign-in` router state (`{ notice }`) after a sign-out the server could not confirm.
 * Router state only: never the URL.
 */
export const SIGN_OUT_UNCONFIRMED_NOTICE = 'sign-out-unconfirmed';

/** `/sign-in` router state after "Sign out everywhere" revoked every session. */
export const SIGNED_OUT_EVERYWHERE_NOTICE = 'signed-out-everywhere';

/**
 * `/sign-in` router state after "Sign out everywhere" found this session already ended:
 * no other session was signed out by that request.
 */
export const SIGN_OUT_EVERYWHERE_UNCONFIRMED_NOTICE = 'sign-out-everywhere-unconfirmed';

export type SignOutNotice =
  | typeof SIGN_OUT_UNCONFIRMED_NOTICE
  | typeof SIGNED_OUT_EVERYWHERE_NOTICE
  | typeof SIGN_OUT_EVERYWHERE_UNCONFIRMED_NOTICE;

/** The copy shown for each sign-out notice. */
export const SIGN_OUT_NOTICE_COPY: Record<SignOutNotice, string> = {
  [SIGN_OUT_UNCONFIRMED_NOTICE]:
    'You are signed out on this device, but the server could not confirm it. That session ends within 12 hours; to end it now, sign in and use Sign out everywhere.',
  [SIGNED_OUT_EVERYWHERE_NOTICE]: 'You have been signed out everywhere.',
  [SIGN_OUT_EVERYWHERE_UNCONFIRMED_NOTICE]:
    'Your session had already ended, so no other session was signed out. Sign in and use Sign out everywhere again.',
};

/** The notice for a `signOut()` outcome: none for a confirmed or already-ended session. */
export function signOutNotice(outcome: SignOutOutcome): SignOutNotice | null {
  return outcome === 'local-only' ? SIGN_OUT_UNCONFIRMED_NOTICE : null;
}

/** The notice for a `signOutEverywhere()` outcome that signed this browser out. */
export function signOutEverywhereNotice(outcome: Exclude<SignOutEverywhereOutcome, 'failed'>): SignOutNotice {
  if (outcome === 'server-revoked') return SIGNED_OUT_EVERYWHERE_NOTICE;
  return outcome === 'already-ended' ? SIGN_OUT_EVERYWHERE_UNCONFIRMED_NOTICE : SIGN_OUT_UNCONFIRMED_NOTICE;
}

/** The copy for a notice handed over in router state; null for anything else. */
export function signOutNoticeCopy(notice: unknown): string | null {
  for (const [key, copy] of Object.entries(SIGN_OUT_NOTICE_COPY)) {
    if (key === notice) return copy;
  }
  return null;
}
