import type { CampaignIn, CampaignMode, CampaignOut, CampaignPatch, CampaignStatus, LocationOut } from '@/api/types';
import { DEFAULT_CAMPAIGN_MODE, DEFAULT_CAMPAIGN_STATUS } from './campaignVocabulary';

/** The campaign form's values, in the shapes the controls produce. */
export interface CampaignFormValues {
  name: string;
  goal: string;
  mode: CampaignMode;
  status: CampaignStatus;
  /** The member location ids, sorted (a membership is a set; the API answers it sorted). */
  locationIds: string[];
}

const sorted = (ids: readonly string[]) => [...new Set(ids)].sort();

export function emptyCampaignForm(): CampaignFormValues {
  return {
    name: '',
    goal: '',
    mode: DEFAULT_CAMPAIGN_MODE,
    status: DEFAULT_CAMPAIGN_STATUS,
    locationIds: [],
  };
}

export function campaignFormFrom(campaign: CampaignOut): CampaignFormValues {
  return {
    name: campaign.name,
    goal: campaign.goal ?? '',
    mode: campaign.mode,
    status: campaign.status,
    locationIds: sorted(campaign.location_ids),
  };
}

/** A name the API will accept and a person can recognise: not blank. */
export const hasName = (values: CampaignFormValues) => values.name.trim() !== '';

const goalOf = (values: CampaignFormValues) => (values.goal.trim() === '' ? null : values.goal.trim());

/** The create body: every field the form shows. */
export function createCampaignBody(values: CampaignFormValues): CampaignIn {
  return {
    name: values.name.trim(),
    goal: goalOf(values),
    mode: values.mode,
    status: values.status,
    location_ids: sorted(values.locationIds),
  };
}

/**
 * The PATCH body (P6-PLAT-9): only what changed. `location_ids` replaces the membership
 * whole on the API, so it is sent only when the set itself changed.
 */
export function campaignPatchBody(seed: CampaignFormValues, values: CampaignFormValues): CampaignPatch {
  const body: CampaignPatch = {};
  if (values.name.trim() !== seed.name.trim()) body.name = values.name.trim();
  if (goalOf(values) !== goalOf(seed)) body.goal = goalOf(values);
  if (values.mode !== seed.mode) body.mode = values.mode;
  if (values.status !== seed.status) body.status = values.status;
  const before = sorted(seed.locationIds);
  const after = sorted(values.locationIds);
  if (before.join('\n') !== after.join('\n')) body.location_ids = after;
  return body;
}

/**
 * The locations a campaign form offers: every active location of the workspace, plus any
 * deactivated one that is already a member (the API lets a member stay but refuses to add a
 * deactivated location). Workspace order.
 */
export function offeredLocations(locations: readonly LocationOut[], memberIds: readonly string[]): LocationOut[] {
  const members = new Set(memberIds);
  return locations.filter((location) => location.is_active || members.has(location.id));
}

/** A location's display name: its name, then its city, region and country when known. */
export function locationName(location: LocationOut): string {
  const place = [location.city, location.state_province, location.country]
    .map((part) => (part ?? '').trim())
    .filter(Boolean)
    .join(', ');
  return place && place !== location.name ? `${location.name} (${place})` : location.name;
}
