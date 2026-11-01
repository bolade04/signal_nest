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
import { DECISION_ACTION, NOTE_MAX_LENGTH, reasonsForDecision } from './approvalVocabulary';
import { useRecordDecision } from './useApprovals';

export interface DecisionTarget {
  workspaceId: string;
  opportunityId: string;
  briefId: string;
  /** The exact version the decider is looking at; the server refuses any other. */
  briefVersion: number;
}

const DESCRIPTION: Readonly<Record<ApprovalDecision, string>> = {
  approved:
    'Your approval is recorded against this exact version of the brief. A later version needs its own decision.',
  rejected:
    'Your rejection is recorded against this exact version of the brief. The author can write a new version.',
  revoked:
    'Revoking withdraws the current approval of this version. The earlier decisions stay on record.',
};

/**
 * Record one decision on one exact brief version. Mounted only while open (keyed by
 * the caller), so a pending reason or note never carries over to another brief.
 */
export function DecisionDialog({
  target,
  decision,
  onClose,
}: {
  target: DecisionTarget;
  decision: ApprovalDecision;
  onClose: () => void;
}) {
  const record = useRecordDecision(target);
  const [reason, setReason] = useState<ApprovalReason | null>(null);
  const [note, setNote] = useState('');
  const reasons = reasonsForDecision(decision);
  const action = DECISION_ACTION[decision];
  const noteId = `decision-note-${target.briefId}`;

  function submit() {
    const trimmed = note.trim();
    record.mutate(
      {
        decision,
        subject_version: target.briefVersion,
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
            {action} version {target.briefVersion}
          </DialogTitle>
          <DialogDescription>{DESCRIPTION[decision]}</DialogDescription>
        </DialogHeader>

        <div className="space-y-2">
          <p className="text-sm font-medium" id={`decision-reason-${target.briefId}`}>
            Reason (optional)
          </p>
          <div
            role="group"
            aria-labelledby={`decision-reason-${target.briefId}`}
            className="flex flex-wrap gap-2"
          >
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
          <Label htmlFor={noteId}>Note (optional)</Label>
          <Textarea
            id={noteId}
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
