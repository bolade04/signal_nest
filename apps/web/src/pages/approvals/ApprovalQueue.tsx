import { ClipboardCheck, ShieldCheck } from 'lucide-react';
import { useState } from 'react';
import type { ApprovalDecision, RecommendationBriefOut } from '@/api/types';
import { useAuth } from '@/auth/AuthContext';
import { EmptyState, ErrorState, LoadingRows } from '@/components/common/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { canDecideApprovals, useActorRole } from '@/lib/roles';
import { formatRelative } from '@/lib/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { isBriefAuthor, snapshotName } from './approvalVocabulary';
import { BriefDecisions } from './BriefDecisions';
import { DecisionDialog, type DecisionTarget } from './DecisionDialog';
import { isApprovalsDark, useApprovalQueue } from './useApprovals';

/**
 * The approval queue (Phase 5B): the active workspace's briefs awaiting a decision —
 * the latest version of each opportunity's brief that has no controlling approval or
 * rejection — oldest first, with the decision controls.
 *
 * NOT MOUNTED on any route in this lane: the route and its nav entry are added at the
 * I-5B integration cycle (Phase 5 plan §6.3). Until then it is exercised by its own test
 * harness (`__tests__/approval-queue.test.tsx`).
 *
 * Only the reviewer role decides (exact set, operator decision O-2); every other role
 * sees why, and no queue request is made for it. The server also refuses the brief's
 * author, so the author's own brief is listed with the controls disabled and the reason
 * stated. While approvals are dark for the workspace (503) the page says so.
 */
export function ApprovalQueuePage() {
  const { workspaceId, organizationId } = useWorkspace();
  const actor = useActorRole(organizationId);

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="flex items-center gap-2 text-xl font-semibold">
          <ClipboardCheck className="size-5 text-primary" /> Approvals
        </h1>
        <p className="text-sm text-muted-foreground">
          Recommendation briefs waiting for a reviewer&apos;s decision in this workspace.
        </p>
      </header>
      {!workspaceId || actor.status === 'unknown' ? (
        <LoadingRows rows={2} />
      ) : canDecideApprovals(actor.role) ? (
        <QueueList workspaceId={workspaceId} />
      ) : (
        <EmptyState
          icon={ShieldCheck}
          title="Only reviewers decide on briefs"
          description="Approval decisions are made by members with the Reviewer role. An owner or admin can grant it under Settings → Members; the role applies in every workspace of the organization."
        />
      )}
    </div>
  );
}

interface Pending {
  brief: RecommendationBriefOut;
  decision: ApprovalDecision;
}

function QueueList({ workspaceId }: { workspaceId: string }) {
  const { user } = useAuth();
  const queue = useApprovalQueue(workspaceId);
  const [pending, setPending] = useState<Pending | null>(null);

  if (queue.isPending) return <LoadingRows rows={3} />;
  if (queue.isError) {
    if (isApprovalsDark(queue.error)) {
      return (
        <EmptyState
          title="Approvals are not available for this workspace yet"
          description="Recommendation briefs and their approvals are switched off here."
        />
      );
    }
    return <ErrorState error={queue.error} onRetry={() => queue.refetch()} />;
  }

  const { items, total } = queue.data;
  if (!items.length) {
    return (
      <EmptyState
        icon={ClipboardCheck}
        title="Nothing is waiting for a decision"
        description="New briefs appear here until a reviewer approves or rejects them."
      />
    );
  }

  return (
    <>
      <p className="text-sm text-muted-foreground">
        {total === items.length
          ? `${total} brief${total === 1 ? '' : 's'} awaiting a decision`
          : `Showing the oldest ${items.length} of ${total} briefs awaiting a decision`}
      </p>
      <ul aria-label="Briefs awaiting a decision" className="space-y-4">
        {items.map((brief) => (
          <li key={brief.id}>
            <QueueItem
              brief={brief}
              workspaceId={workspaceId}
              isAuthor={isBriefAuthor(brief, user?.id)}
              onDecide={(decision) => setPending({ brief, decision })}
            />
          </li>
        ))}
      </ul>
      {pending ? (
        <DecisionDialog
          key={`${pending.brief.id}-${pending.decision}`}
          target={targetOf(workspaceId, pending.brief)}
          decision={pending.decision}
          onClose={() => setPending(null)}
        />
      ) : null}
    </>
  );
}

function targetOf(workspaceId: string, brief: RecommendationBriefOut): DecisionTarget {
  return {
    workspaceId,
    opportunityId: brief.opportunity_id,
    briefId: brief.id,
    briefVersion: brief.brief_version,
  };
}

function QueueItem({
  brief,
  workspaceId,
  isAuthor,
  onDecide,
}: {
  brief: RecommendationBriefOut;
  workspaceId: string;
  isAuthor: boolean;
  onDecide: (decision: ApprovalDecision) => void;
}) {
  const [showHistory, setShowHistory] = useState(false);
  const headingId = `brief-${brief.id}-heading`;
  return (
    <Card aria-labelledby={headingId}>
      <CardHeader className="space-y-1.5">
        <CardTitle id={headingId} className="flex flex-wrap items-center gap-2 text-base">
          Brief version {brief.brief_version}
          {brief.is_simulated ? <Badge intent="muted">Simulated</Badge> : null}
        </CardTitle>
        <CardDescription>
          Written by {snapshotName(brief.created_by_snapshot)} · {formatRelative(brief.created_at)}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <p>{brief.summary}</p>
        <p className="text-muted-foreground">{brief.rationale}</p>
        {brief.recommended_actions?.length ? (
          <div>
            <p className="font-medium">Recommended actions</p>
            <ul className="list-disc pl-5">
              {brief.recommended_actions.map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {brief.risk_notes?.length ? (
          <div>
            <p className="font-medium">Risks</p>
            <ul className="list-disc pl-5">
              {brief.risk_notes.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {isAuthor ? (
          <p className="text-xs text-muted-foreground" role="note">
            You wrote this brief, so another reviewer must decide on it.
          </p>
        ) : null}
        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={() => onDecide('approved')} disabled={isAuthor}>
            Approve
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => onDecide('rejected')}
            disabled={isAuthor}
          >
            Reject
          </Button>
          <Button
            size="sm"
            variant="ghost"
            aria-expanded={showHistory}
            onClick={() => setShowHistory((v) => !v)}
          >
            {showHistory ? 'Hide decision history' : 'Show decision history'}
          </Button>
        </div>
        {showHistory ? (
          <BriefDecisions target={targetOf(workspaceId, brief)} brief={brief} />
        ) : null}
      </CardContent>
    </Card>
  );
}
