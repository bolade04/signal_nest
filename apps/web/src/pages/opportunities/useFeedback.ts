import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { FeedbackCreate } from '@/api/types';
import { useToast } from '@/components/ui/toast';

/**
 * A feature-dark deployment answers every feedback call with 503
 * ``capability_unavailable``. That is a deterministic gate, not a transient
 * fault, so callers use this to hide the UI (rather than surface an error) and
 * never retry it.
 */
export function isFeatureDark(error: unknown): boolean {
  return error instanceof ApiError && error.status === 503;
}

/**
 * Read one record's append-only feedback history. The query key embeds the
 * intelligence record id, so switching the bound record (or opportunity, or
 * workspace) yields a fresh cache entry with no cross-record leakage. Client
 * (4xx) errors and the 503 feature gate are deterministic and never retried.
 */
export function useFeedbackHistory({
  workspaceId,
  opportunityId,
  intelligenceRecordId,
  enabled = true,
}: {
  workspaceId: string;
  opportunityId: string;
  intelligenceRecordId: string;
  enabled?: boolean;
}) {
  return useQuery({
    queryKey: queryKeys.opportunityFeedback(workspaceId, opportunityId, intelligenceRecordId),
    queryFn: ({ signal }) => api.listOpportunityFeedback(workspaceId, opportunityId, {}, signal),
    enabled:
      enabled &&
      Boolean(workspaceId) &&
      Boolean(opportunityId) &&
      Boolean(intelligenceRecordId),
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
      if (isFeatureDark(error)) return false;
      return failureCount < 2;
    },
  });
}

/**
 * Append one immutable feedback event for the bound record. The tenant +
 * opportunity + record scope is captured in this closure, so a late-resolving
 * submit can only ever invalidate *its own* record's history — never a
 * different record/opportunity that the user may have navigated to meanwhile.
 * Mutations never retry (see the global QueryClient config) to avoid duplicate
 * append-only writes.
 */
export function useSubmitFeedback({
  workspaceId,
  opportunityId,
  intelligenceRecordId,
}: {
  workspaceId: string;
  opportunityId: string;
  intelligenceRecordId: string;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();

  return useMutation({
    mutationFn: (body: FeedbackCreate) =>
      api.submitOpportunityFeedback(workspaceId, opportunityId, body),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.opportunityFeedback(workspaceId, opportunityId, intelligenceRecordId),
      });
      // Append-only: every submission is a new immutable event, never an edit of
      // a prior one — the copy deliberately says "recorded", not "updated".
      toast({ title: 'Feedback recorded', intent: 'success' });
    },
    onError: (err) =>
      toast({
        title: 'Could not record feedback',
        description: err instanceof Error ? err.message : undefined,
        intent: 'error',
      }),
  });
}
