import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import type { JobOut, ScoutRequestOut } from '@/api/types';
import { runOutcomeLabels, runOutcomeReasons } from '@/lib/run-outcome';
import { editAvailability, RUN_REASONS, runNowAvailability } from '@/lib/scout-actions';
import { changedFields, cleanKeywords, formOf } from '@/lib/scout-edit';
import { zeroResult } from '@/lib/zero-result';
import { orgModel } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * W16 customer-workflow repairs in the web app (PROPOSED). The API shapes used here are the
 * generated contract's (`ScoutRequestOut.execution`, `JobOut.outcome`/`outcome_reason`,
 * `OpportunityCard.seen_count`/`last_seen_at`, `RunStats.opportunities_new`/`_seen_again`).
 */
const P = (path: string) => `*${API_PREFIX}${path}`;
type Execution = NonNullable<ScoutRequestOut['execution']>;
const exec = (state: Execution['state'], reason: Execution['reason']): Execution => ({
  state,
  reason,
  next_action: 'wait',
  since: null,
});

describe('Run now and Edit availability mirror the API (W16 §B/§C)', () => {
  it.each([
    ['draft', exec('idle', 'none'), true, null],
    ['completed', exec('idle', 'none'), true, null],
    ['paused', exec('paused', 'paused_by_user'), false, RUN_REASONS.paused],
    ['failed', exec('needs_attention', 'operation_overdue'), false, RUN_REASONS.needsAttention],
    ['paused', exec('needs_attention', 'operation_outcome_unknown'), false, RUN_REASONS.needsAttention],
    ['completed', exec('in_progress', 'operation_finishing'), true, RUN_REASONS.finishing],
  ] as const)('status %s, %o: available %s', (status, execution, available, reason) => {
    const got = runNowAvailability({ status, execution });
    expect(got.available).toBe(available);
    expect(got.reason).toBe(reason);
  });

  it('labels the in-flight states as before', () => {
    expect(runNowAvailability({ status: 'queued', execution: exec('in_progress', 'run_active') }).label).toBe('Queued…');
    expect(runNowAvailability({ status: 'running', execution: exec('in_progress', 'run_active') }).label).toBe('Running…');
    expect(runNowAvailability({ status: 'draft', execution: null }, true).label).toBe('Starting…');
  });

  it('refuses Edit only while a run is queued, running or live', () => {
    expect(editAvailability({ status: 'queued', execution: null }).available).toBe(false);
    expect(editAvailability({ status: 'completed', execution: exec('in_progress', 'run_active') }).available).toBe(false);
    expect(editAvailability({ status: 'paused', execution: exec('paused', 'paused_by_user') }).available).toBe(true);
    expect(editAvailability({ status: 'failed', execution: exec('needs_attention', 'operation_overdue') }).available).toBe(true);
    expect(editAvailability({ status: 'completed', execution: exec('in_progress', 'operation_finishing') }).available).toBe(true);
  });
});

describe('the edit form sends only what changed (W16 §B)', () => {
  const original = {
    name: 'Dallas',
    resolved_market: 'Dallas, TX',
    keywords: ['oat milk'],
    source_types: ['manual', 'rss_news'],
    notes: null,
  } as unknown as ScoutRequestOut;

  it('cleans keywords like the API', () => {
    expect(cleanKeywords([' oat  milk ', '', 'OAT MILK', 'latte'])).toEqual(['oat milk', 'latte']);
  });

  it('sends nothing for an untouched form and only the changed fields otherwise', () => {
    expect(changedFields(original, formOf(original))).toEqual({});
    expect(changedFields(original, { ...formOf(original), name: '  Dallas 2 ' })).toEqual({ name: 'Dallas 2' });
    expect(changedFields(original, { ...formOf(original), resolved_market: '' })).toEqual({ resolved_market: null });
    expect(changedFields(original, { ...formOf(original), keywords: ['oat milk', ' '] })).toEqual({});
  });
});

