import { useState } from 'react';
import type { ApprovalDecision, ApprovalReason } from '@/api/types';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  DECISION_ACTION,
  NOTE_MAX_LENGTH,
  reasonsForDecision,
} from '@/pages/approvals/approvalVocabulary';
import { useRecordDraftDecision } from './useCreative';

export interface DraftDecisionTarget {
  workspaceId: string;
  draftId: string;
  /** The exact version the reviewer is looking at; the server refuses any other. */
  draftVersion: number;
}

const DESCRIPTION: Readonly<Record<ApprovalDecision, string>> = {
  approved:
    'Your approval is recorded against this exact version of the draft. A revised version needs its own decision.',
  rejected:
    'Your rejection is recorded against this exact version of the draft. An editor can generate a revised version.',
  revoked:
    'Revoking withdraws the current approval of this version. The earlier decisions stay on record.',
};

/**
 * Record one decision on one exact draft version (I-5C). The 5B brief dialog's form (the same
 * closed reason vocabulary, polarity-filtered, and the same bounded note) bound to the draft
 * decision route. Mounted only while open (keyed by the caller), so a pending reason or note
 * never carries over to another draft.
 */
export function DraftDecisionDialog({
  target,
  decision,
  onClose,
}: {
  target: DraftDecisionTarget;
  decision: ApprovalDecision;
  onClose: () => void;
}) {
  const record = useRecordDraftDecision(target.workspaceId, target.draftId);
  const [reason, setReason] = useState<ApprovalReason | null>(null);
  const [note, setNote] = useState('');
  const reasons = reasonsForDecision(decision);
  const action = DECISION_ACTION[decision];
  const idPrefix = `draft-decision-${target.draftId}`;

  function submit() {
    const trimmed = note.trim();
    record.mutate(
      {
        decision,
        subject_version: target.draftVersion,
        ...(reason ? { reason_code: reason } : {}),
        ...(trimmed ? { note: trimmed } : {}),
      },
      { onSuccess: onClose },
    );
  }

  return (
    <Dialog open onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>
            {action} draft version {target.draftVersion}
          </DialogTitle>
          <DialogDescription>{DESCRIPTION[decision]}</DialogDescription>
        </DialogHeader>

        <div className="space-y-2">
          <p className="text-sm font-medium" id={`${idPrefix}-reason`}>
            Reason (optional)
          </p>
          <div role="group" aria-labelledby={`${idPrefix}-reason`} className="flex flex-wrap gap-2">
            {reasons.map((r) => {
              const active = reason === r.code;
              return (
                <Button
                  key={r.code}
                  type="button"
                  size="sm"
                  variant={active ? 'default' : 'outline'}
                  aria-pressed={active}
                  onClick={() => setReason(active ? null : r.code)}
                  disabled={record.isPending}
                >
                  {r.label}
                </Button>
              );
            })}
          </div>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor={`${idPrefix}-note`}>Note (optional)</Label>
          <Textarea
            id={`${idPrefix}-note`}
            value={note}
            maxLength={NOTE_MAX_LENGTH}
            onChange={(e) => setNote(e.target.value)}
            disabled={record.isPending}
            rows={3}
          />
          <p className="text-xs text-muted-foreground">
            {note.length}/{NOTE_MAX_LENGTH} characters
          </p>
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={record.isPending}>
            Cancel
          </Button>
          <Button
            onClick={submit}
            disabled={record.isPending}
            variant={decision === 'approved' ? 'default' : 'destructive'}
          >
            {record.isPending ? 'Recording…' : action}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
