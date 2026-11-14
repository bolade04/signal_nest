import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { CampaignIn, CampaignPatch } from '@/api/types';

/**
 * The campaign pages' data access, over the dedicated campaigns router (5D-W1). The list is
 * keyed by the campaign collection's own key (`queryKeys.context(ws, 'campaigns')`, which the
 * campaign-context page shares for the same answer) and one campaign by `queryKeys.campaign`,
 * which lies under it: invalidating the list refreshes every campaign of the workspace.
 */
export const campaignListKey = (workspaceId: string) => queryKeys.context(workspaceId, 'campaigns');

export function useCampaignList(workspaceId: string) {
  return useQuery({
    queryKey: campaignListKey(workspaceId),
    queryFn: ({ signal }) => api.listCampaigns(workspaceId, signal),
  });
}

export function useCampaign(workspaceId: string, campaignId: string) {
  return useQuery({
    queryKey: queryKeys.campaign(workspaceId, campaignId),
    queryFn: ({ signal }) => api.getCampaign(workspaceId, campaignId, signal),
    enabled: Boolean(campaignId),
  });
}

/** The workspace's locations (active and deactivated), for naming and choosing members. */
export function useWorkspaceLocations(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.locations(workspaceId),
    queryFn: ({ signal }) => api.listLocations(workspaceId, signal),
  });
}

export function useCreateCampaign(workspaceId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: CampaignIn) => api.createCampaign(workspaceId, body),
    onSettled: () => queryClient.invalidateQueries({ queryKey: campaignListKey(workspaceId) }),
  });
}

export function useUpdateCampaign(workspaceId: string, campaignId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: CampaignPatch) => api.updateCampaign(workspaceId, campaignId, body),
    onSuccess: (campaign) => queryClient.setQueryData(queryKeys.campaign(workspaceId, campaignId), campaign),
    // Refused or not, re-read: a refusal can mean the campaign or a location is gone.
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: campaignListKey(workspaceId) }),
        queryClient.invalidateQueries({ queryKey: queryKeys.locations(workspaceId) }),
      ]),
  });
}

export function useDeleteCampaign(workspaceId: string, campaignId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.deleteCampaign(workspaceId, campaignId),
    // Deleted: refresh the list only (the page leaves for it), so the open campaign is not
    // re-read and briefly shown as missing. Refused: re-read everything, the campaign too.
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: campaignListKey(workspaceId), exact: true }),
    onError: () => queryClient.invalidateQueries({ queryKey: campaignListKey(workspaceId) }),
  });
}