describe('zero-result explanations say only what the run recorded (W16 §E)', () => {
  const base = { resolved_market: 'Lagos, Lagos', last_run_at: '2026-06-01T12:00:00Z' };
  const covered = { status: 'covered', reason: 'fixture_library', is_simulated: true, sources: 0, sources_failed: 0 } as const;
  it.each([
    ['not_covered', { scanned: 0, noise_filtered: 0, signals_analyzed: 0, opportunities: 0, fetch_limits: [] }, { ...covered, status: 'not_covered', reason: 'no_source_for_market' }],
    ['no_items', { scanned: 0, noise_filtered: 0, signals_analyzed: 0, opportunities: 0, fetch_limits: [{ fetched: 0 }] }, covered],
    ['items_unavailable', { scanned: 0, noise_filtered: 0, signals_analyzed: 0, opportunities: 0, fetch_limits: [{ fetched: 2 }] }, covered],
    ['all_filtered', { scanned: 3, noise_filtered: 1, signals_analyzed: 0, opportunities: 0 }, covered],
    ['none_qualified', { scanned: 3, noise_filtered: 0, signals_analyzed: 3, opportunities: 0 }, covered],
    ['all_seen_again', { scanned: 8, noise_filtered: 1, signals_analyzed: 6, opportunities: 3, opportunities_new: 0, opportunities_seen_again: 3 }, covered],
  ] as const)('%s', (code, stats, coverage) => {
    const got = zeroResult({ ...base, stats: stats as Record<string, unknown>, coverage: coverage as ScoutRequestOut['coverage'] });
    expect(got?.code).toBe(code);
  });

  it('is silent when the run made a new card, before any run, and for older runs that found cards', () => {
    const covered2 = covered as ScoutRequestOut['coverage'];
    expect(zeroResult({ ...base, stats: { scanned: 8, signals_analyzed: 6, opportunities: 3, opportunities_new: 1 }, coverage: covered2 })).toBeNull();
    expect(zeroResult({ ...base, last_run_at: null, stats: {}, coverage: null })).toBeNull();
    expect(zeroResult({ ...base, stats: { scanned: 8, signals_analyzed: 6, opportunities: 3 }, coverage: covered2 })).toBeNull();
  });

  it('counts in the singular when the last run collected one item (round 1a)', () => {
    const got = zeroResult({ ...base, stats: { scanned: 1, noise_filtered: 1, signals_analyzed: 0, opportunities: 0 }, coverage: covered as ScoutRequestOut['coverage'] });
    expect(got?.body).toBe('The last run collected 1 item, but it was filtered: 1 as noise and 0 as duplicates.');
  });

  it('names the duplicates it can derive', () => {
    const got = zeroResult({ ...base, stats: { scanned: 3, noise_filtered: 1, signals_analyzed: 0, opportunities: 0 }, coverage: covered as ScoutRequestOut['coverage'] });
    expect(got?.body).toMatch(/The last run collected 3 items, but all were filtered: 1 as noise and 2 as duplicates/);
  });

  it('every line states only what the last run recorded: past tense, no prediction, no current settings (round 1a CF1)', () => {
    const cases: [Record<string, unknown>, ScoutRequestOut['coverage']][] = [
      [{ scanned: 0, signals_analyzed: 0, opportunities: 0, fetch_limits: [] }, { ...covered, status: 'not_covered', reason: 'no_source_for_market' }],
      [{ scanned: 0, signals_analyzed: 0, opportunities: 0, fetch_limits: [] }, { ...covered, status: 'unavailable', reason: 'sources_unavailable' }],
      [{ scanned: 0, noise_filtered: 0, signals_analyzed: 0, opportunities: 0, fetch_limits: [{ fetched: 0 }] }, covered],
      [{ scanned: 0, noise_filtered: 0, signals_analyzed: 0, opportunities: 0, fetch_limits: [{ fetched: 2 }] }, covered],
      [{ scanned: 3, noise_filtered: 1, signals_analyzed: 0, opportunities: 0 }, covered],
      [{ scanned: 3, noise_filtered: 0, signals_analyzed: 3, opportunities: 0 }, covered],
      [{ scanned: 8, signals_analyzed: 6, opportunities: 3, opportunities_new: 0 }, covered],
      [{ opportunities: 0 }, null],
    ] as never;
    const seen = new Set<string>();
    for (const [stats, coverage] of cases) {
      const got = zeroResult({ ...base, stats, coverage });
      expect(got).not.toBeNull();
      seen.add(got!.code);
      const text = `${got!.title} ${got!.body}`;
      expect(text).toMatch(/last run/i);
      // No prediction or advice about a future run (the old not_covered and no_items lines).
      expect(text).not.toMatch(/\bwill\b|won't|\btry\b|running again|run again|needs a source/i);
      // Never the Scout's current settings: the run does not record the market text it used.
      expect(text).not.toContain(base.resolved_market);
      expect(text).not.toMatch(/this scout's source types/i);
    }
    expect(seen.size).toBe(8);
  });
});

describe('every run outcome has a label and every reason a sentence (W16 §D)', () => {
  it('never labels a stop or skip as completed', () => {
    for (const key of ['stopped_paused', 'skipped_paused', 'skipped', 'stopped'] as const) {
      expect(runOutcomeLabels[key]).not.toMatch(/completed|succeeded/i);
    }
    expect(Object.values(runOutcomeReasons).every((t) => t.length > 0)).toBe(true);
  });
});

describe('the pages (W16 §A–§E)', () => {
  beforeEach(() => orgModel.setMembership('org-1', 'user-1', 'owner'));
  afterEach(() => orgModel.reset());

  const SCHEDULE_404 = http.get(P('/workspaces/:ws/scout-requests/:id/schedule'), () =>
    HttpResponse.json({ error: { code: 'not_found', message: 'This request has no schedule.' } }, { status: 404 }),
  );

  it('the list shows Needs attention and Run now unavailable with its reason', async () => {
    server.use(
      http.get(P('/workspaces/:ws/scout-requests'), () =>
        HttpResponse.json([
          {
            id: 'scout-blocked',
            organization_id: 'org-1',
            workspace_id: 'ws-1',
            brand_id: 'brand-1',
            location_id: null,
            campaign_id: null,
            name: 'Blocked scout',
            status: 'failed',
            source_types: [],
            keywords: [],
            product_profile_id: null,
            resolved_market: 'Lagos, NG',
            notes: null,
            last_run_at: null,
            stats: {},
            execution: { state: 'needs_attention', reason: 'operation_overdue', next_action: 'contact_support', since: null },
            created_at: '2026-05-01T00:00:00Z',
            updated_at: '2026-06-01T12:00:00Z',
          },
        ]),
      ),
    );
    const screen = renderApp(<App />, { route: '/scout-requests' });
    const row = await screen.findByText('Blocked scout');
    const card = row.closest('[data-scout-row]') as HTMLElement;
    expect(within(card).getByText('Needs attention')).toBeInTheDocument();
    const run = await within(card).findByRole('button', { name: 'Run now' });
    expect(run).toHaveAttribute('aria-disabled', 'true');
    expect(run).toHaveAccessibleDescription(RUN_REASONS.needsAttention);
  });

  it('the jobs panel labels each outcome and keeps the raw codes secondary', async () => {
    const job = (id: string, status: string, outcome: JobOut['outcome'], reason: JobOut['outcome_reason'], summary: Record<string, unknown> | null, code: string | null) => ({
      id,
      job_type: 'scout_request.execute',
      status,
      scout_request_id: 'scout-loc-dallas',
      location_id: null,
      attempt_count: 1,
      max_attempts: 3,
      priority: 0,
      last_error_code: code,
      result_summary: summary,
      scheduled_for: null,
      started_at: null,
      completed_at: null,
      cancel_requested_at: null,
      cancelled_at: null,
      created_at: '2026-06-01T12:00:00Z',
      updated_at: '2026-06-01T12:00:00Z',
      outcome,
      outcome_reason: reason,
    });
    server.use(
      SCHEDULE_404,
      http.get(P('/workspaces/:ws/jobs'), () =>
        HttpResponse.json({
          items: [
            job('j1', 'succeeded', 'completed', null, { outcome: 'completed' }, null),
            job('j2', 'succeeded', 'stopped_paused', null, { outcome: 'stopped_request_paused' }, null),
            job('j3', 'succeeded', 'skipped_paused', null, { outcome: 'skipped_request_paused' }, null),
            job('j4', 'succeeded', 'skipped', 'another_run_active', { outcome: 'skipped_owner_active' }, null),
            job('j5', 'cancelled', 'cancelled', null, null, null),
            job('j6', 'failed', 'failed', 'earlier_operation_timed_out', null, 'non_retryable'),
          ],
          total: 6,
          limit: 10,
          offset: 0,
        }),
      ),
    );
    const screen = renderApp(<App />, { route: '/scout-requests/scout-loc-dallas' });
    await screen.findByText('Stopped — Scout paused');
    for (const text of ['Completed', 'Skipped — Scout paused', 'Skipped', 'Cancelled', 'Failed']) {
      expect(screen.getAllByText(text).length).toBeGreaterThan(0);
    }
    expect(screen.getByText(runOutcomeReasons.earlier_operation_timed_out)).toBeInTheDocument();
    expect(screen.getByText(runOutcomeReasons.another_run_active)).toBeInTheDocument();
    const raw = screen.getByText(/succeeded · stopped_request_paused/);
    expect(raw).toBeInTheDocument();
    // Round 1a (CF3): the raw codes wrap instead of being cut off at the panel's width.
    expect(raw).not.toHaveClass('truncate');
    expect(raw).toHaveClass('break-words');
    expect(screen.queryByText(/^Succeeded$/)).not.toBeInTheDocument();
  });

  it('with no cards, the last run is explained once, not twice (round 1a CF2)', async () => {
    const NOT_COVERED_BODY = "No source covered this scout's market in its last run, so it collected nothing.";
    server.use(
      SCHEDULE_404,
      http.get(P('/workspaces/:ws/opportunities'), () => HttpResponse.json([])),
      http.get(P('/workspaces/:ws/scout-requests/:id'), () =>
        HttpResponse.json({
          id: 'scout-loc-dallas',
          organization_id: 'org-1',
          workspace_id: 'ws-1',
          brand_id: 'brand-1',
          location_id: null,
          campaign_id: null,
          name: 'Uncovered scout',
          status: 'completed',
          source_types: [],
          keywords: [],
          product_profile_id: null,
          resolved_market: 'Lagos, Lagos',
          notes: null,
          last_run_at: '2026-06-01T12:00:00Z',
          stats: { scanned: 0, noise_filtered: 0, signals_analyzed: 0, opportunities: 0, fetch_limits: [] },
          coverage: { status: 'not_covered', reason: 'no_source_for_market', is_simulated: false, sources: 0, sources_failed: 0 },
          execution: { state: 'idle', reason: 'none', next_action: 'run_now', since: null },
          created_at: '2026-05-01T00:00:00Z',
          updated_at: '2026-06-01T12:00:00Z',
        }),
      ),
    );
    const screen = renderApp(<App />, { route: '/scout-requests/scout-loc-dallas' });
    await screen.findByText(/no opportunities yet/i);
    expect(screen.getAllByText(NOT_COVERED_BODY)).toHaveLength(1);
  });

  it('a card produced again says so', async () => {
    server.use(SCHEDULE_404);
    const screen = renderApp(<App />, { route: '/scout-requests/scout-loc-dallas' });
    expect(await screen.findByText(/Seen in 3 runs/)).toBeInTheDocument();
  });

  it('closing the edit dialog returns keyboard focus to Edit, never to the page body', async () => {
    server.use(SCHEDULE_404);
    const screen = renderApp(<App />, { route: '/scout-requests/scout-loc-dallas' });
    const edit = await screen.findByRole('button', { name: /^edit$/i });
    edit.focus();
    await screen.user.keyboard('{Enter}');
    const dialog = await screen.findByRole('dialog', { name: 'Edit scout' });
    await screen.user.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Edit scout' })).not.toBeInTheDocument());
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: /^edit$/i })));
  });

  it('Edit is offered to an editor, sends only the changed field, and is absent for a viewer', async () => {
    let sent: unknown = null;
    server.use(
      SCHEDULE_404,
      http.put(P('/workspaces/:ws/scout-requests/:id'), async ({ request }) => {
        sent = await request.json();
        return HttpResponse.json({ error: { code: 'scout_in_progress', message: 'This scout is queued or running.' } }, { status: 422 });
      }),
    );
    const screen = renderApp(<App />, { route: '/scout-requests/scout-loc-dallas' });
    const edit = await screen.findByRole('button', { name: /^edit$/i });
    await screen.user.click(edit);
    const dialog = await screen.findByRole('dialog', { name: 'Edit scout' });
    const name = within(dialog).getByLabelText(/^Name/);
    await screen.user.clear(name);
    await screen.user.type(name, 'Dallas renamed');
    await screen.user.click(within(dialog).getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(sent).toEqual({ name: 'Dallas renamed' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/queued or running/);
    screen.unmount();

    orgModel.setMembership('org-1', 'user-1', 'viewer');
    const viewer = renderApp(<App />, { route: '/scout-requests/scout-loc-dallas' });
    await viewer.findByRole('heading', { name: 'Dallas demand scan' });
    expect(viewer.queryByRole('button', { name: /^edit$/i })).not.toBeInTheDocument();
  });
});
