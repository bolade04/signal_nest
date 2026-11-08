import { Megaphone, Plus, RefreshCw } from 'lucide-react';
import { useId, useState } from 'react';
import { Link } from 'react-router-dom';
import { EmptyState, LoadingRows } from '@/components/common/states';
import { PageHeader } from '@/components/layout/page-header';
import { RequireWorkspace } from '@/components/layout/require-workspace';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { canEditWorkspace, useActorRole } from '@/lib/roles';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { CreateCampaignDialog } from './CreateCampaignDialog';
import { campaignReadError } from './campaignErrors';
import { CAMPAIGN_SETTINGS_NOTE, campaignPath, modeLabel, statusLabel } from './campaignVocabulary';
import { useCampaignList } from './useCampaigns';

/**
 * The workspace's campaigns: name, location mode and status, oldest first (the API's order),
 * each linking to its own page; editors (owner, admin, marketer: the campaigns router's
 * `EDITORS`, `canEditWorkspace`) can add one. Every member may read.
 *
 * Not mounted before the I-5D integration cycle (Phase 5 plan §6.3); see `CAMPAIGNS_PATH`.
 */
function CampaignList({ workspaceId }: { workspaceId: string }) {
  const { organizationId } = useWorkspace();
  // An unresolved role is denied until it resolves (lib/roles.ts).
  const canEdit = canEditWorkspace(useActorRole(organizationId).role);
  const campaigns = useCampaignList(workspaceId);
  const [creating, setCreating] = useState(false);
  const idPrefix = useId();

  // Drop a latched create intent when the role gate closes, so the dialog cannot spring back.
  if (!canEdit && creating) setCreating(false);

  const addButton = canEdit ? (
    <Button onClick={() => setCreating(true)}>
      <Plus className="size-4" /> New campaign
    </Button>
  ) : null;

  let body: React.ReactNode;
  if (campaigns.isPending) {
    body = <LoadingRows rows={3} />;
  } else if (campaigns.isError) {
    const { message, retryable } = campaignReadError('list', campaigns.error);
    body = (
      <div
        role="alert"
        className="rounded-lg border border-destructive/40 bg-destructive/5 px-6 py-10 text-center text-sm"
      >
        <p className="font-semibold">The campaigns could not be shown</p>
        <p className="mt-1 text-muted-foreground">{message}</p>
        {retryable ? (
          <Button variant="outline" size="sm" className="mt-4" onClick={() => campaigns.refetch()}>
            <RefreshCw className="size-4" /> Try again
          </Button>
        ) : null}
      </div>
    );
  } else if (campaigns.data.length === 0) {
    body = (
      <EmptyState
        icon={Megaphone}
        title="No campaigns yet"
        description={
          canEdit
            ? 'Add a campaign to record its location mode, status and locations.'
            : 'Owners, admins and marketers can add campaigns.'
        }
        action={addButton}
      />
    );
  } else {
    body = (
      <ul className="space-y-2" aria-label="Campaigns">
        {campaigns.data.map((campaign) => {
          const nameId = `${idPrefix}-${campaign.id}`;
          return (
            <li key={campaign.id}>
              <Card>
                <CardContent className="flex flex-col gap-2 p-4 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <Link id={nameId} to={campaignPath(campaign.id)} className="font-medium hover:underline">
                      {campaign.name}
                    </Link>
                    <p className="text-sm text-muted-foreground">
                      <span className="sr-only">Location mode: </span>
                      {modeLabel(campaign.mode)}
                    </p>
                  </div>
                  <Badge intent="neutral">
                    <span className="sr-only">Status: </span>
                    {statusLabel(campaign.status)}
                  </Badge>
                </CardContent>
              </Card>
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <div>
      <PageHeader
        title="Campaigns"
        description={`The campaigns in this workspace. ${CAMPAIGN_SETTINGS_NOTE}`}
        actions={campaigns.data && campaigns.data.length > 0 ? addButton : null}
      />
      {body}
      {canEdit ? (
        <CreateCampaignDialog workspaceId={workspaceId} open={creating} onOpenChange={setCreating} />
      ) : null}
    </div>
  );
}

export function CampaignsPage() {
  return <RequireWorkspace>{({ workspaceId }) => <CampaignList workspaceId={workspaceId} />}</RequireWorkspace>;
}
