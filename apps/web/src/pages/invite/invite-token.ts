import { ApiError } from '@/api/client';

// Invitation links and the invitation token in the browser (P6-AUTH-1).
//
// A link is `<origin>/invite#token=<raw>`. The token rides in the URL fragment
// because a browser never puts the fragment into the HTTP request it makes, so the
// token cannot reach a server, proxy or access log through the page load. The
// invite page reads it from the address bar into component memory and scrubs the
// address bar before any request. From there it goes only into the JSON body of the
// preview / register / accept calls — never into localStorage, sessionStorage, a
// cookie, a React Query key or cache entry, a mutation, the console or page text.
// There is no email transport: an administrator copies the link and shares it.

export const INVITE_PATH = '/invite';

/** Mirrors the backend's MAX_TOKEN_LENGTH; issued tokens are 43 characters. */
export const MAX_INVITE_TOKEN_LENGTH = 128;

function fragmentParts(hash: string): string[] {
  const fragment = hash.startsWith('#') ? hash.slice(1) : hash;
  return fragment ? fragment.split('&') : [];
}

/** The decoded token of a `#token=<raw>` fragment; null when missing, empty or malformed. */
export function parseInviteFragment(hash: string): string | null {
  for (const part of fragmentParts(hash)) {
    if (!part.startsWith('token=')) continue;
    try {
      return decodeURIComponent(part.slice('token='.length)) || null;
    } catch {
      // A malformed percent-escape: not a link this app produced.
      return null;
    }
  }
  return null;
}

/** Whether a fragment carries a token parameter at all, well-formed or not. */
export function fragmentHasInviteToken(hash: string): boolean {
  return fragmentParts(hash).some((part) => part === 'token' || part.startsWith('token='));
}

/**
 * Remove an invitation fragment from the address bar, in place: no navigation and
 * no new history entry, so Back cannot return to the tokened URL. The history state
 * object is passed through untouched because React Router keeps its own bookkeeping
 * in it. Idempotent, and a no-op for a fragment that carries no token.
 */
export function scrubInviteFragment(): void {
  if (!fragmentHasInviteToken(window.location.hash)) return;
  const { pathname, search } = window.location;
  window.history.replaceState(window.history.state, '', `${pathname}${search}`);
}

/** Whether a captured value could be an issued token; anything else is invalid without asking the server. */
export function isPlausibleInviteToken(token: string | null | undefined): token is string {
  return typeof token === 'string' && token.length > 0 && token.length <= MAX_INVITE_TOKEN_LENGTH;
}

/**
 * The share link for a freshly created invitation: `<origin>/invite#token=<token>`.
 * Fragment only — never a query string or a path segment. Issued tokens are URL-safe
 * base64, which `encodeURIComponent` leaves byte-for-byte unchanged; the encoding only
 * matters for a character that could otherwise end the fragment parameter early.
 */
export function buildInviteLink(origin: string, token: string): string {
  return `${origin.replace(/\/+$/, '')}${INVITE_PATH}#token=${encodeURIComponent(token)}`;
}

// --- Error codes -------------------------------------------------------------

/**
 * The stable `code` of the API's `{"error": {code, message, ...}}` envelope, or null.
 * Reads that one key and nothing else from the payload.
 */
export function apiErrorCode(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  const payload: unknown = error.detail;
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
  const envelope = (payload as { error?: unknown }).error;
  if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope)) return null;
  const code = (envelope as { code?: unknown }).code;
  return typeof code === 'string' ? code : null;
}

/**
 * Every `invitation_*` code 6B-3A can return. The first eight are the token-holder
 * outcomes (preview / register / accept, plus create's duplicate check); the last
 * three only come from the administrator's create and revoke calls.
 */
export type InvitationErrorCode =
  | 'invitation_invalid'
  | 'invitation_expired'
  | 'invitation_revoked'
  | 'invitation_already_used'
  | 'invitation_already_member'
  | 'invitation_inviter_not_authorized'
  | 'invitation_account_exists'
  | 'invitation_pending_exists'
  | 'invitation_owner_forbidden'
  | 'invitation_role_forbidden'
  | 'invitation_not_found';

export interface InvitationErrorCopy {
  title: string;
  description: string;
}

/**
 * User-facing copy for each invitation code, so no screen shows a raw backend error.
 * The token-holder entries speak to the invitee.
 */
export const INVITATION_ERROR_COPY: Readonly<Record<InvitationErrorCode, InvitationErrorCopy>> = {
  invitation_invalid: {
    title: 'This invitation link is invalid',
    description:
      'Check that you opened the complete link. If it still does not work, ask an administrator of the organization for a new invitation.',
  },
  invitation_expired: {
    title: 'This invitation has expired',
    description: 'Ask an administrator of the organization for a new invitation.',
  },
  invitation_revoked: {
    title: 'This invitation was revoked',
    description:
      'An administrator of the organization withdrew it. Ask them for a new invitation if you still need access.',
  },
  invitation_already_used: {
    title: 'This invitation has already been used',
    description: 'Each invitation link works once. If you accepted it, sign in to continue.',
  },
  invitation_already_member: {
    title: 'You already belong to this organization',
    description: 'Your account is already a member, so there is nothing to accept.',
  },
  invitation_inviter_not_authorized: {
    title: 'This invitation can no longer be accepted',
    description:
      'The person who invited you can no longer grant this access. Ask an administrator of the organization for a new invitation.',
  },
  invitation_account_exists: {
    title: 'You already have an account',
    description:
      'An account with this email address already exists. Sign in to accept the invitation.',
  },
  invitation_pending_exists: {
    title: 'An invitation is already pending',
    description:
      'This email address already has a pending invitation to this organization. Revoke it before creating a new one.',
  },
  invitation_owner_forbidden: {
    title: 'The Owner role cannot be given by invitation',
    description: 'An Owner can make an existing member an Owner from the member list.',
  },
  invitation_role_forbidden: {
    title: 'You cannot invite with that role',
    description: 'You can only invite members with a role at or below your own.',
  },
  invitation_not_found: {
    title: 'Invitation not found',
    description: 'It may no longer be pending. Refresh the list and try again.',
  },
};

/** The invitation code carried by an API error, when it is one this module knows. */
export function invitationErrorCode(error: unknown): InvitationErrorCode | null {
  const code = apiErrorCode(error);
  return code !== null && Object.prototype.hasOwnProperty.call(INVITATION_ERROR_COPY, code)
    ? (code as InvitationErrorCode)
    : null;
}
