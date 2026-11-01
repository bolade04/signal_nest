import { apiRequest, apiRequestPage } from './client';
import type {
  FeedbackCapability,
  AccountDeletionOut,
  AccountDeletionPreviewOut,
  AccountDeletionRequest,
  AccountExportOut,
  AudienceIn,
  BrandOut,
  BrandVoiceIn,
  Capability,
  CapabilityEffectiveList,
  CapabilityOverrideMutation,
  CapabilityOverridePage,
  CapabilityOverrideSetIn,
  CapabilityRegistry,
  ChangePasswordRequest,
  BusinessProfileBase,
  BusinessProfileOut,
  CampaignIn,
  ChannelPrefIn,
  ClaimIn,
  CompetitorIn,
  ContextRow,
  EmailVerificationConfirmRequest,
  EmailVerificationRequest,
  FeedbackCreate,
  FeedbackHistoryOut,
  FeedbackOut,
  RecommendationBriefCreate,
  RecommendationBriefListOut,
  RecommendationBriefOut,
  GeoCoverageBase,
  GeoCoverageOut,
  GeocodeRequest,
  GeocodeResponse,
  InvitationCreate,
  InvitationCreatedOut,
  InvitationOut,
  InvitationPreviewOut,
  InvitationRegisterRequest,
  InvitationTokenRequest,
  JobEventOut,
  JobListOut,
  JobOut,
  LocationBase,
  LocationOut,
  LoginRequest,
  MemberRoleUpdate,
  OfferIn,
  OnboardingRequest,
  OnboardingResult,
  OperationalOverview,
  OpportunityCard,
  OpportunityDetail,
  OpportunityFilters,
  OpportunityIntelligenceResponse,
  OrganizationDeletionOut,
  OrganizationDeletionRequest,
  OrganizationExportOut,
  OrganizationExportPageOut,
  OrganizationMemberOut,
  OrganizationOut,
  PasswordResetConfirmRequest,
  PasswordResetRequest,
  ProductIn,
  RegisterRequest,
  RuntimeCapabilities,
  RuntimeSummary,
  ScoutRequestCreate,
  ScoutRequestOut,
  ScoutRequestUpdate,
  ScoutRunResult,
  ScoutScheduleCreate,
  ScoutScheduleOut,
  SchedulingCapability,
  SessionOut,
  SourcePrefIn,
  TelemetryStatus,
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

// ---- Operator observability + capability governance (4A-D) ----
// Every endpoint below is operator-only: the backend's require_operator dependency
// rejects non-operators with 403. The frontend gates these calls on the
// server-authoritative `user.is_operator` so a customer session never issues them.
export const getOperationalOverview = (signal?: AbortSignal) =>
  apiRequest<OperationalOverview>('/internal/system/overview', { signal });

export const getTelemetryStatus = (signal?: AbortSignal) =>
  apiRequest<TelemetryStatus>('/internal/system/telemetry', { signal });

export const getCapabilityRegistry = (signal?: AbortSignal) =>
  apiRequest<CapabilityRegistry>('/internal/system/capabilities/registry', { signal });

export const getCapabilityEffective = (
  organizationId: string,
  workspaceId: string,
  signal?: AbortSignal,
) =>
  apiRequest<CapabilityEffectiveList>('/internal/system/capabilities/effective', {
    query: { organization_id: organizationId, workspace_id: workspaceId },
    signal,
  });

export const getCapabilityOverrides = (
  organizationId: string,
  workspaceId: string,
  signal?: AbortSignal,
) =>
  apiRequest<CapabilityOverridePage>('/internal/system/capabilities/overrides', {
    query: { organization_id: organizationId, workspace_id: workspaceId },
    signal,
  });

export const setCapabilityOverride = (body: CapabilityOverrideSetIn) =>
  apiRequest<CapabilityOverrideMutation>('/internal/system/capabilities/overrides', {
    method: 'PUT',
    body,
  });

export const clearCapabilityOverride = (
  organizationId: string,
  workspaceId: string,
  capability: Capability,
) =>
  apiRequest<CapabilityOverrideMutation>('/internal/system/capabilities/overrides', {
    method: 'DELETE',
    query: { organization_id: organizationId, workspace_id: workspaceId, capability },
  });

// ---- Auth ----
export const login = (body: LoginRequest, signal?: AbortSignal) =>
  apiRequest<SessionOut>('/auth/login', { method: 'POST', body, signal });

export const register = (body: RegisterRequest, signal?: AbortSignal) =>
  apiRequest<SessionOut>('/auth/register', { method: 'POST', body, signal });

export const getSession = (signal?: AbortSignal) =>
  apiRequest<SessionOut>('/auth/me', { signal });

// ---- Sessions (P6-AUTH-4) ----
// Only the bearer is sent; each answers an empty 204. Sign out ends the session the
// token belongs to; sign out everywhere ends every session of the account.
export const logout = (signal?: AbortSignal) =>
  apiRequest<void>('/auth/logout', { method: 'POST', signal });

export const logoutAll = (signal?: AbortSignal) =>
  apiRequest<void>('/auth/logout-all', { method: 'POST', signal });

// ---- Invitation token holders (P6-AUTH-1) ----
// The raw token travels only in the JSON body — never a path segment or query
// string. The organization, role and email are the invitation's own; none of these
// requests accepts them. Preview is public and spends nothing; register is public
// and creates the account; accept needs the session of the invited email.
export const previewInvitation = (body: InvitationTokenRequest, signal?: AbortSignal) =>
  apiRequest<InvitationPreviewOut>('/auth/invitations/preview', { method: 'POST', body, signal });

export const registerWithInvitation = (body: InvitationRegisterRequest, signal?: AbortSignal) =>
  apiRequest<SessionOut>('/auth/invitations/register', { method: 'POST', body, signal });

export const acceptInvitation = (body: InvitationTokenRequest, signal?: AbortSignal) =>
  apiRequest<SessionOut>('/auth/invitations/accept', { method: 'POST', body, signal });

// ---- Password reset and email verification (P6-AUTH-2) ----
// Every answer is an empty 204. A token travels only in the JSON body — never a path
// segment or query string. Reset is public: the request answers alike for every
// address, and the confirm spends the token, signs every session of the account out
// and returns no session. Verification needs the session of the token's own account.
export const requestPasswordReset = (body: PasswordResetRequest, signal?: AbortSignal) =>
  apiRequest<void>('/auth/password-reset/request', { method: 'POST', body, signal });

export const confirmPasswordReset = (body: PasswordResetConfirmRequest, signal?: AbortSignal) =>
  apiRequest<void>('/auth/password-reset/confirm', { method: 'POST', body, signal });

// The body is always the empty object: the address is the account's own, and a
// request without a body is refused (422).
export const requestEmailVerification = (signal?: AbortSignal) => {
  const body: EmailVerificationRequest = {};
  return apiRequest<void>('/auth/email-verification/request', { method: 'POST', body, signal });
};

export const confirmEmailVerification = (body: EmailVerificationConfirmRequest, signal?: AbortSignal) =>
  apiRequest<void>('/auth/email-verification/confirm', { method: 'POST', body, signal });

// ---- Password change (P6-UI-017) ----
// Bearer-authenticated; both passwords travel only in the JSON body. The answer is an
// empty 204, after which the server refuses EVERY token of the account — the one that
// sent this request included — and returns no session: the caller signs in again.
export const changePassword = (body: ChangePasswordRequest, signal?: AbortSignal) =>
  apiRequest<void>('/auth/password/change', { method: 'POST', body, signal });

// ---- Data export and deletion (P6-PRIV-1) ----
// Exports are read-only. Both deletions are re-authenticated: the current password travels
// only in the JSON body. After a 200 from deleteAccount the account no longer exists, so
// every token of it is refused: the caller forgets its session locally.
export const exportAccount = (signal?: AbortSignal) =>
  apiRequest<AccountExportOut>('/account/export', { signal });

export const getAccountDeletionPreview = (signal?: AbortSignal) =>
  apiRequest<AccountDeletionPreviewOut>('/account/deletion-preview', { signal });

export const deleteAccount = (body: AccountDeletionRequest, signal?: AbortSignal) =>
  apiRequest<AccountDeletionOut>('/account/delete', { method: 'POST', body, signal });

// The organization export is complete but paged (D-9f): this index call, then one keyset
// page per cursor (`next_cursor` null = the table is done). lib/organization-export.ts walks
// them; it is the only caller.
export const exportOrganization = (orgId: string, signal?: AbortSignal) =>
  apiRequest<OrganizationExportOut>(`/organizations/${orgId}/export`, { signal });

export const exportOrganizationTablePage = (
  orgId: string,
  table: string,
  cursor: string,
  limit: number,
  signal?: AbortSignal,
) =>
  apiRequest<OrganizationExportPageOut>(
    `/organizations/${orgId}/export/${encodeURIComponent(table)}`,
    { query: { cursor, limit }, signal },
  );

export const deleteOrganization = (orgId: string, body: OrganizationDeletionRequest, signal?: AbortSignal) =>
  apiRequest<OrganizationDeletionOut>(`/organizations/${orgId}/delete`, { method: 'POST', body, signal });

// ---- Organizations / workspaces ----
export const listOrganizations = (signal?: AbortSignal) =>
  apiRequest<OrganizationOut[]>('/organizations', { signal });

export const listWorkspaces = (orgId: string, signal?: AbortSignal) =>
  apiRequest<WorkspaceOut[]>(`/organizations/${orgId}/workspaces`, { signal });

export const createWorkspace = (orgId: string, body: WorkspaceCreate) =>
  apiRequest<WorkspaceOut>(`/organizations/${orgId}/workspaces`, { method: 'POST', body });

export const getWorkspace = (workspaceId: string, signal?: AbortSignal) =>
  apiRequest<WorkspaceOut>(`/workspaces/${workspaceId}`, { signal });

// ---- Organization members + invitations (6B-3A; P6-AUTH-1) ----
// Any member may read the member list. Everything else — inviting, listing and
// revoking invitations, changing a role, removing a member — is exact OWNER/ADMIN
// on the backend, which also enforces the self, OWNER-ceiling and last-owner rules.
export const listOrganizationMembers = (orgId: string, signal?: AbortSignal) =>
  apiRequest<OrganizationMemberOut[]>(`/organizations/${orgId}/members`, { signal });

export const changeMemberRole = (orgId: string, userId: string, body: MemberRoleUpdate) =>
  apiRequest<OrganizationMemberOut>(`/organizations/${orgId}/members/${userId}/role`, {
    method: 'PUT',
    body,
  });

export const removeMember = (orgId: string, userId: string) =>
  apiRequest<void>(`/organizations/${orgId}/members/${userId}`, { method: 'DELETE' });

// The list never carries a token. The create response is the one place the raw
// token exists: the server keeps only its hash, so it cannot be shown again.
export const listInvitations = (orgId: string, signal?: AbortSignal) =>
  apiRequest<InvitationOut[]>(`/organizations/${orgId}/invitations`, { signal });

export const createInvitation = (orgId: string, body: InvitationCreate) =>
  apiRequest<InvitationCreatedOut>(`/organizations/${orgId}/invitations`, {
    method: 'POST',
    body,
  });

export const revokeInvitation = (orgId: string, invitationId: string) =>
  apiRequest<void>(`/organizations/${orgId}/invitations/${invitationId}`, { method: 'DELETE' });

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

// P6-UI-023: a location is retired, never deleted, so its history keeps its market.
export const deactivateLocation = (workspaceId: string, locationId: string) =>
  apiRequest<LocationOut>(`/workspaces/${workspaceId}/locations/${locationId}/deactivate`, {
    method: 'POST',
  });

export const reactivateLocation = (workspaceId: string, locationId: string) =>
  apiRequest<LocationOut>(`/workspaces/${workspaceId}/locations/${locationId}/reactivate`, {
    method: 'POST',
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

// Workspace-scoped like every other tenant call: the route resolves its tenant through
// get_tenant_context, so `workspace_id` is a REQUIRED query parameter (openapi.json);
// without it the API answers 422 before the lookup runs (UI-AUDIT F2).
export const geocode = (workspaceId: string, body: GeocodeRequest) =>
  apiRequest<GeocodeResponse>('/geocode', {
    method: 'POST',
    body,
    query: { workspace_id: workspaceId },
  });

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

// ---- Scout schedules (SB-C; dark-deployed recurrence) ----
// The read is available to any member even while the feature is dark; mutations
// require an editor role and answer 503 (capability_unavailable) while scheduling
// resolves disabled FOR THAT WORKSPACE (P6-CAP-1: the capability resolver, in which a
// workspace override outranks the global flag).
const schedulePath = (workspaceId: string, requestId: string) =>
  `/workspaces/${workspaceId}/scout-requests/${requestId}/schedule`;

// Workspace-effective scheduling availability (P6-UI-006), decided by the same
// resolver call the schedule mutation gate enforces with. Member-gated like the
// schedule read, and deliberately NOT capability-gated: it answers 200
// {enabled:false} while dark rather than 503.
export const getSchedulingCapability = (workspaceId: string, signal?: AbortSignal) =>
  apiRequest<SchedulingCapability>(`/workspaces/${workspaceId}/scheduling-capability`, {
    signal,
  });

export const getScoutSchedule = (workspaceId: string, requestId: string, signal?: AbortSignal) =>
  apiRequest<ScoutScheduleOut>(schedulePath(workspaceId, requestId), { signal });

export const createScoutSchedule = (
  workspaceId: string,
  requestId: string,
  body: ScoutScheduleCreate,
) =>
  apiRequest<ScoutScheduleOut>(schedulePath(workspaceId, requestId), { method: 'POST', body });

export const pauseScoutSchedule = (workspaceId: string, requestId: string) =>
  apiRequest<ScoutScheduleOut>(`${schedulePath(workspaceId, requestId)}/pause`, { method: 'POST' });

export const resumeScoutSchedule = (workspaceId: string, requestId: string) =>
  apiRequest<ScoutScheduleOut>(`${schedulePath(workspaceId, requestId)}/resume`, {
    method: 'POST',
  });

export const deleteScoutSchedule = (workspaceId: string, requestId: string) =>
  apiRequest<void>(schedulePath(workspaceId, requestId), { method: 'DELETE' });

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

/** One page of the feed (P6-PLAT-12). `total` is the whole filtered count, not the page. */
export const listOpportunityPage = (
  workspaceId: string,
  filters: OpportunityFilters,
  cursor: string | null,
  signal?: AbortSignal,
) =>
  apiRequestPage<OpportunityCard>(`/workspaces/${workspaceId}/opportunities`, {
    query: { ...filters, cursor },
    signal,
  });

export const getOpportunity = (workspaceId: string, opportunityId: string, signal?: AbortSignal) =>
  apiRequest<OpportunityDetail>(`/workspaces/${workspaceId}/opportunities/${opportunityId}`, {
    signal,
  });

// Read-only persisted intelligence for one opportunity (Batch 4B). A 200 with
// ``{ intelligence: null }`` is a valid empty result, not an error.
export const getOpportunityIntelligence = (
  workspaceId: string,
  opportunityId: string,
  signal?: AbortSignal,
) =>
  apiRequest<OpportunityIntelligenceResponse>(
    `/workspaces/${workspaceId}/opportunities/${opportunityId}/intelligence`,
    { signal },
  );

export const updateOpportunityStatus = (
  workspaceId: string,
  opportunityId: string,
  status: string,
) =>
  apiRequest<OpportunityCard>(
    `/workspaces/${workspaceId}/opportunities/${opportunityId}/status`,
    { method: 'PUT', body: { status } },
  );

// ---- Opportunity feedback (3C-C; dark-deployed human feedback loop) ----
// The history read and the write are capability-gated *and* editor-gated: while
// the capability resolves disabled FOR THAT WORKSPACE they answer 503
// (capability_unavailable), and a view-only member is 403. The append-only
// history is a bounded page. The capability reflection added below is the
// exception: it is editor-gated but deliberately NOT capability-gated, and
// answers 200 {enabled:false} while dark rather than 503 — gating the endpoint
// that reports the decision on that same decision would make it useless.
const feedbackPath = (workspaceId: string, opportunityId: string) =>
  `/workspaces/${workspaceId}/opportunities/${opportunityId}/feedback`;

// Workspace-effective feedback availability, decided by the same resolver the
// feedback gate enforces with. Workspace-scoped, not opportunity-scoped: the
// resolver's decision domain is (capability, workspace).
export const getFeedbackCapability = (workspaceId: string, signal?: AbortSignal) =>
  apiRequest<FeedbackCapability>(`/workspaces/${workspaceId}/feedback-capability`, { signal });

export const listOpportunityFeedback = (
  workspaceId: string,
  opportunityId: string,
  params: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
) =>
  apiRequest<FeedbackHistoryOut>(feedbackPath(workspaceId, opportunityId), {
    query: { ...params },
    signal,
  });

export const submitOpportunityFeedback = (
  workspaceId: string,
  opportunityId: string,
  body: FeedbackCreate,
) => apiRequest<FeedbackOut>(feedbackPath(workspaceId, opportunityId), { method: 'POST', body });

// ---- Project Phase 5A recommendation briefs (lane P5A) ----
// Every brief endpoint is capability-gated: while `recommendation_briefs` resolves
// disabled FOR THAT WORKSPACE it answers 503 (capability_unavailable). Reads are open
// to every workspace member; capture is editor-only (403 otherwise). Briefs are
// immutable versions: there is no update or delete call.
const briefsPath = (workspaceId: string, opportunityId: string) =>
  `/workspaces/${workspaceId}/opportunities/${opportunityId}/briefs`;

export const listOpportunityBriefs = (
  workspaceId: string,
  opportunityId: string,
  params: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
) =>
  apiRequest<RecommendationBriefListOut>(briefsPath(workspaceId, opportunityId), {
    query: { ...params },
    signal,
  });

export const getOpportunityBrief = (
  workspaceId: string,
  opportunityId: string,
  briefId: string,
  signal?: AbortSignal,
) =>
  apiRequest<RecommendationBriefOut>(`${briefsPath(workspaceId, opportunityId)}/${briefId}`, {
    signal,
  });

export const createOpportunityBrief = (
  workspaceId: string,
  opportunityId: string,
  body: RecommendationBriefCreate,
) =>
  apiRequest<RecommendationBriefOut>(briefsPath(workspaceId, opportunityId), {
    method: 'POST',
    body,
  });
// ---- end Phase 5A recommendation briefs ----
