import type { JobOut } from '@/api/types';
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
  earlier_operation_timed_out: 'Waiting for an earlier operation timed out.',
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
