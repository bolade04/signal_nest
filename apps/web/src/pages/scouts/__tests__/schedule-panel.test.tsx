import { waitFor } from '@testing-library/react';
import { useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { API_PREFIX } from '@/api/config';
import { queryKeys } from '@/api/queryKeys';
import { useAuth } from '@/auth/AuthContext';
import type { ScoutScheduleOut } from '@/api/types';
import { formatDateTime } from '@/lib/utils';
import { SchedulePanel } from '@/pages/scouts/SchedulePanel';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

const WS = 'ws-1';
const REQ = 'scout-loc-dallas';
const P = (path: string) => `*${API_PREFIX}${path}`;
const SCHEDULE = P(`/workspaces/${WS}/scout-requests/${REQ}/schedule`);

// The demo session is an OWNER (an editor). Override /auth/me to demote the user
// to a view-only role for the read-only-permission test.
function asRole(role: string) {
  server.use(
    http.get(P('/auth/me'), () =>
      HttpResponse.json({
        access_token: 'test-token',
        token_type: 'bearer',
        user: { id: 'user-1', email: 'demo@signalnest.dev', full_name: 'Demo', is_operator: true },
        memberships: [{ organization_id: 'org-1', role }],
      }),
    ),
  );
}

function scheduleRow(overrides: Record<string, unknown> = {}) {
  // `satisfies` pins the base row to the generated response model, so a field the
  // API adds or renames fails type-check here instead of passing silently.
  const base = {
    id: 'sched-1',
    scout_request_id: REQ,
    location_id: 'loc-dallas',
    interval: 'daily',
    state: 'active',
    enabled: true,
    next_run_at: '2026-07-18T09:00:00Z',
    last_tick_at: '2026-07-17T09:00:00Z',
    last_tick_failed_at: null,
    created_at: '2026-07-17T08:00:00Z',
    updated_at: '2026-07-17T09:00:00Z',
  } satisfies ScoutScheduleOut;
  return { ...base, ...overrides };
}

const notFound = () => HttpResponse.json({ error: { code: 'not_found', message: 'no schedule' } }, { status: 404 });

// The panel's gate: the workspace-effective decision (P6-UI-006), never the
// raw-global runtime summary.
const SCHEDULING_CAPABILITY = P(`/workspaces/${WS}/scheduling-capability`);
const CAPABILITIES = P('/system/capabilities');

// The default handler reports scheduling dark for every workspace, mirroring the
// server default. A test that needs scheduling live opts in here; the dark path is
// therefore what renders unless a test explicitly asks for the enabled one.
function enableScheduling(enabled = true) {
  server.use(http.get(SCHEDULING_CAPABILITY, () => HttpResponse.json({ enabled })));
}

/** A runtime summary whose RAW global scheduling flag is `rawFlag`. */
function rawGlobalFlag(rawFlag: boolean) {
  server.use(
    http.get(CAPABILITIES, () =>
      HttpResponse.json({
        app_mode: 'local',
        environment: 'development',
        is_local_mode: true,
        all_configured: true,
        features: {
          opportunity_feedback_enabled: false,
          scout_scheduling_enabled: rawFlag,
          connector_rss_enabled: false,
          recommendation_briefs_enabled: false,
          creative_generation_enabled: false,
        },
      }),
    ),
  );
}

/**
 * Test-only probe: renders the resolved membership role. Awaiting it proves
 * `/auth/me` has landed AND that an `asRole(...)` override actually took
 * effect, so a following absence assertion cannot pass inside the auth-loading
 * window. Nothing is added to the shipped component for this.
 */
function AuthProbe() {
  const { memberships } = useAuth();
  return <span data-testid="resolved-role">{memberships[0]?.role ?? ''}</span>;
}

/** Test-only probe exposing cache invalidation from inside the provider tree. */
let invalidateCapability: () => void = () => {};
function CacheProbe() {
  const qc = useQueryClient();
  useEffect(() => {
    const fn = () => qc.invalidateQueries({ queryKey: queryKeys.schedulingCapability(WS) });
    invalidateCapability = fn;
    // Without a cleanup the binding outlives the test, and a later caller would
    // invalidate a dead QueryClient while the live one never refetches — a
    // failure that surfaces only as an unexplained timeout. The identity check
    // means a probe only retires its OWN binding.
    //
    // Known limit: this assumes a single mounted probe, which is all any test
    // uses. With two mounted at once, the second unmounting still retires a
    // binding the first is relying on — unfixable while the handle is a module
    // global, since `invalidateCapability` would have no correct client to target.
    // If a test ever needs two, return a per-probe handle from `render()`
    // instead of generalising this now.
    return () => {
      if (invalidateCapability === fn) {
        invalidateCapability = () => {
          throw new Error('invalidateCapability called outside a mounted CacheProbe');
        };
      }
    };
  }, [qc]);
  return null;
}

function render() {
  return renderApp(
    <>
      <AuthProbe />
      <CacheProbe />
      <SchedulePanel workspaceId={WS} requestId={REQ} />
    </>,
    { route: `/scouts/${REQ}` },
  );
}

describe('SchedulePanel', () => {
  it('offers daily and weekly creation when no schedule exists (editor)', async () => {
    enableScheduling();
    server.use(http.get(SCHEDULE, notFound));
    const screen = render();

    expect(await screen.findByRole('button', { name: /schedule daily/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /schedule weekly/i })).toBeInTheDocument();
  });

  it('creates a schedule and reflects the active state after refetch', async () => {
    enableScheduling();
    let created = false;
    server.use(
      http.get(SCHEDULE, () => (created ? HttpResponse.json(scheduleRow()) : notFound())),
      http.post(SCHEDULE, async ({ request }) => {
        const body = (await request.json()) as { interval: string };
        expect(body.interval).toBe('daily');
        created = true;
        return HttpResponse.json(scheduleRow(), { status: 201 });
      }),
    );
    const screen = render();

    const button = await screen.findByRole('button', { name: /schedule daily/i });
    await screen.user.click(button);

    expect(await screen.findByText('Active')).toBeInTheDocument();
    expect(await screen.findByText('Schedule created')).toBeInTheDocument();
  });

  it('shows the activation-required state with an explanatory hint and Activate action', async () => {
    enableScheduling();
    server.use(http.get(SCHEDULE, () => HttpResponse.json(scheduleRow({ state: 'activation_required' }))));
    const screen = render();

    expect(await screen.findByText('Activation required')).toBeInTheDocument();
    expect(screen.getByText(/has not been activated yet/i)).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /activate/i })).toBeInTheDocument();
  });

  it('pauses an active schedule', async () => {
    enableScheduling();
    server.use(
      http.get(SCHEDULE, () => HttpResponse.json(scheduleRow())),
      http.post(`${SCHEDULE}/pause`, () =>
        HttpResponse.json(scheduleRow({ state: 'paused', enabled: false })),
      ),
    );
    const screen = render();

    const pause = await screen.findByRole('button', { name: /pause/i });
    await screen.user.click(pause);

    expect(await screen.findByText('Schedule paused')).toBeInTheDocument();
  });

  it('confirms before deleting a schedule', async () => {
    enableScheduling();
    let deleted = false;
    server.use(
      http.get(SCHEDULE, () => (deleted ? notFound() : HttpResponse.json(scheduleRow()))),
      http.delete(SCHEDULE, () => {
        deleted = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const screen = render();

    await screen.user.click(await screen.findByRole('button', { name: /delete/i }));
    // A confirmation dialog gates the destructive action.
    await screen.user.click(await screen.findByRole('button', { name: /delete schedule/i }));

    expect(await screen.findByText('Schedule deleted')).toBeInTheDocument();
  });

  it('still surfaces a 503 gracefully for a client whose capability cache is stale', async () => {
    // Defence in depth: the server gate is the real boundary and must keep
    // answering 503 for a client that believes the feature is live. This test
    // deliberately runs with scheduling ENABLED client-side so the affordance
    // exists and the doomed POST is issued.
    enableScheduling();
    server.use(
      http.get(SCHEDULE, notFound),
      http.post(SCHEDULE, () =>
        HttpResponse.json(
          { error: { code: 'capability_unavailable', message: 'Scout scheduling is not available yet.' } },
          { status: 503 },
        ),
      ),
    );
    const screen = render();

    await screen.user.click(await screen.findByRole('button', { name: /schedule daily/i }));

    expect(await screen.findByText('Could not create schedule')).toBeInTheDocument();
  });

  it('hides mutation controls from a view-only member', async () => {
    enableScheduling();
    asRole('viewer');
    server.use(http.get(SCHEDULE, () => HttpResponse.json(scheduleRow())));
    const screen = render();

    // Prove the role resolved to `viewer` before asserting controls are absent:
    // `canEdit` is also false in the pre-auth window, so without this the
    // assertions below could pass even if `asRole` had silently failed.
    await waitFor(() => expect(screen.getByTestId('resolved-role')).toHaveTextContent('viewer'));
    expect(await screen.findByText('Active')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /pause/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument();
  });
});

/**
 * P6-UI-002 — scheduling dark-state truthfulness.
 *
 * Schedule READS stay available while the capability is dark; schedule
 * MUTATIONS answer 503. The panel must mirror that split: an existing schedule
 * remains readable, no mutation affordance is offered, and nothing on screen
 * asserts the capability is live.
 *
 * The server 503 is the enforcement boundary. Everything below is truthfulness:
 * it stops the UI claiming something the backend will refuse.
 */
describe('SchedulePanel — scheduling capability dark', () => {
  it('offers no creation affordance when no schedule exists', async () => {
    server.use(http.get(SCHEDULE, notFound));
    const screen = render();

    // Await the dark note, not the generic copy: it renders only once the role
    // AND the capability flag are known, so the absence assertions below cannot
    // pass merely because auth has not resolved yet.
    expect(await screen.findByText(/scheduling is not available yet/i)).toBeInTheDocument();
    expect(screen.getByText(/no recurring schedule/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /schedule daily/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /schedule weekly/i })).not.toBeInTheDocument();
  });

  it('issues no schedule mutation on mount while dark, and would if enabled', async () => {
    let posted = false;
    server.use(
      http.get(SCHEDULE, notFound),
      http.post(SCHEDULE, () => {
        posted = true;
        return HttpResponse.json(scheduleRow(), { status: 201 });
      }),
    );
    const screen = render();

    await screen.findByText(/scheduling is not available yet/i);
    // No mutation of the current code can break this line — nothing mounts a
    // schedule POST — so it carries no mutation coverage. It is kept as an
    // insertion guard: it is the only assertion here about network effects
    // rather than rendered controls, and an auto-create effect added later is
    // exactly the regression that would otherwise land silently.
    expect(posted).toBe(false);
    // The claim is about the rendered control set, not about an interaction
    // that is impossible to perform: no mutation affordance exists at all.
    const labels = screen.queryAllByRole('button').map((b) => b.textContent ?? '');
    expect(labels.some((t) => /schedule daily|schedule weekly|pause|activate|delete/i.test(t))).toBe(
      false,
    );
  });

  it.each([
    ['paused', 'Paused'],
    ['activation_required', 'Activation required'],
    ['active', 'Active'],
  ])('keeps an existing %s schedule readable without mutation controls', async (state) => {
    server.use(http.get(SCHEDULE, () => HttpResponse.json(scheduleRow({ state }))));
    const screen = render();

    // The persisted schedule stays visible — darkness gates mutations, not reads.
    expect(await screen.findByText(/scheduling is not available yet/i)).toBeInTheDocument();
    expect(screen.getByText('Daily')).toBeInTheDocument();
    expect(screen.getByText(/last run/i)).toBeInTheDocument();

    for (const name of [/pause/i, /activate/i, /delete/i]) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument();
    }
  });

  it('never claims the capability is enabled while it is dark', async () => {
    server.use(
      http.get(SCHEDULE, () => HttpResponse.json(scheduleRow({ state: 'activation_required' }))),
    );
    const screen = render();

    await screen.findByText(/scheduling is not available yet/i);
    // The activation hint asserts "Recurring scouting is enabled" — false while dark.
    expect(screen.queryByText(/recurring scouting is enabled/i)).not.toBeInTheDocument();
  });

  it('shows neither an Active badge nor a Next run date for a dark schedule', async () => {
    // Flipping the flag off does not cancel an enqueued tick, so the API keeps
    // reporting `active` for up to a full interval. Both the badge and the
    // timestamp would be false for that whole window.
    const row = scheduleRow({ state: 'active' });
    server.use(http.get(SCHEDULE, () => HttpResponse.json(row)));
    const screen = render();

    await screen.findByText(/scheduling is not available yet/i);
    expect(screen.queryByText('Active')).not.toBeInTheDocument();
    // Derived from the fixture, never a hardcoded year: a literal like /2026/
    // silently stops testing anything the day someone refreshes the fixture
    // dates, and nothing would fail to announce that it had been disarmed.
    expect(screen.queryByText(formatDateTime(row.next_run_at))).not.toBeInTheDocument();
  });

  it('leaves role restrictions intact and adds no editor note for a viewer', async () => {
    // Feature darkness is an ADDITIONAL gate, never a replacement for the role
    // check: a viewer sees the same permission copy whether or not the
    // capability is live, and never the editor-only dark note.
    asRole('viewer');
    server.use(http.get(SCHEDULE, notFound));
    const screen = render();

    await waitFor(() => expect(screen.getByTestId('resolved-role')).toHaveTextContent('viewer'));
    expect(await screen.findByText(/do not have permission/i)).toBeInTheDocument();
    expect(screen.getByText(/no recurring schedule/i)).toBeInTheDocument();
    expect(screen.queryByText(/scheduling is not available yet/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /schedule daily/i })).not.toBeInTheDocument();
  });

  it('tells a viewer the truth about an existing active schedule while dark', async () => {
    // The one combination the other viewer tests miss: a read-only member
    // looking at a schedule the API still reports as `active`. The truthfulness
    // overrides are not role-gated, so a viewer must see `Unavailable` and no
    // next-run time — never the stale `Active` badge — while still getting the
    // permission copy rather than the editor-only dark note.
    asRole('viewer');
    const row = scheduleRow({ state: 'active' });
    server.use(http.get(SCHEDULE, () => HttpResponse.json(row)));
    const screen = render();

    await waitFor(() => expect(screen.getByTestId('resolved-role')).toHaveTextContent('viewer'));
    expect(await screen.findByText('Unavailable')).toBeInTheDocument();
    expect(screen.queryByText('Active')).not.toBeInTheDocument();
    expect(screen.queryByText(formatDateTime(row.next_run_at))).not.toBeInTheDocument();
    expect(screen.queryAllByRole('button', { hidden: true })).toHaveLength(0);
    // The editor-only dark note stays suppressed for a viewer. (The "no
    // permission" copy belongs to the empty-state branch and is deliberately
    // absent here: with a schedule present the viewer sees the read-only row.)
    expect(screen.queryByText(/scheduling is not available yet/i)).not.toBeInTheDocument();
  });

  it('reports an unrelated GET failure as an error, not as feature darkness', async () => {
    server.use(
      http.get(SCHEDULE, () =>
        HttpResponse.json({ error: { code: 'server_error', message: 'boom' } }, { status: 500 }),
      ),
    );
    const screen = render();

    // A 500 is retried twice with backoff before the error surfaces, so this
    // needs longer than the default 1s — the point is that it lands on the
    // error state, never on the dark-state copy.
    expect(
      await screen.findByRole('button', { name: /try again/i }, { timeout: 5000 }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/no recurring schedule/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/scheduling is not available yet/i)).not.toBeInTheDocument();
  });

  it('does not expose mutation controls while the capability state is still unknown', async () => {
    // A deep link to this route resolves the schedule GET before the capability
    // lookup, so the answer is briefly unknown. Unknown must render as dark.
    server.use(
      http.get(SCHEDULING_CAPABILITY, async () => {
        await new Promise((resolve) => setTimeout(resolve, 80));
        return HttpResponse.json({ enabled: true });
      }),
      http.get(SCHEDULE, notFound),
    );
    const screen = render();

    await screen.findByText(/scheduling is not available yet/i);
    // Before the lookup resolves the answer is unknown — that must read as dark.
    expect(screen.queryByRole('button', { name: /schedule daily/i })).not.toBeInTheDocument();
    // Once it resolves enabled, the affordance appears.
    expect(await screen.findByRole('button', { name: /schedule daily/i })).toBeInTheDocument();
  });
});

