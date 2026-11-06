import type { ApprovalDecision, ApprovalReason } from '@/api/types';
import type { Intent } from '@/lib/labels';

/**
 * The closed approval-reason vocabulary and its polarity, mirroring
 * `apps/api/app/approvals/reasons.py` (`APPROVAL_REASONS`, `REJECTION_REASONS`,
 * `REVOCATION_REASONS`). Each code may accompany exactly one decision; the dialog offers
 * only the codes of the decision being made, so a polarity mismatch (a 422 from the API)
 * cannot be built here. A reason is optional.
 *
 * `satisfies Record<ApprovalReason, …>` makes a code added to or removed from the
 * generated contract a compile error here, and the polarity of each code is pinned in
 * this module's tests against the server's partition.
 */
const REASONS = {
  evidence_sufficient: {
    decision: 'approved',
    label: 'Evidence is sufficient',
  },
  aligned_with_strategy: {
    decision: 'approved',
    label: 'Aligned with strategy',
  },
  approved_other: { decision: 'approved', label: 'Other' },
  insufficient_evidence: {
    decision: 'rejected',
    label: 'Insufficient evidence',
  },
  off_strategy: { decision: 'rejected', label: 'Off strategy' },
  brand_or_compliance_risk: {
    decision: 'rejected',
    label: 'Brand or compliance risk',
  },
  inaccurate: { decision: 'rejected', label: 'Inaccurate' },
  rejected_other: { decision: 'rejected', label: 'Other' },
  superseded: { decision: 'revoked', label: 'Superseded' },
  circumstances_changed: {
    decision: 'revoked',
    label: 'Circumstances changed',
  },
  approved_in_error: { decision: 'revoked', label: 'Approved in error' },
  revoked_other: { decision: 'revoked', label: 'Other' },
} as const satisfies Record<ApprovalReason, { decision: ApprovalDecision; label: string }>;

export interface ApprovalReasonOption {
  code: ApprovalReason;
  label: string;
}

/** The reason codes that may accompany `decision`, in vocabulary order. */
export function reasonsForDecision(decision: ApprovalDecision): ApprovalReasonOption[] {
  return (Object.keys(REASONS) as ApprovalReason[])
    .filter((code) => REASONS[code].decision === decision)
    .map((code) => ({ code, label: REASONS[code].label }));
}

export function reasonLabel(code: ApprovalReason): string {
  return REASONS[code]?.label ?? code;
}

export const DECISION_LABEL: Readonly<Record<ApprovalDecision, string>> = {
  approved: 'Approved',
  rejected: 'Rejected',
  revoked: 'Approval revoked',
};

export const DECISION_INTENT: Readonly<Record<ApprovalDecision, Intent>> = {
  approved: 'success',
  rejected: 'danger',
  revoked: 'warning',
};

/** What the dialog calls the action of recording `decision`. */
export const DECISION_ACTION: Readonly<Record<ApprovalDecision, string>> = {
  approved: 'Approve',
  rejected: 'Reject',
  revoked: 'Revoke approval',
};

/** The bound of the free-text note (`APPROVAL_NOTE_MAX_LENGTH` in approvals/models.py). */
export const NOTE_MAX_LENGTH = 1000;

/**
 * The display name inside an actor snapshot, `"<user id> <full name> <<email>>"`
 * (`apps/api/app/core/actor_snapshot.py`), or a neutral label for an erased account.
 * The id and the address are never shown.
 */
export function snapshotName(snapshot: string): string {
  if (snapshot === '[erased]') return 'Erased account';
  const withoutId = snapshot.slice(snapshot.indexOf(' ') + 1);
  const open = withoutId.lastIndexOf(' <');
  const name = (open >= 0 ? withoutId.slice(0, open) : withoutId).trim();
  return name || 'Unknown member';
}

/** The account id that leads an actor snapshot (`null` for an erased one). */
export function snapshotUserId(snapshot: string): string | null {
  if (snapshot === '[erased]') return null;
  const id = snapshot.split(' ', 1)[0];
  return id || null;
}

/**
 * Whether `userId` wrote the brief — by the live `created_by` or, once that foreign key
 * is nulled, by the id leading the snapshot. Mirrors `is_brief_author` in
 * `apps/api/app/approvals/service.py`, which refuses such a decision with 403.
 */
export function isBriefAuthor(
  brief: { created_by: string | null; created_by_snapshot: string },
  userId: string | null | undefined,
): boolean {
  if (!userId) return false;
  return brief.created_by === userId || snapshotUserId(brief.created_by_snapshot) === userId;
}
