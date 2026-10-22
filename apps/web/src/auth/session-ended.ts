/**
 * The `/sign-in` notice for a session the server stopped accepting while this browser
 * held it (P6-UI-013): a request answered 401 because the 12-hour limit of the sign-in
 * passed (P6-AUTH-4), or because the account was signed out everywhere or its password
 * changed on another device. Never for a sign-out the user chose here.
 *
 * Router state only (`{ notice }`), never the URL — as the sign-out notices.
 */
export const SESSION_ENDED_NOTICE = 'session-ended';

/** What sign-in says. "Ended", not "expired": a 401 does not say which of the causes it was. */
export const SESSION_ENDED_COPY = 'Your session has ended. Sign in again to continue where you left off.';
