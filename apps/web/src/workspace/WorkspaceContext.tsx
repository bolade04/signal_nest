import { useQuery } from '@tanstack/react-query';
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { BrandOut, LocationOut, OrganizationOut, WorkspaceOut } from '@/api/types';
import { useAuth } from '@/auth/AuthContext';

const ORG_KEY = 'signalnest-active-org';
const WS_KEY = 'signalnest-active-workspace';
const LOC_KEY = 'signalnest-active-location';

interface WorkspaceContextValue {
  organizations: OrganizationOut[];
  workspaces: WorkspaceOut[];
  brands: BrandOut[];
  locations: LocationOut[];
  organizationId: string | null;
  workspaceId: string | null;
  brandId: string | null;
  /** null = "All locations" */
  locationId: string | null;
  activeOrganization: OrganizationOut | null;
  activeWorkspace: WorkspaceOut | null;
  activeLocation: LocationOut | null;
  isLoading: boolean;
  setOrganizationId: (id: string) => void;
  setWorkspaceId: (id: string) => void;
  setLocationId: (id: string | null) => void;
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const { status, memberships } = useAuth();
  const authed = status === 'authenticated';

  const [organizationId, setOrgId] = useState<string | null>(() => localStorage.getItem(ORG_KEY));
  const [workspaceId, setWsId] = useState<string | null>(() => localStorage.getItem(WS_KEY));
  const [locationId, setLocId] = useState<string | null>(() => localStorage.getItem(LOC_KEY));

  const orgQuery = useQuery({
    queryKey: queryKeys.organizations,
    queryFn: ({ signal }) => api.listOrganizations(signal),
    enabled: authed,
  });

  const organizations = useMemo(() => orgQuery.data ?? [], [orgQuery.data]);

  // Resolve a valid active organization from persisted value / memberships / list.
  // Adjusted during render (React's endorsed alternative to a setState-in-effect):
  // when the current selection isn't in the freshly-loaded list we fall back
  // immediately. The guard is self-limiting — once a valid id is selected the
  // condition is false — so it cannot loop. localStorage is synced separately in
  // the write-only effects below.
  if (organizations.length && !organizations.some((o) => o.id === organizationId)) {
    const fallback =
      memberships.find((m) => organizations.some((o) => o.id === m.organization_id))
        ?.organization_id ?? organizations[0]!.id;
    setOrgId(fallback);
  }

  const wsQuery = useQuery({
    queryKey: organizationId ? queryKeys.workspaces(organizationId) : ['workspaces', 'none'],
    queryFn: ({ signal }) => api.listWorkspaces(organizationId!, signal),
    enabled: authed && Boolean(organizationId),
  });

  const workspaces = useMemo(() => wsQuery.data ?? [], [wsQuery.data]);

  if (workspaces.length && !workspaces.some((w) => w.id === workspaceId)) {
    // Only wipe the active location on an actual switch away from a
    // previously-resolved workspace. On initial resolution (workspaceId was
    // null) keep any persisted location — the resolver below drops it if it
    // doesn't belong to this workspace.
    const wasResolved = workspaceId !== null;
    setWsId(workspaces[0]!.id);
    if (wasResolved) setLocId(null);
  }

  const brandsQuery = useQuery({
    queryKey: workspaceId ? queryKeys.brands(workspaceId) : ['brands', 'none'],
    queryFn: ({ signal }) => api.listBrands(workspaceId!, signal),
    enabled: authed && Boolean(workspaceId),
  });

  const locationsQuery = useQuery({
    queryKey: workspaceId ? queryKeys.locations(workspaceId) : ['locations', 'none'],
    queryFn: ({ signal }) => api.listLocations(workspaceId!, signal),
    enabled: authed && Boolean(workspaceId),
  });

  const brands = useMemo(() => brandsQuery.data ?? [], [brandsQuery.data]);
  const locations = useMemo(() => locationsQuery.data ?? [], [locationsQuery.data]);

  // If the active location no longer belongs to the workspace, drop it.
  if (locationId && locations.length && !locations.some((l) => l.id === locationId)) {
    setLocId(null);
  }

  // Persist the resolved selections. These effects only sync to an external
  // system (localStorage) and never call setState, which is what effects are for.
  useEffect(() => {
    if (organizationId) localStorage.setItem(ORG_KEY, organizationId);
    else localStorage.removeItem(ORG_KEY);
  }, [organizationId]);
  useEffect(() => {
    if (workspaceId) localStorage.setItem(WS_KEY, workspaceId);
    else localStorage.removeItem(WS_KEY);
  }, [workspaceId]);
  useEffect(() => {
    if (locationId) localStorage.setItem(LOC_KEY, locationId);
    else localStorage.removeItem(LOC_KEY);
  }, [locationId]);

  const setOrganizationId = useCallback((id: string) => {
    setOrgId(id);
    // Force workspace/location re-resolution for the new org. localStorage is
    // kept in sync by the write-only effects above.
    setWsId(null);
    setLocId(null);
  }, []);

  const setWorkspaceId = useCallback((id: string) => {
    setWsId(id);
    setLocId(null);
  }, []);

  const setLocationId = useCallback((id: string | null) => {
    setLocId(id);
  }, []);

  const value = useMemo<WorkspaceContextValue>(() => {
    return {
      organizations,
      workspaces,
      brands,
      locations,
      organizationId,
      workspaceId,
      brandId: brands[0]?.id ?? null,
      locationId,
      activeOrganization: organizations.find((o) => o.id === organizationId) ?? null,
      activeWorkspace: workspaces.find((w) => w.id === workspaceId) ?? null,
      activeLocation: locations.find((l) => l.id === locationId) ?? null,
      isLoading: orgQuery.isLoading || wsQuery.isLoading,
      setOrganizationId,
      setWorkspaceId,
      setLocationId,
    };
  }, [
    organizations,
    workspaces,
    brands,
    locations,
    organizationId,
    workspaceId,
    locationId,
    orgQuery.isLoading,
    wsQuery.isLoading,
    setOrganizationId,
    setWorkspaceId,
    setLocationId,
  ]);

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useWorkspace(): WorkspaceContextValue {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) throw new Error('useWorkspace must be used within WorkspaceProvider');
  return ctx;
}
