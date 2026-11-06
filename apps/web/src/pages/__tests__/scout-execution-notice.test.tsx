import { act, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { API_PREFIX } from '@/api/config';
import { executionNotice, isExecutionInProgress, type ScoutExecution } from '@/lib/scout-execution';
import { ScoutRequestDetailPage } from '@/pages/ScoutRequestDetail';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * D-25 blocked-Scout recovery (PROPOSED): the Scout page presents the API's derived
 * `execution` state, and keeps re-reading while it is `in_progress` under a settled status
 * (an earlier operation still finishing), so it reaches `needs_attention` without a reload.
 */

const WS = 'ws-1';
const REQ = 'scout-loc-dallas';
const P = (path: string) => `*${API_PREFIX}${path}`;
const DETAIL = P(`/workspaces/${WS}/scout-requests/${REQ}`);
const SCHEDULE = P(`/workspaces/${WS}/scout-requests/${REQ}/schedule`);
const OPPORTUNITIES = P(`/workspaces/${WS}/opportunities`);
const INTERVAL = 2000;
const WAIT = { timeout: 900 } as const;

let status = 'completed';
let execution: ScoutExecution | null = null;
let detailGets = 0;

const exec = (
  state: ScoutExecution['state'],
  reason: ScoutExecution['reason'],
  next_action: ScoutExecution['next_action'],
): ScoutExecution => ({ state, reason, next_action, since: null });

function row() {
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
    execution,
    created_at: '2026-05-01T00:00:00Z',
    updated_at: '2026-06-01T12:00:00Z',
  };
}

beforeEach(() => {
  status = 'completed';
  execution = null;
  detailGets = 0;
  server.use(
    http.get(DETAIL, () => {
      detailGets += 1;
      return HttpResponse.json(row());
    }),
    http.get(SCHEDULE, () =>
      HttpResponse.json({ error: { code: 'not_found', message: 'no schedule' } }, { status: 404 }),
    ),
    http.get(OPPORTUNITIES, () => HttpResponse.json([])),
  );
  vi.useFakeTimers({ shouldAdvanceTime: true, advanceTimeDelta: 1 });
});

afterEach(() => {
  vi.useRealTimers();
});

async function openDetail() {
  const screen = renderApp(
    <Routes>
      <Route path="/scout-requests/:requestId" element={<ScoutRequestDetailPage />} />
    </Routes>,
    { route: `/scout-requests/${REQ}` },
  );
  await screen.findByRole('heading', { name: /Dallas demand scan/i }, WAIT);
  return screen;
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe('executionNotice copy', () => {
  it('admission_unresolved claims neither "could not start" nor a wait (W21 FU-Q1-2 fix pass, PROPOSED)', () => {
    const n = executionNotice(exec('stopped', 'admission_unresolved', 'run_now'), 'failed');
    expect(n?.title).toBe('The last run could not continue');
    expect(n?.body).toBe(
      'The last run could not continue because an earlier operation had no confirmed outcome. ' +
        'That operation has since been confirmed as ended, so this scout can run again.',
    );
    expect(`${n?.title} ${n?.body}`).not.toMatch(/could not start|wait|timed out/i);
  });

  it('says what needs_attention means, that Resume and Run now will not help, and that findings stay', () => {
    const n = executionNotice(exec('needs_attention', 'operation_outcome_unknown', 'contact_support'), 'failed');
    expect(n?.tone).toBe('attention');
    expect(n?.title).toBe('This scout needs attention');
    expect(n?.body).toMatch(/did not report an outcome/);
    expect(n?.body).toMatch(/Resume and Run now will not restore service/);
    expect(n?.body).toMatch(/Previous findings and run history stay available/);
  });

  it.each([
    [exec('paused', 'paused_by_user', 'resume'), 'paused', /Paused/],
    [exec('stopped', 'run_cancelled', 'run_now'), 'draft', /last run was cancelled/],
    [exec('stopped', 'run_failed', 'run_now'), 'failed', /did not complete/],
    [exec('stopped', 'admission_unresolved', 'run_now'), 'failed', /could not continue/],
    [exec('stopped', 'run_interrupted', 'contact_support'), 'queued', /interrupted/],
    [exec('in_progress', 'operation_finishing', 'wait'), 'draft', /still finishing/],
  ] as const)('%o under %s has its own notice', (e, s, title) => {
    expect(executionNotice(e, s)?.title).toMatch(title);
  });

  it.each([
    [exec('idle', 'none', 'run_now'), 'completed'],
    [exec('in_progress', 'run_active', 'wait'), 'running'],
    [null, 'completed'],
  ] as const)('%o shows no notice', (e, s) => {
    expect(executionNotice(e, s)).toBeNull();
  });

  it('is in progress only for the in_progress state', () => {
    expect(isExecutionInProgress({ execution: exec('in_progress', 'operation_finishing', 'wait') })).toBe(true);
    expect(isExecutionInProgress({ execution: exec('needs_attention', 'operation_overdue', 'contact_support') })).toBe(false);
    expect(isExecutionInProgress({ execution: null })).toBe(false);
  });
});

describe('the Scout page', () => {
  it('shows the needs-attention notice with findings and history still reachable', async () => {
    status = 'failed';
    execution = exec('needs_attention', 'operation_outcome_unknown', 'contact_support');
    const screen = await openDetail();
    const title = await screen.findByText('This scout needs attention', {}, WAIT);
    const region = title.closest('[role="status"]');
    expect(region).toHaveAttribute('data-execution-state', 'needs_attention');
    expect(region).toHaveTextContent('Resume and Run now will not restore service');
    expect(screen.getByRole('heading', { name: /Opportunities from this scout/i })).toBeInTheDocument();
  });

  it('keeps re-reading while execution is in progress under a settled status, then stops', async () => {
    status = 'draft';
    execution = exec('in_progress', 'operation_finishing', 'wait');
    const screen = await openDetail();
    expect(screen.getByText('An earlier operation is still finishing')).toBeInTheDocument();
    const first = detailGets;

    // The earlier operation's deadline passes: only the page's own timer can see this.
    execution = exec('needs_attention', 'operation_overdue', 'contact_support');
    await advance(INTERVAL);
    await waitFor(() => expect(screen.getByText('This scout needs attention')).toBeInTheDocument(), WAIT);
    expect(detailGets).toBe(first + 1);

    // Not in progress any more: no further reads.
    const atAttention = detailGets;
    await advance(20 * INTERVAL);
    expect(detailGets).toBe(atAttention);
  });

  it('does not poll an idle settled Scout (unchanged behaviour)', async () => {
    status = 'completed';
    execution = exec('idle', 'none', 'run_now');
    await openDetail();
    const first = detailGets;
    await advance(10 * INTERVAL);
    expect(detailGets).toBe(first);
  });
});
