import { ArrowLeft, Megaphone, Pencil, RefreshCw, Trash2 } from 'lucide-react';
import { useId, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import type { CampaignOut, LocationOut } from '@/api/types';
import { ConfirmDialog } from '@/components/common/confirm-dialog';
import { EmptyState, LoadingRows } from '@/components/common/states';
import { PageHeader } from '@/components/layout/page-header';
import { RequireWorkspace } from '@/components/layout/require-workspace';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { useToast } from '@/components/ui/toast';
import { canEditWorkspace, useActorRole } from '@/lib/roles';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { CampaignFields } from './CampaignFields';
import { campaignReadError, campaignWriteError } from './campaignErrors';
import { campaignFormFrom, campaignPatchBody, hasName, locationName, type CampaignFormValues } from './campaignForm';
import { CAMPAIGN_SETTINGS_NOTE, CAMPAIGNS_PATH, modeLabel, statusLabel } from './campaignVocabulary';
import { useCampaign, useDeleteCampaign, useUpdateCampaign, useWorkspaceLocations } from './useCampaigns';

const BackLink = () => (
  <Button asChild variant="ghost" size="sm" className="mb-2 -ml-2">
    <Link to={CAMPAIGNS_PATH}>
      <ArrowLeft className="size-4" /> All campaigns
    </Link>
  </Button>
);

function Alert({ children }: { children: React.ReactNode }) {
  return (
    <div role="alert" className="mb-4 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
      {children}
    </div>
  );
}

/** The campaign's member locations, by name. Never an id or any tenant field. */
function MemberLocations({
  campaign,
  locations,
  locationsError,
}: {
  campaign: CampaignOut;
  locations: readonly LocationOut[] | undefined;
  locationsError: boolean;
}) {
  const count = campaign.location_ids.length;
  if (count === 0) return <p className="text-sm text-muted-foreground">No locations.</p>;
  if (!locations) {
    return (
      <p className="text-sm text-muted-foreground">
        {count} {count === 1 ? 'location' : 'locations'}
        {locationsError ? '. Their names could not be loaded.' : '.'}
      </p>
    );
  }
  const byId = new Map(locations.map((location) => [location.id, location]));
  return (
    <ul className="space-y-1 text-sm">
      {campaign.location_ids.map((id) => {
        const location = byId.get(id);
        return (
          <li key={id}>
            {location ? locationName(location) : 'A location that is no longer listed'}
            {location && !location.is_active ? <span className="text-muted-foreground"> (deactivated)</span> : null}
          </li>
        );
      })}
    </ul>
  );
}

function CampaignView({ workspaceId, campaignId }: { workspaceId: string; campaignId: string }) {
  const navigate = useNavigate();
  const { toast } = useToast();
  const { organizationId } = useWorkspace();
  // Editing and deleting are editor actions (the campaigns router's EDITORS); every member
  // reads. An unresolved role is denied until it resolves.
  const canEdit = canEditWorkspace(useActorRole(organizationId).role);
  const idPrefix = useId();
  const campaign = useCampaign(workspaceId, campaignId);
  const locations = useWorkspaceLocations(workspaceId);
  const update = useUpdateCampaign(workspaceId, campaignId);
  const remove = useDeleteCampaign(workspaceId, campaignId);

  const [editing, setEditing] = useState<{ seed: CampaignFormValues; values: CampaignFormValues } | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Drop latched edit and delete intents when the role gate closes.
  if (!canEdit && (editing || confirmingDelete)) {
    setEditing(null);
    setConfirmingDelete(false);
  }

  if (campaign.isPending) return <LoadingRows rows={3} />;
  if (campaign.isError) {
    const { message, retryable, notFound } = campaignReadError('campaign', campaign.error);
    if (notFound) {
      return (
        <div>
          <BackLink />
          {error ? <Alert>{error}</Alert> : null}
          <EmptyState icon={Megaphone} title="Campaign not found" description={message} />
        </div>
      );
    }
    return (
      <div>
        <BackLink />
        {error ? <Alert>{error}</Alert> : null}
        <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/5 px-6 py-10 text-center text-sm">
          <p className="font-semibold">This campaign could not be shown</p>
          <p className="mt-1 text-muted-foreground">{message}</p>
          {retryable ? (
            <Button variant="outline" size="sm" className="mt-4" onClick={() => campaign.refetch()}>
              <RefreshCw className="size-4" /> Try again
            </Button>
          ) : null}
        </div>
      </div>
    );
  }

  const current = campaign.data;
  const changes = editing ? campaignPatchBody(editing.seed, editing.values) : {};
  const unchanged = Object.keys(changes).length === 0;

  const startEdit = () => {
    const seed = campaignFormFrom(current);
    setError(null);
    setEditing({ seed, values: seed });
  };

  const save = () => {
    if (!editing) return;
    setError(null);
    update.mutate(changes, {
      onSuccess: () => {
        toast({ title: 'Campaign updated', intent: 'success' });
        setEditing(null);
      },
      onError: (err) => setError(campaignWriteError('update', err)),
    });
  };

  const confirmDelete = async () => {
    setError(null);
    try {
      await remove.mutateAsync();
    } catch (err) {
      setError(campaignWriteError('delete', err));
      return;
    }
    toast({ title: 'Campaign deleted', intent: 'success' });
    navigate(CAMPAIGNS_PATH);
  };

  return (
    <div>
      <BackLink />
      <PageHeader
        title={current.name}
        description={CAMPAIGN_SETTINGS_NOTE}
        actions={
          canEdit && !editing ? (
            <>
              <Button variant="outline" onClick={startEdit}>
                <Pencil className="size-4" /> Edit campaign
              </Button>
              <Button variant="outline" onClick={() => setConfirmingDelete(true)} disabled={remove.isPending}>
                <Trash2 className="size-4" /> Delete campaign
              </Button>
            </>
          ) : null
        }
      />

      {error ? <Alert>{error}</Alert> : null}

      {editing ? (
        <Card>
          <CardContent className="space-y-4 p-4">
            <p className="text-sm text-muted-foreground">
              Only the fields you change are saved. Choosing locations replaces the campaign’s
              list of locations with the ones ticked.
            </p>
            <CampaignFields
              values={editing.values}
              onChange={(values) => setEditing({ ...editing, values })}
              seedLocationIds={editing.seed.locationIds}
              locations={locations.data}
              locationsError={locations.isError}
              idPrefix={idPrefix}
            />
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setEditing(null)} disabled={update.isPending}>
                Cancel
              </Button>
              <Button onClick={save} disabled={update.isPending || unchanged || !hasName(editing.values)}>
                Save changes
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-4">
            <dl className="grid gap-4 sm:grid-cols-2">
              <div className="sm:col-span-2">
                <dt className="text-sm font-medium">Goal</dt>
                <dd className="text-sm text-muted-foreground">{current.goal?.trim() ? current.goal : 'No goal recorded.'}</dd>
              </div>
              <div>
                <dt className="text-sm font-medium">Location mode</dt>
                <dd className="text-sm">{modeLabel(current.mode)}</dd>
              </div>
              <div>
                <dt className="text-sm font-medium">Status</dt>
                <dd>
                  <Badge intent="neutral">{statusLabel(current.status)}</Badge>
                </dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-sm font-medium">Locations</dt>
                <dd>
                  <MemberLocations campaign={current} locations={locations.data} locationsError={locations.isError} />
                </dd>
              </div>
            </dl>
          </CardContent>
        </Card>
      )}

      {canEdit ? (
        <ConfirmDialog
          open={confirmingDelete}
          onOpenChange={setConfirmingDelete}
          title="Delete this campaign?"
          description={`“${current.name}” and its list of locations are deleted. Scout requests and opportunities that used it are kept, without the campaign. This cannot be undone.`}
          confirmLabel="Delete campaign"
          destructive
          onConfirm={confirmDelete}
        />
      ) : null}
    </div>
  );
}

/**
 * One campaign: its goal, location mode, status and locations, read by every member.
 * Editors (owner, admin, marketer) can edit it in place and delete it after confirming.
 *
 * Mounted at `${CAMPAIGNS_PATH}/:campaignId` by the I-5D integration cycle (Phase 5 plan §6.3).
 */
export function CampaignDetailPage() {
  const { campaignId = '' } = useParams<{ campaignId: string }>();
  return (
    <RequireWorkspace>
      {({ workspaceId }) => <CampaignView key={`${workspaceId}:${campaignId}`} workspaceId={workspaceId} campaignId={campaignId} />}
    </RequireWorkspace>
  );
}
