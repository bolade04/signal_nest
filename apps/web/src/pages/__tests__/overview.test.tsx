import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
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
