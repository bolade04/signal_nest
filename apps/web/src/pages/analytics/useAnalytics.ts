import { useQuery } from '@tanstack/react-query';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import { canReadAnalytics, useActorRole } from '@/lib/roles';
import { useNotificationsAnalyticsCapability } from '@/pages/notifications/useNotifications';
import { useWorkspace } from '@/workspace/WorkspaceContext';

/**
 * Status analytics (Project Phase 5E, lane 5E-W2): who may see it, whether it is on for the
 * workspace, and the read itself. UX truthfulness only — `GET /workspaces/{ws}/analytics`
 * re-decides role, capability and location scope on every request.
 */

/**
 * Reading status analytics — exactly owner, admin, marketer or reviewer — and the O-12(B)
 * reflection now live in their shared places (I-5E): `canReadAnalytics` in `lib/roles.ts`
 * (the single role-policy module) and `useNotificationsAnalyticsCapability` in
 * `pages/notifications/useNotifications.ts` (one hook, one key,
 * `queryKeys.notificationsAnalyticsCapability`, one wrapper,
 * `api.getNotificationsAnalyticsCapability`; `enabled === true` only). Re-exported here so
 * this module's surface is unchanged.
 */
export { canReadAnalytics } from '@/lib/roles';
export {
  type NotificationsCapabilityStatus as AnalyticsCapabilityStatus,
  useNotificationsAnalyticsCapability,
} from '@/pages/notifications/useNotifications';

/**
 * The signed-in member's access to status analytics in the active workspace:
 *
 * - `loading` — the role or the reflection is not known yet;
 * - `not_reader` — viewer or compliance reviewer (the API answers 403); the reflection is not
 *   even asked, so no analytics-related request is sent;
 * - `dark` — a reader, but the capability is off for the workspace (or its reflection could
 *   not be read): no analytics request is sent;
 * - `ready` — a reader with the capability on: the analytics request may be sent.
 */
export type AnalyticsAccess = 'loading' | 'not_reader' | 'dark' | 'ready';

export function useAnalyticsAccess(): AnalyticsAccess {
  const { workspaceId, organizationId } = useWorkspace();
  const actor = useActorRole(organizationId);
  const reader = actor.status === 'known' && canReadAnalytics(actor.role);
  const capability = useNotificationsAnalyticsCapability(workspaceId ?? '', { enabled: reader });
  if (!workspaceId || actor.status === 'unknown') return 'loading';
  if (!reader) return 'not_reader';
  if (capability.status === 'loading') return 'loading';
  return capability.isEnabled ? 'ready' : 'dark';
}

/**
 * Whether the analytics entry belongs in the sidebar (for the integration owner's `nav.ts`
 * entry): a reader with the capability on. Unknown is `false`.
 */
export function useAnalyticsVisible(): boolean {
  return useAnalyticsAccess() === 'ready';
}

/** The analytics read answered 503: the capability is off for the workspace. */
export function isAnalyticsDark(error: unknown): boolean {
  return error instanceof ApiError && error.status === 503;
}

/** The analytics read answered 403: the member's role may not read analytics. */
export function isAnalyticsRefused(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}

/** The analytics read answered 404: the location is not this workspace's (or the workspace is gone). */
export function isAnalyticsNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** A refusal that a retry cannot change (the approvals hook's rule): 503 dark, or any 4xx. */
function deterministic(error: unknown): boolean {
  return (
    isAnalyticsDark(error) ||
    (error instanceof ApiError && error.status >= 400 && error.status < 500)
  );
}

/**
 * The status analytics of the workspace, or of one of its locations (`locationId`; `null` =
 * the whole workspace). Keyed per workspace AND location (`queryKeys.analytics`), so a
 * location view never shares an entry with the workspace view or another location. Sent only
 * while `enabled` (pass `access === 'ready'`).
 */
export function useWorkspaceAnalytics(
  workspaceId: string,
  locationId: string | null,
  { enabled = true }: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: queryKeys.analytics(workspaceId, locationId),
    queryFn: ({ signal }) =>
      api.getWorkspaceAnalytics(workspaceId, { location_id: locationId }, signal),
    enabled: enabled && Boolean(workspaceId),
    staleTime: 30_000,
    retry: (failureCount, error) => !deterministic(error) && failureCount < 2,
  });
}
