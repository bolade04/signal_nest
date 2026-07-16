import { within, type RenderResult } from '@testing-library/react';
import { delay, http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { API_PREFIX } from '@/api/config';
import { App } from '@/App';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

const P = (path: string) => `*${API_PREFIX}${path}`;
const detailRoute = (id: string) => `/opportunities/${id}`;

// The Batch 4B intelligence payload lives inside the "Signal intelligence" card.
// Scope every assertion to that card so we never accidentally match the sibling
// opportunity-detail sections (which use their own "Observed evidence" / "AI
// inference" wording and a separate "Relevance" score meter).
function panelOf(screen: RenderResult) {
  const title = screen.getByRole('heading', { name: /signal intelligence/i });
  const card = title.closest('.rounded-lg');
  if (!card) throw new Error('intelligence panel card not found');
  return within(card as HTMLElement);
}

async function renderPanel(id: string) {
  const screen = renderApp(<App />, { route: detailRoute(id) });
  // The panel is ready once its persisted content (or empty copy) has loaded.
  await screen.findByRole('heading', { name: /signal intelligence/i });
  return screen;
}

// Count intelligence requests so we can prove the feed never fans out into an
// N+1 of per-row intelligence calls.
let intelRequests: string[] = [];
function onRequestStart({ request }: { request: Request }) {
  const path = new URL(request.url).pathname;
  if (path.endsWith('/intelligence')) intelRequests.push(path);
}
beforeEach(() => {
  intelRequests = [];
  server.events.on('request:start', onRequestStart);
});
afterEach(() => server.events.removeListener('request:start', onRequestStart));

describe('opportunity intelligence panel', () => {
  it('separates observed facts from interpretation and frames inference as unverified', async () => {
    const screen = await renderPanel('opp-loc-dallas-0');
    const panel = panelOf(screen);

    // Both epistemic sections are present, labelled, and distinct.
    expect(await panel.findByRole('heading', { name: /observed facts/i })).toBeInTheDocument();
    expect(panel.getByRole('heading', { name: /interpretation/i })).toBeInTheDocument();
    expect(panel.getByText(/nothing here is interpreted/i)).toBeInTheDocument();
    expect(panel.getByText(/not verified truth/i)).toBeInTheDocument();
  });

  it('renders inferred attributes with a bounded confidence percentage', async () => {
    const screen = await renderPanel('opp-loc-dallas-0');
    const panel = panelOf(screen);

    expect(await panel.findByText('Demand Signal')).toBeInTheDocument();
    expect(panel.getByText('82% confidence')).toBeInTheDocument();
    expect(panel.getByText('71% confidence')).toBeInTheDocument();
    expect(panel.getByText('64% confidence')).toBeInTheDocument();
  });

  it('exposes the relevance and score-breakdown meters with accessible names', async () => {
    const screen = await renderPanel('opp-loc-dallas-0');
    const panel = panelOf(screen);

    await panel.findByRole('heading', { name: /observed facts/i });
    const relevance = panel.getByRole('meter', { name: /relevance score/i });
    expect(relevance).toHaveAttribute('aria-valuenow', '78');
    const total = panel.getByRole('meter', { name: /total/i });
    expect(total).toHaveAttribute('aria-valuenow', '80');
    expect(panel.getByRole('heading', { name: /score breakdown/i })).toBeInTheDocument();
  });

  it('previews a bounded set of evidence and discloses the rest on demand', async () => {
    const screen = await renderPanel('opp-loc-dallas-0');
    const panel = panelOf(screen);

    await panel.findByRole('heading', { name: /^evidence$/i });
    // Four fixture excerpts, three shown by default; the fourth is hidden.
    expect(panel.queryByText('Fourth corroborating mention.')).not.toBeInTheDocument();
    const showMore = panel.getByRole('button', { name: /show 1 more/i });
    expect(showMore).toHaveAttribute('aria-expanded', 'false');

    await screen.user.click(showMore);
    expect(panel.getByText('Fourth corroborating mention.')).toBeInTheDocument();
    const showFewer = panel.getByRole('button', { name: /show fewer/i });
    expect(showFewer).toHaveAttribute('aria-expanded', 'true');
  });

  it('keeps provenance and version behind a disclosure', async () => {
    const screen = await renderPanel('opp-loc-dallas-0');
    const panel = panelOf(screen);

    const disclosure = await panel.findByRole('button', { name: /provenance & version/i });
    expect(disclosure).toHaveAttribute('aria-expanded', 'false');
    // Version metadata is hidden until the user opts in.
    expect(panel.queryByText(/analysis version/i)).not.toBeInTheDocument();

    await screen.user.click(disclosure);
    expect(disclosure).toHaveAttribute('aria-expanded', 'true');
    expect(panel.getByText(/analysis version/i)).toBeInTheDocument();
    expect(panel.getByText('deterministic')).toBeInTheDocument();
  });

  it('shows a neutral empty state when no intelligence is persisted', async () => {
    const screen = await renderPanel('opp-loc-london-1');
    const panel = panelOf(screen);

    expect(await panel.findByText(/no intelligence analysis is available/i)).toBeInTheDocument();
    // The empty result is not an error.
    expect(panel.queryByRole('alert')).not.toBeInTheDocument();
    expect(panel.queryByRole('heading', { name: /observed facts/i })).not.toBeInTheDocument();
  });

  it('surfaces a recoverable error state when the endpoint fails', async () => {
    // A 4xx is a non-retryable client error, so the error state settles at once.
    server.use(
      http.get(P('/workspaces/:ws/opportunities/:id/intelligence'), () =>
        HttpResponse.json({}, { status: 403 }),
      ),
    );
    const screen = await renderPanel('opp-loc-dallas-0');
    const panel = panelOf(screen);

    expect(await panel.findByRole('alert')).toBeInTheDocument();
    expect(panel.getByRole('heading', { name: /unable to load/i })).toBeInTheDocument();
    expect(panel.getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });

  it('defensively clamps out-of-range scores and confidences', async () => {
    server.use(
      http.get(P('/workspaces/:ws/opportunities/:id/intelligence'), ({ params }) =>
        HttpResponse.json({
          opportunity_id: params.id,
          intelligence: {
            classification: 'validated',
            decision: 'act_now',
            is_simulated: true,
            rationale: null,
            created_at: '2026-06-01T09:00:00Z',
            facts: {
              source_type: 'rss_news',
              market: 'Dallas, TX',
              language: 'en',
              published_days_ago: 3,
              char_count: 10,
              word_count: 2,
              excerpt: 'x',
              distinct_source_types: 1,
              duplicate_count: 0,
              engagement: 0,
            },
            inference: {
              signal_type: { value: 'demand_signal', confidence: 2.5, method: 'lexical_match' },
              has_buying_intent: false,
              has_competitor_dissatisfaction: false,
            },
            relevance: {
              score: -10,
              below_action_floor: true,
              keyword_hits: [],
              pain_point_hits: [],
              audience_hits: [],
              competitor_hits: [],
            },
            score: { total: 250, classification: 'validated', version: '3b.1', factors: {} },
            evidence: [],
            provenance: { enricher: 'deterministic', analysis_version: '3b', scoring_version: '3b.1' },
            version: { analysis_version: '3b', scoring_version: '3b.1' },
          },
        }),
      ),
    );
    const screen = await renderPanel('opp-loc-dallas-0');
    const panel = panelOf(screen);

    await panel.findByRole('heading', { name: /observed facts/i });
    expect(panel.getByRole('meter', { name: /relevance score/i })).toHaveAttribute(
      'aria-valuenow',
      '0',
    );
    expect(panel.getByRole('meter', { name: /total/i })).toHaveAttribute('aria-valuenow', '100');
    expect(panel.getByText('100% confidence')).toBeInTheDocument();
  });

  it('never surfaces internal identifiers or write/feedback controls', async () => {
    const screen = await renderPanel('opp-loc-dallas-0');
    const panel = panelOf(screen);

    await panel.findByRole('heading', { name: /observed facts/i });
    // No fingerprint or raw record/opportunity id leaks into the read-only view.
    expect(panel.queryByText(/fingerprint/i)).not.toBeInTheDocument();
    expect(panel.queryByText(/opp-loc-/i)).not.toBeInTheDocument();
    // Batch 4C is strictly read-only: no approve/reject/feedback/regenerate actions.
    expect(
      panel.queryByRole('button', {
        name: /approve|reject|thumbs|feedback|helpful|regenerate|rescore|edit|delete/i,
      }),
    ).not.toBeInTheDocument();
  });

  it('announces a busy loading state before content resolves', async () => {
    server.use(
      http.get(P('/workspaces/:ws/opportunities/:id/intelligence'), async () => {
        await delay('infinite');
        return HttpResponse.json({});
      }),
    );
    const screen = renderApp(<App />, { route: detailRoute('opp-loc-dallas-0') });
    await screen.findByRole('heading', { name: /signal intelligence/i });
    const panel = panelOf(screen);
    expect(await panel.findByText(/loading intelligence/i)).toBeInTheDocument();
  });

  it('does not trigger per-row intelligence requests from the feed (no N+1)', async () => {
    const screen = renderApp(<App />, { route: '/opportunities', activeLocation: 'loc-dallas' });
    await screen.findByText(/Dallas customers want faster delivery 1/i);
    expect(intelRequests).toHaveLength(0);
  });
});

// Each market is analysed independently; the panel for one opportunity must show
// only that opportunity's market, even though the excerpt text is identical
// across all four cities (isolation cannot rely on distinct free text).
describe('opportunity intelligence panel — four-market isolation', () => {
  const cases: [string, string, string][] = [
    ['Dallas', 'opp-loc-dallas-0', 'Dallas, TX'],
    ['London', 'opp-loc-london-0', 'London, UK'],
    ['Lagos', 'opp-loc-lagos-0', 'Lagos, NG'],
    ['Nairobi', 'opp-loc-nairobi-0', 'Nairobi, KE'],
  ];
  const allMarkets = cases.map(([, , market]) => market);

  it.each(cases)('%s shows only its own market in the panel', async (_city, id, market) => {
    const screen = await renderPanel(id);
    const panel = panelOf(screen);

    expect(await panel.findByText(market)).toBeInTheDocument();
    for (const other of allMarkets) {
      if (other === market) continue;
      expect(panel.queryByText(other)).not.toBeInTheDocument();
    }
  });
});
