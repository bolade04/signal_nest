import type { ScoutRequestOut } from '@/api/types';
import type { ScoutExecution } from '@/lib/scout-execution';
import {
  runNextActions,
  runNextActionsForNonEditors,
  runNextStepAfter,
  runResultCopy,
  runResultSummaries,
  sourceLabel,
  type RunResult,
  type RunSource,
} from '@/lib/run-outcome';
import { formatStat, statValue } from '@/lib/scout-stats';
import { formatDateTime } from '@/lib/utils';

/**
 * The lines of the run-result panel (W21 FU-Q1-1, Q1 (a+), PROPOSED): what ran, what did not
 * run and the one next step of the latest recorded run.
 *
 * Every fact is typed: the outcome, its sources, the refusal and the next action come from
 * `ScoutRequestOut.run_outcome` (lifted by the API from what the run recorded); the counts come
 * from `stats` through the generated `RunStats` keys only (lib/scout-stats.ts), so an absent
 * count reads "—", never a confident zero. No untyped `stats` key is read here. The wording is
 * lib/run-outcome.ts's (PROPOSED, pending the copy owner's sign-off). The next step depends on
 * whether the viewer can edit and run the Scout (`canEdit`, the same gate that shows Edit and
 * Run now): a member who cannot is never told to edit or run it.
 */
/**
 * Whether the page shows the run-result panel for this outcome (review r1 MINOR-1). A run that
 * read no source (`sources: []`: only a market no source covers, `fetch_signals`' default batch;
 * every other path reports at least one source) is not shown here: "complete" would claim that
 * every source was read when none was, and the zero-result panel already explains that run
 * ("No source covered the last run"), once (W16 round 1a CF2). `sources: null` (a refusal
 * recorded before per-source facts existed) is still shown.
 */
export function showsRunResult(result: RunResult | null | undefined): result is RunResult {
  return Boolean(result) && !(result!.sources && result!.sources.length === 0);
}

/**
 * Whether `execution` says the latest run ended without writing a result, so the panel shows an
 * earlier run's (review r1 MINOR-2): the latest run failed to admit an operation, was cancelled,
 * or was interrupted. Not `run_failed` (a refusal ends so and is itself the result), not paused,
 * in progress or needs attention (the latest run may still have written its result).
 */
export function isEarlierRunResult(execution: ScoutExecution | null | undefined): boolean {
  return (
    execution?.state === 'stopped' &&
    (execution.reason === 'admission_unresolved' ||
      execution.reason === 'run_cancelled' ||
      execution.reason === 'run_interrupted')
  );
}

export interface RunResultLines {
  ran: string[];
  didNot: string[];
  next: string | null;
}

/**
 * W21 FU-HELD-BODY-1 (PROPOSED): the source was not contacted in this run - its last read failed
 * and it was cooling down - and the stored copy of its last good read was used. It was not read.
 */
export function usedHeldCopy(source: RunSource): boolean {
  return !source.failed && source.read_mode === 'held';
}

/**
 * The coverage reasons that positively establish an entirely simulated source path, where no copy
 * could be held: the demo library and the sample feed read their content in every run and hold no
 * cache (operator ruling on packet item 48, option B). A CLOSED allow-list: any other value - a live
 * path, no source, or a reason this build does not know - is not positively simulated.
 *
 * Guardrail (W21-REV-HELDBODY r2): this relies on a run never mixing live and simulated sources.
 * `fetch_signals` (apps/api/app/scouting_requests/connectors.py) takes exactly one source path per
 * run, and only the live path holds a feed cache, so a run recorded with one of these reasons cannot
 * have used a held copy. A change that lets one run mix live and simulated sources must revisit this
 * rule.
 */
const SIMULATED_ONLY_REASONS: ReadonlySet<string> = new Set(['fixture_library', 'sandbox_feed']);

