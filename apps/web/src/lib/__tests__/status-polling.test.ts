import { describe, expect, it, vi } from 'vitest';
import { inFlightSignature, nextStatusPoll, STATUS_POLL_POLICY, StatusPollSchedule } from '@/lib/status-polling';

/**
 * W11 A2 (review F-02 on #277): the bounded, backed-off status-polling policy, as a pure
 * function and as the restart-point bookkeeping the pages share.
 */

const S = 1000;

/** The read times (ms after the restart point) the schedule produces with nothing changing. */
function readTimes(): number[] {
  const times: number[] = [];
  let t = 0;
  for (;;) {
    const delay = nextStatusPoll(t);
    if (delay === false) return times;
    t += delay;
    times.push(t);
  }
}

describe('nextStatusPoll: the schedule (W11 A2)', () => {
  it('states the coordinator policy', () => {
    expect(STATUS_POLL_POLICY).toEqual({
      fastMs: 2 * S,
      fastWindowMs: 30 * S,
      backoffMs: [4 * S, 8 * S, 16 * S],
      capMs: 30 * S,
      boundMs: 600 * S,
    });
  });

  it.each([
    [-5 * S, 2 * S],
    [0, 2 * S],
    [29_900, 2 * S],
    [29_999, 2 * S],
    [30_000, 4 * S],
    [33_999, 4 * S],
    [34_000, 8 * S],
    [41_999, 8 * S],
    [42_000, 16 * S],
    [57_999, 16 * S],
    [58_000, 30 * S],
    [300_000, 30 * S],
    [568_000, 30 * S],
    [590_000, 10 * S],
    [598_000, 2 * S],
    [599_999, 1],
    [600_000, false],
    [3_600_000, false],
  ] as const)('after %i ms -> %s', (elapsed, expected) => {
    expect(nextStatusPoll(elapsed)).toBe(expected);
  });

  it('with nothing changing: 2 s for 30 s, then 4/8/16 s, then 30 s, last read at 10 min, 37 reads in all', () => {
    const times = readTimes();
    expect(times.slice(0, 15)).toEqual(Array.from({ length: 15 }, (_, i) => (i + 1) * 2 * S));
    expect(times.slice(15, 19)).toEqual([34 * S, 42 * S, 58 * S, 88 * S]);
    expect(times.slice(-3)).toEqual([568 * S, 598 * S, 600 * S]);
    expect(times).toHaveLength(37);
    // A fixed 2 s cadence makes 300 reads in the same 10 minutes.
    expect(600 / 2).toBe(300);
  });
});

describe('StatusPollSchedule: restart points shared by a page (W11 A2)', () => {
  it('starts when a source enters in-flight; an unchanged status does not restart; a change does', () => {
    const s = new StatusPollSchedule();
    s.observe('request', null, 0);
    expect(s.next('request', 0)).toBe(false);
    s.observe('request', 'r1:queued', 1_000);
    expect(s.next('request', 1_000)).toBe(2 * S);
    // Same status 40 s later: no restart, backed off.
    s.observe('request', 'r1:queued', 41_000);
    expect(s.next('request', 41_000)).toBe(8 * S);
    // Observed change while in flight: restart.
    s.observe('request', 'r1:running', 50_000);
    expect(s.next('request', 50_000)).toBe(2 * S);
  });

  it('stops at the bound, says so once, and a manual restart resumes at 2 s', () => {
    const s = new StatusPollSchedule();
    const listener = vi.fn();
    s.subscribe(listener);
    s.observe('request', 'r1:queued', 0);
    expect(s.isStopped()).toBe(false);
    expect(s.next('request', 600 * S)).toBe(false);
    expect(s.isStopped()).toBe(true);
    expect(s.next('request', 610 * S)).toBe(false);
    expect(listener).toHaveBeenCalledTimes(1);
    s.restart(700 * S);
    expect(s.isStopped()).toBe(false);
    expect(s.next('request', 700 * S)).toBe(2 * S);
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it('leaving in-flight clears the schedule and the paused state', () => {
    const s = new StatusPollSchedule();
    s.observe('request', 'r1:queued', 0);
    s.next('request', 700 * S);
    expect(s.isStopped()).toBe(true);
    s.observe('request', null, 701 * S);
    expect(s.isStopped()).toBe(false);
    expect(s.next('request', 701 * S)).toBe(false);
  });

  it('sources share one restart point; each polls only while it shows something in flight', () => {
    const s = new StatusPollSchedule();
    s.observe('request', 'r1:queued', 0);
    s.observe('jobs', null, 0);
    expect(s.next('jobs', 0)).toBe(false); // no active job: the jobs list does not poll
    s.observe('jobs', 'j1:pending', 100_000); // a job appears: a status change, restart
    expect(s.next('jobs', 100_000)).toBe(2 * S);
    expect(s.next('request', 100_000)).toBe(2 * S);
    // One source leaving while the other stays is also a change: restart.
    s.observe('jobs', null, 200_000);
    expect(s.next('request', 200_000)).toBe(2 * S);
  });
});

describe('inFlightSignature', () => {
  it('is the sorted id:status of in-flight items, or null', () => {
    const inFlight = (status: string) => status === 'queued' || status === 'running';
    expect(inFlightSignature(undefined, inFlight)).toBeNull();
    expect(inFlightSignature([{ id: 'b', status: 'completed' }], inFlight)).toBeNull();
    expect(
      inFlightSignature(
        [
          { id: 'b', status: 'running' },
          { id: 'a', status: 'queued' },
          { id: 'c', status: 'paused' },
        ],
        inFlight,
      ),
    ).toBe('a:queued,b:running');
  });
});
