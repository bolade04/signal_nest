import { MessageSquarePlus, ThumbsDown, ThumbsUp } from 'lucide-react';
import { useState } from 'react';
import { ApiError } from '@/api/client';
import type { FeedbackCreate, FeedbackOut, FeedbackReason } from '@/api/types';
import { useAuth } from '@/auth/AuthContext';
import { ErrorState } from '@/components/common/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import { formatRelative } from '@/lib/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { reasonLabel, reasonsForVerdict } from './feedbackReasons';
import {
  isFeatureDark,
  useFeedbackCapability,
  useFeedbackHistory,
  useSubmitFeedback,
} from './useFeedback';

// Mirrors the server-side EDITORS gate (owner / admin / marketer). View-only
// members never see the control — the API would 403 them anyway; this only
// hides an affordance they could not use.
const EDITOR_ROLES = new Set(['owner', 'admin', 'marketer']);

/**
 * Feature-gated, role-aware opportunity-feedback control, bound to one immutable
 * intelligence record. Rendered inside the intelligence panel and keyed by the
 * record id so it fully remounts (resetting any pending verdict) when the record
 * changes — the strongest form of stale-context protection.
 */
export function OpportunityFeedbackPanel({
  workspaceId,
  opportunityId,
  intelligenceRecordId,
}: {
  workspaceId: string;
  opportunityId: string;
  intelligenceRecordId: string;
}) {
  const { memberships } = useAuth();
  const { organizationId } = useWorkspace();
  const role = memberships.find((m) => m.organization_id === organizationId)?.role;
  const canGiveFeedback = role ? EDITOR_ROLES.has(role) : false;

  // Role gate first: a view-only member gets no feedback UI at all. Returning
  // before the data hooks is safe because the hooks live in the inner component.
  if (!canGiveFeedback) return null;

  return (
    <FeedbackInner
      workspaceId={workspaceId}
      opportunityId={opportunityId}
      intelligenceRecordId={intelligenceRecordId}
    />
  );
}

