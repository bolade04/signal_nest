import { delay, http, HttpResponse } from 'msw';
import { within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { resetCapabilityOverrides } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

const P = (path: string) => `*${API_PREFIX}${path}`;

// The MSW capability handlers are deliberately non-enumerating: any scope other
// than the demo org/workspace 404s. Every successful render below therefore
// also proves the page sent the active workspace's real organization_id +
// workspace_id — the tenant-scoping requirement — without a dedicated test.

const nonOperatorSession = () =>
  server.use(
    http.get(P('/auth/me'), () =>
      HttpResponse.json({
        access_token: 'test-token',
        token_type: 'bearer',
        user: { id: 'user-2', email: 'customer@signalnest.dev', full_name: 'Customer', is_operator: false },
        memberships: [{ organization_id: 'org-1', role: 'owner' }],
      }),
    ),
  );

/** The capability row container for a given label (waits for it to render). */
async function capabilityRow(
  screen: ReturnType<typeof renderApp>,
  label: string,
): Promise<HTMLElement> {
  const row = (await screen.findByText(label)).closest('div.rounded-md');
  if (!row) throw new Error(`no capability row for ${label}`);
  return row as HTMLElement;
}

beforeEach(() => resetCapabilityOverrides());

describe('operations page — operator access', () => {
  it('renders activation state, queue, workers, schedules and telemetry for an operator', async () => {
    const screen = renderApp(<App />, { route: '/operations' });

    expect(await screen.findByText('Capability activation')).toBeInTheDocument();

    // All three registered capabilities render with the dark default explained:
    // disabled, decided by the global configuration.
    for (const label of ['Opportunity Feedback', 'Scout Scheduling', 'RSS Connector']) {
      expect(await screen.findByText(label)).toBeInTheDocument();
    }
    expect(screen.getAllByText('Disabled')).toHaveLength(3);
    expect(screen.getAllByText('Global configuration')).toHaveLength(3);

    // Queue health from the composed overview read.
    expect(await screen.findByText('Stuck jobs')).toBeInTheDocument();
    expect(screen.getByText('Dead-lettered')).toBeInTheDocument();

    // Worker fleet + schedules.
    expect(screen.getByText('Active workers')).toBeInTheDocument();
    expect(screen.getByText('Stale workers')).toBeInTheDocument();
    expect(screen.getByText('Total schedules')).toBeInTheDocument();

    // Telemetry posture is secret-free status data.
    expect(screen.getByText('Logging format')).toBeInTheDocument();
    expect(screen.getByText('json')).toBeInTheDocument();
    expect(screen.getByText('Redaction')).toBeInTheDocument();

    // The operator sees the Operations nav entry.
    expect(screen.getAllByRole('link', { name: /operations/i }).length).toBeGreaterThan(0);
  });

  it('shows loading skeletons while the overview is in flight', async () => {
    server.use(
      http.get(P('/internal/system/overview'), async () => {
        await delay(150);
        return HttpResponse.json({
          as_of: '2026-06-01T12:00:00Z',
          stale_after_seconds: 120,
          jobs: { total: 0, stuck_count: 0, dead_letter_count: 0, status_counts: {} },
          workers: { active_count: 0, stale_count: 0, status_counts: {} },
          schedules: { total: 0, state_counts: {} },
        });
      }),
    );

    const screen = renderApp(<App />, { route: '/operations' });
    expect(await screen.findByText('Queue health')).toBeInTheDocument();
    // Loading rows are announced while the query is in flight…
    expect(screen.container.querySelectorAll('[aria-busy="true"]').length).toBeGreaterThan(0);
    // …and resolve to the empty numbers.
    expect(await screen.findByText('Total jobs')).toBeInTheDocument();
    expect(screen.getAllByText('No records yet.').length).toBeGreaterThan(0);
  });

  it('shows a recoverable error state when an operator read fails', async () => {
    server.use(
      http.get(P('/internal/system/overview'), () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 }),
      ),
    );

    const screen = renderApp(<App />, { route: '/operations' });

    // The failing overview surfaces error states with a retry affordance…
    const alerts = await screen.findAllByRole('alert');
    expect(alerts.length).toBeGreaterThan(0);
    expect(screen.getAllByRole('button', { name: /try again/i }).length).toBeGreaterThan(0);
    // …while the independent capability section still renders its data.
    expect(await screen.findByText('Opportunity Feedback')).toBeInTheDocument();
  });
});

