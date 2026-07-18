import { within as domWithin } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { API_PREFIX } from '@/api/config';
import { SchedulePanel } from '@/pages/scouts/SchedulePanel';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

// SB-D: prove per-request (per-market) isolation in the UI. Two SchedulePanels
// for different scout requests share one QueryClient, but their schedule caches
// are keyed by requestId, so each panel must reflect only its own market's
// state and a lifecycle action on one must not disturb the other.
const WS = 'ws-1';
const P = (path: string) => `*${API_PREFIX}${path}`;
const schedulePath = (req: string) => P(`/workspaces/${WS}/scout-requests/${req}/schedule`);

// Four independent markets, mirroring the backend worker-integration coverage.
const MARKETS = {
  dallas: 'scout-loc-dallas',
  london: 'scout-loc-london',
  lagos: 'scout-loc-lagos',
  nairobi: 'scout-loc-nairobi',
} as const;

function scheduleRow(req: string, overrides: Record<string, unknown> = {}) {
  return {
    id: `sched-${req}`,
    scout_request_id: req,
    location_id: req.replace(/^scout-/, ''),
    interval: 'daily',
    state: 'active',
    enabled: true,
    next_run_at: '2026-07-18T09:00:00Z',
    last_tick_at: '2026-07-17T09:00:00Z',
    created_at: '2026-07-17T08:00:00Z',
    updated_at: '2026-07-17T09:00:00Z',
    ...overrides,
  };
}

const notFound = () =>
  HttpResponse.json({ error: { code: 'not_found', message: 'no schedule' } }, { status: 404 });

describe('SchedulePanel isolation (SB-D)', () => {
  it('renders each market with its own independent state', async () => {
    server.use(
      http.get(schedulePath(MARKETS.dallas), () => HttpResponse.json(scheduleRow(MARKETS.dallas))),
      http.get(schedulePath(MARKETS.london), () =>
        HttpResponse.json(scheduleRow(MARKETS.london, { state: 'paused', enabled: false })),
      ),
      http.get(schedulePath(MARKETS.lagos), () =>
        HttpResponse.json(scheduleRow(MARKETS.lagos, { state: 'activation_required', enabled: true })),
      ),
      http.get(schedulePath(MARKETS.nairobi), notFound),
    );

    const screen = renderApp(
      <div>
        <div data-testid="panel-dallas">
          <SchedulePanel workspaceId={WS} requestId={MARKETS.dallas} />
        </div>
        <div data-testid="panel-london">
          <SchedulePanel workspaceId={WS} requestId={MARKETS.london} />
        </div>
        <div data-testid="panel-lagos">
          <SchedulePanel workspaceId={WS} requestId={MARKETS.lagos} />
        </div>
        <div data-testid="panel-nairobi">
          <SchedulePanel workspaceId={WS} requestId={MARKETS.nairobi} />
        </div>
      </div>,
      { route: '/scouts' },
    );

    // Each market lands in its own derived state, resolved from its own query.
    const dallas = within(screen, 'panel-dallas');
    const london = within(screen, 'panel-london');
    const lagos = within(screen, 'panel-lagos');
    const nairobi = within(screen, 'panel-nairobi');

    expect(await dallas.findByText('Active')).toBeInTheDocument();
    expect(await london.findByText('Paused')).toBeInTheDocument();
    expect(await lagos.findByText('Activation required')).toBeInTheDocument();
    // Nairobi has no schedule (404) → offers creation instead of a state badge.
    expect(await nairobi.findByRole('button', { name: /schedule daily/i })).toBeInTheDocument();

    // No cross-contamination: an active market does not leak a paused badge, etc.
    expect(dallas.queryByText('Paused')).not.toBeInTheDocument();
    expect(london.queryByText('Active')).not.toBeInTheDocument();
  });

  it('keeps a lifecycle action scoped to the acting market', async () => {
    let dallasState = 'active';
    server.use(
      http.get(schedulePath(MARKETS.dallas), () =>
        HttpResponse.json(scheduleRow(MARKETS.dallas, { state: dallasState })),
      ),
      http.post(`${schedulePath(MARKETS.dallas)}/pause`, () => {
        dallasState = 'paused';
        return HttpResponse.json(scheduleRow(MARKETS.dallas, { state: 'paused', enabled: false }));
      }),
      // London must never receive a pause call and stays active throughout.
      http.get(schedulePath(MARKETS.london), () => HttpResponse.json(scheduleRow(MARKETS.london))),
      http.post(`${schedulePath(MARKETS.london)}/pause`, () => {
        throw new Error('London pause should never be called');
      }),
    );

    const screen = renderApp(
      <div>
        <div data-testid="panel-dallas">
          <SchedulePanel workspaceId={WS} requestId={MARKETS.dallas} />
        </div>
        <div data-testid="panel-london">
          <SchedulePanel workspaceId={WS} requestId={MARKETS.london} />
        </div>
      </div>,
      { route: '/scouts' },
    );

    const dallas = within(screen, 'panel-dallas');
    const london = within(screen, 'panel-london');

    expect(await dallas.findByText('Active')).toBeInTheDocument();
    expect(await london.findByText('Active')).toBeInTheDocument();

    await screen.user.click(await dallas.findByRole('button', { name: /pause/i }));

    // Dallas transitions to Paused after its own refetch...
    expect(await dallas.findByText('Paused')).toBeInTheDocument();
    // ...while London remains Active and still offers Pause (untouched).
    expect(london.getByText('Active')).toBeInTheDocument();
    expect(london.getByRole('button', { name: /pause/i })).toBeInTheDocument();
  });
});

// Scope queries to a single panel's subtree so same-named badges/buttons in
// sibling panels never collide.
function within(screen: ReturnType<typeof renderApp>, testId: string) {
  return domWithin(screen.getByTestId(testId));
}
