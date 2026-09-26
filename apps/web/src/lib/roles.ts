import { useMemo } from 'react';
import type { components } from '@/api/schema';
import { useAuth } from '@/auth/AuthContext';

/**
 * The single frontend role-policy module.
 *
 * Every "may this member see this control?" decision lives here, so no component
 * compares role strings itself. Each predicate mirrors one backend authorization
 * seam, named after the question it answers rather than after a role list: the
 * backend answers different questions with different mechanisms (a rank floor for
 * workspace editing, exact membership for organization administration and for the
 * audit log), and collapsing them into one generic `isEditor` is how a compliance
 * reviewer ends up offered an editor action.
 *
 * This is UX truthfulness, not security. The API re-decides every request and
 * remains the only enforcement boundary; these predicates only stop the UI from
 * offering an action the API will predictably refuse. Anything unrecognised — no
 * session yet, no membership, a role string this build does not know — fails
 * closed: every predicate answers `false` (or `[]`).
 */

/** The six organization roles, as generated from the API contract. */
export type Role = components['schemas']['Role'];

/** The five roles an invitation may carry, as generated from the API contract. Never `owner`. */
export type InvitableRole = components['schemas']['InvitationCreate']['role'];

/**
 * What a caller may hold in place of a role: a known role, an arbitrary string
 * (session memberships are typed `role: string`), or nothing yet.
 */
export type RoleInput = string | null | undefined;

/**
 * Privilege rank, higher = more privilege. PINNED to the backend's `_ROLE_RANK`
 * (`apps/api/app/auth/dependencies.py:23-30`) — it is not generated, because the
 * contract carries the role names but not their ordering. Do not reorder, merge or
 * extend it without the backend changing first. `satisfies` makes a role added to
 * or removed from the contract a compile error here.
 *
 * `reviewer` and `compliance_reviewer` share rank 1: neither outranks the other,
 * and neither is an editor.
 */
export const ROLE_RANK = {
  viewer: 0,
  reviewer: 1,
  compliance_reviewer: 1,
  marketer: 2,
  admin: 3,
  owner: 4,
} as const satisfies Record<Role, number>;

// Display position, most privileged first. A Record rather than an array so the
// compiler rejects a contract role missing here, or a stray extra one.
const ROLE_ORDER = {
  owner: 0,
  admin: 1,
  marketer: 2,
  reviewer: 3,
  compliance_reviewer: 4,
  viewer: 5,
} as const satisfies Record<Role, number>;

/** Every role, most privileged first (display order for role pickers). */
export const ROLES: readonly Role[] = Object.freeze(
  (Object.keys(ROLE_ORDER) as Role[]).sort((a, b) => ROLE_ORDER[a] - ROLE_ORDER[b]),
);

// Exactly the contract's invitation roles; `owner` here is an excess-property
// error. OWNER is never invitable: the backend refuses it in the request schema
// and again in the service.
const INVITABLE: Readonly<Record<InvitableRole, true>> = {
  admin: true,
  marketer: true,
  reviewer: true,
  compliance_reviewer: true,
  viewer: true,
};

// In ROLES order, which is the authorization's invitation order (§27).
const INVITABLE_ROLES: readonly InvitableRole[] = ROLES.filter(
  (role): role is InvitableRole => role in INVITABLE,
);

// A Set, not `key in ROLE_RANK`: `'constructor' in {}` is true.
const KNOWN_ROLES: ReadonlySet<string> = new Set<string>(ROLES);

// `require_exact_organization_roles(OWNER, ADMIN)` — exact membership, no rank floor.
const ORGANIZATION_ADMIN_ROLES: ReadonlySet<Role> = new Set<Role>(['owner', 'admin']);

// `require_exact_roles(OWNER, ADMIN, COMPLIANCE_REVIEWER)` — exact membership.
const AUDIT_LOG_READER_ROLES: ReadonlySet<Role> = new Set<Role>(['owner', 'admin', 'compliance_reviewer']);

/** Whether `value` is one of the six contract roles. Unknown strings are not roles. */
export function isRole(value: unknown): value is Role {
  return typeof value === 'string' && KNOWN_ROLES.has(value);
}

/**
 * Whether `role` is exactly `owner`. For comparing against a member list (for
 * example counting owners for the last-owner rule) without a role string
 * comparison in a component. Anything else, unknown strings included, is `false`.
 */
export function isOwnerRole(role: RoleInput): boolean {
  return isRole(role) && role === 'owner';
}

/**
 * Workspace editing: onboarding, locations, campaign context, scout requests and
 * their run/pause/resume, job cancel, opportunity status, schedules, feedback.
 *
 * Mirrors `require_role(OWNER, ADMIN, MARKETER)`, a RANK FLOOR at `marketer`
 * (`dependencies.py:210-219`): owner, admin and marketer are admitted; reviewer,
 * compliance_reviewer and viewer are refused.
 */
export function canEditWorkspace(role: RoleInput): boolean {
  return isRole(role) && ROLE_RANK[role] >= ROLE_RANK.marketer;
}

/**
 * Organization administration: exactly owner or admin, by name. Mirrors
 * `require_exact_organization_roles(OWNER, ADMIN)` (`dependencies.py:176-207`).
 * A marketer is refused even though it is an editor.
 */
