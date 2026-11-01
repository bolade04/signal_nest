import type { components } from '@/api/schema';
import type { JobOut, ScoutRequestOut } from '@/api/types';
import type { Intent } from '@/lib/labels';

/**
 * A Scout run's label in the jobs panel (W16 customer repairs §D, PROPOSED; a proposed Q1
 * presentation, not a Q1 ruling). The API derives `outcome` / `outcome_reason` read-only from
 * the job's status, its result's outcome and its error code (apps/api/app/jobs/outcomes.py);
 * this only words them. A run in flight (no outcome) keeps the job-status wording, and the raw
 * codes stay available as secondary detail.
 */
export type RunOutcome = NonNullable<JobOut['outcome']>;
export type RunOutcomeReason = NonNullable<JobOut['outcome_reason']>;

export const runOutcomeLabels: Record<RunOutcome, string> = {
  completed: 'Completed',
  stopped_paused: 'Stopped — Scout paused',
  skipped_paused: 'Skipped — Scout paused',
  skipped: 'Skipped',
  stopped: 'Stopped',
  cancelled: 'Cancelled',
  failed: 'Failed',
};

export const runOutcomeIntent: Record<RunOutcome, Intent> = {
  completed: 'success',
  stopped_paused: 'warning',
  skipped_paused: 'warning',
  skipped: 'muted',
  stopped: 'warning',
  cancelled: 'muted',
  failed: 'danger',
};

export const runOutcomeReasons: Record<RunOutcomeReason, string> = {
  paused_during_run: 'The scout was paused during the run; its findings were kept.',
  another_run_active: 'Another run of this scout was in progress.',
  another_run_took_over: 'Another run of this scout took over.',
  worker_lost: 'The worker lost this run.',
  scout_changed: 'The scout changed during the run.',
  scout_removed: 'The scout was removed.',
  // W21 FU-Q1-2 (PROPOSED, pending the copy owner's sign-off): "timed out" only where the job
  // recorded that this run waited until the earlier operation's deadline; an immediate refusal,
  // where nothing waited, reads `earlier_operation_unresolved`, which says nothing about a wait.
  earlier_operation_timed_out: 'Waiting for an earlier operation timed out.',
  earlier_operation_unresolved: 'An earlier operation had no confirmed outcome, so this run could not go on.',
  too_many_signals: 'The sources returned more items than one run can analyse.',
  retries_exhausted: 'It kept failing after every attempt.',
  worker_stopped_responding: 'The worker stopped responding.',
  internal: 'An internal error stopped it.',
};

/** The raw codes behind a label, for the secondary line ("succeeded · skipped_request_paused"). */
export function rawRunCodes(job: Pick<JobOut, 'status' | 'result_summary' | 'last_error_code'>): string {
  const outcome = job.result_summary?.['outcome'];
  return [job.status, typeof outcome === 'string' ? outcome : null, job.last_error_code]
    .filter(Boolean)
    .join(' · ');
}

// ---------------------------------------------------------------------------------------------
// The latest run's result: what ran, what did not, one next step (W21 FU-Q1-1, Q1 (a+)).
//
// PROPOSED WORDING, PENDING THE COPY OWNER'S SIGN-OFF. No copy owner is named
// (DECISION-PACK-closeout-02 Addendum v8 §C). Every customer-facing string of the run-result
// panel (pages/scouts/RunResultPanel.tsx) is in this module, with the run labels above, so a
// wording ruling is a one-file change. The facts come typed from `ScoutRequestOut.run_outcome`
// (apps/api/app/scouting_requests/schemas.py); the counts from `stats` through the `RunStats`
// keys (lib/scout-stats.ts). Each next step names something a customer can do in the product
// today: Edit (source types, keywords) and Run now.
// ---------------------------------------------------------------------------------------------
export type RunResult = NonNullable<ScoutRequestOut['run_outcome']>;
export type RunResultKind = RunResult['kind'];
export type RunNextAction = NonNullable<RunResult['next_action']>;
export type RunSource = components['schemas']['ScoutRunSource'];
export type SourceFailureReason = components['schemas']['SourceFailureReason'];

const items = (n: number) => `${n} ${n === 1 ? 'item' : 'items'}`;
const sources = (n: number) => `${n} ${n === 1 ? 'source' : 'sources'}`;
/** A formatted count (`—` when not reported) with its noun, singular only for exactly one. */
const count = (n: string, one: string, many: string) => `${n} ${n === '1' ? one : many}`;

export const runResultCopy = {
  heading: 'Latest run result',
  whatRan: 'What ran',
  whatDidNot: 'What did not run',
  nextStep: 'Next step',
  /** The counts of a run that completed (from the `RunStats` keys; `—` when not reported). */
  counts: (collected: string, analysed: string, opportunities: string) =>
    `${count(collected, 'item', 'items')} collected, ${count(analysed, 'signal', 'signals')} analysed, ` +
    `${count(opportunities, 'opportunity', 'opportunities')}.`,
  /** A refused run collected items but analysed none. */
  refusedCounts: (collected: string) => `${count(collected, 'item', 'items')} collected; none were analysed.`,
  sourcesRead: (read: number, consulted: number) =>
    read === consulted ? `Read ${sources(consulted)}.` : `Read ${read} of ${sources(consulted)}.`,
  noSource: 'No source was read.',
  sourceRead: (label: string, fetched: number) => `${label}: ${items(fetched)} read.`,
  refusal: (bound: number | null) =>
    bound === null
      ? 'The run was refused: its sources returned more items than one run can analyse.'
      : `The run was refused: its sources returned more items than one run can analyse (at most ${bound}).`,
  clipped: (label: string, limit: number | null) =>
    limit === null
      ? `${label}: held more items than it reads in one run; the rest were not read.`
      : `${label}: held more than its limit of ${items(limit)}; the rest were not read.`,
  failed: (label: string, reason: SourceFailureReason | null) =>
    `${label}: could not be read (${reason ? sourceFailureReasons[reason] : 'reason not recorded'}).`,
};

export const runResultLabels: Record<RunResultKind, string> = {
  complete: 'Complete',
  partial: 'Partial',
  refused: 'Refused',
};

export const runResultIntent: Record<RunResultKind, Intent> = {
  complete: 'success',
  partial: 'warning',
  refused: 'danger',
};

export const runResultSummaries: Record<RunResultKind, string> = {
  complete: 'Every source was read in full.',
  partial: 'The run finished, but not every source was read in full.',
  refused: 'The run was refused before anything was analysed.',
};

export const runNextActions: Record<RunNextAction, string> = {
  narrow_request: 'Narrow this scout: edit it to use fewer source types or keywords, then run it again.',
  run_again_later: 'Run this scout again later. A source that failed may be available then.',
  no_action: "No action needed: the results come from the items read within each source's limit.",
};

export const sourceFailureReasons: Record<SourceFailureReason, string> = {
  feed_too_large: 'the feed was too large',
  xml_unsafe: 'the feed was refused for safety',
  xml_malformed: 'the feed was not readable',
  rate_limited: 'the source limited how often it can be read',
  retries_exhausted: 'the source did not answer after several tries',
  response_refused: 'the source did not send a feed',
  egress_blocked: "the source's address is not allowed",
  fetch_error: 'the source returned an error',
};

/** The names of the simulated sources this build reads; any other source shows its key. */
const sourceNames = new Map<string, string>([
  ['fixture', 'Sample library'],
  ['rss_news', 'Sample news feed'],
]);

export function sourceLabel(key: string): string {
  return sourceNames.get(key) ?? key;
}
