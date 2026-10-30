import { within } from '@testing-library/react';
import { getResponse, http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { handlers, pagedJson } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * UI-AUDIT F7 / P6: the confidence vocabulary disagreed with itself on the same card
 * ("CONFIDENCE 71 Moderate" beside the badge "High confidence"). The pill and the detail
 * meter applied the generic 0–100 band (>= 75 Strong, >= 50 Moderate, >= 40 Fair) to the
 * confidence score, while the badge shows the API's `confidence_level`.
 *
 * The API's level is the definition: apps/api/app/scoring/opportunity.py `confidence_level`
 * (< 40 low, < 70 medium, otherwise high). `apiConfidenceLevel` below mirrors it ONLY to
 * build fixtures that agree with the API (the fixture-can-agree-with-a-bug lesson); the UI
 * itself never derives the level from the score.
 */

function apiConfidenceLevel(score: number): 'low' | 'medium' | 'high' {
  if (score < 40) return 'low';
  if (score < 70) return 'medium';
  return 'high';
}

const WORD = { low: 'Low', medium: 'Medium', high: 'High' } as const;
const P = (path: string) => `*${API_PREFIX}${path}`;

// The two band edges, plus the seeded fixture scores seen on the preview (61, 71, 87).
const SCORES = [39, 40, 69, 70, 61, 71, 87];

async function defaultFeed(): Promise<Record<string, unknown>[]> {
  const res = await getResponse(handlers, new Request(`http://localhost${API_PREFIX}/workspaces/ws-1/opportunities?limit=100`));
  return (await res!.json()) as Record<string, unknown>[];
}

describe('confidence labels agree with the API band (UI-AUDIT P6)', () => {
  it('the shared fixture agrees with the API definition of confidence_level', async () => {
    const rows = await defaultFeed();
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      expect(row.confidence_level, `${row.id}: ${row.confidence_score}`).toBe(apiConfidenceLevel(row.confidence_score as number));
    }
  });

  it('feed card: the confidence pill and the confidence badge show the same band at 39/40 and 69/70', async () => {
    const base = (await defaultFeed())[0]!;
    const rows = SCORES.map((score, i) => ({
      ...base,
      id: `opp-conf-${score}`,
      title: `Confidence edge ${score}`,
      confidence_score: score,
      confidence_level: apiConfidenceLevel(score),
      opportunity_score: 60,
      relevance_score: 79,
      created_at: `2026-06-0${(i % 9) + 1}T09:00:00Z`,
    }));
    server.use(http.get(P('/workspaces/:ws/opportunities'), ({ request }) => pagedJson(rows, new URL(request.url))));

    const screen = renderApp(<App />, { route: '/opportunities' });
    for (const score of SCORES) {
      const link = await screen.findByRole('link', { name: `Confidence edge ${score}` });
      const card = link.closest('.rounded-lg') as HTMLElement;
      const pill = within(card).getByText('Confidence').parentElement!;
      const pillValue = pill.querySelectorAll('p')[1]!;
      const pillWord = pillValue.querySelector('span')?.textContent ?? null;
      const badge = within(card).getByText(/ confidence$/).textContent;
      const expected = WORD[apiConfidenceLevel(score)];
      expect(pillValue.textContent, `pill at ${score}`).toBe(`${score}${expected}`);
      expect(badge, `badge at ${score}`).toBe(`${expected} confidence`);
      expect(pillWord, `pill word = badge word at ${score}`).toBe(badge!.replace(/ confidence$/, ''));
      // The other two pills keep the generic band, unchanged.
      expect(within(card).getByText('Score').parentElement!.querySelectorAll('p')[1]!.textContent).toBe('60Moderate');
      expect(within(card).getByText('Relevance').parentElement!.querySelectorAll('p')[1]!.textContent).toBe('79Strong');
    }
  });

  it.each(SCORES)('detail page at confidence %i: the meter band and the badge agree with the API level', async (score) => {
    server.use(
      http.get(P('/workspaces/:ws/opportunities/:id'), async ({ request }) => {
        const original = await getResponse(handlers, request);
        const body = (await original!.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...body, confidence_score: score, confidence_level: apiConfidenceLevel(score) });
      }),
    );
    const screen = renderApp(<App />, { route: '/opportunities/opp-loc-dallas-0' });
    await screen.findByRole('heading', { name: /Dallas customers want faster delivery 1/i });
    const expected = WORD[apiConfidenceLevel(score)];

    const meter = screen.getByRole('meter', { name: 'Confidence' });
    const reading = meter.previousElementSibling!.lastElementChild!;
    expect(reading.textContent).toBe(`${score}· ${expected}`);
    expect(screen.getByText(/^(Low|Medium|High) confidence$/).textContent).toBe(`${expected} confidence`);
    // No generic band word is applied to confidence any more.
    expect(reading.textContent).not.toMatch(/Strong|Moderate|Fair/);
  });
});
