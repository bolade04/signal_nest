import { useState, useSyncExternalStore } from 'react';

/**
 * Status polling for scout requests that are `queued` or `running` (review F-02 on #277).
 *
 * The run endpoint answers `queued` at once and a durable worker settles the request later;
 * nothing pushes that change to the client, so a page re-reads it. The UI cannot tell a
 * stranded `queued` (for example after its pending job was cancelled) from a slow one, so
 * one bounded policy covers both. This module is the single source of truth for it; the
 * scout list, the scout detail page and the detail page's jobs panel all use it.
 *
 * Policy (coordinator, lane W11-UIFIX2):
 * - **Interval:** every 2 s for the first 30 s after the restart point; then 4 s, 8 s and
 *   16 s; then every 30 s (cap).
 * - **Restart point:** polling (re)starts when a shown request enters `queued`/`running`, on
 *   any observed status change while one is in flight (including one request leaving while
 *   another stays), and on a manual "Refresh status".
 * - **Bound:** automatic polling stops 10 minutes after the last restart point if nothing
 *   changed; the last automatic read is at the 10-minute mark. That is about 37 reads per
 *   source instead of about 300 at a fixed 2 s.
 * - **Time is wall-clock** from the restart point, so time spent in a hidden tab counts
 *   toward the 10 minutes. While the tab is hidden no read is made at all (TanStack Query
 *   skips interval ticks while `document.visibilityState` is "hidden"). On becoming visible
 *   while something is in flight, the page reads once (`refetchOnWindowFocus: 'always'`,
 *   driven by `visibilitychange`), then continues the schedule from the wall-clock elapsed
 *   time — or restarts it if that read shows a status change. A hidden tab therefore never
 *   polls, and returning to it after the bound gives one fresh read and the stopped note.
 * - **When it stops:** a role="status" note reads "Automatic status updates stopped. Refresh
 *   status to check again." ("stopped", not "paused", so it is not read as the scout's own
 *   Pause). Nothing interprets the state (no "stuck", "failed" or "cancelled", no
 *   pause/resume/cancel advice, no run-outcome wording).
 * - **Manual refresh:** "Refresh status" is offered whenever something shown is in flight,
 *   both while reads are automatic and after they stop; it reads at once and restarts the
 *   schedule. Activating it while something stays in flight keeps it mounted and focused.
 *   When a read shows nothing in flight any more it is removed; if it had keyboard focus at
 *   that moment, the page moves focus to the request's Run now (fallback: the page heading),
 *   and otherwise focus is never moved.
 */
export const STATUS_POLL_POLICY = {
  fastMs: 2_000,
  fastWindowMs: 30_000,
  backoffMs: [4_000, 8_000, 16_000],
  capMs: 30_000,
  boundMs: 10 * 60_000,
} as const;

/**
 * The pure schedule: given the time elapsed since the restart point at the moment of a
 * read, the delay until the next automatic read, or `false` to stop.
 *
 * Reads therefore fall at 2, 4, …, 30 s, then 34, 42, 58 s, then every 30 s up to 598 s,
 * and a last one at 600 s (the remaining time is used when less than a step is left).
 */
export function nextStatusPoll(elapsedMs: number): number | false {
  const policy = STATUS_POLL_POLICY;
  const t = Math.max(0, elapsedMs);
  if (t >= policy.boundMs) return false;
  let step: number = policy.capMs;
  if (t < policy.fastWindowMs) {
    step = policy.fastMs;
  } else {
    let edge: number = policy.fastWindowMs;
    for (const backoff of policy.backoffMs) {
      edge += backoff;
      if (t < edge) {
        step = backoff;
        break;
      }
    }
  }
  return Math.min(step, policy.boundMs - t);
}

/**
 * Restart-point bookkeeping shared by the queries of one page. Each query ("source") reports
 * what it showed at its read time: a signature of its in-flight statuses (e.g.
 * "scout-1:queued"), or null when it shows nothing in flight. Plain class; `useStatusPolling`
 * wires it to React.
 */
