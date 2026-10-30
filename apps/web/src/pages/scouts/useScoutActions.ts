import { useMutation, useMutationState, useQueryClient } from '@tanstack/react-query';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import { useToast } from '@/components/ui/toast';

export function useScoutActions(workspaceId: string) {
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const invalidate = async (requestId: string) => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.scoutRequests(workspaceId) }),
      queryClient.invalidateQueries({ queryKey: queryKeys.scoutRequest(workspaceId, requestId) }),
      queryClient.invalidateQueries({ queryKey: ['workspaces', workspaceId, 'opportunities'] }),
    ]);
  };

  const pause = useMutation({
    mutationFn: (requestId: string) => api.pauseScoutRequest(workspaceId, requestId),
    onSuccess: async (r) => {
      await invalidate(r.id);
      toast({ title: 'Scout paused', intent: 'success' });
    },
    onError: (err) => toast({ title: 'Could not pause', description: msg(err), intent: 'error' }),
  });

  const resume = useMutation({
    mutationFn: (requestId: string) => api.resumeScoutRequest(workspaceId, requestId),
    onSuccess: async (r) => {
      await invalidate(r.id);
      toast({ title: 'Scout resumed', intent: 'success' });
    },
    onError: (err) => toast({ title: 'Could not resume', description: msg(err), intent: 'error' }),
  });

  // One key for every Run-now POST of this workspace, so any view can see which requests
  // have a POST in flight (the list and the detail page share the mutation cache).
  const runKey = ['workspaces', workspaceId, 'scout-run'] as const;
  const run = useMutation({
    mutationKey: runKey,
    mutationFn: (requestId: string) => api.runScoutRequest(workspaceId, requestId),
    onSuccess: async (r) => {
      await invalidate(r.scout_request_id);
      await queryClient.invalidateQueries({ queryKey: ['workspaces', workspaceId, 'jobs'] });
      toast({
        title: 'Scout queued',
        description: 'A background job is processing this scout. Results appear as it completes.',
        intent: 'success',
      });
    },
    onError: async (err, requestId) => {
      toast({ title: 'Could not queue scout', description: msg(err), intent: 'error' });
      // Re-read the request (only): if the refusal was "already queued or running" (another
      // tab or client started it), the button then shows that state; otherwise it stays usable.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.scoutRequests(workspaceId) }),
        queryClient.invalidateQueries({ queryKey: queryKeys.scoutRequest(workspaceId, requestId) }),
      ]);
    },
  });

  // Request ids whose Run-now POST is in flight (reactive, for rendering).
  const pendingRuns = useMutationState({
    filters: { mutationKey: runKey, status: 'pending' },
    select: (mutation) => mutation.state.variables as string,
  });

  // UI-AUDIT F8 / P8: one POST per click burst. `mutate` marks the mutation pending in the
  // cache synchronously, but React re-renders (and disables the button) only after the
  // query client's next notification tick, so a fast second click can still land on an
  // enabled button. The cache is checked synchronously here instead of the rendered state.
  const startRun = (requestId: string) => {
    const inFlight = queryClient.isMutating({
      mutationKey: runKey,
      predicate: (mutation) => mutation.state.variables === requestId,
    });
    if (inFlight > 0) return;
    run.mutate(requestId);
  };
  const isRunPending = (requestId: string) => pendingRuns.includes(requestId);

  return { pause, resume, run, startRun, isRunPending };
}

function msg(err: unknown): string | undefined {
  return err instanceof Error ? err.message : undefined;
}
