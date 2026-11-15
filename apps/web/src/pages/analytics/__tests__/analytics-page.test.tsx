import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import type { WorkspaceAnalytics } from '@/api/types';
import { AnalyticsPage } from '@/pages/analytics/AnalyticsPage';
import { analyticsFixture, orgModel, type ModelRole } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

// Project Phase 5E (lane 5E-W4): the status-analytics page (5E-W2's AnalyticsPage).
//
// - A figure the API could not compute is `null` and renders as "—" with "Not enough data
//   yet", never "0", "0%" or "0 s"; a real zero still renders as zero.
// - Viewer and compliance reviewer get the refused state and send no reflection and no
//   analytics request; an API 403 for anyone renders the same refused state.
// - While the O-12(B) reflection is not literally `{enabled: true}` (off, a truthy non-boolean,
//   failed) the page shows its dark state and sends no analytics request; an API 503 renders
//   the same dark state.
// - A location the workspace does not have (404) has its own state; any other failure is the
//   error state with a retry.

const P = (path: string) => `*${API_PREFIX}${path}`;
const READERS: ModelRole[] = ['owner', 'admin', 'marketer', 'reviewer'];
const REFUSED: ModelRole[] = ['viewer', 'compliance_reviewer'];
const REFUSED_TITLE = 'Status analytics is not available to your role';
const DARK_TITLE = 'Status analytics is not available for this workspace yet';

interface Traffic {
  reflection: number;
  analytics: URL[];
}

function backend({
  reflection = { status: 200, body: { enabled: true } as unknown },
  analytics,
}: {
  reflection?: { status: number; body: unknown };
  analytics?: { status: number; body: unknown } | ((ws: string) => WorkspaceAnalytics);
} = {}): Traffic {
  const traffic: Traffic = { reflection: 0, analytics: [] };
  server.use(
    http.get(P('/workspaces/:ws/notifications-analytics-capability'), () => {
      traffic.reflection += 1;
      return HttpResponse.json(reflection.body as object, { status: reflection.status });
    }),
    http.get(P('/workspaces/:ws/analytics'), ({ request, params }) => {
      traffic.analytics.push(new URL(request.url));
      const ws = String(params.ws);
      if (typeof analytics === 'function') return HttpResponse.json(analytics(ws));
      const answer = analytics ?? { status: 200, body: analyticsFixture(ws) };
      return HttpResponse.json(answer.body as object, { status: answer.status });
    }),
  );
  return traffic;
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 60));
const apiError = (code: string) => ({ error: { code, message: 'SERVER TEXT', request_id: 'r' } });

/** The row of the latency table headed `subject`. */
function latencyRow(screen: ReturnType<typeof renderApp>, subject: 'Briefs' | 'Drafts') {
  const table = screen.getByRole('table', { name: 'Approval latency for briefs and drafts' });
  return within(table).getByRole('rowheader', { name: subject }).closest('tr')!;
}

beforeEach(() => orgModel.reset());

describe('analytics page — figures', () => {
  it('renders every family, and a null figure as the dash with words, never zero', async () => {
    backend();
    const screen = renderApp(<AnalyticsPage />);
    expect(await screen.findByRole('heading', { name: 'Counts by state' })).toBeInTheDocument();
    for (const title of [
      'Opportunities',
      'Briefs',
      'Approval decisions',
      'Drafts',
      'Content reviews',
      'Exports',
    ]) {
      expect(screen.getAllByText(title).length).toBeGreaterThan(0);
    }

    // Briefs have a basis (3 decided): real durations. Drafts have none: both are null.
    const briefs = latencyRow(screen, 'Briefs');
    expect(within(briefs).getByText('1 h')).toBeInTheDocument();
    expect(within(briefs).getByText('1 h 30 min')).toBeInTheDocument();
    expect(within(briefs).queryByText('Not enough data yet')).not.toBeInTheDocument();
    const drafts = latencyRow(screen, 'Drafts');
    expect(within(drafts).getAllByText('—')).toHaveLength(2);
    expect(within(drafts).getAllByText('Not enough data yet')).toHaveLength(2);
    expect(within(drafts).getByText('0')).toBeInTheDocument(); // `decided` is a count: 0

    // Both review rates are null in the fixture.
    const passed = screen.getByText('Reviews passed').closest('div')!;
    const blocked = screen.getByText('Reviews blocked').closest('div')!;
    for (const row of [passed, blocked]) {
      expect(within(row).getByText('—')).toBeInTheDocument();
      expect(within(row).getByText('Not enough data yet')).toBeInTheDocument();
    }
    expect(screen.queryByText('0%')).not.toBeInTheDocument();
    expect(screen.queryByText('0 s')).not.toBeInTheDocument();
    expect(screen.getByText(/A dash \(—\) marks a figure there is not enough data/)).toBeInTheDocument();
    expect(screen.getByText(/advertising performance is not measured here/)).toBeInTheDocument();
  });

  it('a real zero is shown as zero, not as the dash', async () => {
    backend({
      analytics: (ws) => ({
        ...analyticsFixture(ws),
        approval_latency: {
          briefs: { decided: 1, mean_seconds: 0, median_seconds: 0 },
          drafts: { decided: 1, mean_seconds: 45, median_seconds: 45 },
        },
        outcomes: {
          drafts_generated: 2,
          revisions: 0,
          simulated: 2,
          review_pass_rate: 0,
          review_block_rate: 1,
        },
      }),
    });
    const screen = renderApp(<AnalyticsPage />);
    await screen.findByRole('heading', { name: 'Counts by state' });
    expect(within(latencyRow(screen, 'Briefs')).getAllByText('0 s')).toHaveLength(2);
    expect(within(latencyRow(screen, 'Drafts')).getAllByText('45 s')).toHaveLength(2);
    const passed = screen.getByText('Reviews passed').closest('div')!;
    expect(within(passed).getByText('0%')).toBeInTheDocument();
    expect(within(screen.getByText('Reviews blocked').closest('div')!).getByText('100%')).toBeInTheDocument();
    expect(screen.queryByText('Not enough data yet')).not.toBeInTheDocument();
  });

  it('lists each location, and the "No location" row only when the API sends one', async () => {
    backend();
    const first = renderApp(<AnalyticsPage />);
    const table = await first.findByRole('table', { name: 'Totals by location' });
    expect(within(table).getByText('Dallas')).toBeInTheDocument();
    expect(within(table).getByText('No location')).toBeInTheDocument();
    first.unmount();

    backend({ analytics: (ws) => ({ ...analyticsFixture(ws), location_id: 'loc-dallas', unassigned: null }) });
    const second = renderApp(<AnalyticsPage />, { activeLocation: 'loc-dallas' });
    const located = await second.findByRole('table', { name: 'Totals by location' });
    expect(within(located).getByText('Dallas')).toBeInTheDocument();
    expect(within(located).queryByText('No location')).not.toBeInTheDocument();
  });

  it('a location view asks for that location only', async () => {
    const traffic = backend();
    renderApp(<AnalyticsPage />, { activeLocation: 'loc-dallas' });
    await waitFor(() => expect(traffic.analytics).toHaveLength(1));
    expect(traffic.analytics[0]!.searchParams.get('location_id')).toBe('loc-dallas');
  });

  it.each(READERS)('%s reads the figures', async (role) => {
    orgModel.setMembership('org-1', 'user-1', role);
    const traffic = backend();
    const screen = renderApp(<AnalyticsPage />);
    expect(await screen.findByRole('heading', { name: 'Counts by state' })).toBeInTheDocument();
    expect(traffic.analytics).toHaveLength(1);
  });
});