/**
 * Transition coverage. The dark -> enabled direction is covered above; this is
 * the reverse, which is the only direction that can strand component state
 * behind a closing gate.
 */
describe('SchedulePanel — capability transitions', () => {
  it('closes the delete confirmation when the capability goes dark and does not reopen it', async () => {
    server.use(http.get(SCHEDULE, () => HttpResponse.json(scheduleRow())));
    enableScheduling();
    const screen = render();

    // 1. Enabled: open the destructive confirmation.
    await screen.user.click(await screen.findByRole('button', { name: /delete/i }));
    expect(await screen.findByRole('button', { name: /delete schedule/i })).toBeInTheDocument();

    // 2. Capability goes dark. `staleTime` means the lookup will not refetch on
    //    its own, so the cache is invalidated explicitly — this models a
    //    long-lived session that re-reads it after the server changed (the
    //    operator console invalidates exactly this key on an override change).
    enableScheduling(false);
    invalidateCapability();
    await screen.findByText(/scheduling is not available yet/i);
    // Awaited, not asserted synchronously: the dialog unmounts through a portal,
    // so a bare query here races the unmount and can report 0 while the dialog
    // is still on screen.
    await waitFor(() => expect(screen.queryAllByText('Delete schedule')).toHaveLength(0));
    expect(screen.queryAllByRole('button', { hidden: true })).toHaveLength(0);

    // 3. Capability returns: the confirmation must NOT reappear on its own. A
    //    destructive dialog that reopens without a user action is the defect.
    enableScheduling(true);
    invalidateCapability();
    // Wait for the panel to actually come back before judging the dialog:
    // asserting absence mid-refetch would pass for the wrong reason.
    expect(await screen.findByText('Active', {}, { timeout: 4000 })).toBeInTheDocument();
    // THE DEFECT: the confirmation must not stand open again without the user
    // asking for it a second time. Queried by text, not by role — a reopened
    // dialog marks the rest of the tree `aria-hidden`, which silently hides it
    // from role queries and would make this assertion pass for the wrong reason.
    expect(screen.queryAllByText('Delete schedule')).toHaveLength(0);
    // And the panel beneath must be reachable rather than inert behind a modal.
    expect(await screen.findByRole('button', { name: /^delete$/i })).toBeInTheDocument();
  });

  it('treats a capability answer that omits the enabled key as dark', async () => {
    server.use(
      // Key absent entirely — must not read as enabled.
      http.get(SCHEDULING_CAPABILITY, () => HttpResponse.json({})),
      http.get(SCHEDULE, notFound),
    );
    const screen = render();

    expect(await screen.findByText(/scheduling is not available yet/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /schedule daily/i })).not.toBeInTheDocument();
  });

  it('treats a non-boolean truthy flag as dark rather than enabled', async () => {
    // `=== true` and a truthy check diverge only here. A string "true" from a
    // mis-serialised backend must read as dark, not as permission to mutate.
    server.use(
      http.get(SCHEDULING_CAPABILITY, () => HttpResponse.json({ enabled: 'true' })),
      http.get(SCHEDULE, notFound),
    );
    const screen = render();

    expect(await screen.findByText(/scheduling is not available yet/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /schedule daily/i })).not.toBeInTheDocument();
  });

  it('treats a failed capability lookup as dark, not as enabled', async () => {
    // The lookup is a separate request from the schedule itself. If it fails,
    // the capability is unknown — and unknown must not present mutation
    // controls the server would refuse.
    server.use(
      http.get(SCHEDULING_CAPABILITY, () =>
        HttpResponse.json({ error: { code: 'server_error', message: 'boom' } }, { status: 500 }),
      ),
      http.get(SCHEDULE, notFound),
    );
    const screen = render();

    expect(await screen.findByText(/scheduling is not available yet/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /schedule daily/i })).not.toBeInTheDocument();
  });
});