export class StatusPollSchedule {
  #restartAt: number | null = null;
  #seen = new Map<string, string | null>();
  #stopped = false;
  #snapshot: { active: boolean; stopped: boolean } = { active: false, stopped: false };
  #listeners = new Set<() => void>();

  /** Record what `source` showed at `at` (its read time); a change while in flight restarts. */
  observe(source: string, signature: string | null, at: number): void {
    const before = this.#seen.get(source) ?? null;
    if (before === signature) return;
    this.#seen.set(source, signature);
    if (this.#inFlight()) {
      this.restart(at);
    } else {
      this.#restartAt = null;
      this.#setStopped(false);
    }
    this.#publish();
  }

  /**
   * The delay before `source`'s next automatic read after its read at `at`, or false. A
   * source polls only while it itself shows something in flight; all sources of a page share
   * one restart point, so they back off and stop together.
   */
  next(source: string, at: number): number | false {
    if (this.#restartAt === null || (this.#seen.get(source) ?? null) === null) return false;
    const delay = nextStatusPoll(at - this.#restartAt);
    this.#setStopped(delay === false);
    return delay;
  }

  /** A restart point now (manual refresh). */
  restart(at: number): void {
    this.#restartAt = at;
    this.#setStopped(false);
  }

  /** True once automatic reads have stopped while something is still shown in flight. */
  isStopped = (): boolean => this.#stopped;

  /** True while any source shows something in flight. */
  isActive = (): boolean => this.#inFlight();

  /** Stable snapshot for React: { active, stopped }, replaced only when either changes. */
  getSnapshot = (): { active: boolean; stopped: boolean } => this.#snapshot;

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  #inFlight(): boolean {
    for (const signature of this.#seen.values()) if (signature !== null) return true;
    return false;
  }

  #setStopped(stopped: boolean): void {
    if (this.#stopped === stopped) return;
    this.#stopped = stopped;
    this.#publish();
  }

  #publish(): void {
    const active = this.#inFlight();
    if (this.#snapshot.active === active && this.#snapshot.stopped === this.#stopped) return;
    this.#snapshot = { active, stopped: this.#stopped };
    this.#listeners.forEach((listener) => listener());
  }
}

/** The query options one polled source needs, bound to a page's schedule. */
export function pollingOptions<T>(
  schedule: StatusPollSchedule,
  source: string,
  signatureOf: (data: T | undefined) => string | null,
) {
  return {
    refetchInterval: (query: { state: { data: T | undefined; dataUpdatedAt: number } }) => {
      schedule.observe(source, signatureOf(query.state.data), query.state.dataUpdatedAt);
      return schedule.next(source, query.state.dataUpdatedAt);
    },
    // One fresh read when the tab becomes visible again while something is in flight (the
    // app's default is not to refetch on focus). Interval reads stay off while hidden.
    refetchOnWindowFocus: (query: { state: { data: T | undefined } }) =>
      signatureOf(query.state.data) !== null ? ('always' as const) : false,
  };
}

/**
 * A page's schedule; whether anything shown is in flight (`active`: offer "Refresh status");
 * and whether automatic reads have stopped (`stopped`: show the note). Re-renders on change.
 */
export function useStatusPolling() {
  const [schedule] = useState(() => new StatusPollSchedule());
  const { active, stopped } = useSyncExternalStore(schedule.subscribe, schedule.getSnapshot, schedule.getSnapshot);
  return { schedule, active, stopped };
}

/** "id:status" of the in-flight items, sorted, or null when none is in flight. */
export function inFlightSignature(
  items: ReadonlyArray<{ id: string; status: string }> | undefined,
  isInFlight: (status: string) => boolean,
): string | null {
  const parts = (items ?? []).filter((item) => isInFlight(item.status)).map((item) => `${item.id}:${item.status}`);
  return parts.length ? parts.sort().join(',') : null;
}
