import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { API_PREFIX } from '@/api/config';
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
  return {
    id: 'sched-1',
    scout_request_id: REQ,
    location_id: 'loc-dallas',
    interval: 'daily',
    state: 'active',
    enabled: true,
    next_run_at: '2026-07-18T09:00:00Z',
    last_tick_at: '2026-07-17T09:00:00Z',
    created_at: '2026-07-17T08:00:00Z',
    updated_at: '2026-07-17T09:00:00Z',
    ...overrides,
  };
}

const notFound = () => HttpResponse.json({ error: { code: 'not_found', message: 'no schedule' } }, { status: 404 });

function render() {
  return renderApp(<SchedulePanel workspaceId={WS} requestId={REQ} />, {
    route: `/scouts/${REQ}`,
  });
}

describe('SchedulePanel', () => {
  it('offers daily and weekly creation when no schedule exists (editor)', async () => {
    server.use(http.get(SCHEDULE, notFound));
    const screen = render();

    expect(await screen.findByRole('button', { name: /schedule daily/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /schedule weekly/i })).toBeInTheDocument();
  });

  it('creates a schedule and reflects the active state after refetch', async () => {
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
    server.use(http.get(SCHEDULE, () => HttpResponse.json(scheduleRow({ state: 'activation_required' }))));
    const screen = render();

    expect(await screen.findByText('Activation required')).toBeInTheDocument();
    expect(screen.getByText(/has not been activated yet/i)).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /activate/i })).toBeInTheDocument();
  });

  it('pauses an active schedule', async () => {
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

  it('surfaces the feature-disabled (503) response as a graceful error', async () => {
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
    // The create affordance remains so the user can retry once the feature is on.
    expect(screen.getByRole('button', { name: /schedule daily/i })).toBeInTheDocument();
  });

  it('hides mutation controls from a view-only member', async () => {
    asRole('viewer');
    server.use(http.get(SCHEDULE, () => HttpResponse.json(scheduleRow())));
    const screen = render();

    expect(await screen.findByText('Active')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /pause/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument();
  });
});
