import type { ScoutRequestOut } from '@/api/types';
import { statValue } from '@/lib/scout-stats';

/**
 * Why the unfiltered opportunity feed is empty (P6-UI-004).
 *
 * The old empty state told everyone to "Run a scout request" — including the user who
 * just ran one and got nothing back, which is the guaranteed outcome for any market the
 * fixture source does not cover. This tells the cases apart from what the scout requests
 * already report: whether one exists, whether it has run, whether it is in flight or
 * failed, and the counts its last run stored (`stats`: `scanned`, `signals_analyzed`,
 * `opportunities`).
 *
 * SEAM — P6-DATA-2. The backend cannot yet say WHY a run found nothing (for example
 * "this market is not covered by any source"); that reason code is P6-DATA-2's, owned by
 * the real-data lane. Until a scout request carries one, the copy below states only what
 * the stored counts prove and never guesses a cause. When it lands, map the code in
 * `reasonFromBackend` and it will take precedence.
 */
export type EmptyFeed =
  | { kind: 'no-scouts' }
  | { kind: 'never-ran'; scout: ScoutRequestOut }
  | { kind: 'in-flight'; scout: ScoutRequestOut }
  | { kind: 'last-run-failed'; scout: ScoutRequestOut }
  | {
      kind: 'ran-found-nothing';
      scout: ScoutRequestOut;
      /** Signals the last run analysed, when the run recorded it. */
      signalsAnalyzed: number | null;
      /** A backend-supplied reason (P6-DATA-2); null until the backend sends one. */
      reason: string | null;
    };

const IN_FLIGHT = new Set(['queued', 'running']);

/** P6-DATA-2 seam: no scout request carries a no-result reason on `main` yet. */
function reasonFromBackend(_scout: ScoutRequestOut): string | null {
  return null;
}

/** The newest run first; a request that never ran sorts last. */
function byLastRun(a: ScoutRequestOut, b: ScoutRequestOut): number {
  return (b.last_run_at ?? '').localeCompare(a.last_run_at ?? '');
}

/**
 * Classify the empty feed for the scouts in scope (the active location's, or all of the
 * workspace's). Only meaningful when no filter is applied and the feed is empty.
 */
export function describeEmptyFeed(scouts: readonly ScoutRequestOut[]): EmptyFeed {
  if (scouts.length === 0) return { kind: 'no-scouts' };
  const inFlight = scouts.find((s) => IN_FLIGHT.has(s.status));
  if (inFlight) return { kind: 'in-flight', scout: inFlight };
  // A failed run records no `last_run_at` (apps/api/app/jobs/handlers.py marks the request
  // FAILED only), so a failure is recognised by status, as the most recent change.
  const newest = [...scouts].sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0]!;
  if (newest.status === 'failed') return { kind: 'last-run-failed', scout: newest };
  const latest = scouts.filter((s) => s.last_run_at).sort(byLastRun)[0];
  if (!latest) return { kind: 'never-ran', scout: scouts[0]! };
  return {
    kind: 'ran-found-nothing',
    scout: latest,
    signalsAnalyzed: statValue(latest.stats, 'signals_analyzed'),
    reason: reasonFromBackend(latest),
  };
}