describe('analytics page — refused (403)', () => {
  it.each(REFUSED)('%s gets the refused state and sends no request at all', async (role) => {
    orgModel.setMembership('org-1', 'user-1', role);
    const traffic = backend();
    const screen = renderApp(<AnalyticsPage />);
    expect(await screen.findByText(REFUSED_TITLE)).toBeInTheDocument();
    await settle();
    expect(traffic.reflection).toBe(0);
    expect(traffic.analytics).toEqual([]);
    expect(screen.queryByRole('heading', { name: 'Counts by state' })).not.toBeInTheDocument();
  });

  it('an API 403 for a reader renders the refused state, not an error', async () => {
    backend({ analytics: { status: 403, body: apiError('permission_denied') } });
    const screen = renderApp(<AnalyticsPage />);
    expect(await screen.findByText(REFUSED_TITLE)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByText('SERVER TEXT')).not.toBeInTheDocument();
  });
});

describe('analytics page — dark (O-12(B))', () => {
  it.each([
    ['off', { status: 200, body: { enabled: false } }],
    ['the string "true"', { status: 200, body: { enabled: 'true' } }],
    ['the number 1', { status: 200, body: { enabled: 1 } }],
    ['failed', { status: 500, body: apiError('internal') }],
  ] as const)('a reflection that is %s shows the dark state and sends no analytics request', async (_n, reflection) => {
    const traffic = backend({ reflection });
    const screen = renderApp(<AnalyticsPage />);
    expect(await screen.findByText(DARK_TITLE)).toBeInTheDocument();
    await settle();
    expect(traffic.reflection).toBeGreaterThan(0);
    expect(traffic.analytics).toEqual([]);
  });

  it('the shipped default (MSW dark) sends no analytics request', async () => {
    const seen: string[] = [];
    const record = ({ request }: { request: Request }) => {
      seen.push(new URL(request.url).pathname);
    };
    server.events.on('request:start', record);
    try {
      const screen = renderApp(<AnalyticsPage />);
      expect(await screen.findByText(DARK_TITLE)).toBeInTheDocument();
      await settle();
      expect(seen.filter((p) => p.endsWith('/analytics'))).toEqual([]);
    } finally {
      server.events.removeListener('request:start', record);
    }
  });

  it('an API 503 renders the same dark state', async () => {
    backend({ analytics: { status: 503, body: apiError('capability_unavailable') } });
    const screen = renderApp(<AnalyticsPage />);
    expect(await screen.findByText(DARK_TITLE)).toBeInTheDocument();
    expect(screen.queryByText('SERVER TEXT')).not.toBeInTheDocument();
  });

  it('the /analytics route shows the dark state by default, from the shell', async () => {
    const screen = renderApp(<App />, { route: '/analytics' });
    expect(await screen.findByText(DARK_TITLE)).toBeInTheDocument();
  });
});

describe('analytics page — other answers', () => {
  it('a location outside the workspace (404) has its own state', async () => {
    backend({ analytics: { status: 404, body: apiError('not_found') } });
    const screen = renderApp(<AnalyticsPage />, { activeLocation: 'loc-dallas' });
    expect(await screen.findByText('This location is not part of the workspace')).toBeInTheDocument();
  });

  it('any other failure is the error state, and the retry asks again', async () => {
    // A 422 is not retried by the hook (deterministic), so the state shows at once.
    const traffic = backend({ analytics: { status: 422, body: apiError('validation_error') } });
    const screen = renderApp(<AnalyticsPage />);
    const alert = await screen.findByRole('alert');
    const before = traffic.analytics.length;
    await screen.user.click(within(alert).getByRole('button', { name: /Try again/ }));
    await waitFor(() => expect(traffic.analytics.length).toBeGreaterThan(before));
  });
});
