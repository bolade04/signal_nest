import { useMutation, useQueryClient } from '@tanstack/react-query';
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

  const run = useMutation({
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
    onError: (err) => toast({ title: 'Could not queue scout', description: msg(err), intent: 'error' }),
  });

  return { pause, resume, run };
}

function msg(err: unknown): string | undefined {
  return err instanceof Error ? err.message : undefined;
}
