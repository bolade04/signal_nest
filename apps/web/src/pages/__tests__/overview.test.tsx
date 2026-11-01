import { http, HttpResponse } from 'msw';
import { waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { pagedJson } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

// P6-UI-019 and P6-UI-020 on the Overview.
//
// 019: the signals figure read `stats.signals_processed`, a key the backend has
// never written, so the tile rendered a confident 0 beside a nonzero noise count —
// an internally impossible pair that nobody caught because the MSW fixture invented
// the same key.
//
// 020: the tile headlined a transient sub-count ("Active", = queued|running|paused)
// with the population demoted to the hint, so a workspace of four completed scouts
// read "0". It also counted `paused`, which the run endpoint refuses to execute.

const P = (path: string) => `*${API_PREFIX}${path}`;

interface ScoutSeed {
  status: string;
  stats: Record<string, unknown>;
  last_run_at: string | null;
}

/** Replace the scout-request list with an exact, minimal set. */
function seedScouts(seeds: ScoutSeed[]): void {
  server.use(
    http.get(P('/workspaces/:ws/scout-requests'), () =>
      HttpResponse.json(
        seeds.map((s, i) => ({
          id: `scout-${i}`,
          workspace_id: 'ws-1',
          location_id: 'loc-1',
          organization_id: 'org-1',
          brand_id: 'brand-1',
          name: `Scout ${i}`,
          status: s.status,
          market: 'Dallas, TX',
          resolved_market: 'Dallas, TX',
          keywords: ['coffee'],
          source_types: ['rss'],
          product_profile_id: null,
          notes: null,
          last_run_at: s.last_run_at,
          stats: s.stats,
          created_at: '2026-05-01T00:00:00Z',
          updated_at: '2026-06-01T12:00:00Z',
        })),
      ),
    ),
  );
}

const RAN = { scanned: 9, noise_filtered: 1, signals_analyzed: 7, opportunities: 3 };

describe('Overview — signal statistics (P6-UI-019)', () => {
  it('renders the analyzed-signal count the backend actually reports', async () => {
    seedScouts([{ status: 'completed', stats: RAN, last_run_at: '2026-06-01T12:00:00Z' }]);
    const screen = renderApp(<App />, { route: '/' });
    // 7, not 0 (the old wrong key) and not 8 (scanned - noise_filtered).
    expect(await screen.findByText(/1 noise filtered · 7 signals/)).toBeInTheDocument();
  });

  it('sums across scouts and ignores those that have never run', async () => {
    seedScouts([
      { status: 'completed', stats: RAN, last_run_at: '2026-06-01T12:00:00Z' },
      { status: 'completed', stats: RAN, last_run_at: '2026-06-01T12:00:00Z' },
      { status: 'draft', stats: {}, last_run_at: null },
    ]);
    const screen = renderApp(<App />, { route: '/' });
    expect(await screen.findByText(/2 noise filtered · 14 signals/)).toBeInTheDocument();
  });

  it('shows an em dash, not 0, when no scout has ever run', async () => {
    seedScouts([{ status: 'draft', stats: {}, last_run_at: null }]);
    const screen = renderApp(<App />, { route: '/' });
    expect(await screen.findByText(/— noise filtered · — signals/)).toBeInTheDocument();
  });

  it('says "1 signal" for exactly one analysed signal (W21 Q1 fix pass)', async () => {
    seedScouts([
      {
        status: 'completed',
        stats: { scanned: 1, noise_filtered: 0, signals_analyzed: 1, opportunities: 1 },
        last_run_at: '2026-06-01T12:00:00Z',
      },
    ]);
    const screen = renderApp(<App />, { route: '/' });
    expect(await screen.findByText(/0 noise filtered · 1 signal$/)).toBeInTheDocument();
  });

  it('shows a genuine zero as 0 — a run that found nothing is not a run that never happened', async () => {
    seedScouts([
      {
        status: 'completed',
        stats: { scanned: 0, noise_filtered: 0, signals_analyzed: 0, opportunities: 0 },
        last_run_at: '2026-06-01T12:00:00Z',
      },
    ]);
    const screen = renderApp(<App />, { route: '/' });
    expect(await screen.findByText(/0 noise filtered · 0 signals/)).toBeInTheDocument();
  });
});

describe('Overview — scout request tile (P6-UI-020)', () => {
  it('headlines the scout population, not a transient sub-count', async () => {
    seedScouts([
      { status: 'completed', stats: RAN, last_run_at: '2026-06-01T12:00:00Z' },
      { status: 'completed', stats: RAN, last_run_at: '2026-06-01T12:00:00Z' },
      { status: 'completed', stats: RAN, last_run_at: '2026-06-01T12:00:00Z' },
      { status: 'completed', stats: RAN, last_run_at: '2026-06-01T12:00:00Z' },
    ]);
    const screen = renderApp(<App />, { route: '/' });
    // "Scout requests" also names a sidebar link; the tile renders its label as a <p>.
    await screen.findByText(/last run/);
    const label = screen.getAllByText('Scout requests').find((el) => el.tagName === 'P')!;
    // Exact match on the value element: `toHaveTextContent` on the tile would be a
    // substring test and would pass just as happily for 14 or 40.
    const value = label.nextElementSibling;
    expect(value).toHaveTextContent(/^4$/);
    expect(screen.queryByText('Active scout requests')).not.toBeInTheDocument();
  });

  it('surfaces in-flight work in the hint when a run is under way', async () => {
    seedScouts([
      { status: 'running', stats: {}, last_run_at: null },
      { status: 'completed', stats: RAN, last_run_at: '2026-06-01T12:00:00Z' },
    ]);
    const screen = renderApp(<App />, { route: '/' });
    expect(await screen.findByText(/1 in progress/)).toBeInTheDocument();
  });

  it('does not count paused as running — the run endpoint refuses a paused request', async () => {
    seedScouts([
      { status: 'paused', stats: RAN, last_run_at: '2026-06-01T12:00:00Z' },
      { status: 'completed', stats: RAN, last_run_at: '2026-06-01T12:00:00Z' },
    ]);
    const screen = renderApp(<App />, { route: '/' });
    expect(await screen.findByText(/1 paused/)).toBeInTheDocument();
    expect(screen.queryByText(/in progress/)).not.toBeInTheDocument();
  });

  it('counts a queued request as in flight — the API treats queued and running alike', async () => {
    seedScouts([{ status: 'queued', stats: {}, last_run_at: null }]);
    const screen = renderApp(<App />, { route: '/' });
    expect(await screen.findByText(/1 in progress/)).toBeInTheDocument();
  });

  it('reports a failed request as failed and never as in flight', async () => {
    seedScouts([{ status: 'failed', stats: RAN, last_run_at: '2026-06-01T12:00:00Z' }]);
    const screen = renderApp(<App />, { route: '/' });
    expect(await screen.findByText(/1 failed/)).toBeInTheDocument();
    expect(screen.queryByText(/in progress/)).not.toBeInTheDocument();
  });

  it('never describes a completed scout as in progress', async () => {
    seedScouts([{ status: 'completed', stats: RAN, last_run_at: '2026-06-01T12:00:00Z' }]);
    const screen = renderApp(<App />, { route: '/' });
    await screen.findByText(/last run/);
    expect(screen.queryByText(/in progress/)).not.toBeInTheDocument();
  });

  it('falls back to recency when nothing is in flight', async () => {
    seedScouts([{ status: 'completed', stats: RAN, last_run_at: '2026-06-01T12:00:00Z' }]);
    const screen = renderApp(<App />, { route: '/' });
    expect(await screen.findByText(/last run/)).toBeInTheDocument();
  });

  it('says so plainly when nothing has ever run', async () => {
    seedScouts([{ status: 'draft', stats: {}, last_run_at: null }]);
    const screen = renderApp(<App />, { route: '/' });
    expect(await screen.findByText(/none run yet/)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------------
// The Opportunities tile counted the rows of a 200-row page, so a workspace with more
// than 200 opportunities read "200" — the same defect class as P6-UI-022. The count is
// now the API's X-Total-Count (P6-PLAT-12); the figures derived from the rows say they
// describe only the rows shown.
// ---------------------------------------------------------------------------------

function opportunities(count: number) {
  return Array.from({ length: count }, (_, i) => ({
    id: `opp-${i}`,
    title: `Opportunity ${i}`,
    classification: 'validated',
    decision: 'monitor',
    opportunity_score: 70,
    confidence_score: 60,
    confidence_level: 'medium',
    priority_score: 50,
    relevance_score: 80,
    risk_level: 'low',
    resolved_market: 'Dallas, TX',
    inside_scout_area: true,
    why_it_matters: null,
    recommended_action: null,
    audience_fit: null,
    urgency: null,
    commercial_value: null,
    source_summary: [],
    status: 'new',
    location_id: 'loc-dallas',
    campaign_id: null,
    scout_request_id: 'scout-loc-dallas',
    is_simulated: true,
    created_at: '2026-06-01T09:00:00Z',
  }));
}

/** The value of the stat tile whose label is exactly `label`. */
async function tileValue(screen: ReturnType<typeof renderApp>, label: string): Promise<string> {
  await screen.findAllByText(label);
  const caption = screen
    .getAllByText(label)
    .find((el) => el.tagName === 'P' && el.nextElementSibling?.classList.contains('text-2xl'));
  if (!caption) throw new Error(`no stat tile labelled ${label}`);
  return caption.nextElementSibling!.textContent ?? '';
}

describe('Overview — opportunity count is the total, not the page (same class as P6-UI-022)', () => {
  it('shows the X-Total-Count total when there are more rows than the page', async () => {
    const seen: URL[] = [];
    server.use(
      http.get(P('/workspaces/:ws/opportunities'), ({ request }) => {
        const url = new URL(request.url);
        seen.push(url);
        return pagedJson(opportunities(250), url);
      }),
    );
    const screen = renderApp(<App />, { route: '/' });
    await waitFor(async () => expect(await tileValue(screen, 'Opportunities')).toBe('250'));
    expect(screen.getByText('1 markets in the 200 shown')).toBeInTheDocument();
    expect(
      screen.getByText(/computed from the 200\s+highest-priority opportunities of 250\./),
    ).toBeInTheDocument();
    expect(seen[0]!.searchParams.get('limit')).toBe('200');
  });

  it('says "200+" — never "200" — when more rows follow but no total is reported', async () => {
    server.use(
      http.get(P('/workspaces/:ws/opportunities'), () =>
        HttpResponse.json(opportunities(200), { headers: { 'X-Next-Cursor': 'opaque' } }),
      ),
    );
    const screen = renderApp(<App />, { route: '/' });
    await waitFor(async () => expect(await tileValue(screen, 'Opportunities')).toBe('200+'));
  });

  it('shows the exact count, with no sampling note, when the page is the whole result', async () => {
    server.use(
      http.get(P('/workspaces/:ws/opportunities'), ({ request }) =>
        pagedJson(opportunities(12), new URL(request.url)),
      ),
    );
    const screen = renderApp(<App />, { route: '/' });
    await waitFor(async () => expect(await tileValue(screen, 'Opportunities')).toBe('12'));
    expect(screen.getByText('1 markets')).toBeInTheDocument();
    expect(screen.queryByText(/computed from the/)).not.toBeInTheDocument();
  });
});
