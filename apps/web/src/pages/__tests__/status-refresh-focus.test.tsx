import { act, waitFor, within, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '@/App';
import { orgModel } from '@/test/handlers';
import { installRunBackend, resetRunBackend, RUN_REQ, RUN_REQ_NAME, runBackend } from '@/test/scout-run-backend';
import { renderApp } from '@/test/utils';

/**
 * W11 A3 (review r2 R2-01, R2-06): "Refresh status" no longer drops keyboard focus.
 *
 * At F (8a85416d) the button existed only after automatic reads had stopped, so activating it
 * restarted the schedule, removed the button, and focus fell to <body>. Now it is offered
 * whenever something shown is queued/running (the stopped note appears beside it only after
 * the stop), so activating it while the request stays in flight keeps it mounted and
 * focused. When a read shows nothing in flight it is removed; only if it had focus then does
 * the page move focus — to the request's Run now (list: that row's), else the page heading.
 * The note reads "stopped", not "paused" (R2-06).
 */

const S = 1000;
const WAIT = { timeout: 900 } as const;
const NOTE = 'Automatic status updates stopped. Refresh status to check again.';

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function runFor(ms: number, step = 1000) {
  for (let done = 0; done < ms; done += step) await advance(step);
}

function refreshButton(screen: RenderResult) {
  return screen.getByRole('button', { name: 'Refresh status' });
}

async function openDetail(role?: 'viewer') {
  if (role) orgModel.setMembership('org-1', 'user-1', role);
  const screen = renderApp(<App />, { route: `/scout-requests/${RUN_REQ}` });
  await screen.findByRole('heading', { level: 1, name: RUN_REQ_NAME }, WAIT);
  await screen.findByRole('button', { name: 'Refresh status' }, WAIT);
  return screen;
}

async function openList(role?: 'viewer') {
  if (role) orgModel.setMembership('org-1', 'user-1', role);
  const screen = renderApp(<App />, { route: '/scout-requests' });
  await screen.findByRole('link', { name: RUN_REQ_NAME }, WAIT);
  await screen.findByRole('button', { name: 'Refresh status' }, WAIT);
  return screen;
}

function headerRunNow(screen: RenderResult) {
  return within(screen.getByRole('heading', { level: 1, name: RUN_REQ_NAME }).closest('div.space-y-6') as HTMLElement)
    .getAllByRole('button', { name: /^(run now|queued…|running…|starting…)$/i })[0]!;
}

beforeEach(() => {
  orgModel.reset();
  resetRunBackend('queued');
  installRunBackend();
  vi.useFakeTimers({ shouldAdvanceTime: true, advanceTimeDelta: 1 });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('"Refresh status" keeps keyboard focus (W11 A3, review r2 R2-01/R2-06)', () => {
  it('is offered while polling, without the note; the note (worded "stopped") joins it after the stop, same button', async () => {
    const screen = await openDetail();
    const button = refreshButton(screen);
    expect(screen.queryByText(NOTE)).toBeNull();
    // The live region exists, empty, before the note is written into it.
    expect(button.parentElement!.querySelector('[role="status"]')).toBeEmptyDOMElement();
    await runFor(11 * 60 * S, 2 * S);
    const note = screen.getByText(NOTE);
    expect(note).toHaveAttribute('role', 'status');
    expect(note.textContent).not.toMatch(/paused/i);
    expect(refreshButton(screen)).toBe(button);
  }, 60_000);

  it('activated by keyboard while polling: one read at once, stays mounted and focused, schedule restarted', async () => {
    const screen = await openDetail();
    await runFor(60 * S, 1 * S); // backed off to 16-30 s
    const button = refreshButton(screen);
    button.focus();
    const before = runBackend.detailGets;
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.keyboard('{Enter}');
    await waitFor(() => expect(runBackend.detailGets).toBe(before + 1), WAIT);
    await advance(200);
    expect(refreshButton(screen)).toBe(button);
    expect(button).toHaveFocus();
    await runFor(2 * S + 250, 250);
    expect(runBackend.detailGets).toBe(before + 2); // 2 s cadence again
    expect(button).toHaveFocus();
  }, 60_000);

  it('activated by keyboard after the stop while still in flight: the note goes, the button stays focused', async () => {
    const screen = await openDetail();
    await runFor(11 * 60 * S, 2 * S);
    const button = refreshButton(screen);
    button.focus();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.keyboard(' ');
    await waitFor(() => expect(screen.queryByText(NOTE)).toBeNull(), WAIT);
    expect(refreshButton(screen)).toBe(button);
    expect(button).toHaveFocus();
  }, 60_000);

  it('detail: a refresh that shows the run settled removes the button and moves its focus to Run now', async () => {
    const screen = await openDetail();
    const button = refreshButton(screen);
    button.focus();
    runBackend.phase = 'completed';
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.keyboard('{Enter}');
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Refresh status' })).toBeNull(), WAIT);
    const runNow = headerRunNow(screen);
    await waitFor(() => expect(runNow).toHaveFocus(), WAIT);
    expect(runNow).toHaveAccessibleName('Run now');
    expect(runNow).not.toHaveAttribute('aria-disabled');
  });

  it('detail: an automatic read that shows the run settled does not move focus the user put elsewhere', async () => {
    const screen = await openDetail();
    const pause = screen.getByRole('button', { name: /pause/i });
    pause.focus();
    runBackend.phase = 'completed';
    await runFor(3 * S, 250);
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Refresh status' })).toBeNull(), WAIT);
    expect(pause).toHaveFocus();
  });

  it('detail: with focus nowhere in particular, settling moves nothing', async () => {
    const screen = await openDetail();
    (document.activeElement as HTMLElement | null)?.blur();
    runBackend.phase = 'completed';
    await runFor(3 * S, 250);
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Refresh status' })).toBeNull(), WAIT);
    expect(document.activeElement).toBe(document.body);
  });

  it('detail, viewer (no Run now): focus moves to the page heading', async () => {
    const screen = await openDetail('viewer');
    expect(screen.queryByRole('button', { name: /run now|queued…/i })).toBeNull();
    refreshButton(screen).focus();
    runBackend.phase = 'completed';
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.keyboard('{Enter}');
    await waitFor(() => expect(screen.getByRole('heading', { level: 1, name: RUN_REQ_NAME })).toHaveFocus(), WAIT);
  });

  it('list: a refresh that shows the row settled moves focus to that row\'s Run now', async () => {
    const screen = await openList();
    const button = refreshButton(screen);
    button.focus();
    runBackend.phase = 'completed';
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.keyboard('{Enter}');
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Refresh status' })).toBeNull(), WAIT);
    const row = within(screen.getByRole('link', { name: RUN_REQ_NAME }).closest('.rounded-lg') as HTMLElement);
    await waitFor(() => expect(row.getByRole('button', { name: 'Run now' })).toHaveFocus(), WAIT);
  });

  it('list: activating while the row stays queued keeps the button mounted and focused', async () => {
    const screen = await openList();
    const button = refreshButton(screen);
    button.focus();
    const before = runBackend.listGets;
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.keyboard('{Enter}');
    await waitFor(() => expect(runBackend.listGets).toBe(before + 1), WAIT);
    await advance(200);
    expect(refreshButton(screen)).toBe(button);
    expect(button).toHaveFocus();
  });

  it('list, viewer (no Run now): focus moves to the list heading', async () => {
    const screen = await openList('viewer');
    refreshButton(screen).focus();
    runBackend.phase = 'completed';
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.keyboard('{Enter}');
    await waitFor(() => expect(screen.getByRole('heading', { level: 1, name: 'Scout requests' })).toHaveFocus(), WAIT);
  });
});
