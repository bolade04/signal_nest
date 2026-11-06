import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { CreativeDraftCreate, JobOut } from '@/api/types';
import { pollingOptions, type StatusPollSchedule } from '@/lib/status-polling';
import { isGenerationInFlight } from './creativeLabels';

/**
 * Workspace-effective creative-generation availability (the feedback-capability pattern):
 * a TRI-STATE, `=== true` only, never retried. Advisory: every draft route re-decides and
 * answers 503 while disabled.
 */
export type CreativeCapabilityStatus = 'loading' | 'enabled' | 'disabled';

export function useCreativeCapability(workspaceId: string) {
  const query = useQuery({
    queryKey: queryKeys.creativeCapability(workspaceId),
    queryFn: ({ signal }) => api.getCreativeCapability(workspaceId, signal),
    staleTime: 60_000,
    retry: false,
  });
  const status: CreativeCapabilityStatus = query.isPending
    ? 'loading'
    : query.data?.enabled === true
      ? 'enabled'
      : 'disabled';
  return { status, isEnabled: status === 'enabled' };
}

/** A brief's draft versions, newest first (the server's order). Never issued while dark. */
export function useBriefDrafts(workspaceId: string, briefId: string, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.briefDrafts(workspaceId, briefId),
    queryFn: ({ signal }) => api.listBriefDrafts(workspaceId, briefId, { limit: 100 }, signal),
    enabled,
    retry: false,
  });
}

/** POST a generate (or revise) request; the answer is the durable job to poll. */
export function useGenerateDraft(workspaceId: string, briefId: string) {
  return useMutation({
    mutationFn: (body: CreativeDraftCreate) => api.generateCreativeDraft(workspaceId, briefId, body),
  });
}

/**
 * Poll one generation job on the shared bounded schedule (`lib/status-polling`) until it is
 * no longer in flight. The key is the workspace's jobs prefix (`queryKeys.jobs`) with the
 * job id as its filter, so it is tenant-scoped and reached by every jobs invalidation;
 * `queryKeys.ts` is the CF freeze's and gains no entry here. When the job succeeds, the
 * brief's draft list is invalidated so the new version appears.
 */
export function useGenerationJob(
  workspaceId: string,
  briefId: string,
  jobId: string | null,
  schedule: StatusPollSchedule,
) {
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: queryKeys.jobs(workspaceId, { creative_job: jobId }),
    queryFn: async ({ signal }) => {
      const job = await api.getJob(workspaceId, jobId as string, signal);
      if (job.status === 'succeeded') {
        await queryClient.invalidateQueries({ queryKey: queryKeys.briefDrafts(workspaceId, briefId) });
      }
      return job;
    },
    enabled: jobId !== null,
    retry: false,
    ...pollingOptions<JobOut>(schedule, `creative-job:${jobId ?? 'none'}`, (job) =>
      job && isGenerationInFlight(job.status) ? `${job.id}:${job.status}` : null,
    ),
  });
}
