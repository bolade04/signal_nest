import type { ScoutRequestOut } from '@/api/types';
import { isScoutInFlight } from '@/lib/scout-status';

/**
 * Whether Run now and Edit are available for a Scout, and the visible reason when not
 * (W16 customer repairs §C/§B, PROPOSED).
 *
 * These mirror the API's refusals exactly; the API stays the authority and a refusal after a
 * race is still shown. Run now (`POST .../run`, apps/api/app/scouting_requests/routes.py) is
 * refused when the request is queued or running, paused, or needs attention. Edit
 * (`PUT .../{id}`) is refused while the request is queued or running, or a run it has not been
 * paused out of is live (`execution` `in_progress` · `run_active`).
 */
export type ScoutForActions = Pick<ScoutRequestOut, 'status' | 'execution'>;

export interface Availability {
  available: boolean;
  /** What the button reads while unavailable (otherwise its normal label). */
  label: string | null;
  /** The visible reason, or an informational note while available; null when none applies. */
  reason: string | null;
}

export const RUN_REASONS = {
  needsAttention:
    'Needs attention: SignalNest must confirm an earlier operation has ended before this scout can run.',
  paused: 'Paused: resume the scout to run it.',
  finishing: 'An earlier operation is still finishing; a run started now waits for it.',
} as const;

export function runNowAvailability(request: ScoutForActions, postPending = false): Availability {
  if (postPending) return { available: false, label: 'Starting…', reason: null };
  if (request.status === 'running') return { available: false, label: 'Running…', reason: null };
  if (isScoutInFlight(request.status)) return { available: false, label: 'Queued…', reason: null };
  if (request.execution?.state === 'needs_attention') {
    return { available: false, label: null, reason: RUN_REASONS.needsAttention };
  }
  if (request.status === 'paused') return { available: false, label: null, reason: RUN_REASONS.paused };
  if (request.execution?.state === 'in_progress' && request.execution.reason === 'operation_finishing') {
    return { available: true, label: null, reason: RUN_REASONS.finishing };
  }
  return { available: true, label: null, reason: null };
}

export const EDIT_IN_PROGRESS =
  'This scout is queued or running. Wait for the run to finish, or cancel it, then edit.';

export function editAvailability(request: ScoutForActions): Availability {
  const live =
    isScoutInFlight(request.status) ||
    (request.execution?.state === 'in_progress' && request.execution.reason === 'run_active');
  return live
    ? { available: false, label: null, reason: EDIT_IN_PROGRESS }
    : { available: true, label: null, reason: null };
}
