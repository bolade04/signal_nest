import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { RecommendationBriefCreate } from '@/api/types';
import { useToast } from '@/components/ui/toast';

/**
 * A brief endpoint answers 503 `capability_unavailable` while the capability resolves
 * disabled for the workspace. That is a deterministic gate, not a fault: the panel hides
 * itself and never retries it.
 */
export function isBriefsDark(error: unknown): boolean {
  return error instanceof ApiError && error.status === 503;
}

/**
 * Authoritative, pre-request capability gate for the brief panel — WORKSPACE-EFFECTIVE.
 *
 * Briefs are workspace-enableable (O-3): an operator override can enable them for one
 * workspace while the global flag stays off, so the raw-global runtime summary cannot
 * answer this (the P6-UI-005 lesson). This asks the server the same question the brief
 * gate asks, for the same workspace (`GET /workspaces/{workspace_id}/brief-capability`).
 *
 * Tri-state: an unresolved query is not a denial; the panel renders nothing for both.
 * `=== true`, so a non-boolean reads as dark. A failed lookup is unknown, and unknown
 * never presents controls the server would refuse; it is not retried.
 *
 * Advisory only. Every brief endpoint's own 503 remains the enforcement boundary.
 */
export type BriefCapabilityStatus = 'loading' | 'enabled' | 'disabled';

export function useBriefCapability(
  workspaceId: string,
  // I-5A/I-5B: the sidebar asks only for a member who could use the approval queue.
  { enabled = true }: { enabled?: boolean } = {},
): {
  status: BriefCapabilityStatus;
  isEnabled: boolean;
} {
  const query = useQuery({
    queryKey: queryKeys.briefCapability(workspaceId),
    queryFn: ({ signal }) => api.getBriefCapability(workspaceId, signal),
    enabled: enabled && Boolean(workspaceId),
    staleTime: 60_000,
    retry: false,
  });
  const status: BriefCapabilityStatus = query.isPending
    ? 'loading'
    : query.data?.enabled === true
      ? 'enabled'
      : 'disabled';
  return { status, isEnabled: status === 'enabled' };
}

/**
 * The opportunity's brief versions, newest first. The key embeds the workspace and
 * opportunity (CF-1 `queryKeys.opportunityBriefs`), so nothing is shared across tenants
 * or opportunities. 4xx answers and the 503 gate are deterministic and never retried.
 */
export function useOpportunityBriefs({
  workspaceId,
  opportunityId,
  enabled = true,
}: {
  workspaceId: string;
  opportunityId: string;
  enabled?: boolean;
}) {
  return useQuery({
    queryKey: queryKeys.opportunityBriefs(workspaceId, opportunityId),
    queryFn: ({ signal }) => api.listOpportunityBriefs(workspaceId, opportunityId, {}, signal),
    enabled: enabled && Boolean(workspaceId) && Boolean(opportunityId),
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
      if (isBriefsDark(error)) return false;
      return failureCount < 2;
    },
  });
}

/**
 * Generate and capture a new brief version. The workspace and opportunity are captured in
 * this closure, so a late answer only ever refreshes its own opportunity's briefs. Never
 * retried (each POST is a paid model call and a new immutable version).
 */
export function useCreateBrief({
  workspaceId,
  opportunityId,
}: {
  workspaceId: string;
  opportunityId: string;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();

  return useMutation({
    mutationFn: (body: RecommendationBriefCreate) =>
      api.createOpportunityBrief(workspaceId, opportunityId, body),
    retry: false,
    onSuccess: async (brief) => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.opportunityBriefs(workspaceId, opportunityId),
      });
      toast({ title: `Brief version ${brief.brief_version} saved`, intent: 'success' });
    },
    onError: (err) =>
      toast({
        title: 'No brief was saved',
        description: err instanceof Error ? err.message : undefined,
        intent: 'error',
      }),
  });
}
