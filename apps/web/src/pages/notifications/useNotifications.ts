import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type {
  NotificationFeedOut,
  NotificationOut,
  NotificationPreferenceItem,
  NotificationPreferencesOut,
} from '@/api/types';
import { useAuth } from '@/auth/AuthContext';
import { useWorkspace } from '@/workspace/WorkspaceContext';

/**
 * Data hooks of the in-app notification feed (Project Phase 5E, 5E-W3).
 *
 * Every request is gated twice. The server gates the four notification routes on the O-12(B)
 * capability (503 while off). Here, no feed or preferences request is sent unless the
 * workspace-effective reflection says `enabled === true`: unknown (loading, failed, no
 * workspace) counts as off.
 *
 * The feed and preferences keys carry the PER-USER dimension (`queryKeys.notifications`,
 * `queryKeys.notificationPreferences`), so a user switch inside one workspace never reads
 * another user's cached feed. The user id is the signed-in session's.
 */

/** The page size of the bell's feed. */
export const FEED_LIMIT = 20;

export type NotificationsCapabilityStatus = 'loading' | 'enabled' | 'disabled';

/**
 * Workspace-effective availability of notifications and analytics (O-12(B)): a tri-state,
 * `=== true` only, never retried. Advisory: every notification route re-decides.
 *
 * The one reflection hook of the codebase (I-5E): keyed `queryKeys.notificationsAnalyticsCapability`
 * and read through `api.getNotificationsAnalyticsCapability`, and used by the shell's bell and
 * by the analytics page, its nav entry and the Overview summary (`useAnalytics.ts`).
 */
export function useNotificationsAnalyticsCapability(
  workspaceId: string,
  { enabled = true }: { enabled?: boolean } = {},
) {
  const query = useQuery({
    queryKey: queryKeys.notificationsAnalyticsCapability(workspaceId),
    queryFn: ({ signal }) => api.getNotificationsAnalyticsCapability(workspaceId, signal),
    enabled: enabled && Boolean(workspaceId),
    staleTime: 60_000,
    retry: false,
  });
  const status: NotificationsCapabilityStatus = query.isPending
    ? 'loading'
    : query.data?.enabled === true
      ? 'enabled'
      : 'disabled';
  return { status, isEnabled: status === 'enabled' };
}

/** The active workspace and the signed-in user, or nulls when either is unknown. */
export function useFeedScope(): { workspaceId: string | null; userId: string | null } {
  const { workspaceId } = useWorkspace();
  const { user } = useAuth();
  return { workspaceId: workspaceId || null, userId: user?.id || null };
}

/**
 * Whether the notification feed exists for the signed-in user in the active workspace. Unknown
 * is `false`. The shell renders its placeholder, and sends nothing, while this is `false`.
 */
export function useNotificationsVisible(): boolean {
  const { workspaceId, userId } = useFeedScope();
  const capability = useNotificationsAnalyticsCapability(workspaceId ?? '', {
    enabled: Boolean(workspaceId && userId),
  });
  return Boolean(workspaceId && userId) && capability.isEnabled;
}

/** 4xx answers and the 503 gate are deterministic: never retried. */
function deterministic(error: unknown): boolean {
  return (
    error instanceof ApiError && (error.status === 503 || (error.status >= 400 && error.status < 500))
  );
}

function retryTransient(failureCount: number, error: unknown): boolean {
  return !deterministic(error) && failureCount < 2;
}

/** The first page of the user's feed. Never sent unless `enabled` (the reflection is on). */
export function useNotificationFeed(workspaceId: string, userId: string, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.notifications(workspaceId, userId),
    queryFn: ({ signal }) => api.listNotifications(workspaceId, { limit: FEED_LIMIT }, signal),
    enabled: enabled && Boolean(workspaceId && userId),
    retry: retryTransient,
  });
}

/**
 * Mark one notification read. The scope is captured in this closure, so a late answer updates
 * only its own user's feed in its own workspace. The answer replaces the row in the cached page
 * (the first `read_at` the server kept). A 404 (gone, or never this user's) refreshes the feed.
 * Never retried, though the server's mark-read is idempotent.
 */
export function useMarkNotificationRead(workspaceId: string, userId: string) {
  const queryClient = useQueryClient();
  const key = queryKeys.notifications(workspaceId, userId);
  return useMutation({
    mutationFn: (notificationId: string) => api.markNotificationRead(workspaceId, notificationId),
    retry: false,
    onSuccess: (row: NotificationOut) => {
      queryClient.setQueryData<NotificationFeedOut>(key, (page) => {
        if (!page) return page;
        const before = page.items.find((item) => item.id === row.id);
        if (!before) return page;
        const becameRead = before.read_at === null && row.read_at !== null;
        return {
          ...page,
          items: page.items.map((item) => (item.id === row.id ? row : item)),
          unread: becameRead ? Math.max(0, page.unread - 1) : page.unread,
        };
      });
    },
    onError: async (error) => {
      if (error instanceof ApiError && error.status === 404) {
        await queryClient.invalidateQueries({ queryKey: key });
      }
    },
  });
}

/** Every event code with whether it is delivered. Never sent unless `enabled`. */
export function useNotificationPreferences(workspaceId: string, userId: string, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.notificationPreferences(workspaceId, userId),
    queryFn: ({ signal }) => api.getNotificationPreferences(workspaceId, signal),
    enabled: enabled && Boolean(workspaceId && userId),
    retry: retryTransient,
  });
}

/** Set one or more event codes; the answer (every code) replaces the cached preferences. */
export function useUpdateNotificationPreferences(workspaceId: string, userId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (items: NotificationPreferenceItem[]) =>
      api.updateNotificationPreferences(workspaceId, { items }),
    retry: false,
    onSuccess: (answer: NotificationPreferencesOut) => {
      queryClient.setQueryData(queryKeys.notificationPreferences(workspaceId, userId), answer);
    },
  });
}

/** Whether `error` is the capability gate's dark answer (503). */
export function isDark(error: unknown): boolean {
  return error instanceof ApiError && error.status === 503;
}
