import type { CampaignMode, CampaignStatus } from '@/api/types';
import { campaignModeLabels, campaignStatusLabels, label } from '@/lib/labels';

/**
 * The campaign's two closed vocabularies, exactly as the generated contract declares them
 * (`CampaignMode`, `CampaignStatus`; Project Phase 5D campaign hardening). Each is a
 * `satisfies Record<…>` object rather than a hand-written array, so a member added to or
 * removed from the contract is a compile error here, never a silently missing or invented
 * option. The values give the display order.
 */
const MODE_ORDER = {
  same_for_all: 0,
  per_location: 1,
  grouped: 2,
  recommend: 3,
} as const satisfies Record<CampaignMode, number>;

const STATUS_ORDER = {
  draft: 0,
  active: 1,
  paused: 2,
  completed: 3,
  archived: 4,
} as const satisfies Record<CampaignStatus, number>;

/** Every location mode, in display order. */
export const CAMPAIGN_MODES: readonly CampaignMode[] = Object.freeze(
  (Object.keys(MODE_ORDER) as CampaignMode[]).sort((a, b) => MODE_ORDER[a] - MODE_ORDER[b]),
);

/** Every campaign status, in display order. */
export const CAMPAIGN_STATUSES: readonly CampaignStatus[] = Object.freeze(
  (Object.keys(STATUS_ORDER) as CampaignStatus[]).sort((a, b) => STATUS_ORDER[a] - STATUS_ORDER[b]),
);

/** The API's own defaults for a new campaign (`CampaignIn`: `per_location`, `active`). */
export const DEFAULT_CAMPAIGN_MODE: CampaignMode = 'per_location';
export const DEFAULT_CAMPAIGN_STATUS: CampaignStatus = 'active';

export const isCampaignMode = (value: unknown): value is CampaignMode =>
  typeof value === 'string' && (CAMPAIGN_MODES as readonly string[]).includes(value);

export const isCampaignStatus = (value: unknown): value is CampaignStatus =>
  typeof value === 'string' && (CAMPAIGN_STATUSES as readonly string[]).includes(value);

/** Display labels, read through `lib/labels.ts`. */
export const modeLabel = (mode: string | null | undefined) => label(campaignModeLabels, mode);
export const statusLabel = (status: string | null | undefined) => label(campaignStatusLabels, status);

/**
 * Where the campaign pages live. The I-5D integration cycle (Phase 5 plan §6.3) mounted them
 * with `App.tsx` routes and a `nav.ts` entry for exactly these paths: `CAMPAIGNS_PATH` for
 * `CampaignsPage` and `${CAMPAIGNS_PATH}/:campaignId` for `CampaignDetailPage`.
 */
export const CAMPAIGNS_PATH = '/campaigns';
export const campaignPath = (campaignId: string) =>
  `${CAMPAIGNS_PATH}/${encodeURIComponent(campaignId)}`;

/**
 * What every campaign page says about mode, status and locations. True as built: the API
 * stores them and nothing in scouting or scoring reads them yet.
 */
export const CAMPAIGN_SETTINGS_NOTE =
  'A campaign’s location mode, status and locations are saved with it. Scouting does not use them yet.';
