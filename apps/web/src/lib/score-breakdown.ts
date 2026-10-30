/**
 * The explainable score breakdown of an opportunity, as the API persists and returns it.
 *
 * `ScoreBreakdown.breakdown` is typed `{ [key: string]: unknown }` in the contract, but
 * every value is one scoring component in the shape `apps/api/app/scoring/opportunity.py`
 * `_weighted` writes: `{ weight, value, points }`, with `points = round(value * weight, 2)`
 * and the panel `total = round(sum(points))` (so the listed points agree with the total
 * to within 0.5). Nothing here re-scores anything: the UI only reads those numbers.
 */

export interface ScoreComponent {
  key: string;
  /** Points this component contributed, or null when the API sent no usable number. */
  points: number | null;
  /** The component's maximum (its weight), or null when absent. */
  weight: number | null;
}

/** The API rounds the panel total to a whole number from the 2-decimal points. */
export const BREAKDOWN_ROUNDING_TOLERANCE = 0.5;

function finite(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/**
 * One row per component, in the API's order. A component whose value is missing, null,
 * not an object, or without numeric `points` is unavailable (`points: null`); a component
 * that genuinely scored zero keeps `points: 0`.
 */
export function readScoreComponents(breakdown: Record<string, unknown> | null | undefined): ScoreComponent[] {
  return Object.entries(breakdown ?? {}).map(([key, raw]) => {
    if (raw == null || typeof raw !== 'object' || Array.isArray(raw)) return { key, points: null, weight: null };
    const component = raw as Record<string, unknown>;
    return { key, points: finite(component.points), weight: finite(component.weight) };
  });
}

const pointsFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

export function formatPoints(value: number): string {
  return pointsFormat.format(value);
}

/** "17.5 / 25", "0 / 15", "8.5" (no weight) or "—" (no data). */
export function formatComponent(component: ScoreComponent): string {
  if (component.points == null) return '—';
  if (component.weight == null) return formatPoints(component.points);
  return `${formatPoints(component.points)} / ${formatPoints(component.weight)}`;
}

export interface BreakdownCheck {
  /** Sum of the points of the components that have data. */
  listed: number;
  /** Components with no usable points (rendered "—"). */
  unavailable: number;
  /** The listed points add up to the API's total within its rounding (±0.5). */
  sumAgrees: boolean;
}

export function checkBreakdown(components: readonly ScoreComponent[], total: number): BreakdownCheck {
  const listed = components.reduce((sum, c) => sum + (c.points ?? 0), 0);
  const unavailable = components.filter((c) => c.points == null).length;
  const sumAgrees = Math.abs(listed - total) <= BREAKDOWN_ROUNDING_TOLERANCE + 1e-9;
  return { listed, unavailable, sumAgrees };
}
