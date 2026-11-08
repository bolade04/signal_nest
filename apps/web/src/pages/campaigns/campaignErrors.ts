import { ApiError } from '@/api/client';

/**
 * What the campaign pages say when the API refuses or cannot answer. Every message names
 * what happened and what, if anything, the person can do; "try again" appears only where a
 * repeat can succeed (a network or server failure, a rate limit). The refusals are the
 * dedicated campaigns router's (apps/api/app/campaign_context/campaigns.py) and the tenant
 * gate's (apps/api/app/auth/dependencies.py):
 *
 * - 403: not a member of the workspace's organization (reads), or a member whose role is not
 *   owner, admin or marketer (writes);
 * - 404: the workspace, the campaign, or a requested location is not in this workspace;
 * - 409: the campaign changed in a way that conflicts with this request;
 * - 422: the request was invalid; the API's own message says how.
 */
export type CampaignWrite = 'create' | 'update' | 'delete';

const NOT_SAVED: Record<CampaignWrite, string> = {
  create: 'The campaign was not added.',
  update: 'Your changes were not saved.',
  delete: 'The campaign was not deleted.',
};

export const NOT_AN_EDITOR = 'Only owners, admins and marketers can add, change or delete campaigns.';

export const CAMPAIGN_NOT_FOUND = 'This campaign is not in this workspace. It may have been deleted.';

const statusOf = (error: unknown) => (error instanceof ApiError ? error.status : null);

/** The message for a failed read of the campaign list or of one campaign. */
export function campaignReadError(
  read: 'list' | 'campaign',
  error: unknown,
): { message: string; retryable: boolean; notFound: boolean } {
  const status = statusOf(error);
  const answer = (message: string, retryable: boolean, notFound = false) => ({ message, retryable, notFound });
  if (status === 403) return answer('You do not have access to this workspace’s campaigns.', false);
  if (status === 404) {
    // The list's only 404 is the tenant gate's: the workspace itself is gone.
    return read === 'list'
      ? answer('This workspace was not found. Choose another workspace from the switcher.', false)
      : answer(CAMPAIGN_NOT_FOUND, false, true);
  }
  if (status === 429) return answer('Too many requests in a short time. Wait a moment, then try again.', true);
  if (status === 0 || status === null) {
    return answer('The server could not be reached. Check your connection, then try again.', true);
  }
  if (status >= 500) return answer('The server had a problem loading this. Try again in a moment.', true);
  return answer((error as ApiError).message, false);
}

/** The message for a refused or failed write. */
export function campaignWriteError(action: CampaignWrite, error: unknown): string {
  const lead = NOT_SAVED[action];
  const status = statusOf(error);
  switch (status) {
    case 403:
      return `${lead} ${NOT_AN_EDITOR}`;
    case 404:
      return action === 'create'
        ? // Create names no campaign: the workspace or a requested location is gone.
          `${lead} ${(error as ApiError).message}`
        : action === 'delete'
          ? 'This campaign is no longer in this workspace, so there was nothing to delete.'
          : `${lead} This campaign, or a location you chose, is no longer in this workspace.`;
    case 409:
      return `${lead} The campaign changed at the same time. Reload it to see the current version.`;
    case 422:
      return `${lead} ${(error as ApiError).message}`;
    case 429:
      return `${lead} Too many requests in a short time. Wait a moment, then try again.`;
    case 0:
    case null:
      return `${lead} The server could not be reached. Check your connection, then try again.`;
    default:
      return status !== null && status >= 500
        ? `${lead} The server had a problem. Try again in a moment.`
        : `${lead} ${(error as ApiError).message}`;
  }
}
