import type { ScoutRequestOut } from '@/api/types';
import {
  runNextActions,
  runNextActionsForNonEditors,
  runResultCopy,
  sourceLabel,
  type RunResult,
} from '@/lib/run-outcome';
import { formatStat, statValue } from '@/lib/scout-stats';

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
export interface RunResultLines {
  ran: string[];
  didNot: string[];
  next: string | null;
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
    const read = result.sources.filter((source) => !source.failed);
    ran.push(
      result.sources.length
        ? runResultCopy.sourcesRead(read.length, result.sources.length)
        : runResultCopy.noSource,
    );
    for (const source of read) {
      ran.push(runResultCopy.sourceRead(sourceLabel(source.source_key), source.fetched));
    }
    for (const source of result.sources) {
      const label = sourceLabel(source.source_key);
      if (source.clipped) didNot.push(runResultCopy.clipped(label, source.max_items));
      if (source.failed) didNot.push(runResultCopy.failed(label, source.failure_reason));
    }
  }
  const nextSteps = canEdit ? runNextActions : runNextActionsForNonEditors;
  return { ran, didNot, next: result.next_action ? nextSteps[result.next_action] : null };
}
