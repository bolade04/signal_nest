import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { formatCount } from '@/lib/scout-stats';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

// P6-UI-019 on the two remaining surfaces. The same wrong key was read on three
// screens; covering all three means a single corrected screen cannot hide the others.
// The Overview limb lives in overview.test.tsx.

const P = (path: string) => `*${API_PREFIX}${path}`;
const RAN = { scanned: 9, noise_filtered: 1, signals_analyzed: 7, opportunities: 3 };

function scout(overrides: Record<string, unknown>) {
  return {
    id: 'scout-x',
    workspace_id: 'ws-1',
    location_id: 'loc-1',
    organization_id: 'org-1',
    brand_id: 'brand-1',
    name: 'Dallas specialty-coffee scout',
    status: 'completed',
    market: 'Dallas, TX',
    resolved_market: 'Dallas, TX',
    keywords: ['coffee'],
    source_types: ['rss'],
    product_profile_id: null,
    notes: null,
    last_run_at: '2026-06-01T12:00:00Z',
    stats: RAN,
    created_at: '2026-05-01T00:00:00Z',
    updated_at: '2026-06-01T12:00:00Z',
    ...overrides,
  };
}

function seedList(overrides: Record<string, unknown> = {}): void {
  server.use(
    http.get(P('/workspaces/:ws/scout-requests'), () =>
      HttpResponse.json([scout(overrides)]),
    ),
  );
}

function seedDetail(overrides: Record<string, unknown> = {}): void {
  server.use(
    http.get(P('/workspaces/:ws/scout-requests'), () =>
      HttpResponse.json([scout(overrides)]),
    ),
    http.get(P('/workspaces/:ws/scout-requests/:id'), () =>
      HttpResponse.json(scout(overrides)),
    ),
  );
}

describe('Scout Requests list — signal statistics', () => {
  it('renders the analyzed-signal count, not the invented key and not a subtraction', async () => {
    seedList();
    const screen = renderApp(<App />, { route: '/scout-requests' });
    expect(await screen.findByText(/3 opportunities · 7 signals/)).toBeInTheDocument();
  });

  it('shows an em dash rather than 0 for a scout that has never run', async () => {
    seedList({ status: 'draft', stats: {}, last_run_at: null });
    const screen = renderApp(<App />, { route: '/scout-requests' });
    expect(await screen.findByText(/— opportunities · — signals/)).toBeInTheDocument();
  });

  it('shows a genuine zero as 0', async () => {
    seedList({ stats: { scanned: 0, noise_filtered: 0, signals_analyzed: 0, opportunities: 0 } });
    const screen = renderApp(<App />, { route: '/scout-requests' });
    expect(await screen.findByText(/0 opportunities · 0 signals/)).toBeInTheDocument();
  });
});

describe('Scout Request detail — signal statistics', () => {
  it('renders the analyzed-signal count', async () => {
    seedDetail();
    const screen = renderApp(<App />, { route: '/scout-requests/scout-x' });
    expect(await screen.findByText(/3 opportunities · 7 signals/)).toBeInTheDocument();
  });

  it('shows an em dash rather than 0 for a scout that has never run', async () => {
    seedDetail({ status: 'draft', stats: {}, last_run_at: null });
    const screen = renderApp(<App />, { route: '/scout-requests/scout-x' });
    expect(await screen.findByText(/— opportunities · — signals/)).toBeInTheDocument();
  });
});

describe('Scout Request detail — counts in the header are singular only for one (W21 Q1 fix pass)', () => {
  it('formatCount: plural for 0, singular for 1, plural for 2 and for "not reported"', () => {
    expect(formatCount(0, 'opportunity', 'opportunities')).toBe('0 opportunities');
    expect(formatCount(1, 'opportunity', 'opportunities')).toBe('1 opportunity');
    expect(formatCount(2, 'opportunity', 'opportunities')).toBe('2 opportunities');
    expect(formatCount(null, 'opportunity', 'opportunities')).toBe('— opportunities');
  });

  it.each([
    [0, '0 opportunities · 0 signals'],
    [1, '1 opportunity · 1 signal'],
    [2, '2 opportunities · 2 signals'],
  ])('%i: the header reads "%s"', async (n, text) => {
    seedDetail({ stats: { scanned: n, noise_filtered: 0, signals_analyzed: n, opportunities: n } });
    const screen = renderApp(<App />, { route: '/scout-requests/scout-x' });
    const h1 = await screen.findByRole('heading', { level: 1, name: 'Dallas specialty-coffee scout' });
    expect(h1.nextElementSibling?.textContent).toMatch(new RegExp(`^${text} · `));
  });
});
