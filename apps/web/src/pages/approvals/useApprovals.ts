import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { ApprovalDecisionCreate } from '@/api/types';
import { useToast } from '@/components/ui/toast';

/** The page size of the approval queue as this UI reads it. */
export const QUEUE_PAGE_SIZE = 50;

/**
 * Approvals are dark behind `recommendation_briefs_enabled`: every approval call answers
 * 503 `capability_unavailable` while the capability resolves disabled for the workspace.
 * That is a deterministic answer, so callers show a "not available" state instead of an
 * error and never retry it.
 */
export function isApprovalsDark(error: unknown): boolean {
  return error instanceof ApiError && error.status === 503;
}

function deterministic(error: unknown): boolean {
  return (
    isApprovalsDark(error) ||
    (error instanceof ApiError && error.status >= 400 && error.status < 500)
  );
}

/** The workspace's briefs awaiting a decision, oldest first (reviewer only). */
export function useApprovalQueue(
  workspaceId: string,
  { enabled = true }: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: queryKeys.approvalQueue(workspaceId),
    queryFn: ({ signal }) => api.getApprovalQueue(workspaceId, { limit: QUEUE_PAGE_SIZE }, signal),
    enabled: enabled && Boolean(workspaceId),
    retry: (failureCount, error) => !deterministic(error) && failureCount < 2,
  });
}

/**
 * One brief version's append-only decisions, newest `decision_seq` first. The first item,
 * when present, is the controlling decision. The key embeds workspace, opportunity and
 * brief, so no entry is shared across tenants or subjects.
 */
export function useBriefDecisions({
  workspaceId,
  opportunityId,
  briefId,
  enabled = true,
}: {
  workspaceId: string;
  opportunityId: string;
  briefId: string;
  enabled?: boolean;
}) {
  return useQuery({
    queryKey: queryKeys.briefApprovals(workspaceId, opportunityId, briefId),
    queryFn: ({ signal }) =>
      api.listBriefDecisions(workspaceId, opportunityId, briefId, {}, signal),
    enabled: enabled && Boolean(workspaceId && opportunityId && briefId),
    retry: (failureCount, error) => !deterministic(error) && failureCount < 2,
  });
}

/**
 * Record one decision on one exact brief version. The scope is captured in this closure,
 * so a late answer can only refresh its own brief's history and its own workspace's
 * queue. Never retried (an append-only write must not be duplicated); a refusal — the
 * author's own brief, a stale version, nothing to revoke, a concurrent decision — is
 * shown with the server's message.
 */
export function useRecordDecision({
  workspaceId,
  opportunityId,
  briefId,
}: {
  workspaceId: string;
  opportunityId: string;
  briefId: string;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  return useMutation({
    mutationFn: (body: ApprovalDecisionCreate) =>
      api.recordBriefDecision(workspaceId, opportunityId, briefId, body),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: queryKeys.briefApprovals(workspaceId, opportunityId, briefId),
        }),
        queryClient.invalidateQueries({
          queryKey: queryKeys.approvalQueue(workspaceId),
        }),
      ]);
      // Append-only: a decision is a new record, never an edit of an earlier one.
      toast({ title: 'Decision recorded', intent: 'success' });
    },
    onError: (err) =>
      toast({
        title: 'Could not record the decision',
        description: err instanceof Error ? err.message : undefined,
        intent: 'error',
      }),
  });
}
