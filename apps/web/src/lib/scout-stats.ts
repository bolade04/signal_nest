import type { components } from '@/api/schema';

/**
 * Reading a scout request's run statistics.
 *
 * `ScoutRequestOut.stats` is generated as an open index signature
 * (`{ [key: string]: unknown }`), because the API declares it as a bare `dict`.
 * That means *any* key type-checks, and the previous per-page helper coerced a
 * missing key to `0` — so the UI read `signals_processed`, a key the pipeline has
 * never written, and rendered a confident zero on three screens. The test double
 * invented the same key, so nothing failed.
 *
 * Two properties here make that quiet failure mode impossible to repeat. They
 * cover different cases, and it is worth being exact about which:
 *
 *  - The key is constrained to the generated `RunStats` contract, derived rather
 *    than hand-written. A *contract-level* rename reaches `RunStats`, and CI fails
 *    on any `schema.d.ts` diff, so regenerating narrows this union and the call
 *    sites stop compiling. A rename in the pipeline alone does not reach here:
 *    `ScoutRequestOut.stats` is assigned verbatim with no key check, so the value
 *    simply arrives absent.
 *  - An absent key is `null`, never `0` — which is what covers that second case,
 *    and the reason these screens degrade to "not reported" instead of asserting a
 *    confident zero. "Never ran" and "ran and found nothing" are different facts
 *    and must not render identically.
 */
export type RunStatKey = keyof components['schemas']['RunStats'];

/** The four keys `apps/api/app/jobs/pipeline.py` persists, in contract order. */
export const RUN_STAT_KEYS = [
  'scanned',
  'noise_filtered',
  'signals_analyzed',
  'opportunities',
] as const satisfies readonly RunStatKey[];

/**
 * One statistic from one scout's last completed run.
 *
 * `null` means "not reported" — the scout has never completed a run (the column
 * defaults to `{}`), or the contract changed underneath us. It never means zero.
 */
export function statValue(
  stats: Record<string, unknown> | null | undefined,
  key: RunStatKey,
): number | null {
  if (!stats) return null;
  const value = stats[key];
  // Own-property and finiteness are separate guards and both are needed: an
  // inherited value is finite, and NaN is an own property. Neither is reachable
  // over the JSON wire today, but one NaN would otherwise poison a whole total.
  // `Object.prototype.hasOwnProperty.call` rather than `Object.hasOwn`: the latter
  // needs the ES2022 lib, and this workspace targets lower.
  const own = Object.prototype.hasOwnProperty.call(stats, key);
  return own && Number.isFinite(value) ? (value as number) : null;
}

/**
 * The same statistic totalled across scouts.
 *
 * Scouts that have never run contribute nothing rather than dragging the total to
 * zero; the result is `null` only when no scout reports the statistic at all.
 */
export function sumStat(
  statsList: readonly (Record<string, unknown> | null | undefined)[],
  key: RunStatKey,
): number | null {
  let total: number | null = null;
  for (const stats of statsList) {
    const value = statValue(stats, key);
    if (value !== null) total = (total ?? 0) + value;
  }
  return total;
}

/** Render a statistic, showing an em dash for "not reported" and `0` for a real zero. */
export function formatStat(value: number | null): string {
  return value === null ? '—' : String(value);
}