/**
 * P6-UI-006 — the panel reflects the SAME per-workspace decision the schedule
 * mutation gate enforces (P6-CAP-1), not the raw global flag. Before the fix the
 * panel read `features.scout_scheduling_enabled` from the runtime summary, so a
 * workspace override and the panel disagreed in both directions.
 */
describe('SchedulePanel — workspace-effective scheduling (P6-UI-006)', () => {
  it('offers controls when the workspace resolves enabled though the global flag is off', async () => {
    rawGlobalFlag(false); // global off …
    enableScheduling(true); // … but an override enables this workspace
    server.use(http.get(SCHEDULE, notFound));
    const screen = render();

    expect(await screen.findByRole('button', { name: /schedule daily/i })).toBeInTheDocument();
    expect(screen.queryByText(/scheduling is not available yet/i)).not.toBeInTheDocument();
  });

  it('withholds controls when the workspace resolves disabled though the global flag is on', async () => {
    rawGlobalFlag(true); // global on …
    enableScheduling(false); // … but a disable override (or the ceiling) applies here
    server.use(http.get(SCHEDULE, () => HttpResponse.json(scheduleRow())));
    const screen = render();

    expect(await screen.findByText(/scheduling is not available yet/i)).toBeInTheDocument();
    expect(screen.getByText('Unavailable')).toBeInTheDocument();
    for (const name of [/pause/i, /activate/i, /delete/i]) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument();
    }
  });

  it('asks about its own workspace and never consults the raw-global runtime summary', async () => {
    const asked: string[] = [];
    let summaryRead = false;
    server.use(
      http.get(P('/workspaces/:workspaceId/scheduling-capability'), ({ params }) => {
        asked.push(String(params.workspaceId));
        return HttpResponse.json({ enabled: true });
      }),
      http.get(CAPABILITIES, () => {
        summaryRead = true;
        return HttpResponse.json({});
      }),
      http.get(SCHEDULE, notFound),
    );
    const screen = render();

    await screen.findByRole('button', { name: /schedule daily/i });
    expect(asked).toEqual([WS]);
    expect(summaryRead).toBe(false);
  });
});

