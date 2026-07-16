import { useQuery } from '@tanstack/react-query';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';

/**
 * Fetch the read-only persisted intelligence (Batch 4B) for one opportunity.
 *
 * The query key embeds both the workspace and opportunity IDs so switching either
 * naturally yields a distinct cache entry (no cross-tenant / cross-opportunity
 * leakage). It is disabled until both IDs are present. An HTTP 200 with
 * ``intelligence: null`` is a successful *empty* result — the panel renders a
 * neutral empty state, never an error. Authentication (401) and not-found (404)
 * surface through the shared {@link ApiError} conventions; we do not retry those.
 */
export function useOpportunityIntelligence({
  workspaceId,
  opportunityId,
  enabled = true,
}: {
  workspaceId: string;
  opportunityId: string;
  enabled?: boolean;
}) {
  return useQuery({
    queryKey: queryKeys.opportunityIntelligence(workspaceId, opportunityId),
    queryFn: ({ signal }) => api.getOpportunityIntelligence(workspaceId, opportunityId, signal),
    enabled: enabled && Boolean(workspaceId) && Boolean(opportunityId),
    // Do not hammer the endpoint on client (401/404/validation) errors.
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
      return failureCount < 2;
    },
  });
}