function FeedbackInner({
  workspaceId,
  opportunityId,
  intelligenceRecordId,
}: {
  workspaceId: string;
  opportunityId: string;
  intelligenceRecordId: string;
}) {
  // Authoritative pre-request gate: consult the runtime-capability reflection
  // first. While feedback is dark this is ``false``, so the history query below
  // is disabled and *no feedback request is ever issued* — the panel renders
  // nothing without probing a 503.
  const capability = useFeedbackCapability();
  const history = useFeedbackHistory({
    workspaceId,
    opportunityId,
    intelligenceRecordId,
    enabled: capability.isEnabled,
  });
  const submit = useSubmitFeedback({ workspaceId, opportunityId, intelligenceRecordId });

  const [pendingVerdict, setPendingVerdict] = useState<boolean | null>(null);
  const [selectedReason, setSelectedReason] = useState<FeedbackReason | null>(null);

  // Stale-context protection is provided by the parent, which mounts this panel
  // with ``key={intelligence_record_id}`` — a change of record fully remounts
  // this component, discarding any pending verdict. Combined with the
  // record-scoped query key and the scope captured in the submit mutation, a
  // verdict can never be applied to a record other than the one on screen.

  // Capability dark (or still resolving) → render nothing and issue no feedback
  // request at all. This is the primary gate; the server 503 below is only a
  // defence-in-depth fallback for a stale client whose cached capability is
  // ahead of a mid-session rollback.
  if (!capability.isEnabled) return null;

  // Feature-dark (503) or unauthorized (403) → render nothing. No partial UI is
  // ever shown for a capability the user cannot use.
  if (history.isError) {
    const err = history.error;
    if (isFeatureDark(err) || (err instanceof ApiError && err.status === 403)) return null;
    return (
      <>
        <Separator />
        <section aria-labelledby="feedback-heading" className="space-y-2">
          <FeedbackHeading />
          <ErrorState error={history.error} onRetry={() => history.refetch()} />
        </section>
      </>
    );
  }

  const openDialog = pendingVerdict !== null;
  const reasons = pendingVerdict === null ? [] : reasonsForVerdict(pendingVerdict);
  const items = history.data?.items ?? [];

  function startVerdict(isUseful: boolean) {
    setSelectedReason(null);
    setPendingVerdict(isUseful);
  }

  function closeDialog() {
    setPendingVerdict(null);
    setSelectedReason(null);
  }

  function submitFeedback() {
    if (pendingVerdict === null) return;
    const body: FeedbackCreate = {
      intelligence_record_id: intelligenceRecordId,
      is_useful: pendingVerdict,
      ...(selectedReason ? { reason_code: selectedReason } : {}),
    };
    submit.mutate(body, { onSuccess: () => closeDialog() });
  }

  return (
    <>
      <Separator />
      <section aria-labelledby="feedback-heading" className="space-y-3">
        <FeedbackHeading />
        {history.isLoading ? (
          <div aria-busy="true" aria-live="polite">
            <span className="sr-only">Loading feedback…</span>
            <Skeleton className="h-9 w-56" />
          </div>
        ) : (
          <>
            <p className="text-xs text-muted-foreground">
              Was this analysis useful? Each response is recorded as a new entry and never
              overwrites a previous one.
            </p>
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={() => startVerdict(true)}
                disabled={submit.isPending}
              >
                <ThumbsUp className="size-4" /> Useful
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => startVerdict(false)}
                disabled={submit.isPending}
              >
                <ThumbsDown className="size-4" /> Not useful
              </Button>
            </div>
            <FeedbackHistoryList items={items} />
          </>
        )}
      </section>

      <Dialog open={openDialog} onOpenChange={(open) => (open ? undefined : closeDialog())}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>
              {pendingVerdict ? 'Mark this analysis useful' : 'Mark this analysis not useful'}
            </DialogTitle>
            <DialogDescription>
              Optionally add a reason. There is no free-text field — pick the closest match, or
              submit without one.
            </DialogDescription>
          </DialogHeader>

          <div role="group" aria-label="Feedback reason" className="flex flex-wrap gap-2">
            {reasons.map((r) => {
              const active = selectedReason === r.code;
              return (
                <Button
                  key={r.code}
                  type="button"
                  size="sm"
                  variant={active ? 'default' : 'outline'}
                  aria-pressed={active}
                  onClick={() => setSelectedReason(active ? null : r.code)}
                  disabled={submit.isPending}
                >
                  {r.label}
                </Button>
              );
            })}
          </div>

          <DialogFooter>
            <Button variant="ghost" onClick={closeDialog} disabled={submit.isPending}>
              Cancel
            </Button>
            <Button onClick={submitFeedback} disabled={submit.isPending}>
              {submit.isPending ? 'Recording…' : 'Submit feedback'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function FeedbackHeading() {
  return (
    <h3 id="feedback-heading" className="flex items-center gap-1.5 text-sm font-semibold">
      <MessageSquarePlus className="size-4 text-primary" /> Feedback
    </h3>
  );
}

function FeedbackHistoryList({ items }: { items: FeedbackOut[] }) {
  if (!items.length) {
    return <p className="text-xs text-muted-foreground">No feedback recorded yet.</p>;
  }
  return (
    <ul aria-label="Feedback history" className="space-y-1.5">
      {items.map((item) => (
        <li key={item.id} className="flex flex-wrap items-center gap-2 text-xs">
          <Badge intent={item.is_useful ? 'success' : 'warning'}>
            {item.is_useful ? 'Useful' : 'Not useful'}
          </Badge>
          {item.reason_code ? (
            <span className="text-foreground">{reasonLabel(item.reason_code)}</span>
          ) : null}
          <span className="text-muted-foreground">· {formatRelative(item.created_at)}</span>
        </li>
      ))}
    </ul>
  );
}
