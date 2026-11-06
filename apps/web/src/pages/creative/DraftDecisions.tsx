import { useState } from 'react';
import { ApiError } from '@/api/client';
import type { ApprovalDecision, ApprovalOut, CreativeDraftOut } from '@/api/types';
import { useAuth } from '@/auth/AuthContext';
import { ErrorState } from '@/components/common/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { canDecideApprovals, canReadApprovalHistory, useActorRole } from '@/lib/roles';
import { formatRelative } from '@/lib/utils';
import {
  DECISION_ACTION,
  DECISION_INTENT,
  DECISION_LABEL,
  isBriefAuthor,
  reasonLabel,
  snapshotName,
} from '@/pages/approvals/approvalVocabulary';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { reviewState } from './creativeLabels';
import { DraftDecisionDialog } from './DraftDecisionDialog';
import { useDraftDecisions } from './useCreative';

/**
 * One draft version's decisions and, for the reviewer, the decision controls (I-5C).
 *
 * The API is the oracle (`app/creative/routes.py`, `app/approvals/service.py`):
 * - the history is readable by every role but viewer, so a viewer gets nothing and no request;
 * - deciding is for the REVIEWER role exactly, and never the draft's author;
 * - Approve only while the stored content review passed (`reviewState === 'passed'`, the
 *   service's stored-review checks). The service also re-reads the claims library at approval,
 *   which the page cannot know in advance, so that refusal is answered with a static message;
 * - Reject is allowed whatever the review says;
 * - Revoke only while the controlling decision is an approval.
 * Renders nothing while the decision route answers dark (503) or refuses the role (403).
 */
export function DraftDecisions({ workspaceId, draft }: { workspaceId: string; draft: CreativeDraftOut }) {
  const { organizationId } = useWorkspace();
  const actor = useActorRole(organizationId);
  if (!canReadApprovalHistory(actor.role)) return null;
  return (
    <DecisionsInner
      workspaceId={workspaceId}
      draft={draft}
      mayDecide={canDecideApprovals(actor.role)}
    />
  );
}

function DecisionsInner({
  workspaceId,
  draft,
  mayDecide,
}: {
  workspaceId: string;
  draft: CreativeDraftOut;
  mayDecide: boolean;
}) {
  const { user } = useAuth();
  const history = useDraftDecisions(workspaceId, draft.id, true);
  const [pending, setPending] = useState<ApprovalDecision | null>(null);

  if (history.isPending) {
    return (
      <div aria-busy="true" aria-live="polite">
        <span className="sr-only">Loading decisions…</span>
        <Skeleton className="h-6 w-48" />
      </div>
    );
  }
  if (history.isError) {
    const err = history.error;
    if (err instanceof ApiError && (err.status === 503 || err.status === 403)) return null;
    return <ErrorState error={err} onRetry={() => history.refetch()} />;
  }

  const items = history.data.items;
  const controlling: ApprovalOut | undefined = items[0];
  const isAuthor = isBriefAuthor(draft, user?.id);
  const canAct = mayDecide && !isAuthor;
  const reviewPassed = reviewState(draft.review) === 'passed';
  const offers: ApprovalDecision[] = [];
  if (canAct && reviewPassed && controlling?.decision !== 'approved') offers.push('approved');
  if (canAct && controlling?.decision !== 'rejected') offers.push('rejected');
  if (canAct && controlling?.decision === 'approved') offers.push('revoked');

  return (
    <section aria-label={`Decisions on draft version ${draft.draft_version}`} className="space-y-2">
      <h4 className="text-sm font-semibold">Approval</h4>
      {items.length === 0 ? (
        <p className="text-xs text-muted-foreground">No decision recorded for this version yet.</p>
      ) : (
        <ol aria-label="Draft decision history" className="space-y-1.5">
          {items.map((item, index) => (
            <li key={item.id} className="flex flex-wrap items-center gap-2 text-xs">
              <Badge intent={DECISION_INTENT[item.decision]}>{DECISION_LABEL[item.decision]}</Badge>
              {index === 0 ? <Badge intent="outline">Current decision</Badge> : null}
              {item.reason_code ? <span>{reasonLabel(item.reason_code)}</span> : null}
              <span className="text-muted-foreground">
                by {snapshotName(item.decided_by_snapshot)} · {formatRelative(item.created_at)}
              </span>
              {item.note ? <q className="basis-full text-muted-foreground">{item.note}</q> : null}
            </li>
          ))}
        </ol>
      )}
      {mayDecide && isAuthor ? (
        <p className="text-xs text-muted-foreground" role="note">
          You generated this draft, so another reviewer must decide on it.
        </p>
      ) : null}
      {canAct && !reviewPassed ? (
        <p className="text-xs text-muted-foreground" role="note">
          Approval isn&apos;t available: this version&apos;s content review doesn&apos;t allow it.
        </p>
      ) : null}
      {offers.length ? (
        <div className="flex flex-wrap gap-2">
          {offers.map((decision) => (
            <Button
              key={decision}
              size="sm"
              variant={decision === 'approved' ? 'default' : 'outline'}
              onClick={() => setPending(decision)}
            >
              {DECISION_ACTION[decision]}
            </Button>
          ))}
        </div>
      ) : null}
      {pending ? (
        <DraftDecisionDialog
          key={`${draft.id}-${pending}`}
          target={{ workspaceId, draftId: draft.id, draftVersion: draft.draft_version }}
          decision={pending}
          onClose={() => setPending(null)}
        />
      ) : null}
    </section>
  );
}
