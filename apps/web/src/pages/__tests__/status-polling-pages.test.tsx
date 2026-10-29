import { act, waitFor, within, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '@/App';
import { installRunBackend, resetRunBackend, RUN_REQ, RUN_REQ_NAME, runBackend } from '@/test/scout-run-backend';
import { renderApp } from '@/test/utils';

/**
 * W11 A2 (review F-02 on #277): scout-request status polling is bounded and backed off on
 * the list and the detail page (lib/status-polling.ts): 2 s for 30 s, then 4/8/16 s, then
 * 30 s; automatic reads stop 10 minutes after the last restart point, a role="status" note
 * and "Refresh status" appear; nothing is read while the tab is hidden, and one read is
 * made when it becomes visible again. At 4c142280 both pages re-read every 2 s forever.
 *
 * The request is held `queued` by the test (the stranded case the review rendered: its
 * pending job was cancelled, so no job is active), unless a test says otherwise. Every read
 * is time-stamped on the fake clock.
 */

const S = 1000;
const WAIT = { timeout: 900 } as const;
const NOTE = 'Automatic status updates paused. Refresh status to check again.';

let readKind: 'detail' | 'list' = 'detail';
const readTimes = () => (readKind === 'detail' ? runBackend.detailTimes : runBackend.listTimes);

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** Run the fake clock forward in small steps, letting each read finish before the next. */
async function runFor(ms: number, step = 250) {
  for (let done = 0; done < ms; done += step) await advance(step);
}

/** Gaps between consecutive reads, rounded to the nearest 0.5 s. */
const gaps = (times: number[]) => times.slice(1).map((t, i) => Math.round((t - times[i]!) / 500) * 500);

function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') });
  // As browsers fire it: at the document, bubbling to window (where TanStack Query listens).
  document.dispatchEvent(new Event('visibilitychange', { bubbles: true }));
}

async function openDetail() {
  readKind = 'detail';
  const screen = renderApp(<App />, { route: `/scout-requests/${RUN_REQ}` });
  await screen.findByRole('heading', { level: 1, name: RUN_REQ_NAME }, WAIT);
  return screen;
}

async function openList() {
  readKind = 'list';
  const screen = renderApp(<App />, { route: '/scout-requests' });
  await screen.findByRole('link', { name: RUN_REQ_NAME }, WAIT);
  return screen;
}

function runButton(screen: RenderResult) {
  return screen.getAllByRole('button', { name: /^(run now|starting…|queued…|running…)$/i })[0]!;
}

beforeEach(() => {
  resetRunBackend('queued');
  installRunBackend();
  vi.useFakeTimers({ shouldAdvanceTime: true, advanceTimeDelta: 1 });
});

afterEach(() => {
  // Restore the real visibility getter (jsdom defines it on the prototype).
  delete (document as unknown as Record<string, unknown>).visibilityState;
  vi.useRealTimers();
});