export function canAdminOrganization(role: RoleInput): boolean {
  return isRole(role) && ORGANIZATION_ADMIN_ROLES.has(role);
}

/** `POST /organizations/{organization_id}/workspaces` — exactly owner or admin. */
export function canCreateWorkspace(role: RoleInput): boolean {
  return canAdminOrganization(role);
}

/**
 * Opening the invitation UI and listing/revoking invitations — exactly owner or
 * admin. Which roles the actor may offer is `invitableRoles`.
 */
export function canInviteMembers(role: RoleInput): boolean {
  return canAdminOrganization(role);
}

/**
 * `GET /workspaces/{workspace_id}/audit-logs` — exactly owner, admin or
 * compliance_reviewer (`require_exact_roles`). A duty boundary, not a rank: a
 * marketer outranks a compliance reviewer and is still refused, and reading the
 * audit log makes a compliance reviewer no kind of editor.
 */
export function canReadAuditLogs(role: RoleInput): boolean {
  return isRole(role) && AUDIT_LOG_READER_ROLES.has(role);
}

/**
 * The roles `actor` may put on an invitation, in display order: never `owner`,
 * never above the actor's own rank, and empty for anyone who cannot invite.
 * Mirrors `create_invitation` (`apps/api/app/organizations/invitations.py`).
 * Returns a fresh array.
 */
export function invitableRoles(actor: RoleInput): InvitableRole[] {
  if (!isRole(actor) || !ORGANIZATION_ADMIN_ROLES.has(actor)) return [];
  const ceiling = ROLE_RANK[actor];
  return INVITABLE_ROLES.filter((role) => ROLE_RANK[role] <= ceiling);
}

/**
 * Whether `actor` may change an existing member TO `role`. Mirrors the new-role
 * half of `change_member_role` (`apps/api/app/organizations/members.py`):
 *   - the actor must be owner or admin;
 *   - only an owner grants `owner` (`member_owner_required`);
 *   - nobody grants a role ranked above their own (`member_role_ceiling`).
 *
 * Unlike an invitation, a role change CAN grant `owner` — when the actor is an
 * owner. Pair with `canManageMember`, which covers the target's current role.
 */
export function canGrantRole(actor: RoleInput, role: RoleInput): boolean {
  if (!isRole(actor) || !isRole(role) || !ORGANIZATION_ADMIN_ROLES.has(actor)) return false;
  if (role === 'owner' && actor !== 'owner') return false;
  return ROLE_RANK[role] <= ROLE_RANK[actor];
}

/**
 * Whether `actor` may change the role of, or remove, a member currently holding
 * `target`. Mirrors the rules `change_member_role` and `remove_member` share
 * (`apps/api/app/organizations/members.py`):
 *   - the actor must be owner or admin;
 *   - nobody manages themselves (`member_self_change_forbidden`,
 *     `member_self_removal_forbidden`) — `isSelf` is required so a caller cannot
 *     forget it;
 *   - only an owner manages an owner (`member_owner_required`);
 *   - nobody manages a member ranked above them (`member_role_ceiling`).
 *
 * The last-owner rule (`organization_last_owner`, 409) is deliberately not
 * modelled: from any consistent snapshot it cannot apply. Only an owner may manage
 * an owner, and not themselves, so the actor and the target are two distinct
 * owners. The 409 is reachable only through a stale snapshot, so callers must
 * still handle it from the API.
 */
export function canManageMember(
  actor: RoleInput,
  target: RoleInput,
  { isSelf }: { isSelf: boolean },
): boolean {
  if (!isRole(actor) || !isRole(target) || !ORGANIZATION_ADMIN_ROLES.has(actor)) return false;
  if (isSelf) return false;
  if (target === 'owner' && actor !== 'owner') return false;
  return ROLE_RANK[target] <= ROLE_RANK[actor];
}

/**
 * The signed-in user's role in one organization. `unknown` is an explicit state,
 * not an absent role: it covers memberships not loaded yet, an organization not
 * resolved yet, no membership in that organization, and a role string this build
 * does not recognise. Every predicate above treats it (`role: null`) as denied.
 */
export type ActorRole =
  | { readonly status: 'known'; readonly role: Role }
  | { readonly status: 'unknown'; readonly role: null };

const UNKNOWN_ROLE: ActorRole = { status: 'unknown', role: null };

/**
 * Pure form of `useActorRole`, for callers (and tests) that already hold the
 * session memberships.
 */
export function actorRoleFor(
  memberships: readonly { organization_id: string; role: string }[],
  organizationId: string | null | undefined,
): ActorRole {
  const role = memberships.find((m) => m.organization_id === organizationId)?.role;
  return isRole(role) ? { status: 'known', role } : UNKNOWN_ROLE;
}

/**
 * The signed-in user's role in `organizationId`, read from the session
 * memberships (the same source the backend's membership row is reflected into).
 * Pass the active organization from `useWorkspace()` for workspace pages.
 */
export function useActorRole(organizationId: string | null | undefined): ActorRole {
  const { memberships } = useAuth();
  return useMemo(() => actorRoleFor(memberships, organizationId), [memberships, organizationId]);
}
