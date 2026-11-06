import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { ApprovalDecisionCreate, CreativeDraftCreate, JobOut } from '@/api/types';
import { useToast } from '@/components/ui/toast';
import { pollingOptions, type StatusPollSchedule } from '@/lib/status-polling';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { isGenerationInFlight } from './creativeLabels';

/**
 * Workspace-effective creative-generation availability (the feedback-capability pattern):
 * a TRI-STATE, `=== true` only, never retried. Advisory: every draft route re-decides and
 * answers 503 while disabled.
 */
export type CreativeCapabilityStatus = 'loading' | 'enabled' | 'disabled';

export function useCreativeCapability(
  workspaceId: string,
  { enabled = true }: { enabled?: boolean } = {},
) {
  const query = useQuery({
    queryKey: queryKeys.creativeCapability(workspaceId),
    queryFn: ({ signal }) => api.getCreativeCapability(workspaceId, signal),
    enabled: enabled && Boolean(workspaceId),
    staleTime: 60_000,
    retry: false,
  });
  const status: CreativeCapabilityStatus = query.isPending
    ? 'loading'
    : query.data?.enabled === true
      ? 'enabled'
      : 'disabled';
  return {
    status,
    isEnabled: status === 'enabled',
    // I-5C (battery O-5): whether a generate request can be accepted here (every daily
    // ceiling usable). `=== true`, and only while enabled: unknown never offers Generate.
    generationConfigured: status === 'enabled' && query.data?.generation_configured === true,
  };
}

/**
 * Whether the creative-drafts surface exists in the active workspace (I-5C): drafts are
 * readable by every member, so this is the workspace-effective capability alone. Unknown
 * (loading, no workspace, a failed lookup) is `false`. UX only — every draft route re-decides.
 */
export function useCreativeVisible(): boolean {
  const { workspaceId } = useWorkspace();
  return useCreativeCapability(workspaceId ?? '').isEnabled;
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

/** 4xx answers and the 503 gate are deterministic: never retried. */
function deterministic(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 503 || (error.status >= 400 && error.status < 500));
}

/**
 * One draft version's append-only decisions, newest `decision_seq` first; the first item, when
 * present, is the controlling decision (I-5C). Keyed by workspace and draft.
 */
export function useDraftDecisions(workspaceId: string, draftId: string, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.draftApprovals(workspaceId, draftId),
    queryFn: ({ signal }) => api.listDraftDecisions(workspaceId, draftId, {}, signal),
    enabled: enabled && Boolean(workspaceId && draftId),
    retry: (failureCount, error) => !deterministic(error) && failureCount < 2,
  });
}

/** The static message for a refused draft decision (never the server's text). */
export function draftDecisionFailureMessage(error: unknown): string {
  const code =
    error instanceof ApiError
      ? ((error.detail as { error?: { code?: string } } | null)?.error?.code ?? null)
      : null;
  switch (code) {
    case 'approval_review_blocked':
      return "This draft can't be approved: its content review doesn't allow it, or the claims library changed since it was reviewed.";
    case 'approval_author_conflict':
      return 'You generated this draft, so another reviewer must decide on it.';
    case 'approval_stale_version':
      return 'This version changed. Reload the drafts and decide again.';
    case 'approval_nothing_to_revoke':
      return 'There is no current approval to revoke.';
    case 'approval_concurrent_decision':
      return 'Another decision was recorded at the same time. Reload and try again.';
    default:
      return 'The decision could not be recorded. Try again.';
  }
}

/**
 * Record one decision on one exact draft version (I-5C). The scope is captured in this
 * closure, so a late answer refreshes only its own draft's history. Never retried: an
 * append-only write must not be duplicated.
 */
export function useRecordDraftDecision(workspaceId: string, draftId: string) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  return useMutation({
    mutationFn: (body: ApprovalDecisionCreate) =>
      api.recordDraftDecision(workspaceId, draftId, body),
    retry: false,
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.draftApprovals(workspaceId, draftId),
      });
      toast({ title: 'Decision recorded', intent: 'success' });
    },
    onError: (err) =>
      toast({
        title: 'Could not record the decision',
        description: draftDecisionFailureMessage(err),
        intent: 'error',
      }),
  });
}
