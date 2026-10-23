import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { ScheduleInterval } from '@/api/types';
import { useToast } from '@/components/ui/toast';

// Mutations for one request's schedule. Each invalidates the schedule query so
// the panel re-derives state (paused / active / activation_required) from the
// server rather than guessing locally.
export function useScheduleActions(workspaceId: string, requestId: string) {
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: queryKeys.scoutSchedule(workspaceId, requestId) });

  const create = useMutation({
    mutationFn: (interval: ScheduleInterval) =>
      api.createScoutSchedule(workspaceId, requestId, { interval }),
    onSuccess: async () => {
      await invalidate();
      toast({ title: 'Schedule created', intent: 'success' });
    },
    onError: (err) =>
      toast({ title: 'Could not create schedule', description: msg(err), intent: 'error' }),
  });

  const pause = useMutation({
    mutationFn: () => api.pauseScoutSchedule(workspaceId, requestId),
    onSuccess: async () => {
      await invalidate();
      toast({ title: 'Schedule paused', intent: 'success' });
    },
    onError: (err) =>
      toast({ title: 'Could not pause schedule', description: msg(err), intent: 'error' }),
  });

  const resume = useMutation({
    mutationFn: () => api.resumeScoutSchedule(workspaceId, requestId),
    onSuccess: async () => {
      await invalidate();
      toast({ title: 'Schedule activated', intent: 'success' });
    },
    onError: (err) =>
      toast({ title: 'Could not activate schedule', description: msg(err), intent: 'error' }),
  });

  const remove = useMutation({
    mutationFn: () => api.deleteScoutSchedule(workspaceId, requestId),
    onSuccess: async () => {
      await invalidate();
      toast({ title: 'Schedule deleted', intent: 'success' });
    },
    onError: (err) =>
      toast({ title: 'Could not delete schedule', description: msg(err), intent: 'error' }),
  });

  return { create, pause, resume, remove };
}

function msg(err: unknown): string | undefined {
  return err instanceof Error ? err.message : undefined;
}

/**
 * Authoritative, *pre-request* capability gate for scout scheduling —
 * **workspace-effective** (P6-UI-006).
 *
 * Since P6-CAP-1 the schedule mutation gate decides per workspace through the
 * capability resolver, so a workspace override can enable scheduling while the
 * global flag is off, or disable it while the flag is on. The coarse runtime
 * summary (`GET /system/capabilities`) deliberately reflects the **raw global**
 * flag and can no longer answer this question; reading it here would repeat the
 * P6-UI-005 defect for scheduling. This asks the server the same question the gate
 * asks, scoped to the same workspace. Schedule *reads* are not gated, so this never
 * hides an existing schedule; it decides whether mutation affordances may be
 * offered and whether a row's live state may be shown as live.
 *
 * Tri-state like `useFeedbackCapability`: `status` separates "not yet resolved"
 * from a settled denial so tests can tell them apart; the panel consumes only
 * `isEnabled`, and loading renders exactly as dark.
 *
 * `=== true` on purpose: loading, an error, `undefined`, `null`, a missing key and
 * a non-boolean all read as dark. The server 503 remains the enforcement boundary
 * for a stale or hand-rolled client.
 */
export type ScheduleCapabilityStatus = 'loading' | 'enabled' | 'disabled';

export function useScheduleCapability(workspaceId: string) {
  const query = useQuery({
    queryKey: queryKeys.schedulingCapability(workspaceId),
    queryFn: ({ signal }) => api.getSchedulingCapability(workspaceId, signal),
    staleTime: 60_000,
    // Unknown must not present controls the server would refuse, and retrying would
    // only widen the window in which the panel looks dark for the wrong reason. An
    // errored query holds no data, so a later mount re-probes (see
    // `useFeedbackCapability` for the accepted cost).
    retry: false,
  });

  const status: ScheduleCapabilityStatus = query.isPending
    ? 'loading'
    : query.data?.enabled === true
      ? 'enabled'
      : 'disabled';

  return { status, isEnabled: status === 'enabled' };
}
