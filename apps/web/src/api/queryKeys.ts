import type { OpportunityFilters } from './types';

// Query-key factory. Tenant-scoped keys always embed workspace_id (and, where
// relevant, location/campaign/scout-request) so that switching tenant context
// naturally produces distinct cache entries and prevents cross-tenant leakage.
export const queryKeys = {
  session: ['session'] as const,
  runtimeCapabilities: ['system', 'capabilities'] as const,
  organizations: ['organizations'] as const,
  workspaces: (orgId: string) => ['organizations', orgId, 'workspaces'] as const,
  workspace: (workspaceId: string) => ['workspaces', workspaceId] as const,

  brands: (workspaceId: string) => ['workspaces', workspaceId, 'brands'] as const,
  businessProfile: (workspaceId: string) =>
    ['workspaces', workspaceId, 'business-profile'] as const,

  context: (workspaceId: string, kind: string) =>
    ['workspaces', workspaceId, 'context', kind] as const,

  locations: (workspaceId: string) => ['workspaces', workspaceId, 'locations'] as const,
  geoCoverage: (workspaceId: string, locationId: string) =>
    ['workspaces', workspaceId, 'locations', locationId, 'geo-coverage'] as const,

  scoutRequests: (workspaceId: string) => ['workspaces', workspaceId, 'scout-requests'] as const,
  scoutRequest: (workspaceId: string, requestId: string) =>
    ['workspaces', workspaceId, 'scout-requests', requestId] as const,

  opportunities: (workspaceId: string, filters: OpportunityFilters) =>
    ['workspaces', workspaceId, 'opportunities', filters] as const,
  opportunity: (workspaceId: string, opportunityId: string) =>
    ['workspaces', workspaceId, 'opportunities', 'detail', opportunityId] as const,

  auditLogs: (workspaceId: string) => ['workspaces', workspaceId, 'audit-logs'] as const,
} as const;
