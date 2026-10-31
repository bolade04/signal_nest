import { act, fireEvent, waitFor, within, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { getResponse, http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { handlers } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * UI-AUDIT F8 / P8: "Run now" stayed enabled while the request was queued, and a rapid
 * double click sent two POSTs (rendered at 7a183a12: statuses 200 then 422, P6-W10-UIFIX-01
 * raw/016). The list page never re-read a queued request, so its row stayed "Queued".
 *
 * The backend here is stateful and refuses a run as apps/api/app/scouting_requests/routes.py
 * does (messages as captured from the local API): 422 "Request is already queued or running."
 * while queued/running, 422 "Resume the request before running it." while paused. Status
 * changes only through the test's `phase` (the "worker"); every later assertion must be
 * reached by the page's own polling, never by test code refetching.
 */

const REQ = 'scout-loc-dallas';
const NAME = 'Dallas demand scan';
const P = (path: string) => `*${API_PREFIX}${path}`;
const INTERVAL = 2000;
const WAIT = { timeout: 900 } as const;

let phase = 'completed';
let posts = 0;
let failNextPost: number | null = null;

function refuse(message: string) {
  return HttpResponse.json({ error: { code: 'validation_error', message, request_id: 'req-run' } }, { status: 422 });
}

function installBackend({ emptyOpportunities = false } = {}) {
  server.use(
    http.get(P('/workspaces/:ws/scout-requests'), async ({ request }) => {
      const rows = (await (await getResponse(handlers, request))!.json()) as Array<Record<string, unknown>>;
      return HttpResponse.json(rows.map((r) => (r.id === REQ ? { ...r, status: phase } : r)));
    }),
    http.get(P('/workspaces/:ws/scout-requests/:id'), async ({ request, params }) => {
      const row = (await (await getResponse(handlers, request))!.json()) as Record<string, unknown>;
      return HttpResponse.json(params.id === REQ ? { ...row, status: phase } : row);
    }),
    http.post(P('/workspaces/:ws/scout-requests/:id/run'), ({ params }) => {
      posts += 1;
      if (failNextPost !== null) {
        const status = failNextPost;
        failNextPost = null;
        return HttpResponse.json({ error: { code: 'internal_error', message: 'Temporarily unavailable.', request_id: 'req-run' } }, { status });
      }
      if (phase === 'queued' || phase === 'running') return refuse('Request is already queued or running.');
      if (phase === 'paused') return refuse('Resume the request before running it.');
      phase = 'queued';
      return HttpResponse.json({ scout_request_id: String(params.id), status: 'queued', stats: { job_id: `job-${posts}`, job_status: 'pending' } });
    }),
    http.get(P('/workspaces/:ws/scout-requests/:id/schedule'), () =>
      HttpResponse.json({ error: { code: 'not_found', message: 'no schedule' } }, { status: 404 }),
    ),
    ...(emptyOpportunities ? [http.get(P('/workspaces/:ws/opportunities'), () => HttpResponse.json([], { headers: { 'X-Total-Count': '0' } }))] : []),
  );
}

/** Busy is `aria-disabled` and stays focusable; never native `disabled` (W11 A1, review F-01). */
function expectBusy(el: HTMLElement) {
  expect(el).toHaveAttribute('aria-disabled', 'true');
  expect(el).not.toBeDisabled();
}
function expectAvailable(el: HTMLElement) {
  expect(el).not.toHaveAttribute('aria-disabled');
  expect(el).toBeEnabled();
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** The page header of the detail view (status badge row + title). */
function header(screen: RenderResult) {
  return within(screen.getByRole('heading', { level: 1, name: NAME }).parentElement!);
}

/** The Run control(s), whatever they are labelled right now. */
function runButtons(scope: { queryAllByRole: RenderResult['queryAllByRole'] }) {
  return scope.queryAllByRole('button', { name: /^(run now|starting…|queued…|running…)$/i });
}

/** The list row (card) of the Dallas scout. */
function dallasRow(screen: RenderResult) {
  return within(screen.getByRole('link', { name: NAME }).closest('.rounded-lg') as HTMLElement);
}

async function openDetail() {
  const screen = renderApp(<App />, { route: `/scout-requests/${REQ}` });
  await screen.findByRole('heading', { level: 1, name: NAME }, WAIT);
  return screen;
}

beforeEach(() => {
  phase = 'completed';
  posts = 0;
  failNextPost = null;
  vi.useFakeTimers({ shouldAdvanceTime: true, advanceTimeDelta: 1 });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('Run now while a run is queued or running (UI-AUDIT P8)', () => {
  it('a rapid double click sends exactly one POST', async () => {
    installBackend();
    const screen = await openDetail();
    const [run] = runButtons(screen);
    expect(run).toHaveAccessibleName('Run now');
    // Two clicks in the same task, before React can re-render the button disabled.
    fireEvent.click(run!);
    fireEvent.click(run!);
    await waitFor(() => expect(header(screen).getByText('Queued')).toBeInTheDocument(), WAIT);
    expect(posts).toBe(1);
  });

  it('detail page: disabled and labelled while queued, then running; usable again when the run completes', async () => {
    installBackend();
    const screen = await openDetail();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(runButtons(screen)[0]!);

    await waitFor(() => expect(header(screen).getByText('Queued')).toBeInTheDocument(), WAIT);
    const queued = await screen.findByRole('button', { name: 'Queued…' }, WAIT);
    expectBusy(queued);
    expect(queued).toHaveAttribute('aria-busy', 'true');
    await user.click(queued);
    expect(posts).toBe(1);

    phase = 'running';
    await advance(INTERVAL);
    const running = await screen.findByRole('button', { name: 'Running…' }, WAIT);
    expectBusy(running);

    phase = 'completed';
    await advance(INTERVAL);
    await waitFor(() => expect(header(screen).getByText('Completed')).toBeInTheDocument(), WAIT);
    const again = screen.getByRole('button', { name: 'Run now' });
    expectAvailable(again);
    await user.click(again);
    await waitFor(() => expect(posts).toBe(2), WAIT);
  });

  it('detail page: the empty-state Run now follows the same state', async () => {
    installBackend({ emptyOpportunities: true });
    phase = 'queued';
    const screen = await openDetail();
    await screen.findByText('No opportunities yet', undefined, WAIT);
    const buttons = runButtons(screen);
    expect(buttons).toHaveLength(2);
    for (const button of buttons) {
      expect(button).toHaveAccessibleName('Queued…');
      expectBusy(button);
    }
  });

  it('list page: the row follows the run to completion without a reload, and its button with it', async () => {
    installBackend();
    const screen = renderApp(<App />, { route: '/scout-requests' });
    await screen.findByRole('link', { name: NAME }, WAIT);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(runButtons(dallasRow(screen))[0]!);

    await waitFor(() => expect(dallasRow(screen).getByText('Queued')).toBeInTheDocument(), WAIT);
    expectBusy(dallasRow(screen).getByRole('button', { name: 'Queued…' }));
    // Other rows are untouched.
    const london = within(screen.getByRole('link', { name: 'London demand scan' }).closest('.rounded-lg') as HTMLElement);
    expect(runButtons(london)[0]).toHaveAccessibleName('Run now');

    phase = 'running';
    await advance(INTERVAL);
    await waitFor(() => expectBusy(dallasRow(screen).getByRole('button', { name: 'Running…' })), WAIT);

    phase = 'completed';
    await advance(INTERVAL);
    await waitFor(() => expect(dallasRow(screen).getByText('Completed')).toBeInTheDocument(), WAIT);
    expectAvailable(dallasRow(screen).getByRole('button', { name: 'Run now' }));
    expect(posts).toBe(1);
  });

  it('a failed POST leaves the button usable, and a retry is sent', async () => {
    installBackend();
    failNextPost = 500;
    const screen = await openDetail();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(runButtons(screen)[0]!);
    expect(await screen.findByText('Could not queue scout', undefined, WAIT)).toBeInTheDocument();
    await waitFor(() => expectAvailable(screen.getByRole('button', { name: 'Run now' })), WAIT);
    await user.click(screen.getByRole('button', { name: 'Run now' }));
    await waitFor(() => expect(header(screen).getByText('Queued')).toBeInTheDocument(), WAIT);
    expect(posts).toBe(2);
  });

  it('a failed run leaves the button usable', async () => {
    installBackend();
    phase = 'queued';
    const screen = await openDetail();
    expectBusy(screen.getByRole('button', { name: 'Queued…' }));
    phase = 'failed';
    await advance(INTERVAL);
    await waitFor(() => expect(header(screen).getByText('Failed')).toBeInTheDocument(), WAIT);
    expectAvailable(screen.getByRole('button', { name: 'Run now' }));
  });

  it('a run started elsewhere (422 "already queued or running") re-reads the request and shows it queued', async () => {
    installBackend();
    const screen = await openDetail();
    // Another tab or client queued it after this page loaded.
    phase = 'queued';
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(screen.getByRole('button', { name: 'Run now' }));
    expect(await screen.findByText('Request is already queued or running.', undefined, WAIT)).toBeInTheDocument();
    await waitFor(() => expectBusy(screen.getByRole('button', { name: 'Queued…' })), WAIT);
    expect(posts).toBe(1);
  });

  it('keeps Pause while queued; on a paused request Run now mirrors the API refusal (W16 §C)', async () => {
    installBackend();
    phase = 'queued';
    let screen = await openDetail();
    // Pause stays offered while queued, as before.
    expect(screen.getByRole('button', { name: /pause/i })).toBeEnabled();
    screen.unmount();
    phase = 'paused';
    screen = await openDetail();
    // W16 §C (PROPOSED) replaces "Run on a paused request is still offered": the API refuses a
    // run on a paused request ("Resume the request before running it."), so Run now is
    // unavailable, focusable, and says why; Resume stays offered.
    const run = screen.getByRole('button', { name: 'Run now' });
    expect(run).toHaveAttribute('aria-disabled', 'true');
    expect(run).toHaveAccessibleDescription(/paused: resume the scout to run it/i);
    expect(screen.getByRole('button', { name: /resume/i })).toBeEnabled();
  });
});
