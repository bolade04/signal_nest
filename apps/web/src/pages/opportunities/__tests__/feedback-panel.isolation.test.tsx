import { fireEvent, within as domWithin, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { useState } from 'react';
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
const CAPABILITIES = P('/system/capabilities');

// The panel only mounts its data hooks once the runtime-capability reflection
// reports the feature enabled — mirror that precondition in every test here.
function enableCapability(enabled = true) {
  server.use(
    http.get(CAPABILITIES, () =>
      HttpResponse.json({
        app_mode: 'local',
        environment: 'development',
        is_local_mode: true,
        all_configured: true,
        features: { opportunity_feedback_enabled: enabled },
      }),
    ),
  );
}

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

// Mirrors the production parent (IntelligencePanel), which mounts a single panel
// keyed by ``intelligence_record_id``. Switching the bound record therefore fully
// remounts the panel — the strongest stale-context guarantee. A button lets a
// test flip the active market mid-interaction while keeping one QueryClient.
function MarketSwitcher() {
  const [market, setMarket] = useState<(typeof MARKETS)[keyof typeof MARKETS]>(MARKETS.dallas);
  return (
    <div>
      <button type="button" onClick={() => setMarket(MARKETS.london)}>
        switch-to-london
      </button>
      <div data-testid="active-panel">
        <OpportunityFeedbackPanel
          key={market.rec}
          workspaceId={WS}
          opportunityId={market.opp}
          intelligenceRecordId={market.rec}
        />
      </div>
    </div>
  );
}

describe('OpportunityFeedbackPanel isolation (3C-D)', () => {
  it('renders each market with only its own feedback history', async () => {
    // Each market carries a uniquely identifiable reason so any cross-market
    // leak would be visible.
    enableCapability(true);
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
    enableCapability(true);
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

  it('discards a pending verdict and posts nothing when the bound record switches while the dialog is open', async () => {
    enableCapability(true);
    let dallasPost = 0;
    let londonPost = 0;
    server.use(
      http.get(feedbackPath(MARKETS.dallas.opp), () => page([])),
      http.get(feedbackPath(MARKETS.london.opp), () => page([])),
      http.post(feedbackPath(MARKETS.dallas.opp), () => {
        dallasPost += 1;
        return HttpResponse.json(feedbackRow(MARKETS.dallas), { status: 201 });
      }),
      http.post(feedbackPath(MARKETS.london.opp), () => {
        londonPost += 1;
        return HttpResponse.json(feedbackRow(MARKETS.london), { status: 201 });
      }),
    );

    const screen = renderApp(<MarketSwitcher />, { route: '/opportunities' });

    // Open the verdict dialog on Dallas, then switch the bound record to London.
    await screen.user.click(await screen.findByRole('button', { name: /^useful$/i }));
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    // A modal dialog locks the background (pointer-events: none), so the record
    // switch here models a *programmatic* rebind — e.g. a background intelligence
    // refresh changing the parent's record id — not a user background click.
    fireEvent.click(screen.getByRole('button', { name: /switch-to-london/i, hidden: true }));

    // The record change remounts the panel: the pending verdict is discarded, so
    // the stale dialog cannot submit against the newly-bound record.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(await screen.findByRole('button', { name: /^useful$/i })).toBeInTheDocument();
    expect(dallasPost).toBe(0);
    expect(londonPost).toBe(0);
  });

  it('resolves a submission pending across a record switch into only its own scope', async () => {
    enableCapability(true);
    let dallasCreated = false;
    let resolvePost!: () => void;
    const gate = new Promise<void>((r) => {
      resolvePost = r;
    });
    server.use(
      http.get(feedbackPath(MARKETS.dallas.opp), () =>
        page(dallasCreated ? [feedbackRow(MARKETS.dallas, { reason_code: 'useful_insight' })] : []),
      ),
      http.post(feedbackPath(MARKETS.dallas.opp), async () => {
        await gate;
        dallasCreated = true;
        return HttpResponse.json(feedbackRow(MARKETS.dallas), { status: 201 });
      }),
      http.get(feedbackPath(MARKETS.london.opp), () => page([])),
      http.post(feedbackPath(MARKETS.london.opp), () => {
        throw new Error('London feedback POST should never be called');
      }),
    );

    const screen = renderApp(<MarketSwitcher />, { route: '/opportunities' });

    await screen.user.click(await screen.findByRole('button', { name: /^useful$/i }));
    await screen.user.click(await screen.findByRole('button', { name: /submit feedback/i }));
    // Submission is in flight against Dallas (the modal dialog stays open and
    // locks the background); the record is rebound programmatically to London.
    fireEvent.click(screen.getByRole('button', { name: /switch-to-london/i, hidden: true }));
    expect(await screen.findByText(/no feedback recorded yet/i)).toBeInTheDocument();

    // Let the in-flight Dallas write complete: its onSuccess invalidates only the
    // Dallas-scoped query key, so London's now-visible history is never touched.
    resolvePost();
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.getByText(/no feedback recorded yet/i)).toBeInTheDocument();
    expect(screen.queryByText('Useful insight')).not.toBeInTheDocument();
  });

  it('never lands a slow feedback response into the newly-bound record view', async () => {
    enableCapability(true);
    let resolveDallas!: () => void;
    const gate = new Promise<void>((r) => {
      resolveDallas = r;
    });
    server.use(
      http.get(feedbackPath(MARKETS.dallas.opp), async () => {
        await gate;
        return page([feedbackRow(MARKETS.dallas, { reason_code: 'useful_insight' })]);
      }),
      http.get(feedbackPath(MARKETS.london.opp), () =>
        page([feedbackRow(MARKETS.london, { is_useful: false, reason_code: 'wrong_market' })]),
      ),
    );

    const screen = renderApp(<MarketSwitcher />, { route: '/opportunities' });

    // Dallas's history GET is still pending; switch to London before it resolves.
    await screen.user.click(await screen.findByRole('button', { name: /switch-to-london/i }));
    expect(await screen.findByText('Wrong market')).toBeInTheDocument();

    // The stale Dallas response resolves into its own record-keyed cache entry,
    // which nothing renders — it can never appear in the London view.
    resolveDallas();
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByText('Useful insight')).not.toBeInTheDocument();
    expect(screen.getByText('Wrong market')).toBeInTheDocument();
  });

  it('unmounting while a submission is pending issues exactly one POST and does not throw', async () => {
    enableCapability(true);
    let postCount = 0;
    let resolvePost!: () => void;
    const gate = new Promise<void>((r) => {
      resolvePost = r;
    });
    server.use(
      http.get(feedbackPath(MARKETS.dallas.opp), () => page([])),
      http.post(feedbackPath(MARKETS.dallas.opp), async () => {
        postCount += 1;
        await gate;
        return HttpResponse.json(feedbackRow(MARKETS.dallas), { status: 201 });
      }),
    );

    const screen = renderApp(
      <div data-testid="panel-dallas">
        <OpportunityFeedbackPanel
          workspaceId={WS}
          opportunityId={MARKETS.dallas.opp}
          intelligenceRecordId={MARKETS.dallas.rec}
        />
      </div>,
      { route: '/opportunities' },
    );

    const dallas = within(screen, 'panel-dallas');
    await screen.user.click(await dallas.findByRole('button', { name: /^useful$/i }));
    await screen.user.click(await screen.findByRole('button', { name: /submit feedback/i }));

    // Tear the whole tree down mid-flight, then let the write complete.
    screen.unmount();
    resolvePost();
    await new Promise((r) => setTimeout(r, 0));
    // Exactly one append-only write was issued; unmounting mid-flight is inert.
    expect(postCount).toBe(1);
  });
});
