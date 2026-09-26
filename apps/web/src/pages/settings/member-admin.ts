import { useCallback, useEffect, useRef } from 'react';
import { ApiError } from '@/api/client';
import { isOwnerRole } from '@/lib/roles';
import {
  apiErrorCode,
  INVITATION_ERROR_COPY,
  type InvitationErrorCode,
} from '@/pages/invite/invite-token';

// Display support for the Settings member-management area (P6-AUTH-1).
// Role POLICY is not decided here; it lives only in lib/roles.ts. This module maps
// the backend's stable error codes (app/organizations/members.py, invitations.py) to
// plain-language copy, formats dates, and returns focus after a dialog closes.

/** A 403: the actor's own authority (the session role) may be out of date. */
export function isForbidden(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}

const NO_PERMISSION =
  'You no longer have permission to do this in this organization. Your role may have changed.';
const FALLBACK = 'Something went wrong. Please try again.';

function describe(error: unknown, messages: ReadonlyMap<string, string>): string {
  const code = apiErrorCode(error);
  const known = code === null ? undefined : messages.get(code);
  if (known) return known;
  if (isForbidden(error)) return NO_PERMISSION;
  return error instanceof ApiError ? error.message : FALLBACK;
}

/** An administrator-facing entry of the invitation copy, title then description. */
function adminCopy(code: InvitationErrorCode): string {
  const { title, description } = INVITATION_ERROR_COPY[code];
  return `${title}. ${description}`;
}

const MEMBER_GONE = 'This person is no longer a member of this organization.';

const INVITE_MESSAGES: ReadonlyMap<string, string> = new Map([
  ['invitation_pending_exists', adminCopy('invitation_pending_exists')],
  // The shared entry for this code addresses the invitee, so the administrator gets their own.
  ['invitation_already_member', 'This email address already belongs to a member of this organization.'],
  ['invitation_owner_forbidden', adminCopy('invitation_owner_forbidden')],
  ['invitation_role_forbidden', adminCopy('invitation_role_forbidden')],
]);

/** Invite errors that belong to the email field rather than the whole form. */
const EMAIL_CODES: ReadonlySet<string> = new Set([
  'invitation_pending_exists',
  'invitation_already_member',
]);
/** Invite errors that belong to the role field. */
const ROLE_CODES: ReadonlySet<string> = new Set([
  'invitation_owner_forbidden',
  'invitation_role_forbidden',
]);

const ROLE_CHANGE_MESSAGES: ReadonlyMap<string, string> = new Map([
  [
    'organization_last_owner',
    'This member is the organization’s only owner, so their role can’t be changed. Make another member an owner first.',
  ],
  [
    'member_owner_required',
    'Only an owner can change an owner’s role or make someone an owner. Your role, or theirs, may have changed.',
  ],
  [
    'member_role_ceiling',
    'You can’t assign a role above your own or manage a member ranked above you. Your role may have changed.',
  ],
  ['member_self_change_forbidden', 'You can’t change your own role.'],
  ['member_not_found', MEMBER_GONE],
]);

const REMOVE_MESSAGES: ReadonlyMap<string, string> = new Map([
  [
    'organization_last_owner',
    'This member is the organization’s only owner, so they can’t be removed. Make another member an owner first.',
  ],
  [
    'member_owner_required',
    'Only an owner can remove an owner. Your role, or theirs, may have changed.',
  ],
  [
    'member_role_ceiling',
    'You can’t remove a member ranked above you. Your role may have changed.',
  ],
  ['member_self_removal_forbidden', 'You can’t remove yourself from the organization.'],
  ['member_not_found', MEMBER_GONE],
]);

const REVOKE_MESSAGES: ReadonlyMap<string, string> = new Map([
  ['invitation_not_found', adminCopy('invitation_not_found')],
  // The shared entries for these codes address the invitee; these address the administrator.
  ['invitation_revoked', 'This invitation was already revoked.'],
  ['invitation_expired', 'This invitation has already expired.'],
  ['invitation_already_used', 'This invitation has already been accepted.'],
]);

export interface InviteError {
  field: 'email' | 'role' | 'form';
  message: string;
}

/** Copy for a failed invitation create, attached to the field it concerns. */
export function inviteError(error: unknown): InviteError {
  const code = apiErrorCode(error);
  const field =
    code !== null && EMAIL_CODES.has(code) ? 'email' : code !== null && ROLE_CODES.has(code) ? 'role' : 'form';
  return { field, message: describe(error, INVITE_MESSAGES) };
}

export const roleChangeErrorMessage = (error: unknown) => describe(error, ROLE_CHANGE_MESSAGES);
export const removeErrorMessage = (error: unknown) => describe(error, REMOVE_MESSAGES);
export const revokeErrorMessage = (error: unknown) => describe(error, REVOKE_MESSAGES);

/**
 * Whether a failed member mutation means this screen's picture of the organization
 * is out of date: the member list, and possibly the actor's own role in the session.
 * Covers the stale-snapshot outcomes (`organization_last_owner`, `member_owner_required`,
 * `member_role_ceiling`, `member_not_found`) and any other 403.
 */
export function isStaleMemberError(error: unknown): boolean {
  if (isForbidden(error)) return true;
  const code = apiErrorCode(error);
  return code === 'organization_last_owner' || code === 'member_not_found';
}

/**
 * The user id of the organization's only owner, when the loaded member list proves
 * there is exactly one; otherwise null.
 *
 * Settings mixes two snapshots: the actor's role comes from the session, the list is
 * fresh. When they disagree (the actor was demoted elsewhere), the list is what shows
 * that demoting or removing this member would leave the organization ownerless
 * (`organization_last_owner`). The backend stays the final authority.
 */
export function soleOwnerId(members: readonly { user_id: string; role: string }[]): string | null {
  const owners = members.filter((member) => isOwnerRole(member.role));
  return owners.length === 1 ? owners[0]!.user_id : null;
}

/** A calendar date ("Sep 24, 2026"), used for a membership's join time. */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(date);
}

/**
 * Return keyboard focus to the control that opened a dialog with no Radix
 * `DialogTrigger` (the shared ConfirmDialog is opened from state). Radix restores
 * focus only to its own trigger, so without this focus falls to <body> on close.
 *
 * Call the returned function with the opener before opening. Once `open` turns false
 * the focus move is deferred one task, after Radix has torn down the dialog's focus
 * trap (on both the click path and ConfirmDialog's post-await close); an opener that
 * has since left the page, such as a removed row's button, is skipped.
 */
export function useReturnFocus(open: boolean): (opener: HTMLElement | null) => void {
  const opener = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (open) return;
    const element = opener.current;
    opener.current = null;
    if (!element) return;
    const timer = setTimeout(() => {
      if (element.isConnected) element.focus();
    }, 0);
    return () => clearTimeout(timer);
  }, [open]);
  return useCallback((element: HTMLElement | null) => {
    opener.current = element;
  }, []);
}
