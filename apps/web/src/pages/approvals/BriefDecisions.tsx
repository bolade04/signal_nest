import { useState } from 'react';
import { ApiError } from '@/api/client';
import type { ApprovalOut } from '@/api/types';
import { useAuth } from '@/auth/AuthContext';
import { ErrorState } from '@/components/common/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { canDecideApprovals, canReadApprovalHistory, useActorRole } from '@/lib/roles';
import { formatRelative } from '@/lib/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import {
  DECISION_INTENT,
  DECISION_LABEL,
  isBriefAuthor,
  reasonLabel,
  snapshotName,
} from './approvalVocabulary';
import { DecisionDialog, type DecisionTarget } from './DecisionDialog';
import { isApprovalsDark, useBriefDecisions } from './useApprovals';

/**
 * One brief version's decision history, newest first, with the controlling decision
 * marked. Built to drop into the 5A brief panel at integration as well as the queue.
 *
 * Shown only to the roles the history route admits (every role but viewer). A reviewer
 * who did not write the brief may revoke a controlling approval here; nobody else is
 * offered it. Renders nothing while approvals are dark or the role cannot read history.
 */
export function BriefDecisions({
  target,
  brief,
}: {
  target: DecisionTarget;
  brief: { created_by: string | null; created_by_snapshot: string };
}) {
  const { organizationId } = useWorkspace();
  const actor = useActorRole(organizationId);
  if (!canReadApprovalHistory(actor.role)) return null;
  return (
    <DecisionsInner target={target} brief={brief} mayDecide={canDecideApprovals(actor.role)} />
  );
}

function DecisionsInner({
  target,
  brief,
  mayDecide,
}: {
  target: DecisionTarget;
  brief: { created_by: string | null; created_by_snapshot: string };
  mayDecide: boolean;
}) {
  const { user } = useAuth();
  const history = useBriefDecisions(target);
  const [revoking, setRevoking] = useState(false);

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
    if (isApprovalsDark(err) || (err instanceof ApiError && err.status === 403)) return null;
    return <ErrorState error={err} onRetry={() => history.refetch()} />;
  }

  const items = history.data.items;
  const controlling: ApprovalOut | undefined = items[0];
  const canRevoke =
    mayDecide && controlling?.decision === 'approved' && !isBriefAuthor(brief, user?.id);

  return (
    <section aria-label={`Decisions on version ${target.briefVersion}`} className="space-y-2">
      {items.length === 0 ? (
        <p className="text-xs text-muted-foreground">No decision recorded for this version yet.</p>
      ) : (
        <ol aria-label="Decision history" className="space-y-1.5">
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
      {canRevoke ? (
        <Button size="sm" variant="outline" onClick={() => setRevoking(true)}>
          Revoke approval
        </Button>
      ) : null}
      {revoking ? (
        <DecisionDialog target={target} decision="revoked" onClose={() => setRevoking(false)} />
      ) : null}
    </section>
  );
}
