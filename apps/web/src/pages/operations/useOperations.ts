import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { Capability, CapabilityOverrideSetIn } from '@/api/types';
import { useAuth } from '@/auth/AuthContext';

// Data layer for the operator observability page (4A-D). Every query here hits
// an operator-only endpoint, so callers must gate rendering behind
// RequireOperator. Defense-in-depth, matching the Settings.tsx pattern: each
// query is ALSO gated on the server-authoritative operator signal, so even if a
// hook is ever mounted outside the guard, a customer session issues no request.
// The tenant-scoped capability queries are additionally gated on a concrete
// organization + workspace selection so no request is ever issued without the
// scope the backend requires.

function useIsOperator(): boolean {
  const { user } = useAuth();
  return user?.is_operator ?? false;
}

export function useOperationalOverview() {
  const isOperator = useIsOperator();
  return useQuery({
    queryKey: queryKeys.operationsOverview,
    queryFn: ({ signal }) => api.getOperationalOverview(signal),
    enabled: isOperator,
    staleTime: 30_000,
  });
}

export function useTelemetryStatus() {
  const isOperator = useIsOperator();
  return useQuery({
    queryKey: queryKeys.operationsTelemetry,
    queryFn: ({ signal }) => api.getTelemetryStatus(signal),
    enabled: isOperator,
    staleTime: 60_000,
  });
}

export function useCapabilityRegistry() {
  const isOperator = useIsOperator();
  return useQuery({
    queryKey: queryKeys.capabilityRegistry,
    queryFn: ({ signal }) => api.getCapabilityRegistry(signal),
    enabled: isOperator,
    // The registry is a closed, code-defined vocabulary; it changes only on deploy.
    staleTime: Infinity,
  });
}

export function useCapabilityEffective(organizationId: string | null, workspaceId: string | null) {
  const isOperator = useIsOperator();
  return useQuery({
    queryKey: queryKeys.capabilityEffective(organizationId ?? 'none', workspaceId ?? 'none'),
    queryFn: ({ signal }) => api.getCapabilityEffective(organizationId!, workspaceId!, signal),
    enabled: isOperator && Boolean(organizationId && workspaceId),
    staleTime: 30_000,
  });
}

export function useCapabilityOverrides(organizationId: string | null, workspaceId: string | null) {
  const isOperator = useIsOperator();
  return useQuery({
    queryKey: queryKeys.capabilityOverrides(organizationId ?? 'none', workspaceId ?? 'none'),
    queryFn: ({ signal }) => api.getCapabilityOverrides(organizationId!, workspaceId!, signal),
    enabled: isOperator && Boolean(organizationId && workspaceId),
    staleTime: 30_000,
  });
}

/** Invalidate both tenant-scoped capability reads after a mutation. */
function useInvalidateCapabilityState(organizationId: string | null, workspaceId: string | null) {
  const queryClient = useQueryClient();
  return async () => {
    if (!organizationId || !workspaceId) return;
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: queryKeys.capabilityEffective(organizationId, workspaceId),
      }),
      queryClient.invalidateQueries({
        queryKey: queryKeys.capabilityOverrides(organizationId, workspaceId),
      }),
    ]);
  };
}

export function useSetCapabilityOverride(
  organizationId: string | null,
  workspaceId: string | null,
) {
  const invalidate = useInvalidateCapabilityState(organizationId, workspaceId);
  return useMutation({
    mutationFn: (body: CapabilityOverrideSetIn) => api.setCapabilityOverride(body),
    onSuccess: invalidate,
  });
}

export function useClearCapabilityOverride(
  organizationId: string | null,
  workspaceId: string | null,
) {
  const invalidate = useInvalidateCapabilityState(organizationId, workspaceId);
  return useMutation({
    mutationFn: (capability: Capability) =>
      api.clearCapabilityOverride(organizationId!, workspaceId!, capability),
    onSuccess: invalidate,
  });
}
