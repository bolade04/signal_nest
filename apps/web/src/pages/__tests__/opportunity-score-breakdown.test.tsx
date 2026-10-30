import { getResponse, http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { handlers, opportunityDetailFixture } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * UI-AUDIT F1 / P1: the opportunity detail's score breakdown.
 *
 * Every component arrives as `{weight, value, points}` (apps/api/app/scoring/opportunity.py
 * `_weighted`). The page used to pass each one through a string helper that returns null
 * for objects, so all 13 rows read "—" against the real API while the old fixture (bare
 * numbers) kept the test suite green.
 */

const P = (path: string) => `*${API_PREFIX}${path}`;
const ID = 'opp-loc-dallas-0';
const TITLE = /Dallas customers want faster delivery 1/i;

// Captured verbatim from a seeded opportunity on the local preview API at 7a183a12
// (P6-W10-UIFIX-01 raw/010): opportunity total 62 with a zero-point `trend`, confidence 87.
const REAL_SCORES = [
  {
    kind: 'opportunity',
    total: 62,
    breakdown: {
      relevance: { weight: 25, value: 0.7, points: 17.5 },
      trend: { weight: 15, value: 0.0, points: 0.0 },
      discussion: { weight: 15, value: 0.75, points: 11.25 },
      commercial: { weight: 15, value: 0.57, points: 8.55 },
      engagement: { weight: 10, value: 0.63, points: 6.3 },
      source_credibility: { weight: 10, value: 0.85, points: 8.5 },
      recency: { weight: 10, value: 1.0, points: 10.0 },
    },
  },
  {
    kind: 'confidence',
    total: 87,
    breakdown: {
      evidence_quantity: { weight: 20, value: 0.6, points: 12.0 },
      evidence_diversity: { weight: 20, value: 1.0, points: 20.0 },
      source_reliability: { weight: 20, value: 0.85, points: 17.0 },
      signal_clarity: { weight: 15, value: 1.0, points: 15.0 },
      score_consistency: { weight: 15, value: 0.87, points: 13.05 },
      recency_reliability: { weight: 10, value: 1.0, points: 10.0 },
    },
  },
];

function serveScores(scores: unknown) {
  server.use(
    http.get(P('/workspaces/:ws/opportunities/:id'), async ({ request }) => {
      const original = await getResponse(handlers, request);
      const body = (await original!.json()) as Record<string, unknown>;
      return HttpResponse.json({ ...body, scores });
    }),
  );
}

/** The rows of one breakdown panel, found by its heading ("Opportunity · 62"). */
function panel(screen: ReturnType<typeof renderApp>, heading: string) {
  const title = screen.getByText(heading, { selector: 'p' });
  const box = title.parentElement!;
  const rows = Array.from(box.querySelectorAll('dl > div')).map((row) => [
    row.querySelector('dt')!.textContent,
    row.querySelector('dd')!.textContent,
  ]);
  const note = box.querySelector('dl + p')?.textContent ?? null;
  return { rows, note };
}

async function open() {
  const screen = renderApp(<App />, { route: `/opportunities/${ID}` });
  await screen.findByRole('heading', { name: TITLE });
  return screen;
}

describe('opportunity score breakdown (UI-AUDIT P1)', () => {
  it('renders each component of the real API shape as points out of its weight, zero included', async () => {
    serveScores(REAL_SCORES);
    const screen = await open();

    const opportunity = panel(screen, 'Opportunity · 62');
    expect(opportunity.rows).toEqual([
      ['Relevance', '17.5 / 25'],
      ['Trend', '0 / 15'],
      ['Discussion', '11.25 / 15'],
      ['Commercial', '8.55 / 15'],
      ['Engagement', '6.3 / 10'],
      ['Source Credibility', '8.5 / 10'],
      ['Recency', '10 / 10'],
    ]);
    // 62.1 listed against a total of 62: inside the API's whole-number rounding.
    expect(opportunity.note).toBeNull();

    const confidence = panel(screen, 'Confidence · 87');
    expect(confidence.rows).toEqual([
      ['Evidence Quantity', '12 / 20'],
      ['Evidence Diversity', '20 / 20'],
      ['Source Reliability', '17 / 20'],
      ['Signal Clarity', '15 / 15'],
      ['Score Consistency', '13.05 / 15'],
      ['Recency Reliability', '10 / 10'],
    ]);
    expect(confidence.note).toBeNull();
  });

  it('serves the API shape from the shared fixture, and its listed points add up to the displayed total', async () => {
    const fixture = opportunityDetailFixture(ID);
    // Fixture shape guard: every component is {weight, value, points}, never a bare number.
    for (const score of fixture.scores ?? []) {
      for (const component of Object.values(score.breakdown)) {
        expect(component).toEqual({ weight: expect.any(Number), value: expect.any(Number), points: expect.any(Number) });
      }
    }
    const screen = await open();
    for (const score of fixture.scores ?? []) {
      const { rows, note } = panel(screen, `${score.kind === 'opportunity' ? 'Opportunity' : 'Confidence'} · ${score.total}`);
      expect(rows).toHaveLength(Object.keys(score.breakdown).length);
      const listed = rows.reduce((sum, [, value]) => {
        expect(value).toMatch(/^\d+(\.\d+)? \/ \d+$/);
        return sum + Number(value!.split(' / ')[0]);
      }, 0);
      expect(Math.abs(listed - score.total)).toBeLessThanOrEqual(0.5);
      expect(note).toBeNull();
    }
    // The fixture's zero-point component renders as a zero, not as missing data.
    expect(panel(screen, `Opportunity · ${fixture.opportunity_score}`).rows).toContainEqual(['Trend', '0 / 15']);
  });

  it('shows "—" only for a component with no data, and says when the listed points miss the total', async () => {
    serveScores([
      {
        kind: 'opportunity',
        total: 62,
        breakdown: {
          relevance: { weight: 25, value: 0.7, points: 17.5 },
          trend: { weight: 15, value: 0, points: 0 },
          discussion: null,
          commercial: { weight: 15 },
          engagement: 6.3,
          source_credibility: { weight: 10, value: 0.85, points: 8.5 },
          recency: { weight: 10, value: 1, points: 10 },
        },
      },
      {
        kind: 'confidence',
        total: 87,
        breakdown: {
          evidence_quantity: { weight: 20, value: 0.6, points: 12 },
          evidence_diversity: { weight: 20, value: 1, points: 20 },
        },
      },
    ]);
    const screen = await open();

    const opportunity = panel(screen, 'Opportunity · 62');
    expect(opportunity.rows).toEqual([
      ['Relevance', '17.5 / 25'],
      ['Trend', '0 / 15'],
      ['Discussion', '—'],
      ['Commercial', '—'],
      ['Engagement', '—'],
      ['Source Credibility', '8.5 / 10'],
      ['Recency', '10 / 10'],
    ]);
    expect(opportunity.note).toBe('Some components have no data (—). Listed points add up to 36; the total is 62.');

    // Every listed component has data, but they do not reach the API's total.
    expect(panel(screen, 'Confidence · 87').note).toBe('Listed points add up to 32; the total is 87.');
  });
});