/**
 * The status line's summary. A complete run whose record does not say how each source was read (a
 * run recorded before `read_mode` existed) does not claim that every source was read in full, unless
 * the run's own recorded coverage reason establishes an entirely simulated source path (option B):
 *
 * - only where `read_mode` is not recorded; a run that records it is summarised as it says;
 * - keyed on `coverage.reason` alone, never `coverage.is_simulated` (a replayed live source records
 *   `is_simulated: true` and could still have used a held copy);
 * - a missing or unreadable `coverage` (the API lifts an unreadable one to `null`) keeps the
 *   qualified summary.
 *
 * Only the record's own facts decide: never the current configuration or environment. No time is
 * invented and no record is rewritten.
 */
export function runResultSummary(
  result: RunResult,
  coverage: ScoutRequestOut['coverage'] | null | undefined,
): string {
  if (result.kind === 'complete') {
    // `sources: null` records no per-source facts at all, so no `read_mode` either.
    const unrecorded = !result.sources || result.sources.some((s) => !s.failed && s.read_mode == null);
    const simulatedOnly = typeof coverage?.reason === 'string' && SIMULATED_ONLY_REASONS.has(coverage.reason);
    if (unrecorded && !simulatedOnly) return runResultCopy.completeUnrecorded;
  }
  return runResultSummaries[result.kind];
}

/** A recorded time as the page shows times, or `null` when it was not recorded. */
function shownTime(iso: string | null | undefined): string | null {
  return iso ? formatDateTime(iso) : null;
}

function withRetry(line: string, source: RunSource): string {
  const retry = shownTime(source.next_retry_at);
  return retry ? line + runResultCopy.retryAfter(retry) : line;
}

export function runResultLines(
  result: RunResult,
  stats: ScoutRequestOut['stats'] | null | undefined,
  canEdit: boolean,
): RunResultLines {
  const stat = (key: 'scanned' | 'signals_analyzed' | 'opportunities') =>
    formatStat(statValue(stats, key));
  const ran: string[] = [];
  const didNot: string[] = [];
  if (result.kind === 'refused') {
    ran.push(runResultCopy.refusedCounts(stat('scanned')));
    didNot.push(runResultCopy.refusal(result.bound));
  } else {
    ran.push(runResultCopy.counts(stat('scanned'), stat('signals_analyzed'), stat('opportunities')));
  }
  // `null`: the run recorded no per-source facts (a refusal recorded before they existed).
  if (result.sources) {
    // A source whose stored copy was used during a failure cool-down was not read (FU-HELD-BODY-1).
    const read = result.sources.filter((source) => !source.failed && !usedHeldCopy(source));
    ran.push(
      result.sources.length
        ? runResultCopy.sourcesRead(read.length, result.sources.length)
        : runResultCopy.noSource,
    );
    for (const source of result.sources) {
      if (source.failed) continue;
      const label = sourceLabel(source.source_key);
      ran.push(
        usedHeldCopy(source)
          ? runResultCopy.sourceHeldUsed(label, source.fetched)
          : runResultCopy.sourceRead(label, source.fetched),
      );
    }
    for (const source of result.sources) {
      const label = sourceLabel(source.source_key);
      if (source.clipped) didNot.push(runResultCopy.clipped(label, source.max_items));
      if (source.failed) didNot.push(withRetry(runResultCopy.failed(label, source.failure_reason), source));
      if (usedHeldCopy(source)) {
        const line = runResultCopy.held(
          label,
          source.last_failure_reason ?? null,
          shownTime(source.last_read_at),
        );
        didNot.push(withRetry(line, source));
      }
    }
  }
  return { ran, didNot, next: nextStep(result, canEdit) };
}

/**
 * The one next step. During a source's cool-down (`wait_for_source_retry`) it is to run again
 * after the recorded retry time - not "run again later", since a run inside the cool-down may not
 * reach the source - or, with no time recorded, to wait (FU-HELD-BODY-1, PROPOSED).
 */
function nextStep(result: RunResult, canEdit: boolean): string | null {
  if (!result.next_action) return null;
  const retry = shownTime(result.next_retry_at);
  if (result.next_action === 'wait_for_source_retry' && retry) {
    const unread = (result.sources ?? []).filter((s) => s.failed || usedHeldCopy(s)).length;
    return (canEdit ? runNextStepAfter.editor : runNextStepAfter.nonEditor)(retry, unread);
  }
  return (canEdit ? runNextActions : runNextActionsForNonEditors)[result.next_action];
}
