import type { FeedbackReason } from '@/api/types';

// The closed feedback-reason vocabulary, mirroring the backend ``FeedbackReason``
// enum and its polarity sets (``POSITIVE_FEEDBACK_REASONS`` /
// ``NEGATIVE_FEEDBACK_REASONS``). A reason is always *optional* and there is no
// free-text alternative by design. Each code has a fixed polarity: positive codes
// may only accompany a useful verdict, negative codes only a not-useful verdict.
// The UI filters to the matching polarity so a mismatch (which the API rejects
// with a 422) can never be constructed.
export interface FeedbackReasonOption {
  code: FeedbackReason;
  label: string;
}

// Positive — only valid alongside is_useful = true.
export const POSITIVE_REASONS: readonly FeedbackReasonOption[] = [
  { code: 'useful_insight', label: 'Useful insight' },
  { code: 'strong_evidence', label: 'Strong evidence' },
  { code: 'commercially_relevant', label: 'Commercially relevant' },
  { code: 'correct_market', label: 'Correct market' },
];

// Negative — only valid alongside is_useful = false. ``other`` lets a customer
// flag an unmodelled problem without opening free text.
export const NEGATIVE_REASONS: readonly FeedbackReasonOption[] = [
  { code: 'irrelevant', label: 'Irrelevant' },
  { code: 'wrong_market', label: 'Wrong market' },
  { code: 'weak_evidence', label: 'Weak evidence' },
  { code: 'duplicate', label: 'Duplicate' },
  { code: 'outdated', label: 'Outdated' },
  { code: 'not_commercially_useful', label: 'Not commercially useful' },
  { code: 'other', label: 'Other' },
];

const LABELS: Record<FeedbackReason, string> = Object.fromEntries(
  [...POSITIVE_REASONS, ...NEGATIVE_REASONS].map((r) => [r.code, r.label]),
) as Record<FeedbackReason, string>;

export function reasonLabel(code: FeedbackReason): string {
  return LABELS[code] ?? code;
}

/** The polarity-correct reason options for a given verdict. */
export function reasonsForVerdict(isUseful: boolean): readonly FeedbackReasonOption[] {
  return isUseful ? POSITIVE_REASONS : NEGATIVE_REASONS;
}