/**
 * P6-PLAT-6 — a failed scheduled tick is visible instead of silent.
 */
describe('SchedulePanel — tick failure visibility (P6-PLAT-6)', () => {
  it('reports a stalled schedule as a failure with an Activate restart', async () => {
    enableScheduling();
    server.use(
      http.get(SCHEDULE, () =>
        HttpResponse.json(
          scheduleRow({ state: 'stalled', last_tick_failed_at: '2026-07-18T09:05:00Z' }),
        ),
      ),
    );
    const screen = render();

    expect(await screen.findByText('Stalled')).toBeInTheDocument();
    expect(screen.getByText(/recurring runs stopped/i)).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /activate/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /pause/i })).not.toBeInTheDocument();
    // A stall must never read as "not started yet".
    expect(screen.queryByText(/has not been activated yet/i)).not.toBeInTheDocument();
  });

  it('notes a lost occurrence on a schedule that kept running', async () => {
    enableScheduling();
    server.use(
      http.get(SCHEDULE, () =>
        HttpResponse.json(scheduleRow({ last_tick_failed_at: '2026-07-18T09:05:00Z' })),
      ),
    );
    const screen = render();

    expect(await screen.findByText('Active')).toBeInTheDocument();
    expect(screen.getByText(/a scheduled run attempt failed/i)).toBeInTheDocument();
    expect(screen.getByText(/recurring runs continue/i)).toBeInTheDocument();
  });

  it('shows no failure note for a healthy schedule', async () => {
    enableScheduling();
    server.use(http.get(SCHEDULE, () => HttpResponse.json(scheduleRow())));
    const screen = render();

    expect(await screen.findByText('Active')).toBeInTheDocument();
    expect(screen.queryByText(/a scheduled run attempt failed/i)).not.toBeInTheDocument();
  });

  it('does not claim runs continue while scheduling is dark', async () => {
    server.use(
      http.get(SCHEDULE, () =>
        HttpResponse.json(scheduleRow({ last_tick_failed_at: '2026-07-18T09:05:00Z' })),
      ),
    );
    const screen = render();

    await screen.findByText(/scheduling is not available yet/i);
    expect(screen.queryByText(/recurring runs continue/i)).not.toBeInTheDocument();
  });
});
