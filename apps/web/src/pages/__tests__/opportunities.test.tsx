import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { orgModel, pagedJson } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

describe('opportunity feed', () => {
  it('renders scored opportunity cards for the workspace', async () => {
    const screen = renderApp(<App />, { route: '/opportunities' });
    expect(await screen.findByText(/Dallas customers want faster delivery 1/i)).toBeInTheDocument();
    // Human-readable score labels appear alongside the raw numbers.
    expect(screen.getAllByText(/Strong|Moderate|Fair|Low/).length).toBeGreaterThan(0);
    // Simulated data must be clearly labelled.
    expect(screen.getAllByText(/Simulated/i).length).toBeGreaterThan(0);
  });

  it('keeps each location strictly isolated — Dallas shows only Dallas', async () => {
    const screen = renderApp(<App />, { route: '/opportunities', activeLocation: 'loc-dallas' });
    await screen.findByText(/Dallas customers want faster delivery 1/i);
    expect(screen.queryByText(/London customers/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Lagos customers/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Nairobi customers/i)).not.toBeInTheDocument();
  });

  it('keeps each location strictly isolated — London shows only London', async () => {
    const screen = renderApp(<App />, { route: '/opportunities', activeLocation: 'loc-london' });
    await screen.findByText(/London customers want faster delivery 1/i);
    expect(screen.queryByText(/Dallas customers/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Lagos customers/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Nairobi customers/i)).not.toBeInTheDocument();
  });

  it('clears the search box when the active location changes', async () => {
    // Regression guard for the WorkspaceContext/Opportunities state migration:
    // switching markets must reset per-market filter state so one location's
    // search never leaks into another's feed.
    const screen = renderApp(<App />, { route: '/opportunities', activeLocation: 'loc-dallas' });
    await screen.findByText(/Dallas customers want faster delivery 1/i);
    // Scope to the page body: the shell mounts only after the session resolves.
    const main = within(screen.container.querySelector('#main-content') as HTMLElement);

    // The page filter search shares its label with the header global search, so
    // scope to the main region.
    const search = main.getByLabelText(/search opportunities/i);
    await screen.user.type(search, 'faster');
    expect(search).toHaveValue('faster');

    // The switchers are duplicated for desktop + mobile; drive the first one.
    const locationFilter = screen.getAllByRole('combobox', { name: /location filter/i })[0]!;
    await screen.user.click(locationFilter);
    const listbox = await screen.findByRole('listbox');
    await screen.user.click(within(listbox).getByRole('option', { name: /London/i }));

    // The search draft resets on the location switch, and only London shows.
    await waitFor(() => expect(search).toHaveValue(''));
    await main.findByText(/London customers want faster delivery 1/i);
    expect(main.queryByText(/Dallas customers/i)).not.toBeInTheDocument();
  });

  it('filters the feed by classification', async () => {
    const screen = renderApp(<App />, { route: '/opportunities', activeLocation: 'loc-dallas' });
    await screen.findByText(/Dallas customers want faster delivery 1/i);
    // Both a validated (1) and an early (2) card are present initially.
    expect(screen.getByText(/Dallas customers want faster delivery 2/i)).toBeInTheDocument();

    const filter = screen.getByRole('combobox', { name: /filter by classification/i });
    await screen.user.click(filter);
    const listbox = await screen.findByRole('listbox');
    await screen.user.click(within(listbox).getByRole('option', { name: /^Validated$/i }));

    await waitFor(() =>
      expect(screen.queryByText(/Dallas customers want faster delivery 2/i)).not.toBeInTheDocument(),
    );
    expect(screen.getByText(/Dallas customers want faster delivery 1/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------------
// P6-UI-021 / P6-UI-022 / P6-UI-004 and the sort-key contract
// ---------------------------------------------------------------------------------

const P = (path: string) => `*${API_PREFIX}${path}`;

/** The newest element (no `Array.prototype.at`: the workspace targets ES2021). */
const lastOf = <T,>(items: T[]): T | undefined => items[items.length - 1];

/** Feed requests that reached the default handlers (the tenant gate records them). */
function feedRequests(): URL[] {
  return orgModel
    .requests()
    .filter((r) => r.method === 'GET' && r.path.endsWith('/workspaces/ws-1/opportunities'))
    .map((r) => new URL(`http://x${r.path}${r.search}`));
}

/** `count` synthetic opportunities, highest score first, all in Dallas. */
function manyOpportunities(count: number) {
  return Array.from({ length: count }, (_, i) => ({
    id: `opp-many-${String(i).padStart(3, '0')}`,
    title: `Bulk opportunity ${String(i).padStart(3, '0')}`,
    classification: 'validated',
    decision: 'monitor',
    opportunity_score: 90,
    confidence_score: 60,
    confidence_level: 'medium',
    priority_score: 50,
    relevance_score: 60,
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

describe('header global search while already on Opportunities (P6-UI-021)', () => {
  beforeEach(() => orgModel.reset());

  const headerSearch = (screen: ReturnType<typeof renderApp>) =>
    within(screen.getByRole('search')).getByLabelText(/search opportunities/i);
  const pageSearch = (screen: ReturnType<typeof renderApp>) =>
    within(screen.container.querySelector('#main-content') as HTMLElement).getByLabelText(
      /search opportunities/i,
    );

  it('applies a search submitted from the header without remounting the page', async () => {
    const screen = renderApp(<App />, { route: '/opportunities' });
    await screen.findByText(/Dallas customers want faster delivery 1/i);

    await screen.user.type(headerSearch(screen), 'London{Enter}');

    // The page's own box takes the term, the feed is searched for it, and the URL keeps it.
    await waitFor(() => expect(pageSearch(screen)).toHaveValue('London'));
    await waitFor(() =>
      expect(screen.queryByText(/Dallas customers want faster delivery 1/i)).not.toBeInTheDocument(),
    );
    expect(screen.getByText(/London customers want faster delivery 1/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(feedRequests().some((u) => u.searchParams.get('search') === 'London')).toBe(true),
    );
  });

  it('replaces a term typed on the page, and is not rewritten back by the page', async () => {
    const screen = renderApp(<App />, { route: '/opportunities?search=Lagos' });
    await screen.findByText(/Lagos customers want faster delivery 1/i);
    expect(pageSearch(screen)).toHaveValue('Lagos');

    await screen.user.type(headerSearch(screen), 'Nairobi{Enter}');

    await waitFor(() => expect(pageSearch(screen)).toHaveValue('Nairobi'));
    await screen.findByText(/Nairobi customers want faster delivery 1/i);
    // Settled: the last feed request is the header's term, not the page's earlier one.
    await waitFor(() => expect(lastOf(feedRequests())?.searchParams.get('search')).toBe('Nairobi'));
    expect(screen.queryByText(/Lagos customers/i)).not.toBeInTheDocument();
  });
});

describe('feed paging and truthful count (P6-UI-022)', () => {
  beforeEach(() => orgModel.reset());

  it('reports the total, not the page length, and loads the rest on demand', async () => {
    const rows = manyOpportunities(150);
    server.use(http.get(P('/workspaces/:ws/opportunities'), ({ request }) => pagedJson(rows, new URL(request.url))));
    const screen = renderApp(<App />, { route: '/opportunities' });

    expect(await screen.findByText('Showing 100 of 150 opportunities')).toBeInTheDocument();
    expect(screen.getAllByText(/^Bulk opportunity \d{3}$/)).toHaveLength(100);

    await screen.user.click(screen.getByRole('button', { name: /^load more$/i }));

    expect(await screen.findByText('150 opportunities')).toBeInTheDocument();
    expect(screen.getAllByText(/^Bulk opportunity \d{3}$/)).toHaveLength(150);
    expect(screen.queryByRole('button', { name: /^load more$/i })).not.toBeInTheDocument();
  });

  it('never presents a page as the whole result when the total is not reported', async () => {
    const rows = manyOpportunities(120);
    server.use(
      http.get(P('/workspaces/:ws/opportunities'), ({ request }) => {
        const url = new URL(request.url);
        const start = url.searchParams.get('cursor') ? 100 : 0;
        const headers: Record<string, string> = start === 0 ? { 'X-Next-Cursor': 'c-100' } : {};
        return HttpResponse.json(rows.slice(start, start + 100), { headers });
      }),
    );
    const screen = renderApp(<App />, { route: '/opportunities' });
    expect(await screen.findByText('Showing the first 100 opportunities')).toBeInTheDocument();
    await screen.user.click(screen.getByRole('button', { name: /^load more$/i }));
    expect(await screen.findByText('120 opportunities')).toBeInTheDocument();
  });

  it('sends the cursor it was given and keeps the filters for the next page', async () => {
    // An override answers before the recording tenant gate, so it keeps its own log.
    const seen: URL[] = [];
    server.use(
      http.get(P('/workspaces/:ws/opportunities'), ({ request }) => {
        seen.push(new URL(request.url));
        return pagedJson(manyOpportunities(130), new URL(request.url));
      }),
    );
    const screen = renderApp(<App />, { route: '/opportunities?search=Bulk' });
    await screen.findByText('Showing 100 of 130 opportunities');
    await screen.user.click(screen.getByRole('button', { name: /^load more$/i }));
    await screen.findByText('130 opportunities');
    const [first, second] = seen.filter((u) => u.searchParams.get('search') === 'Bulk').slice(-2);
    expect(first!.searchParams.get('cursor')).toBeNull();
    expect(second!.searchParams.get('cursor')).toBe('fixture-cursor-100');
    for (const key of ['sort', 'order', 'limit', 'search']) {
      expect(second!.searchParams.get(key)).toBe(first!.searchParams.get(key));
    }
  });
});

describe('sort options use the API sort keys', () => {
  beforeEach(() => orgModel.reset());

  it('sends a key the API accepts for every option', async () => {
    const screen = renderApp(<App />, { route: '/opportunities' });
    await screen.findByText(/Dallas customers want faster delivery 1/i);
    expect(lastOf(feedRequests())?.searchParams.get('sort')).toBe('opportunity');

    const expected: Record<string, [string, string]> = {
      'Lowest score': ['opportunity', 'asc'],
      'Most relevant': ['relevance', 'desc'],
      'Most confident': ['confidence', 'desc'],
      'Highest priority': ['priority', 'desc'],
      Newest: ['created', 'desc'],
      Oldest: ['created', 'asc'],
    };
    for (const [label, [sort, order]] of Object.entries(expected)) {
      await screen.user.click(screen.getByRole('combobox', { name: /^sort$/i }));
      await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: label }));
      await waitFor(() => {
        const last = lastOf(feedRequests())!;
        expect([last.searchParams.get('sort'), last.searchParams.get('order')]).toEqual([sort, order]);
      });
      // The fixture answers an unknown key with 422, like the API: the feed still renders.
      expect(screen.queryByText(/request validation failed/i)).not.toBeInTheDocument();
    }
  });
});

describe('empty feed says why (P6-UI-004)', () => {
  beforeEach(() => orgModel.reset());

  const scout = (over: Record<string, unknown>) => ({
    id: 'scout-x',
    organization_id: 'org-1',
    workspace_id: 'ws-1',
    brand_id: 'brand-1',
    location_id: 'loc-dallas',
    campaign_id: null,
    name: 'Berlin bakery scan',
    status: 'draft',
    source_types: [],
    keywords: [],
    product_profile_id: null,
    resolved_market: 'Berlin, DE',
    notes: null,
    last_run_at: null,
    stats: {},
    created_at: '2026-05-01T00:00:00Z',
    updated_at: '2026-05-01T00:00:00Z',
    ...over,
  });
  const withScouts = (scouts: unknown[]) =>
    server.use(
      http.get(P('/workspaces/:ws/opportunities'), ({ request }) => pagedJson([], new URL(request.url))),
      http.get(P('/workspaces/:ws/scout-requests'), () => HttpResponse.json(scouts)),
    );
  const OLD_ADVICE = /run a scout request to generate/i;

  it('no scout at all', async () => {
    withScouts([]);
    const screen = renderApp(<App />, { route: '/opportunities' });
    expect(await screen.findByText('No scouts yet')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /go to scout requests/i })).toHaveAttribute('href', '/scout-requests');
  });

  it('a scout that has never run', async () => {
    withScouts([scout({})]);
    const screen = renderApp(<App />, { route: '/opportunities' });
    expect(await screen.findByText('Your scout has not run yet')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /open scout request/i })).toHaveAttribute('href', '/scout-requests/scout-x');
  });

  it('a scout that is running', async () => {
    withScouts([scout({ status: 'running' })]);
    const screen = renderApp(<App />, { route: '/opportunities' });
    expect(await screen.findByText('A scout is running')).toBeInTheDocument();
    expect(screen.queryByText(OLD_ADVICE)).not.toBeInTheDocument();
  });

  it('a scout whose last run failed (a failure records no last_run_at)', async () => {
    withScouts([scout({ status: 'failed', updated_at: '2026-06-02T00:00:00Z' })]);
    const screen = renderApp(<App />, { route: '/opportunities' });
    expect(await screen.findByText('The last scout run failed')).toBeInTheDocument();
  });

  it('a scout that ran and found no signals: never told to run a scout', async () => {
    withScouts([
      scout({
        status: 'completed',
        last_run_at: '2026-06-01T12:00:00Z',
        stats: { scanned: 0, noise_filtered: 0, signals_analyzed: 0, opportunities: 0 },
      }),
    ]);
    const screen = renderApp(<App />, { route: '/opportunities' });
    expect(await screen.findByText('Your scout ran but found no opportunities')).toBeInTheDocument();
    expect(screen.getByText(/It found no signals to analyse\./)).toBeInTheDocument();
    expect(screen.queryByText(OLD_ADVICE)).not.toBeInTheDocument();
    expect(screen.queryByText(/not covered/i)).not.toBeInTheDocument(); // P6-DATA-2's to say
  });

  it('a scout that ran, analysed signals, and none qualified', async () => {
    withScouts([
      scout({
        status: 'completed',
        last_run_at: '2026-06-01T12:00:00Z',
        stats: { scanned: 9, noise_filtered: 2, signals_analyzed: 7, opportunities: 0 },
      }),
    ]);
    const screen = renderApp(<App />, { route: '/opportunities' });
    expect(await screen.findByText(/It analysed 7 signals; none qualified as an opportunity\./)).toBeInTheDocument();
  });

  it("counts only the active location's scouts", async () => {
    withScouts([scout({ location_id: 'loc-london', status: 'completed', last_run_at: '2026-06-01T12:00:00Z' })]);
    const screen = renderApp(<App />, { route: '/opportunities', activeLocation: 'loc-dallas' });
    expect(await screen.findByText('No scouts yet')).toBeInTheDocument();
    expect(screen.getByText(/Create a scout request for Dallas/)).toBeInTheDocument();
  });

  it('keeps the filter-specific message when filters are applied', async () => {
    withScouts([]);
    const screen = renderApp(<App />, { route: '/opportunities?search=zzz' });
    expect(await screen.findByText('No matches')).toBeInTheDocument();
    expect(screen.queryByText('No scouts yet')).not.toBeInTheDocument();
  });
});
