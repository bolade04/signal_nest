import type { ScoutRequestOut } from '@/api/types';

/**
 * Why a Scout's latest completed run produced no new card (W16 customer repairs §E, PROPOSED),
 * from what the run recorded: `stats` (apps/api/app/jobs/pipeline.py) and its coverage outcome.
 * Each branch says only what the recorded numbers support; a run recorded before a statistic
 * existed falls to the generic line. Returns `null` when the latest run produced a new card, or
 * when there is no completed run to explain.
 */
export interface ZeroResult {
  code:
    | 'not_covered'
    | 'sources_unavailable'
    | 'no_items'
    | 'items_unavailable'
    | 'all_filtered'
    | 'none_qualified'
    | 'all_seen_again'
    | 'nothing_new';
  title: string;
  body: string;
}

type Stats = Record<string, unknown> | null | undefined;

function num(stats: Stats, key: string): number | null {
  if (!stats || !Object.prototype.hasOwnProperty.call(stats, key)) return null;
  const value = stats[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function fetchedAny(stats: Stats): boolean {
  const limits = stats?.['fetch_limits'];
  return Array.isArray(limits) && limits.some((f) => typeof f?.fetched === 'number' && f.fetched > 0);
}

export function zeroResult(
  request: Pick<ScoutRequestOut, 'stats' | 'coverage' | 'resolved_market' | 'last_run_at'>,
): ZeroResult | null {
  const stats = request.stats as Stats;
  if (!request.last_run_at || !stats || stats['refused'] === true) return null;
  const opportunities = num(stats, 'opportunities');
  const fresh = num(stats, 'opportunities_new');
  if (opportunities === null) return null;
  if (fresh !== null ? fresh > 0 : opportunities > 0) {
    return null;
  }
  const status = request.coverage?.status;
  if (status === 'not_covered') {
    const market = request.resolved_market ? ` (${request.resolved_market})` : '';
    return {
      code: 'not_covered',
      title: 'No sample coverage for this market',
      body: `No source covers this scout's market${market}. Running again will not help; this market needs a source.`,
    };
  }
  if (status === 'unavailable') {
    return {
      code: 'sources_unavailable',
      title: 'The sources for this market failed',
      body: 'Every live source for this market failed during the last run. Try again later.',
    };
  }
  const scanned = num(stats, 'scanned');
  const analysed = num(stats, 'signals_analyzed');
  const noise = num(stats, 'noise_filtered');
  if (scanned === 0) {
    return fetchedAny(stats)
      ? {
          code: 'items_unavailable',
          title: 'No items were available to analyse',
          body: 'The sources returned items, but none were available to analyse in the last run.',
        }
      : {
          code: 'no_items',
          title: 'No matching items',
          body: "The sources returned no items for this scout's source types and keywords. Try more source types or broader keywords.",
        };
  }
  if (scanned !== null && analysed === 0) {
    const dup = noise !== null ? Math.max(0, scanned - noise) : null;
    const split =
      noise !== null && dup !== null ? ` ${noise} as noise and ${dup} as duplicates.` : '';
    return {
      code: 'all_filtered',
      title: 'Everything collected was filtered',
      body: `${scanned} items were collected, but all were filtered:${split || ' as noise or duplicates.'}`,
    };
  }
  if (analysed !== null && analysed > 0 && opportunities === 0) {
    // Not reachable at 9279c1d2 (every analysed group becomes a card); kept as a stated branch.
    return {
      code: 'none_qualified',
      title: 'Nothing qualified',
      body: 'Signals were analysed, but none qualified as an opportunity.',
    };
  }
  if (fresh === 0 && opportunities > 0) {
    return {
      code: 'all_seen_again',
      title: 'No new findings',
      body: `All ${opportunities} findings of the last run were already known; they are marked as seen again.`,
    };
  }
  return {
    code: 'nothing_new',
    title: 'Nothing new',
    body: 'The last run found nothing new.',
  };
}

/** Supplementary partial-fetch note, when the run recorded one. */
export function partialFetchNote(stats: Stats): string | null {
  return stats?.['partial_fetch'] === true
    ? 'Some sources failed or were clipped during the last run.'
    : null;
}
