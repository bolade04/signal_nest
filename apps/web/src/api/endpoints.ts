import { apiRequest } from './client';
import type {
  AudienceIn,
  BrandOut,
  BrandVoiceIn,
  BusinessProfileBase,
  BusinessProfileOut,
  CampaignIn,
  ChannelPrefIn,
  ClaimIn,
  CompetitorIn,
  ContextRow,
  GeoCoverageBase,
  GeoCoverageOut,
  GeocodeRequest,
  GeocodeResponse,
  JobEventOut,
  JobListOut,
  JobOut,
  LocationBase,
  LocationOut,
  LoginRequest,
  OfferIn,
  OnboardingRequest,
  OnboardingResult,
  OpportunityCard,
  OpportunityDetail,
  OpportunityFilters,
  OrganizationOut,
  ProductIn,
  RegisterRequest,
  RuntimeCapabilities,
  RuntimeSummary,
  ScoutRequestCreate,
  ScoutRequestOut,
  ScoutRequestUpdate,
  ScoutRunResult,
  SessionOut,
  SourcePrefIn,
  WorkspaceCreate,
  WorkspaceOut,
} from './types';

// ---- System (runtime introspection; no secrets) ----
// Coarse summary — safe for any authenticated customer (mode + readiness only).
export const getRuntimeSummary = (signal?: AbortSignal) =>
  apiRequest<RuntimeSummary>('/system/capabilities', { signal });

// Detailed per-capability backend topology — operator-only (403 otherwise).
export const getRuntimeDetail = (signal?: AbortSignal) =>
  apiRequest<RuntimeCapabilities>('/internal/system/capabilities', { signal });

// ---- Auth ----
export const login = (body: LoginRequest, signal?: AbortSignal) =>
  apiRequest<SessionOut>('/auth/login', { method: 'POST', body, signal });

export const register = (body: RegisterRequest, signal?: AbortSignal) =>
  apiRequest<SessionOut>('/auth/register', { method: 'POST', body, signal });

export const getSession = (signal?: AbortSignal) =>
  apiRequest<SessionOut>('/auth/me', { signal });

// ---- Organizations / workspaces ----
export const listOrganizations = (signal?: AbortSignal) =>
  apiRequest<OrganizationOut[]>('/organizations', { signal });

export const listWorkspaces = (orgId: string, signal?: AbortSignal) =>
  apiRequest<WorkspaceOut[]>(`/organizations/${orgId}/workspaces`, { signal });

export const createWorkspace = (orgId: string, body: WorkspaceCreate) =>
  apiRequest<WorkspaceOut>(`/organizations/${orgId}/workspaces`, { method: 'POST', body });

export const getWorkspace = (workspaceId: string, signal?: AbortSignal) =>
  apiRequest<WorkspaceOut>(`/workspaces/${workspaceId}`, { signal });

// ---- Brand / business profile ----
export const listBrands = (workspaceId: string, signal?: AbortSignal) =>
  apiRequest<BrandOut[]>(`/workspaces/${workspaceId}/brands`, { signal });

export const getBusinessProfile = (workspaceId: string, signal?: AbortSignal) =>
  apiRequest<BusinessProfileOut>(`/workspaces/${workspaceId}/business-profile`, { signal });

export const updateBusinessProfile = (workspaceId: string, body: BusinessProfileBase) =>
  apiRequest<BusinessProfileOut>(`/workspaces/${workspaceId}/business-profile`, {
    method: 'PUT',
    body,
  });

export const onboard = (workspaceId: string, body: OnboardingRequest) =>
  apiRequest<OnboardingResult>(`/workspaces/${workspaceId}/onboarding`, { method: 'POST', body });

// ---- Campaign context (loosely-typed rows) ----
export type ContextKind =
  | 'products'
  | 'audiences'
  | 'competitors'
  | 'brand-voice'
  | 'offers'
  | 'claims'
  | 'source-preferences'
  | 'channel-preferences'
  | 'campaigns';

export type ContextInput =
  | ProductIn
  | AudienceIn
  | CompetitorIn
  | BrandVoiceIn
  | OfferIn
  | ClaimIn
  | SourcePrefIn
  | ChannelPrefIn
  | CampaignIn;

export const listContext = (workspaceId: string, kind: ContextKind, signal?: AbortSignal) =>
  apiRequest<ContextRow[]>(`/workspaces/${workspaceId}/${kind}`, { signal });

export const createContext = (workspaceId: string, kind: ContextKind, body: ContextInput) =>
  apiRequest<ContextRow>(`/workspaces/${workspaceId}/${kind}`, { method: 'POST', body });

export const deleteContext = (workspaceId: string, kind: ContextKind, itemId: string) =>
  apiRequest<void>(`/workspaces/${workspaceId}/${kind}/${itemId}`, { method: 'DELETE' });