describe('bounded status polling (W11 A2, review F-02)', () => {
  it('detail page: 2 s for 30 s, then 4/8/16 s, then 30 s; stops after 10 min with a note; about 37 reads, not 300', async () => {
    const screen = await openDetail();
    const [start] = readTimes(); // the first read observed "queued": the restart point
    await runFor(11 * 60 * S, 500);

    const auto = readTimes().filter((t) => t > start!);
    const g = gaps([start!, ...auto]);
    expect(g.slice(0, 15).every((x) => x === 2 * S), `first gaps ${g.slice(0, 16)}`).toBe(true);
    expect(g.slice(15, 18)).toEqual([4 * S, 8 * S, 16 * S]);
    expect(g.slice(18, -2).every((x) => x === 30 * S), `capped gaps ${g.slice(18)}`).toBe(true);
    expect(auto.length).toBeGreaterThanOrEqual(35);
    expect(auto.length).toBeLessThanOrEqual(38);
    expect(auto[auto.length - 1]! - start!).toBeLessThanOrEqual(601 * S);

    expect(screen.getByText(NOTE)).toHaveAttribute('role', 'status');
    expect(screen.getByRole('button', { name: 'Refresh status' })).toBeInTheDocument();
    // The note does not interpret the state or suggest an action on the run.
    expect(screen.getByText(NOTE).textContent).not.toMatch(/stuck|fail|cancel|pause the|resume/i);

    // Stopped means stopped: another 10 minutes, no read.
    const before = runBackend.detailGets;
    await runFor(10 * 60 * S, 5 * S);
    expect(runBackend.detailGets).toBe(before);
  }, 60_000);

  it('detail page: "Refresh status" reads once at once and restarts the 2 s schedule', async () => {
    const screen = await openDetail();
    await runFor(11 * 60 * S, 1 * S);
    const refresh = await screen.findByRole('button', { name: 'Refresh status' });
    const before = runBackend.detailGets;
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    refresh.focus();
    await user.keyboard('{Enter}'); // keyboard-accessible
    await waitFor(() => expect(runBackend.detailGets).toBe(before + 1), WAIT);
    await waitFor(() => expect(screen.queryByText(NOTE)).not.toBeInTheDocument(), WAIT);
    await runFor(2 * S + 250);
    expect(runBackend.detailGets).toBe(before + 2);
    await runFor(2 * S);
    expect(runBackend.detailGets).toBe(before + 3);
  }, 60_000);

  it('detail page: an observed status change restarts the schedule at 2 s', async () => {
    await openDetail();
    await runFor(5 * 60 * S, 1 * S); // backed off to 30 s by now
    const from = readTimes().length;
    runBackend.phase = 'running';
    await runFor(60 * S, 250); // the next 30 s read sees "running"
    const g = gaps(readTimes().slice(from - 1));
    const firstFast = g.findIndex((x) => x === 2 * S);
    expect(firstFast, `gaps ${g}`).toBeGreaterThanOrEqual(0);
    // After the change: consecutive 2 s reads again.
    expect(g.slice(firstFast, firstFast + 5)).toEqual([2 * S, 2 * S, 2 * S, 2 * S, 2 * S]);
  }, 60_000);

  it('detail page: no reads while the tab is hidden; one read when it becomes visible', async () => {
    const screen = await openDetail();
    await runFor(4 * S);
    setHidden(true);
    const hiddenAt = runBackend.detailGets;
    await runFor(60 * S, 500);
    expect(runBackend.detailGets).toBe(hiddenAt);
    setHidden(false);
    await waitFor(() => expect(runBackend.detailGets).toBe(hiddenAt + 1), WAIT);
    await advance(100);
    expect(runBackend.detailGets).toBe(hiddenAt + 1);
    // Hidden time counted: 64 s after the restart, the schedule continues at 30 s, not 2 s.
    await runFor(10 * S, 250);
    expect(runBackend.detailGets).toBe(hiddenAt + 1);
    expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
  }, 60_000);

  it('detail page: a hidden tab past the bound makes no reads, then one read on return and the note', async () => {
    const screen = await openDetail();
    setHidden(true);
    const at = runBackend.detailGets;
    await runFor(12 * 60 * S, 5 * S);
    expect(runBackend.detailGets).toBe(at);
    setHidden(false);
    await waitFor(() => expect(runBackend.detailGets).toBe(at + 1), WAIT);
    expect(await screen.findByText(NOTE)).toBeInTheDocument();
    await runFor(2 * 60 * S, 5 * S);
    expect(runBackend.detailGets).toBe(at + 1);
  }, 60_000);

  it('detail page: completed or failed stops polling, removes the note and makes Run now usable', async () => {
    for (const end of ['completed', 'failed']) {
      resetRunBackend('queued');
      installRunBackend();
      const screen = await openDetail();
      await runFor(11 * 60 * S, 2 * S);
      expect(screen.getByText(NOTE)).toBeInTheDocument();
      expect(runButton(screen)).toHaveAttribute('aria-disabled', 'true');
      runBackend.phase = end;
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      await user.click(screen.getByRole('button', { name: 'Refresh status' }));
      await waitFor(() => expect(runButton(screen)).toHaveAccessibleName('Run now'), WAIT);
      expect(runButton(screen)).not.toHaveAttribute('aria-disabled');
      expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
      const after = runBackend.detailGets;
      await runFor(3 * 60 * S, 2 * S);
      expect(runBackend.detailGets).toBe(after);
      screen.unmount();
    }
  }, 90_000);

  it('detail page: a pending job is read on the same bounded schedule (the jobs panel stops too)', async () => {
    runBackend.jobs = [{ id: 'job-1', status: 'pending' }];
    const screen = await openDetail();
    const jobsAtStart = runBackend.jobsGets;
    await runFor(11 * 60 * S, 1 * S);
    const jobReads = runBackend.jobsGets - jobsAtStart;
    expect(jobReads).toBeGreaterThanOrEqual(35);
    expect(jobReads).toBeLessThanOrEqual(39);
    expect(screen.getByText(NOTE)).toBeInTheDocument();
    const before = runBackend.jobsGets;
    await runFor(5 * 60 * S, 5 * S);
    expect(runBackend.jobsGets).toBe(before);
  }, 60_000);

  it('list page: the same bound, note and "Refresh status"', async () => {
    const screen = await openList();
    const [start] = readTimes();
    await runFor(11 * 60 * S, 500);
    const auto = readTimes().filter((t) => t > start!);
    expect(gaps([start!, ...auto]).slice(0, 18)).toEqual([...Array(15).fill(2 * S), 4 * S, 8 * S, 16 * S]);
    expect(auto.length).toBeGreaterThanOrEqual(35);
    expect(auto.length).toBeLessThanOrEqual(38);
    expect(screen.getByText(NOTE)).toHaveAttribute('role', 'status');
    const before = runBackend.listGets;
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    await waitFor(() => expect(runBackend.listGets).toBe(before + 1), WAIT);
    await waitFor(() => expect(screen.queryByText(NOTE)).not.toBeInTheDocument(), WAIT);
    // The row's Run now follows the status once it settles.
    runBackend.phase = 'completed';
    await runFor(2 * S + 250);
    const row = within(screen.getByRole('link', { name: RUN_REQ_NAME }).closest('.rounded-lg') as HTMLElement);
    await waitFor(() => expect(row.getByRole('button', { name: 'Run now' })).not.toHaveAttribute('aria-disabled'), WAIT);
  }, 60_000);
});
