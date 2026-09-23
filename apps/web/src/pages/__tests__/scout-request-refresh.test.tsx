import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { act, waitFor, within, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { useEffect } from 'react';
import { Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { API_PREFIX } from '@/api/config';
import { queryKeys } from '@/api/queryKeys';
import { ScoutRequestDetailPage } from '@/pages/ScoutRequestDetail';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * P6-UI-018 — the scout detail view must re-read itself while a run is in
 * flight, and must refresh this scout's opportunities on the transient →
 * terminal transition.
 *
 * The run endpoint answers `queued` synchronously and a durable worker settles
 * the request later; nothing pushes that change to the client. Every assertion
 * below therefore has to be reached by the component's OWN timer — no manual
 * rerender, refetch(), invalidateQueries from test code, remount or reload
 * appears anywhere in this file, because any of those would pass against the
 * unfixed component.
 */

const WS = 'ws-1';
const REQ = 'scout-loc-dallas';
const P = (path: string) => `*${API_PREFIX}${path}`;
const DETAIL = P(`/workspaces/${WS}/scout-requests/${REQ}`);
const RUN = P(`/workspaces/${WS}/scout-requests/${REQ}/run`);
const SCHEDULE = P(`/workspaces/${WS}/scout-requests/${REQ}/schedule`);
const OPPORTUNITIES = P(`/workspaces/${WS}/opportunities`);

// The component's poll interval; assertions advance in exact multiples of it.
const INTERVAL = 2000;
// Every waitFor budget must stay strictly BELOW one interval: with
// `shouldAdvanceTime` the fake clock tracks real time, so a waiting assertion
// spends interval budget and could otherwise let a poll fire for the wrong reason.
const WAIT = { timeout: 900 } as const;

// ---- test-owned backend state -------------------------------------------------
// Driven by a named phase, never by call index: the post-mutation invalidation
// costs more than one detail GET (useScoutActions invalidates both the list key
// and the detail key, and the list key is a PREFIX of the detail key), so an
// index-driven fixture would let that burst consume the intermediate phases and
// reach "Running" with no timer advancement at all — a false pass.
let phase = 'completed';
let oppPhase: 'pre' | 'post' = 'pre';
let detailGets = 0;
let oppGets = 0;

function scoutRow(status: string) {
  return {
    id: REQ,
    organization_id: 'org-1',
    workspace_id: WS,
    brand_id: 'brand-1',
    location_id: 'loc-dallas',
    campaign_id: null,
    name: 'Dallas demand scan',
    status,
    source_types: ['manual'],
    keywords: ['dallas'],
    product_profile_id: null,
    resolved_market: 'Dallas, TX',
    notes: null,
    last_run_at: '2026-06-01T12:00:00Z',
    stats: { scanned: 24, noise_filtered: 4, signals_analyzed: 20, opportunities: 2 },
    created_at: '2026-05-01T00:00:00Z',
    updated_at: '2026-06-01T12:00:00Z',
  };
}

function opportunityRow(id: string, title: string) {
  return {
    id,
    title,
    classification: 'validated',
    decision: 'act_now',
    opportunity_score: 80,
    confidence_score: 70,
    confidence_level: 'high',
    priority_score: 75,
    relevance_score: 78,
    risk_level: 'low',
    resolved_market: 'Dallas, TX',
    inside_scout_area: true,
    why_it_matters: 'People in Dallas are actively discussing this need.',
    recommended_action: 'Publish a locally targeted response.',
    audience_fit: 'Time-poor urban households',
    urgency: 'High',
    commercial_value: 'Medium',
    source_summary: ['manual'],
    status: 'new',
    location_id: 'loc-dallas',
    campaign_id: null,
    scout_request_id: REQ,
    is_simulated: true,
    created_at: '2026-06-01T09:00:00Z',
  };
}

const PRE_RUN_OPP = opportunityRow('opp-pre-1', 'Pre-run Dallas opportunity');
const POST_RUN_OPP = opportunityRow('opp-post-1', 'Post-run Dallas opportunity');

function installHandlers() {
  server.use(
    http.get(DETAIL, () => {
      detailGets += 1;
      return HttpResponse.json(scoutRow(phase));
    }),
    http.post(RUN, () => {
      // The backend answers with the queued status it has just written.
      phase = 'queued';
      return HttpResponse.json({
        scout_request_id: REQ,
        status: 'queued',
        stats: { job_id: 'job-1', job_status: 'pending' },
      });
    }),
    // The shared handler set has no schedule route and setup.ts runs MSW with
    // onUnhandledRequest: 'error', so the SchedulePanel's read must be answered
    // here even though the capability renders dark.
    http.get(SCHEDULE, () =>
      HttpResponse.json({ error: { code: 'not_found', message: 'no schedule' } }, { status: 404 }),
    ),
    http.get(OPPORTUNITIES, () => {
      oppGets += 1;
      return HttpResponse.json([oppPhase === 'post' ? POST_RUN_OPP : PRE_RUN_OPP]);
    }),
  );
}

/** Read-only cache probe (getQueryData only — invalidating from a test is forbidden). */
let cacheClient: QueryClient | null = null;
function CacheProbe() {
  const qc = useQueryClient();
  useEffect(() => {
    cacheClient = qc;
    // Identity-checked so a probe only ever retires its OWN binding.
    return () => {
      if (cacheClient === qc) cacheClient = null;
    };
  }, [qc]);
  return null;
}

function renderDetail() {
  return renderApp(
    <>
      <CacheProbe />
      <Routes>
        <Route path="/scout-requests/:requestId" element={<ScoutRequestDetailPage />} />
      </Routes>
    </>,
    { route: `/scout-requests/${REQ}` },
  );
}

/**
 * Status assertions are scoped to the page header. JobsPanel renders the static
 * help text "Running this scout enqueues a durable background job", so an
 * unscoped /running/i would match copy that never changes.
 */
function header(screen: RenderResult) {
  return within(screen.getByRole('heading', { name: /Dallas demand scan/i }).parentElement!);
}

async function openDetail() {
  const screen = renderDetail();
  await screen.findByRole('heading', { name: /Dallas demand scan/i }, WAIT);
  return screen;
}

/** Waits until the detail GET count stops moving, so a burst can be subtracted. */
async function settleDetailGets() {
  let previous = -1;
  await waitFor(
    () => {
      if (detailGets !== previous) {
        previous = detailGets;
        throw new Error(`detail GET count still moving (${detailGets})`);
      }
    },
    { ...WAIT, interval: 50 },
  );
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  phase = 'completed';
  oppPhase = 'pre';
  detailGets = 0;
  oppGets = 0;
  installHandlers();
  // A bare vi.useFakeTimers() deadlocks RTL waitFor and userEvent; the real
  // clock has to keep driving the fake one.
  vi.useFakeTimers({ shouldAdvanceTime: true, advanceTimeDelta: 1 });
});

afterEach(() => {
  vi.useRealTimers();
  cacheClient = null;
});

describe('scout request detail auto-refresh', () => {
  it('follows a queued run to completion without any user action', async () => {
    phase = 'completed';
    const screen = await openDetail();

    // Positive control: the subject is alive and the counter is wired.
    expect(header(screen).getByText('Completed')).toBeInTheDocument();
    expect(detailGets).toBe(1);

    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(screen.getByRole('button', { name: /run now/i }));

    await waitFor(() => expect(header(screen).getByText('Queued')).toBeInTheDocument(), WAIT);
    await settleDetailGets();
    const afterInvalidation = detailGets;

    // The worker starts the job. Only the component's own timer can see this.
    phase = 'running';
    await advance(INTERVAL);
    await waitFor(() => expect(header(screen).getByText('Running')).toBeInTheDocument(), WAIT);
    expect(detailGets).toBe(afterInvalidation + 1);

    // The worker finishes.
    phase = 'completed';
    await advance(INTERVAL);
    await waitFor(() => expect(header(screen).getByText('Completed')).toBeInTheDocument(), WAIT);
    expect(detailGets).toBe(afterInvalidation + 2);

    // A6 — the refreshed value landed under this workspace's key only.
    const cached = cacheClient?.getQueryData(queryKeys.scoutRequest(WS, REQ)) as
      | { status: string }
      | undefined;
    expect(cached?.status).toBe('completed');
    expect(cacheClient?.getQueryData(queryKeys.scoutRequest('ws-other', REQ))).toBeUndefined();
  });

  it('stops polling once the request reaches a terminal status', async () => {
    phase = 'running';
    const screen = await openDetail();
    expect(header(screen).getByText('Running')).toBeInTheDocument();

    phase = 'completed';
    await advance(INTERVAL);
    // Positive control for the negative assertion below: without this, "no
    // further GETs" would also hold for a component that never polled at all.
    await waitFor(() => expect(header(screen).getByText('Completed')).toBeInTheDocument(), WAIT);

    const atTerminal = detailGets;
    await advance(100 * INTERVAL);
    expect(detailGets).toBe(atTerminal);
  });

  it('does not poll or re-invalidate a request that is already terminal on first render', async () => {
    phase = 'completed';
    const screen = await openDetail();
    expect(header(screen).getByText('Completed')).toBeInTheDocument();
    expect(detailGets).toBe(1);

    await advance(5 * INTERVAL);
    expect(detailGets).toBe(1);
    // "Terminal" is not the trigger — the transient → terminal TRANSITION is.
    // A request already completed on arrival never transitioned, so the
    // opportunity grid must not be re-read at all.
    expect(oppGets).toBe(1);
  });

  // An unconstrained String(40) column reaches this component, so the in-flight
  // set has to be an explicit allowlist. Under "anything that is not completed"
  // each of these would poll forever.
  it.each(['draft', 'paused', 'failed', 'some_future_status'])(
    'does not poll a request whose status is %s',
    async (status) => {
      phase = status;
      await openDetail();
      // Positive control: the page mounted and read the request exactly once.
      expect(detailGets).toBe(1);

      await advance(5 * INTERVAL);
      expect(detailGets).toBe(1);
    },
  );

  it('polls a request that is already running on first render', async () => {
    // Isolation control: without this test, deleting the mechanism and dropping
    // `queued` from the in-flight set would fail at the SAME assertion and be
    // indistinguishable.
    phase = 'running';
    const screen = await openDetail();
    expect(header(screen).getByText('Running')).toBeInTheDocument();

    const baseline = detailGets;
    await advance(INTERVAL);
    await waitFor(() => expect(detailGets).toBe(baseline + 1), WAIT);
  });

  it('refreshes this scout opportunities when the run reaches a terminal status', async () => {
    phase = 'queued';
    oppPhase = 'pre';
    const screen = await openDetail();
    expect(header(screen).getByText('Queued')).toBeInTheDocument();
    expect(await screen.findByText('Pre-run Dallas opportunity', undefined, WAIT)).toBeInTheDocument();
    const oppGetsBefore = oppGets;

    // The worker wrote opportunities and then settled the request.
    oppPhase = 'post';
    phase = 'completed';
    await advance(INTERVAL);

    await waitFor(() => expect(header(screen).getByText('Completed')).toBeInTheDocument(), WAIT);
    // Automatic: nothing in this test touched the opportunities query.
    await waitFor(
      () => expect(screen.getByText('Post-run Dallas opportunity')).toBeInTheDocument(),
      WAIT,
    );
    expect(screen.queryByText('Pre-run Dallas opportunity')).not.toBeInTheDocument();
    expect(oppGets).toBe(oppGetsBefore + 1);
  });
});
