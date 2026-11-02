import { useQuery } from '@tanstack/react-query';
import { ArrowLeft, PenLine } from 'lucide-react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import { LoadingRows } from '@/components/common/states';
import { Button } from '@/components/ui/button';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { CreativeDraftsPanel } from './CreativeDraftsPanel';

/**
 * One brief's drafts (I-5C): `/creative/:briefId?opportunity=:opportunityId`, linked from the
 * brief panel. The drafts are read by brief id alone. The opportunity is needed only for the
 * brief itself: its summary here, and its decision history, which decides whether Generate is
 * offered. Without a usable `opportunity` parameter the drafts still show, and Generate is not
 * offered (fail closed). Mounted under `CreativeRoute` (not-found while dark).
 */
export function BriefDraftsPage() {
  const { briefId = '' } = useParams();
  const [params] = useSearchParams();
  const opportunityId = params.get('opportunity');
  const { workspaceId } = useWorkspace();

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <Button variant="ghost" size="sm" asChild>
          <Link to={opportunityId ? `/opportunities/${opportunityId}` : '/creative'}>
            <ArrowLeft className="size-4" />
            {opportunityId ? 'Back to the opportunity' : 'Back to creative drafts'}
          </Link>
        </Button>
        <h1 className="flex items-center gap-2 text-xl font-semibold">
          <PenLine className="size-5 text-primary" /> Drafts for this brief
        </h1>
        {workspaceId && opportunityId ? (
          <BriefSummary workspaceId={workspaceId} opportunityId={opportunityId} briefId={briefId} />
        ) : null}
      </header>
      {workspaceId ? (
        <CreativeDraftsPanel
          workspaceId={workspaceId}
          briefId={briefId}
          opportunityId={opportunityId}
        />
      ) : (
        <LoadingRows rows={2} />
      )}
    </div>
  );
}

/** The brief's summary, for context; nothing when the brief cannot be read (e.g. dark). */
function BriefSummary({
  workspaceId,
  opportunityId,
  briefId,
}: {
  workspaceId: string;
  opportunityId: string;
  briefId: string;
}) {
  const brief = useQuery({
    queryKey: queryKeys.opportunityBrief(workspaceId, opportunityId, briefId),
    queryFn: ({ signal }) => api.getOpportunityBrief(workspaceId, opportunityId, briefId, signal),
    retry: false,
  });
  if (!brief.data) return null;
  return (
    <p className="text-sm text-muted-foreground">
      Brief version {brief.data.brief_version}: {brief.data.summary}
    </p>
  );
}
