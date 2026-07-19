import { within as domWithin } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { API_PREFIX } from '@/api/config';
import { OpportunityFeedbackPanel } from '@/pages/opportunities/FeedbackPanel';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

// 3C-D: prove per-opportunity (per-market) isolation of the feedback UI. Four
// panels for four markets share one QueryClient, but each is keyed by its own
// intelligence record id, so a panel must reflect only its own market's history
// and a submission on one must never touch another's endpoint.
const WS = 'ws-1';
const P = (path: string) => `*${API_PREFIX}${path}`;
const feedbackPath = (opp: string) => P(`/workspaces/${WS}/opportunities/${opp}/feedback`);

// Four independent markets, mirroring the backend worker-integration coverage.
const MARKETS = {
  dallas: { opp: 'opp-loc-dallas-0', rec: 'rec-opp-loc-dallas-0' },
  london: { opp: 'opp-loc-london-0', rec: 'rec-opp-loc-london-0' },
  lagos: { opp: 'opp-loc-lagos-0', rec: 'rec-opp-loc-lagos-0' },
  nairobi: { opp: 'opp-loc-nairobi-0', rec: 'rec-opp-loc-nairobi-0' },
} as const;

function feedbackRow(
  market: (typeof MARKETS)[keyof typeof MARKETS],
  overrides: Record<string, unknown> = {},
) {
  return {
    id: `fb-${market.rec}`,
    opportunity_id: market.opp,
    intelligence_record_id: market.rec,
    is_useful: true,
    reason_code: null,
    submitted_by_user_id: 'user-1',
    analysis_version: '3b',
    scoring_version: '3b.1',
    created_at: '2026-07-17T09:00:00Z',
    ...overrides,
  };
}

function page(items: Array<Record<string, unknown>>) {
  return HttpResponse.json({ items, total: items.length, limit: 20, offset: 0 });
}

function renderMarkets() {
  return renderApp(
    <div>
      {Object.entries(MARKETS).map(([name, m]) => (
        <div key={name} data-testid={`panel-${name}`}>
          <OpportunityFeedbackPanel workspaceId={WS} opportunityId={m.opp} intelligenceRecordId={m.rec} />
        </div>
      ))}
    </div>,
    { route: '/opportunities' },
  );
}

function within(screen: ReturnType<typeof renderApp>, testId: string) {
  return domWithin(screen.getByTestId(testId));
}

describe('OpportunityFeedbackPanel isolation (3C-D)', () => {
  it('renders each market with only its own feedback history', async () => {
    // Each market carries a uniquely identifiable reason so any cross-market
    // leak would be visible.
    server.use(
      http.get(feedbackPath(MARKETS.dallas.opp), () =>
        page([feedbackRow(MARKETS.dallas, { is_useful: true, reason_code: 'useful_insight' })]),
      ),
      http.get(feedbackPath(MARKETS.london.opp), () =>
        page([feedbackRow(MARKETS.london, { is_useful: false, reason_code: 'wrong_market' })]),
      ),
      http.get(feedbackPath(MARKETS.lagos.opp), () =>
        page([feedbackRow(MARKETS.lagos, { is_useful: true, reason_code: 'strong_evidence' })]),
      ),
      http.get(feedbackPath(MARKETS.nairobi.opp), () =>
        page([feedbackRow(MARKETS.nairobi, { is_useful: false, reason_code: 'outdated' })]),
      ),
    );

    const screen = renderMarkets();
    const dallas = within(screen, 'panel-dallas');
    const london = within(screen, 'panel-london');
    const lagos = within(screen, 'panel-lagos');
    const nairobi = within(screen, 'panel-nairobi');

    // Each panel resolves to its own market's reason from its own scoped query.
    expect(await dallas.findByText('Useful insight')).toBeInTheDocument();
    expect(await london.findByText('Wrong market')).toBeInTheDocument();
    expect(await lagos.findByText('Strong evidence')).toBeInTheDocument();
    expect(await nairobi.findByText('Outdated')).toBeInTheDocument();

    // No cross-contamination: one market's reason never appears in another.
    expect(dallas.queryByText('Wrong market')).not.toBeInTheDocument();
    expect(london.queryByText('Useful insight')).not.toBeInTheDocument();
    expect(lagos.queryByText('Outdated')).not.toBeInTheDocument();
    expect(nairobi.queryByText('Strong evidence')).not.toBeInTheDocument();
  });

  it('keeps a submission scoped to the acting market', async () => {
    let dallasCreated = false;
    server.use(
      http.get(feedbackPath(MARKETS.dallas.opp), () =>
        page(dallasCreated ? [feedbackRow(MARKETS.dallas, { reason_code: 'useful_insight' })] : []),
      ),
      http.post(feedbackPath(MARKETS.dallas.opp), async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        // The submission carries Dallas's own record id, never another market's.
        expect(body.intelligence_record_id).toBe(MARKETS.dallas.rec);
        dallasCreated = true;
        return HttpResponse.json(feedbackRow(MARKETS.dallas), { status: 201 });
      }),
      // London stays empty and must never receive a POST.
      http.get(feedbackPath(MARKETS.london.opp), () => page([])),
      http.post(feedbackPath(MARKETS.london.opp), () => {
        throw new Error('London feedback POST should never be called');
      }),
    );

    const screen = renderApp(
      <div>
        <div data-testid="panel-dallas">
          <OpportunityFeedbackPanel
            workspaceId={WS}
            opportunityId={MARKETS.dallas.opp}
            intelligenceRecordId={MARKETS.dallas.rec}
          />
        </div>
        <div data-testid="panel-london">
          <OpportunityFeedbackPanel
            workspaceId={WS}
            opportunityId={MARKETS.london.opp}
            intelligenceRecordId={MARKETS.london.rec}
          />
        </div>
      </div>,
      { route: '/opportunities' },
    );

    const dallas = within(screen, 'panel-dallas');
    const london = within(screen, 'panel-london');

    // Both start with an empty history.
    expect(await dallas.findByText(/no feedback recorded yet/i)).toBeInTheDocument();
    expect(await london.findByText(/no feedback recorded yet/i)).toBeInTheDocument();

    // Submit on Dallas only. The verdict button lives in Dallas's subtree; the
    // dialog is portaled to the document body, so its submit is queried globally.
    await screen.user.click(await dallas.findByRole('button', { name: /^useful$/i }));
    await screen.user.click(await screen.findByRole('button', { name: /submit feedback/i }));

    // Dallas records the entry; London remains untouched and empty.
    expect(await dallas.findByText('Useful insight')).toBeInTheDocument();
    expect(london.getByText(/no feedback recorded yet/i)).toBeInTheDocument();
    expect(london.queryByText('Useful insight')).not.toBeInTheDocument();
  });
});
