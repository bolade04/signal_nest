import { within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import type { JobOut, ScoutRequestOut } from '@/api/types';
import {
  runNextActions,
  runOutcomeReasons,
  runResultCopy,
  runResultLabels,
  runResultSummaries,
  sourceFailureReasons,
} from '@/lib/run-outcome';
import { runResultLines } from '@/lib/run-result';
import captured from '@/test/fixtures/scout-run-outcomes.json';
import { orgModel } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * W21 FU-Q1-1 and FU-Q1-2 in the web app (PROPOSED). Every Scout and job used here is a response
 * captured from the real API: apps/api/app/tests/test_run_outcome_guidance_w21.py produces each
 * case with the real worker and pipeline and asserts that the route returns exactly the values in
 * src/test/fixtures/scout-run-outcomes.json (LANE-BRIEF §5: a hand-written fixture can agree with
 * a bug). The page reads the typed `run_outcome` and the `RunStats` counts only.
 */
type Case = Pick<ScoutRequestOut, 'status' | 'stats' | 'run_outcome'>;
type CaseName = keyof typeof captured.cases;
// The JSON import widens the enums to `string`; the API test pins the values themselves.
const cases = captured.cases as unknown as Record<CaseName, Case>;
const jobs = captured.jobs as unknown as Record<
  keyof typeof captured.jobs,
  Pick<JobOut, 'status' | 'last_error_code' | 'result_summary' | 'outcome' | 'outcome_reason'>
>;

const P = (path: string) => `*${API_PREFIX}${path}`;
const DALLAS = 'scout-loc-dallas';

function scout(overrides: Partial<ScoutRequestOut>): ScoutRequestOut {
  return {
    id: DALLAS,
    organization_id: 'org-1',
    workspace_id: 'ws-1',
    brand_id: 'brand-1',
    location_id: null,
    campaign_id: null,
    name: 'Dallas demand scan',
    status: 'completed',
    source_types: ['rss_news'],
    keywords: ['coffee'],
    product_profile_id: null,
    resolved_market: 'Dallas, TX',
    notes: null,
    last_run_at: '2026-06-01T12:00:00Z',
    stats: {},
    coverage: null,
    execution: { state: 'idle', reason: 'none', next_action: 'run_now', since: null },
    created_at: '2026-05-01T00:00:00Z',
    updated_at: '2026-06-01T12:00:00Z',
    ...overrides,
  };
}

function jobRow(id: string, fields: (typeof jobs)[keyof typeof jobs]): JobOut {
  return {
    id,
    job_type: 'scout_request.execute',
    scout_request_id: DALLAS,
    location_id: null,
    attempt_count: 1,
    max_attempts: 3,
    priority: 0,
    scheduled_for: null,
    started_at: null,
    completed_at: '2026-06-01T12:00:00Z',
    cancel_requested_at: null,
    cancelled_at: null,
    created_at: '2026-06-01T12:00:00Z',
    updated_at: '2026-06-01T12:00:00Z',
    ...fields,
  };
}

const SCHEDULE_404 = http.get(P('/workspaces/:ws/scout-requests/:id/schedule'), () =>
  HttpResponse.json({ error: { code: 'not_found', message: 'This request has no schedule.' } }, { status: 404 }),
);

function serve(request: ScoutRequestOut, jobList: JobOut[] = []) {
  server.use(
    SCHEDULE_404,
    http.get(P('/workspaces/:ws/scout-requests/:id'), () => HttpResponse.json(request)),
    http.get(P('/workspaces/:ws/jobs'), () =>
      HttpResponse.json({ items: jobList, total: jobList.length, limit: 10, offset: 0 }),
    ),
  );
}

async function openPanel(request: ScoutRequestOut) {
  serve(request);
  const screen = renderApp(<App />, { route: `/scout-requests/${DALLAS}` });
  const panel = await screen.findByRole('region', { name: runResultCopy.heading });
  return { screen, panel };
}

/** The panel's sections as read by assistive technology: each term with its definition's lines. */
function sections(panel: HTMLElement): Record<string, string[]> {
  const terms = within(panel).getAllByRole('term');
  const definitions = within(panel).getAllByRole('definition');
  expect(definitions).toHaveLength(terms.length);
  const out: Record<string, string[]> = {};
  terms.forEach((term, i) => {
    const definition = definitions[i]!;
    expect(term.nextElementSibling).toBe(definition);
    const items = within(definition).queryAllByRole('listitem');
    out[term.textContent ?? ''] = items.length
      ? items.map((li) => li.textContent ?? '')
      : [definition.textContent ?? ''];
  });
  return out;
}

describe('the run-result lines say what ran, what did not and one next step (FU-Q1-1)', () => {
  it('complete: the counts and the source read, nothing missing, no next step', () => {
    const c = cases.complete;
    expect(runResultLines(c.run_outcome!, c.stats)).toEqual({
      ran: ['9 items collected, 7 signals analysed, 3 opportunities.', 'Read 1 source.', 'Sample library: 9 items read.'],
      didNot: [],
      next: null,
    });
  });

  it('partial, clipped: the clip is named, and nothing is asked of the customer', () => {
    const c = cases.partial_fetch_limit_reached;
    expect(runResultLines(c.run_outcome!, c.stats)).toEqual({
      ran: ['100 items collected, 100 signals analysed, 1 opportunity.', 'Read 1 source.', 'replay-big: 100 items read.'],
      didNot: ['replay-big: held more than its limit of 100 items; the rest were not read.'],
      next: runNextActions.no_action,
    });
  });

  it('partial, a failed source: the failure and its reason, and run again later', () => {
    const c = cases.partial_source_failed;
    expect(runResultLines(c.run_outcome!, c.stats)).toEqual({
      ran: ['3 items collected, 3 signals analysed, 1 opportunity.', 'Read 1 of 2 sources.', 'replay-ok: 3 items read.'],
      didNot: [`replay-bad: could not be read (${sourceFailureReasons.xml_unsafe}).`],
      next: runNextActions.run_again_later,
    });
  });

  it('refused at the bound: what was collected, the bound, and narrow the scout', () => {
    const c = cases.refused_at_bound;
    expect(runResultLines(c.run_outcome!, c.stats)).toEqual({
      ran: [
        '120 items collected; none were analysed.',
        'Read 2 sources.',
        'replay-a: 60 items read.',
        'replay-b: 60 items read.',
      ],
      didNot: ['The run was refused: its sources returned more items than one run can analyse (at most 100).'],
      next: runNextActions.narrow_request,
    });
  });

  it('an absent count reads "not reported", never a confident zero', () => {
    const c = cases.partial_source_failed;
    expect(runResultLines(c.run_outcome!, {}).ran[0]).toBe('— items collected, — signals analysed, — opportunities.');
  });

  it('every next step is something the product offers today: Edit or Run now', () => {
    expect(runNextActions.narrow_request).toMatch(/edit it/i);
    expect(runNextActions.run_again_later).toMatch(/^Run this scout again later/);
    expect(runNextActions.no_action).toMatch(/^No action needed/);
  });
});

describe('the Scout page presents each outcome (FU-Q1-1)', () => {
  beforeEach(() => orgModel.setMembership('org-1', 'user-1', 'owner'));
  afterEach(() => orgModel.reset());

  it.each([
    ['complete', 'Complete'],
    ['partial_fetch_limit_reached', 'Partial'],
    ['partial_source_failed', 'Partial'],
    ['refused_at_bound', 'Refused'],
  ] as const)('%s', async (name, label) => {
    const c = cases[name];
    const { panel } = await openPanel(scout(c));
    const kind = c.run_outcome!.kind;
    expect(runResultLabels[kind]).toBe(label);
    // A named region with a heading, a polite status line, and term/definition sections.
    expect(within(panel).getByRole('heading', { name: runResultCopy.heading })).toBeInTheDocument();
    const status = within(panel).getByRole('status');
    expect(status).toHaveTextContent(`${label}${runResultSummaries[kind]}`);
    const lines = runResultLines(c.run_outcome!, c.stats);
    const expected: Record<string, string[]> = { [runResultCopy.whatRan]: lines.ran };
    if (lines.didNot.length) expected[runResultCopy.whatDidNot] = lines.didNot;
    if (lines.next) expected[runResultCopy.nextStep] = [lines.next];
    expect(sections(panel)).toEqual(expected);
    expect(panel).toHaveAttribute('data-run-result', kind);
  });

  it('a partial run never looks like a complete one', async () => {
    const complete = await openPanel(scout(cases.complete));
    const completeText = complete.panel.textContent;
    complete.screen.unmount();
    const partial = await openPanel(scout(cases.partial_fetch_limit_reached));
    expect(partial.panel.textContent).not.toBe(completeText);
    expect(within(partial.panel).getByRole('status')).toHaveTextContent('Partial');
    expect(within(partial.panel).queryByText(runResultSummaries.complete)).not.toBeInTheDocument();
  });

  it('reads the typed run_outcome only: untyped stats keys alone show nothing', async () => {
    // The real stats of a partial run, with no run_outcome (as a pre-lift API would send).
    serve(scout({ ...cases.partial_source_failed, run_outcome: null }));
    const screen = renderApp(<App />, { route: `/scout-requests/${DALLAS}` });
    await screen.findByRole('heading', { level: 1, name: 'Dallas demand scan' });
    expect(screen.queryByRole('region', { name: runResultCopy.heading })).not.toBeInTheDocument();
    expect(screen.queryByText(/clipped|could not be read/)).not.toBeInTheDocument();
  });

  it('a run_outcome without the untyped facts in stats is still presented in full', async () => {
    const c = cases.partial_source_failed;
    const { scanned, signals_analyzed, opportunities } = c.stats as Record<string, number>;
    const { panel } = await openPanel(scout({ ...c, stats: { scanned, signals_analyzed, opportunities } }));
    expect(within(panel).getByText(`replay-bad: could not be read (${sourceFailureReasons.xml_unsafe}).`)).toBeInTheDocument();
    expect(within(panel).getByText(runNextActions.run_again_later)).toBeInTheDocument();
  });

  it('shows nothing before any result', async () => {
    serve(scout({ stats: {}, run_outcome: null, last_run_at: null, status: 'draft' }));
    const screen = renderApp(<App />, { route: `/scout-requests/${DALLAS}` });
    await screen.findByRole('heading', { level: 1, name: 'Dallas demand scan' });
    expect(screen.queryByRole('region', { name: runResultCopy.heading })).not.toBeInTheDocument();
  });
});

describe('the wait-expiry reason says only what the job recorded (FU-Q1-2)', () => {
  beforeEach(() => orgModel.setMembership('org-1', 'user-1', 'owner'));
  afterEach(() => orgModel.reset());

  it('an immediate refusal is not worded as a wait; a wait until the deadline is', async () => {
    serve(scout(cases.complete), [
      jobRow('j-now', jobs.admission_refused_at_once),
      jobRow('j-unknown', jobs.admission_waited_until_unknown_end),
      jobRow('j-deadline', jobs.admission_waited_until_deadline),
    ]);
    const screen = renderApp(<App />, { route: `/scout-requests/${DALLAS}` });
    const unresolved = await screen.findAllByText(runOutcomeReasons.earlier_operation_unresolved);
    expect(unresolved).toHaveLength(2); // nothing waited; waited until an unknown end
    expect(screen.getAllByText(runOutcomeReasons.earlier_operation_timed_out)).toHaveLength(1);
    expect(runOutcomeReasons.earlier_operation_unresolved).not.toMatch(/wait|timed out/i);
    const rows = Array.from(document.querySelectorAll('[data-job-outcome="failed"]'));
    expect(rows).toHaveLength(3);
  });
});
