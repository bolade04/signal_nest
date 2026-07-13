import type { components } from './schema';

type S = components['schemas'];

export type SessionOut = S['SessionOut'];
export type UserOut = S['UserOut'];
export type MembershipOut = S['MembershipOut'];
export type LoginRequest = S['LoginRequest'];
export type RegisterRequest = S['RegisterRequest'];

export type OrganizationOut = S['OrganizationOut'];
export type WorkspaceOut = S['WorkspaceOut'];
export type WorkspaceCreate = S['WorkspaceCreate'];
export type BrandOut = S['BrandOut'];

export type BusinessProfileBase = S['BusinessProfileBase'];
export type BusinessProfileOut = S['BusinessProfileOut'];

export type OnboardingRequest = S['OnboardingRequest'];
export type OnboardingResult = S['OnboardingResult'];

export type LocationBase = S['LocationBase'];
export type LocationOut = S['LocationOut'];
export type GeoCoverageBase = S['GeoCoverageBase'];
export type GeoCoverageOut = S['GeoCoverageOut'];
export type GeocodeRequest = S['GeocodeRequest'];
export type GeocodeResponse = S['GeocodeResponse'];

export type ScoutRequestCreate = S['ScoutRequestCreate'];
export type ScoutRequestUpdate = S['ScoutRequestUpdate'];
export type ScoutRequestOut = S['ScoutRequestOut'];
export type ScoutRunResult = S['ScoutRunResult'];

export type JobOut = S['JobOut'];
export type JobListOut = S['JobListOut'];
export type JobEventOut = S['JobEventOut'];

export type OpportunityCard = S['OpportunityCard'];
export type OpportunityDetail = S['OpportunityDetail'];
export type OpportunityStatusUpdate = S['OpportunityStatusUpdate'];
export type ScoreBreakdown = S['ScoreBreakdown'];
export type ValidationEvidenceOut = S['ValidationEvidenceOut'];

export type ProductIn = S['ProductIn'];
export type AudienceIn = S['AudienceIn'];
export type CompetitorIn = S['CompetitorIn'];
export type BrandVoiceIn = S['BrandVoiceIn'];
export type OfferIn = S['OfferIn'];
export type ClaimIn = S['ClaimIn'];
export type CampaignIn = S['CampaignIn'];
export type SourcePrefIn = S['SourcePrefIn'];
export type ChannelPrefIn = S['ChannelPrefIn'];

export type RuntimeSummary = S['RuntimeSummaryOut'];
export type RuntimeCapability = S['CapabilityOut'];
export type RuntimeCapabilities = S['CapabilitiesOut'];

// Campaign-context list endpoints return loosely-typed dict rows; every row
// carries at least an id alongside its input fields.
export type ContextRow = Record<string, unknown> & { id: string };

export interface OpportunityFilters {
  location_id?: string | null;
  campaign_id?: string | null;
  scout_request_id?: string | null;
  classification?: string | null;
  decision?: string | null;
  status?: string | null;
  risk_level?: string | null;
  market?: string | null;
  min_score?: number;
  search?: string | null;
  sort?: string;
  order?: string;
  limit?: number;
}
