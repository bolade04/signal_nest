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
 * Authoritative, *pre-request* capability gate — **workspace-effective**.
 *
 * The feedback backend decides per workspace through the capability resolver, so
 * an override can enable feedback for one workspace while the global flag stays
 * off. The coarse runtime summary (`GET /system/capabilities`) deliberately
 * reflects the **raw global** flags and therefore cannot answer this question;
 * reading it here was the P6-UI-005 defect. This asks the server the same
 * question the gate asks, scoped to the same workspace.
 *
 * Returns a tri-state rather than a bare boolean. Collapsing "not yet resolved"
 * into "disabled" makes an unresolved query and a real denial indistinguishable.
 * Note what that does and does not buy: `status` has NO production reader at all
 * — the panel consumes only `isEnabled`, computed in this hook's own return — and
 * the panel renders nothing while loading exactly as it does while disabled, so no
 * rendered output differs.
 * The gain is OBSERVABILITY — it is the reason dark-state tests could previously
 * pass without the gate doing anything, because a test could not tell a settled
 * denial from a query that had never resolved.
 *
 * `=== true` rather than a truthy check: a non-boolean value must read as dark.
 *
 * Advisory only. The feedback routes' own 503 remains the enforcement boundary.
 */
export type FeedbackCapabilityStatus = 'loading' | 'enabled' | 'disabled';

export function useFeedbackCapability(workspaceId: string) {
  const query = useQuery({
    queryKey: queryKeys.feedbackCapability(workspaceId),
    queryFn: ({ signal }) => api.getFeedbackCapability(workspaceId, signal),
    staleTime: 60_000,
    // A failed lookup means the capability is unknown, and unknown must not
    // present controls the server would refuse. Retrying would only widen the
    // window in which the UI shows nothing while appearing to be loading.
    //
    // This bounds retries WITHIN a query only. An errored query holds no data,
    // so it is always stale and a later mount re-probes — while the endpoint is
    // failing, each mount costs one request. That is accepted: the alternative
    // is caching a failure, which would keep the UI dark after recovery.
    retry: false,
  });

  const status: FeedbackCapabilityStatus = query.isPending
    ? 'loading'
    : query.data?.enabled === true
      ? 'enabled'
      : 'disabled';

  return { status, isEnabled: status === 'enabled' };
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
