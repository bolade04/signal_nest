import { useQuery } from '@tanstack/react-query';
import { ApiError, apiRequest } from '@/api/client';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import { canDecideApprovals, canEditWorkspace, useActorRole, type RoleInput } from '@/lib/roles';
import { useWorkspace } from '@/workspace/WorkspaceContext';

/**
 * Status analytics (Project Phase 5E, lane 5E-W2): who may see it, whether it is on for the
 * workspace, and the read itself. UX truthfulness only — `GET /workspaces/{ws}/analytics`
 * re-decides role, capability and location scope on every request.
 */

/**
 * Reading status analytics — exactly owner, admin, marketer or reviewer
 * (`require_analytics_reader`, `ANALYTICS_READER_ROLES` in `apps/api/app/analytics/policy.py`):
 * viewer and compliance_reviewer are refused by name.
 *
 * I-5E: this predicate belongs in `lib/roles.ts` (the single role-policy module, which this
 * lane does not own). Until the integration owner adds `canReadAnalytics` there, it is composed
 * from that module's own predicates, which today cover exactly the reader set: the editors
 * (`canEditWorkspace`: owner, admin, marketer) plus the approver (`canDecideApprovals`:
 * reviewer). No role string is compared here.
 */
export function canReadAnalytics(role: RoleInput): boolean {
  return canEditWorkspace(role) || canDecideApprovals(role);
}

/** The workspace-effective reflection of the notifications-and-analytics capability. */
const capabilityPath = (workspaceId: string) =>
  `/workspaces/${workspaceId}/notifications-analytics-capability`;

/**
 * The reflection's cache key: per workspace, like every other capability reflection
 * (`queryKeys.creativeCapability`, `queryKeys.briefCapability`), so one workspace's answer is
 * never served to another.
 *
 * I-5E: the reflection route is lane 5E-W3's (`notifications/routes.py`) and its key and
 * wrapper are the integration owner's to add to `queryKeys.ts` / `endpoints.ts`. Replace this
 * key and `readCapability` below with those when they land; the shape of the key is the same.
 */
export const notificationsAnalyticsCapabilityKey = (workspaceId: string) =>
  ['workspaces', workspaceId, 'notifications-analytics-capability'] as const;

/** `{ enabled }`, read defensively: anything but a literal `true` is "off". */
async function readCapability(workspaceId: string, signal?: AbortSignal): Promise<boolean> {
  const answer = await apiRequest<unknown>(capabilityPath(workspaceId), { signal });
  return (
    typeof answer === 'object' &&
    answer !== null &&
    (answer as { enabled?: unknown }).enabled === true
  );
}

/**
 * Whether notifications and analytics resolve enabled for this workspace (operator ruling
 * O-12(B): one flag for both surfaces, off by default). A TRI-STATE, `true` only on a literal
 * `{ enabled: true }`, never retried; a failed lookup is "disabled" (fail closed).
 */
export type AnalyticsCapabilityStatus = 'loading' | 'enabled' | 'disabled';

export function useNotificationsAnalyticsCapability(
  workspaceId: string,
  { enabled = true }: { enabled?: boolean } = {},
) {
  const query = useQuery({
    queryKey: notificationsAnalyticsCapabilityKey(workspaceId),
    queryFn: ({ signal }) => readCapability(workspaceId, signal),
    enabled: enabled && Boolean(workspaceId),
    staleTime: 60_000,
    retry: false,
  });
  const status: AnalyticsCapabilityStatus = query.isPending
    ? 'loading'
    : query.data === true
      ? 'enabled'
      : 'disabled';
  return { status, isEnabled: status === 'enabled' };
}

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