describe('operations page — override mutations', () => {
  it('sets a workspace override behind an explicit confirmation and reflects the new state', async () => {
    const screen = renderApp(<App />, { route: '/operations' });
    const { user } = screen;

    const row = await capabilityRow(screen, 'Opportunity Feedback');
    await user.click(await within(row).findByRole('button', { name: /^enable/i }));

    // Nothing mutates until the confirmation is accepted.
    expect(await screen.findByText(/enable opportunity feedback in this workspace\?/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^confirm$/i }));

    // Success toast + the row now shows the override as the deciding rule.
    expect(await screen.findByText(/opportunity feedback override set/i)).toBeInTheDocument();
    expect(await within(row).findByText('Enabled')).toBeInTheDocument();
    expect(within(row).getByText('Workspace override')).toBeInTheDocument();
    // The other capabilities are untouched — per-workspace, per-capability only.
    const sibling = await capabilityRow(screen, 'Scout Scheduling');
    expect(within(sibling).getByText('Disabled')).toBeInTheDocument();
  });

  it('clears an override behind a destructive confirmation, returning to the dark default', async () => {
    const screen = renderApp(<App />, { route: '/operations' });
    const { user } = screen;

    const row = await capabilityRow(screen, 'Scout Scheduling');
    await user.click(await within(row).findByRole('button', { name: /^disable/i }));
    await user.click(await screen.findByRole('button', { name: /^confirm$/i }));
    expect(await within(row).findByText('Workspace override')).toBeInTheDocument();

    await user.click(within(row).getByRole('button', { name: /^clear/i }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /clear override/i }));

    expect(await screen.findByText(/override cleared for scout scheduling/i)).toBeInTheDocument();
    expect(await within(row).findByText('Global configuration')).toBeInTheDocument();
    expect(within(row).getByText('Disabled')).toBeInTheDocument();
  });

  it('never offers per-workspace enablement for a capability the registry prohibits', async () => {
    const screen = renderApp(<App />, { route: '/operations' });

    const row = await capabilityRow(screen, 'RSS Connector');
    const enable = await within(row).findByRole('button', { name: /^enable/i });
    // Driven by the registry's workspace_enableable=false — not a hardcoded list.
    expect(enable).toBeDisabled();
    // Deny-biased asymmetry: disabling is still allowed.
    expect(within(row).getByRole('button', { name: /^disable/i })).toBeEnabled();
  });

  it('keeps a stored-but-un-honored override visible and clearable', async () => {
    // The resolver reports has_override=false for a stored row it refuses to
    // honor (e.g. an enable row on a capability that is no longer
    // workspace-enableable). The row must still be shown and clearable — an
    // operator cannot be locked out of removing a row that exists.
    server.use(
      http.get(P('/internal/system/capabilities/effective'), () =>
        HttpResponse.json({
          items: [
            {
              capability: 'connector_rss',
              workspace_id: 'ws-1',
              effective_enabled: false,
              decided_by: 'secure_default',
              global_flag: false,
              has_override: false,
              override_value: null,
            },
          ],
        }),
      ),
      http.get(P('/internal/system/capabilities/overrides'), () =>
        HttpResponse.json({
          items: [
            {
              id: 'ovr-stale',
              organization_id: 'org-1',
              workspace_id: 'ws-1',
              capability: 'connector_rss',
              enabled: true,
              reason: 'set before the policy changed',
              set_by_user_id: 'user-1',
              created_at: '2026-06-01T12:00:00Z',
              updated_at: '2026-06-01T12:00:00Z',
            },
          ],
          total: 1,
          limit: 50,
          offset: 0,
        }),
      ),
    );

    const screen = renderApp(<App />, { route: '/operations' });

    const row = await capabilityRow(screen, 'RSS Connector');
    expect(await within(row).findByText(/not honored — secure default applies/i)).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: /^clear/i })).toBeEnabled();
    expect(within(row).getByText('Secure default')).toBeInTheDocument();
  });

  it('surfaces a failed mutation without pretending success', async () => {
    server.use(
      http.put(P('/internal/system/capabilities/overrides'), () =>
        HttpResponse.json({ detail: 'capability_override_not_permitted' }, { status: 422 }),
      ),
    );

    const screen = renderApp(<App />, { route: '/operations' });
    const { user } = screen;

    const row = await capabilityRow(screen, 'Opportunity Feedback');
    await user.click(await within(row).findByRole('button', { name: /^enable/i }));
    await user.click(await screen.findByRole('button', { name: /^confirm$/i }));

    // Error toast, and the effective state is unchanged — no optimistic flip.
    expect(await screen.findByText(/override change failed/i)).toBeInTheDocument();
    expect(within(row).getByText('Disabled')).toBeInTheDocument();
    expect(within(row).getByText('Global configuration')).toBeInTheDocument();
  });
});

describe('operations page — non-operator rejection', () => {
  it('renders not-found in place for a deep-linked non-operator and hides the nav entry', async () => {
    nonOperatorSession();

    // Spy on every internal operator endpoint: a customer session must never
    // ISSUE these requests, not merely fail to render their data.
    const internalHits: string[] = [];
    server.use(
      http.all(P('/internal/:rest*'), ({ request }) => {
        internalHits.push(new URL(request.url).pathname);
        return HttpResponse.json({ detail: 'forbidden' }, { status: 403 });
      }),
    );

    const screen = renderApp(<App />, { route: '/operations' });

    // The guard renders the not-found page in place (no redirect, no data).
    expect(await screen.findByText(/page not found/i)).toBeInTheDocument();
    expect(screen.queryByText('Capability activation')).not.toBeInTheDocument();
    expect(screen.queryByText('Opportunity Feedback')).not.toBeInTheDocument();

    // The sidebar never shows the operator entry point to a non-operator.
    expect(screen.queryByRole('link', { name: /operations/i })).not.toBeInTheDocument();

    // And no internal operator request was ever issued by the customer session.
    expect(internalHits).toEqual([]);
  });
});
