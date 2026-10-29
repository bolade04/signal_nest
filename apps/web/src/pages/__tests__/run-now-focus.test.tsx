import { act, waitFor, within, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '@/App';
import { installRunBackend, resetRunBackend, RUN_REQ, RUN_REQ_NAME, runBackend } from '@/test/scout-run-backend';
import { renderApp } from '@/test/utils';

/**
 * W11 A1 (review F-01 on #277): Run now stays keyboard-accessible while busy.
 *
 * At 4c142280 the busy button used native `disabled`: in a browser, focus then fell to
 * <body> as the label changed from "Starting…" to "Queued…", and did not come back. Busy is
 * now `aria-disabled="true"`: the button stays focusable and in the tab order, its name
 * follows the label, and every activation while busy returns before submitting. Focus is
 * never moved by the app.
 */

const INTERVAL = 2000;
const WAIT = { timeout: 900 } as const;

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function header(screen: RenderResult) {
  return within(screen.getByRole('heading', { level: 1, name: RUN_REQ_NAME }).parentElement!);
}

/** The header's Run control, whatever its label (the empty-state copy is not rendered here). */
function runButton(screen: RenderResult) {
  return screen.getAllByRole('button', { name: /^(run now|starting…|queued…|running…)$/i })[0]!;
}

function expectBusy(button: HTMLElement, label: string) {
  expect(button).toHaveAccessibleName(label);
  expect(button).toHaveAttribute('aria-disabled', 'true');
  expect(button).toHaveAttribute('aria-busy', 'true');
  // Focusable: never the native disabled state.
  expect(button).not.toBeDisabled();
}

function expectAvailable(button: HTMLElement) {
  expect(button).toHaveAccessibleName('Run now');
  expect(button).not.toHaveAttribute('aria-disabled');
  expect(button).not.toHaveAttribute('aria-busy');
  expect(button).toBeEnabled();
}

async function openDetail() {
  const screen = renderApp(<App />, { route: `/scout-requests/${RUN_REQ}` });
  await screen.findByRole('heading', { level: 1, name: RUN_REQ_NAME }, WAIT);
  return screen;
}

beforeEach(() => {
  resetRunBackend('completed');
  installRunBackend();
  vi.useFakeTimers({ shouldAdvanceTime: true, advanceTimeDelta: 1 });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('Run now keeps keyboard focus while busy (W11 A1, review F-01)', () => {
  it('a click keeps focus on the same button through Starting… → Queued… → Running… → Run now', async () => {
    let release!: () => void;
    runBackend.holdPost = new Promise<void>((resolve) => {
      release = resolve;
    });
    const screen = await openDetail();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const button = runButton(screen);
    await user.click(button);

    await waitFor(() => expectBusy(button, 'Starting…'), WAIT);
    expect(button).toHaveFocus();

    runBackend.holdPost = null;
    release();
    await waitFor(() => expectBusy(button, 'Queued…'), WAIT);
    expect(button).toHaveFocus();

    runBackend.phase = 'running';
    await advance(INTERVAL);
    await waitFor(() => expectBusy(button, 'Running…'), WAIT);
    expect(button).toHaveFocus();

    runBackend.phase = 'completed';
    await advance(INTERVAL);
    await waitFor(() => expect(header(screen).getByText('Completed')).toBeInTheDocument(), WAIT);
    expectAvailable(button);
    // Still the very same element, still focused: nothing remounted it or moved focus.
    expect(runButton(screen)).toBe(button);
    expect(button).toHaveFocus();
    expect(runBackend.posts).toBe(1);
  });

  it('Enter starts one run and keeps focus; Enter, Space and click while busy send nothing', async () => {
    const screen = await openDetail();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const button = runButton(screen);
    button.focus();
    await user.keyboard('{Enter}');
    await waitFor(() => expectBusy(button, 'Queued…'), WAIT);
    expect(button).toHaveFocus();
    expect(runBackend.posts).toBe(1);

    await user.keyboard('{Enter}');
    await user.keyboard(' ');
    await user.click(button);
    await advance(300);
    expect(runBackend.posts).toBe(1);
    expect(button).toHaveFocus();
    expectBusy(button, 'Queued…');
  });

  it('Space starts one run and keeps focus', async () => {
    const screen = await openDetail();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const button = runButton(screen);
    button.focus();
    await user.keyboard(' ');
    await waitFor(() => expectBusy(button, 'Queued…'), WAIT);
    expect(button).toHaveFocus();
    expect(runBackend.posts).toBe(1);
  });

  it.each([
    ['a double click', async (user: ReturnType<typeof userEvent.setup>, button: HTMLElement) => user.dblClick(button)],
    ['a double Enter', async (user: ReturnType<typeof userEvent.setup>, button: HTMLElement) => {
      button.focus();
      await user.keyboard('{Enter}{Enter}');
    }],
    ['a double Space', async (user: ReturnType<typeof userEvent.setup>, button: HTMLElement) => {
      button.focus();
      await user.keyboard('  ');
    }],
  ])('%s sends exactly one POST', async (_name, activate) => {
    const screen = await openDetail();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const button = runButton(screen);
    await activate(user, button);
    await waitFor(() => expectBusy(button, 'Queued…'), WAIT);
    await advance(300);
    expect(runBackend.posts).toBe(1);
  });

  it('stays in the tab order while busy; Tab can leave it, and focus is not taken back when the run ends', async () => {
    const screen = await openDetail();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const button = runButton(screen);
    const pause = screen.getByRole('button', { name: /pause/i });
    await user.click(button);
    await waitFor(() => expectBusy(button, 'Queued…'), WAIT);

    // Reachable by keyboard while busy: Shift+Tab from the next control lands on it.
    pause.focus();
    await user.tab({ shift: true });
    expect(button).toHaveFocus();
    // And Tab leaves it.
    await user.tab();
    expect(pause).toHaveFocus();

    runBackend.phase = 'completed';
    await advance(INTERVAL);
    await waitFor(() => expectAvailable(button), WAIT);
    expect(pause).toHaveFocus();
    expect(button).not.toHaveFocus();
  });

  it('list row: focus stays on the row button through Queued… → Run now', async () => {
    const screen = renderApp(<App />, { route: '/scout-requests' });
    const link = await screen.findByRole('link', { name: RUN_REQ_NAME }, WAIT);
    const row = within(link.closest('.rounded-lg') as HTMLElement);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const button = row.getByRole('button', { name: 'Run now' });
    await user.click(button);
    await waitFor(() => expectBusy(button, 'Queued…'), WAIT);
    expect(button).toHaveFocus();
    runBackend.phase = 'completed';
    await advance(INTERVAL);
    await waitFor(() => expectAvailable(button), WAIT);
    expect(button).toHaveFocus();
    expect(runBackend.posts).toBe(1);
  });
});
