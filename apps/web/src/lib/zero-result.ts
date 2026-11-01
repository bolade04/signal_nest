import type { ScoutRequestOut } from '@/api/types';

/**
 * Why a Scout's latest completed run produced no new card (W16 customer repairs §E, PROPOSED),
 * from what the run recorded: `stats` (apps/api/app/jobs/pipeline.py) and its coverage outcome.
 *
 * Every line states only what the LAST RUN recorded, in the past tense, about that run (round 1a,
 * CF1): never a prediction about a future run ("running again will not help" was false for a
 * Scout whose last run predates the market-reading fix), and never a statement about the Scout's
 * CURRENT configuration (an edit may have changed the source types, keywords or market since; the
 * run does not record the market text it used, so none is quoted). A run recorded before a
 * statistic existed falls to the generic line. Returns `null` when the latest run produced a new
 * card, or when there is no completed run to explain.
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

/** What a run says when its record cannot tell the cases apart (or found nothing new). */
const GENERIC: ZeroResult = {
  code: 'nothing_new',
  title: 'Nothing new in the last run',
  body: 'The last run found nothing new.',
};

function num(stats: Stats, key: string): number | null {
  if (!stats || !Object.prototype.hasOwnProperty.call(stats, key)) return null;
  const value = stats[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

const items = (n: number) => `${n} ${n === 1 ? 'item' : 'items'}`;

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
    return {
      code: 'not_covered',
      title: 'No source covered the last run',
      body: "No source covered this scout's market in its last run, so it collected nothing.",
    };
  }
  if (status === 'unavailable') {
    return {
      code: 'sources_unavailable',
      title: 'The sources failed in the last run',
      body: 'Every live source the last run read failed, so it collected nothing.',
    };
  }
  const scanned = num(stats, 'scanned');
  const analysed = num(stats, 'signals_analyzed');
  const noise = num(stats, 'noise_filtered');
  if (!request.coverage && scanned === 0) {
    // Contract §J.3 (review r1 F3): a run recorded before coverage was recorded cannot tell "no
    // items" from "no source for the market"; it gets the generic line.
    return GENERIC;
  }
  if (scanned === 0) {
    return fetchedAny(stats)
      ? {
          code: 'items_unavailable',
          title: 'Nothing was available to analyse in the last run',
          body: "The last run's sources returned items, but none were available to analyse.",
        }
      : {
          code: 'no_items',
          title: 'The last run found no matching items',
          body: "The last run's sources returned no items for the source types and keywords that run used.",
        };
  }
  if (scanned !== null && analysed === 0) {
    const dup = noise !== null ? Math.max(0, scanned - noise) : null;
    const split =
      noise !== null && dup !== null ? ` ${noise} as noise and ${dup} as duplicates.` : '';
    return {
      code: 'all_filtered',
      title: 'The last run filtered everything it collected',
      body: `The last run collected ${items(scanned)}, but ${scanned === 1 ? 'it was' : 'all were'} filtered:${split || ' as noise or duplicates.'}`,
    };
  }
  if (analysed !== null && analysed > 0 && opportunities === 0) {
    // Not reachable at 9279c1d2 (every analysed group becomes a card); kept as a stated branch.
    return {
      code: 'none_qualified',
      title: 'Nothing qualified in the last run',
      body: 'The last run analysed signals, but none qualified as an opportunity.',
    };
  }
  if (fresh === 0 && opportunities > 0) {
    return {
      code: 'all_seen_again',
      title: 'No new findings in the last run',
      body:
        opportunities === 1
          ? 'The finding of the last run was already known; that run recorded it as seen again.'
          : `All ${opportunities} findings of the last run were already known; that run recorded them as seen again.`,
    };
  }
  return GENERIC;
}