// ---- Locations & geo ----
export const listLocations = (workspaceId: string, signal?: AbortSignal) =>
  apiRequest<LocationOut[]>(`/workspaces/${workspaceId}/locations`, { signal });

export const createLocation = (workspaceId: string, body: LocationBase) =>
  apiRequest<LocationOut>(`/workspaces/${workspaceId}/locations`, { method: 'POST', body });

export const updateLocation = (workspaceId: string, locationId: string, body: LocationBase) =>
  apiRequest<LocationOut>(`/workspaces/${workspaceId}/locations/${locationId}`, {
    method: 'PUT',
    body,
  });

export const getGeoCoverage = (workspaceId: string, locationId: string, signal?: AbortSignal) =>
  apiRequest<GeoCoverageOut>(
    `/workspaces/${workspaceId}/locations/${locationId}/geo-coverage`,
    { signal },
  );

export const upsertGeoCoverage = (
  workspaceId: string,
  locationId: string,
  body: GeoCoverageBase,
) =>
  apiRequest<GeoCoverageOut>(`/workspaces/${workspaceId}/locations/${locationId}/geo-coverage`, {
    method: 'PUT',
    body,
  });

export const geocode = (body: GeocodeRequest) =>
  apiRequest<GeocodeResponse>('/geocode', { method: 'POST', body });

// ---- Scout requests ----
export const listScoutRequests = (workspaceId: string, signal?: AbortSignal) =>
  apiRequest<ScoutRequestOut[]>(`/workspaces/${workspaceId}/scout-requests`, { signal });

export const getScoutRequest = (workspaceId: string, requestId: string, signal?: AbortSignal) =>
  apiRequest<ScoutRequestOut>(`/workspaces/${workspaceId}/scout-requests/${requestId}`, { signal });

export const createScoutRequest = (workspaceId: string, body: ScoutRequestCreate) =>
  apiRequest<ScoutRequestOut>(`/workspaces/${workspaceId}/scout-requests`, {
    method: 'POST',
    body,
  });

export const updateScoutRequest = (
  workspaceId: string,
  requestId: string,
  body: ScoutRequestUpdate,
) =>
  apiRequest<ScoutRequestOut>(`/workspaces/${workspaceId}/scout-requests/${requestId}`, {
    method: 'PUT',
    body,
  });

export const pauseScoutRequest = (workspaceId: string, requestId: string) =>
  apiRequest<ScoutRequestOut>(`/workspaces/${workspaceId}/scout-requests/${requestId}/pause`, {
    method: 'POST',
  });

export const resumeScoutRequest = (workspaceId: string, requestId: string) =>
  apiRequest<ScoutRequestOut>(`/workspaces/${workspaceId}/scout-requests/${requestId}/resume`, {
    method: 'POST',
  });

export const runScoutRequest = (workspaceId: string, requestId: string) =>
  apiRequest<ScoutRunResult>(`/workspaces/${workspaceId}/scout-requests/${requestId}/run`, {
    method: 'POST',
  });

// ---- Durable jobs (customer-safe views: lifecycle + outcome, no infra) ----
export interface JobFilters {
  location_id?: string | null;
  scout_request_id?: string | null;
  status?: string | null;
  limit?: number;
  offset?: number;
}

export const listJobs = (workspaceId: string, filters: JobFilters = {}, signal?: AbortSignal) =>
  apiRequest<JobListOut>(`/workspaces/${workspaceId}/jobs`, { query: { ...filters }, signal });

export const getJob = (workspaceId: string, jobId: string, signal?: AbortSignal) =>
  apiRequest<JobOut>(`/workspaces/${workspaceId}/jobs/${jobId}`, { signal });

export const listJobEvents = (workspaceId: string, jobId: string, signal?: AbortSignal) =>
  apiRequest<JobEventOut[]>(`/workspaces/${workspaceId}/jobs/${jobId}/events`, { signal });

export const cancelJob = (workspaceId: string, jobId: string) =>
  apiRequest<JobOut>(`/workspaces/${workspaceId}/jobs/${jobId}/cancel`, { method: 'POST' });

// ---- Opportunities ----
export const listOpportunities = (
  workspaceId: string,
  filters: OpportunityFilters,
  signal?: AbortSignal,
) =>
  apiRequest<OpportunityCard[]>(`/workspaces/${workspaceId}/opportunities`, {
    query: { ...filters },
    signal,
  });

export const getOpportunity = (workspaceId: string, opportunityId: string, signal?: AbortSignal) =>
  apiRequest<OpportunityDetail>(`/workspaces/${workspaceId}/opportunities/${opportunityId}`, {
    signal,
  });

export const updateOpportunityStatus = (
  workspaceId: string,
  opportunityId: string,
  status: string,
) =>
  apiRequest<OpportunityCard>(
    `/workspaces/${workspaceId}/opportunities/${opportunityId}/status`,
    { method: 'PUT', body: { status } },
  );
