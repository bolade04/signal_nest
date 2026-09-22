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
 * Authoritative, *pre-request* capability gate for scout scheduling.
 *
 * The coarse runtime summary (`GET /system/capabilities`, shared app-wide and
 * cached) reflects the raw server `scout_scheduling_enabled` flag — the same
 * value the schedule *mutation* endpoints gate on. Schedule *reads* are not
 * gated, so this never hides an existing schedule; it only decides whether the
 * mutation affordances may be offered.
 *
 * `=== true` on purpose: loading, `undefined`, `null` and a missing key must all
 * read as dark. The server 503 remains the enforcement boundary for a stale or
 * hand-rolled client; this gate exists so the UI stops claiming a capability is
 * available when the server will refuse it.
 */
export function useScheduleCapability() {
  const query = useQuery({
    queryKey: queryKeys.runtimeSummary,
    queryFn: ({ signal }) => api.getRuntimeSummary(signal),
    staleTime: 60_000,
  });
  return { isEnabled: query.data?.features?.scout_scheduling_enabled === true };
}
