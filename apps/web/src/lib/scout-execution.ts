import type { ScoutRequestOut } from '@/api/types';

/**
 * A Scout's derived execution state (D-25 blocked-Scout recovery, PROPOSED): computed by the
 * API on each single-Scout read (`execution`; `null` on the list, where it is not computed).
 * The view only presents it: it never decides whether the Scout can run (the API does).
 */
export type ScoutExecution = NonNullable<ScoutRequestOut['execution']>;

/** The view keeps re-reading while the API reports work still in progress, whatever the
 * request status (an earlier operation may still be finishing after a Cancel or Resume). */
export function isExecutionInProgress(
  request: Pick<ScoutRequestOut, 'execution'> | null | undefined,
): boolean {
  return request?.execution?.state === 'in_progress';
}

export interface ExecutionNotice {
  tone: 'attention' | 'info';
  title: string;
  body: string;
}

/** The customer-facing copy for a state that needs one; `null` for idle and in-progress runs
 * (the existing status refresh covers those). */
export function executionNotice(
  execution: ScoutExecution | null | undefined,
  status: string,
): ExecutionNotice | null {
  if (!execution) return null;
  switch (execution.state) {
    case 'needs_attention':
      return {
        tone: 'attention',
        title: 'This scout needs attention',
        body:
          'An earlier operation did not report an outcome. SignalNest must confirm it has ended ' +
          'before this scout can run again. Resume and Run now will not restore service until ' +
          'then: resuming does not clear the unresolved operation, only SignalNest support can. ' +
          'Previous findings and run history stay available. If this persists, contact support.',
      };
    case 'paused':
      return { tone: 'info', title: 'Paused', body: 'This scout is paused. Resume it to run again.' };
    case 'stopped':
      switch (execution.reason) {
        case 'run_cancelled':
          return { tone: 'info', title: 'The last run was cancelled', body: 'Run now to start a new run.' };
        case 'admission_unresolved':
          // W21 FU-Q1-2 follow-up (PROPOSED, pending the copy owner's sign-off): neutral wording.
          // The run may have been refused at once or after a wait, at its first external operation
          // or a later one; `execution` does not carry the job's recorded wait, so this says
          // neither "could not start" nor "timed out".
          return {
            tone: 'info',
            title: 'The last run could not continue',
            body:
              'The last run could not continue because an earlier operation had no confirmed ' +
              'outcome. That operation has since been confirmed as ended, so this scout can run again.',
          };
        case 'run_interrupted':
          return {
            tone: 'attention',
            title: "This scout's last run was interrupted",
            body: 'It cannot be started from here. Contact support.',
          };
        default:
          return { tone: 'info', title: 'The last run did not complete', body: 'Run now to try again.' };
      }
    case 'in_progress':
      // A settled status with an operation still finishing (after Cancel or Resume).
      if (execution.reason === 'operation_finishing' && status !== 'queued' && status !== 'running') {
        return {
          tone: 'info',
          title: 'An earlier operation is still finishing',
          // W16 §C (PROPOSED; FU-267-R2): Run now is accepted now; the run waits for it.
          body: 'You can run this scout now; the run waits until the earlier operation ends.',
        };
      }
      return null;
    default:
      return null;
  }
}
