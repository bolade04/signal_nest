import type {
  AnalyticsApprovalCounts,
  AnalyticsExportCounts,
  AnalyticsOpportunityCounts,
  AnalyticsReviewCounts,
  AnalyticsSubjectCounts,
  WorkspaceAnalytics,
} from '@/api/types';

/**
 * Display rules for status analytics (Project Phase 5E, lane 5E-W2).
 *
 * The API reports a figure it cannot compute — a rate without a denominator, a latency
 * without a decided version — as `null`, never as 0 (the `RunStats` discipline). The page
 * keeps that distinction visible: `null` renders as an em dash with the words below, never
 * as "0", "0%" or "0 s". A count is never null.
 */
export const NOT_ENOUGH_DATA = 'Not enough data yet';
export const NULL_FIGURE = '—';

const counts = new Intl.NumberFormat('en-US');

/** A count (never null in the contract). */
export function formatCount(value: number): string {
  return counts.format(value);
}

/** A share in [0, 1] as a whole percentage; `null` → the dash. */
export function formatRate(value: number | null): string {
  if (value === null) return NULL_FIGURE;
  return `${Math.round(value * 100)}%`;
}

/** A duration in seconds, in the two largest units; `null` → the dash. */
export function formatDuration(seconds: number | null): string {
  if (seconds === null) return NULL_FIGURE;
  const sign = seconds < 0 ? '-' : '';
  const total = Math.round(Math.abs(seconds));
  if (total < 60) return `${sign}${total} s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${sign}${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  if (hours < 24) return `${sign}${hours} h${restMinutes ? ` ${restMinutes} min` : ''}`;
  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  return `${sign}${days} d${restHours ? ` ${restHours} h` : ''}`;
}

/** One labelled row of a family's breakdown. */
export interface StateRow {
  key: string;
  label: string;
  value: number;
}

/** One entity family: its title, total and state breakdown (schema order). */
export interface FamilySummary {
  key: 'opportunities' | 'briefs' | 'approvals' | 'drafts' | 'reviews' | 'exports';
  title: string;
  total: number;
  rows: StateRow[];
}

const subjectRows = (c: AnalyticsSubjectCounts): StateRow[] => [
  { key: 'awaiting_decision', label: 'Awaiting a decision', value: c.awaiting_decision },
  { key: 'approved', label: 'Approved', value: c.approved },
  { key: 'rejected', label: 'Rejected', value: c.rejected },
  { key: 'superseded', label: 'Superseded by a newer version', value: c.superseded },
];

const opportunityRows = (c: AnalyticsOpportunityCounts): StateRow[] => [
  { key: 'new', label: 'New', value: c.new },
  { key: 'saved', label: 'Saved', value: c.saved },
  { key: 'monitoring', label: 'Monitoring', value: c.monitoring },
  { key: 'actioned', label: 'Actioned', value: c.actioned },
  { key: 'ignored', label: 'Ignored', value: c.ignored },
];

const approvalRows = (c: AnalyticsApprovalCounts): StateRow[] => [
  { key: 'approved', label: 'Approvals', value: c.approved },
  { key: 'rejected', label: 'Rejections', value: c.rejected },
  { key: 'revoked', label: 'Revocations', value: c.revoked },
];

const reviewRows = (c: AnalyticsReviewCounts): StateRow[] => [
  { key: 'passed', label: 'Passed', value: c.passed },
  { key: 'blocked', label: 'Blocked', value: c.blocked },
  { key: 'not_passed', label: 'Missing a required disclaimer', value: c.not_passed },
];

const exportRows = (c: AnalyticsExportCounts): StateRow[] => [
  { key: 'plain_text', label: 'Plain text', value: c.plain_text },
  { key: 'markdown', label: 'Markdown', value: c.markdown },
  { key: 'json_document', label: 'JSON', value: c.json_document },
];

/** The six families, in the order the plan names them. */
export function familySummaries(data: WorkspaceAnalytics): FamilySummary[] {
  return [
    {
      key: 'opportunities',
      title: 'Opportunities',
      total: data.opportunities.total,
      rows: opportunityRows(data.opportunities),
    },
    { key: 'briefs', title: 'Briefs', total: data.briefs.total, rows: subjectRows(data.briefs) },
    {
      key: 'approvals',
      title: 'Approval decisions',
      total: data.approvals.total,
      rows: approvalRows(data.approvals),
    },
    { key: 'drafts', title: 'Drafts', total: data.drafts.total, rows: subjectRows(data.drafts) },
    {
      key: 'reviews',
      title: 'Content reviews',
      total: data.reviews.total,
      rows: reviewRows(data.reviews),
    },
    { key: 'exports', title: 'Exports', total: data.exports.total, rows: exportRows(data.exports) },
  ];
}
