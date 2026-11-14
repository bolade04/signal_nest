import type { NotificationEventCode } from '@/api/types';

/**
 * Presentation of the closed notification vocabulary (Project Phase 5E, 5E-W3).
 *
 * A notification carries no text from the server: only its event code and subject id. Every
 * word the feed shows comes from these static maps, keyed by the generated contract type, so a
 * code added to or removed from the API is a compile error here. The I-5E integration owner
 * may move these labels into `lib/labels.ts` (freeze-owned).
 */

/** The message a notification shows in the feed. */
export const EVENT_MESSAGE: Readonly<Record<NotificationEventCode, string>> = {
  'brief.created': 'A new brief is waiting for review.',
  'approval.decided': 'A reviewer recorded a decision on your brief or draft.',
  'draft.generated': 'Your creative draft is ready.',
  'review.blocked': 'A draft you requested was blocked by its content review.',
  'export.completed': 'A draft you worked on was exported.',
};

/** The name of each event in the preferences view. */
export const EVENT_PREFERENCE_LABEL: Readonly<Record<NotificationEventCode, string>> = {
  'brief.created': 'New briefs to review',
  'approval.decided': 'Decisions on your briefs and drafts',
  'draft.generated': 'Your drafts are ready',
  'review.blocked': 'Your drafts are blocked by review',
  'export.completed': 'Exports of your drafts',
};

/**
 * The section a notification opens, or `null` when the feed cannot name a page for it. The
 * feed knows only the subject's id, so it links to the section that lists the subject, never
 * to the subject itself. An approval decision's subject may be a brief or a draft, so it opens
 * nothing.
 */
export const EVENT_SECTION: Readonly<Record<NotificationEventCode, string | null>> = {
  'brief.created': '/approvals',
  'approval.decided': null,
  'draft.generated': '/creative',
  'review.blocked': '/creative',
  'export.completed': '/creative',
};

/** Every event code, complete by construction (the keys of a total `Record`). */
export const EVENT_CODES: readonly NotificationEventCode[] = Object.freeze(
  Object.keys(EVENT_MESSAGE) as NotificationEventCode[],
);

/** The badge text for an unread count: nothing at zero, `9+` above nine. */
export function unreadBadge(unread: number): string | null {
  if (!Number.isFinite(unread) || unread <= 0) return null;
  return unread > 9 ? '9+' : String(Math.floor(unread));
}
