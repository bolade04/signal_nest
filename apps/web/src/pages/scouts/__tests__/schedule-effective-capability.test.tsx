import { waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { API_PREFIX } from '@/api/config';
import {
  useClearCapabilityOverride,
  useSetCapabilityOverride,
} from '@/pages/operations/useOperations';
import { useScheduleCapability } from '@/pages/scouts/useScheduleActions';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * P6-UI-006 — the operator console and the customer schedule panel must agree about
 * the same workspace in the same session.
 *
 * Since P6-CAP-1 an operator scheduling override changes what the schedule mutation
 * gate does for that workspace, so the operator's override mutation must refresh the
 * customer's workspace-effective answer — not wait out its 60s staleTime, which a
 * mounted observer never does on its own (no refetch-on-focus, no interval).
 */
const P = (path: string) => `*${API_PREFIX}${path}`;
const WS_A = 'ws-1';
const WS_B = 'ws-2';
const CAP = (ws: string) => P(`/workspaces/${ws}/scheduling-capability`);

/** Renders the PRODUCTION hook's status, including while loading. */
function CapProbe({ workspaceId }: { workspaceId: string }) {
  const { status } = useScheduleCapability(workspaceId);
  return <span data-testid={`cap-${workspaceId}`}>{status}</span>;
}

function OperatorEnable({ workspaceId }: { workspaceId: string }) {
  const setOverride = useSetCapabilityOverride('org-1', workspaceId);
  return (
    <button
      type="button"
      onClick={() =>
        setOverride.mutate({
          organization_id: 'org-1',
          workspace_id: workspaceId,
          capability: 'scout_scheduling',
          enabled: true,
        })
      }
    >
      operator-enable
    </button>
  );
}

function OperatorClear({ workspaceId }: { workspaceId: string }) {
  const clearOverride = useClearCapabilityOverride('org-1', workspaceId);
  return (
    <button type="button" onClick={() => clearOverride.mutate('scout_scheduling')}>
      operator-clear
    </button>
  );
}

const overrideRow = (workspaceId: string) => ({
  id: 'ovr-1',
  organization_id: 'org-1',
  workspace_id: workspaceId,
  capability: 'scout_scheduling',
  enabled: true,
  reason: null,
  set_by_user_id: 'user-1',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
});

describe('P6-UI-006 — workspace-effective scheduling reflection', () => {
  it('reflects an operator enable in the same session, and only for that workspace', async () => {
    let enabled = false;
    let bFetches = 0;
    server.use(
      http.get(CAP(WS_A), () => HttpResponse.json({ enabled })),
      http.get(CAP(WS_B), () => {
        bFetches += 1;
        return HttpResponse.json({ enabled: false });
      }),
      http.put(P('/internal/system/capabilities/overrides'), () => {
        enabled = true;
        return HttpResponse.json(overrideRow(WS_A));
      }),
    );

    const screen = renderApp(
      <>
        <CapProbe workspaceId={WS_A} />
        <CapProbe workspaceId={WS_B} />
        <OperatorEnable workspaceId={WS_A} />
      </>,
      { route: '/operations' },
    );

    await waitFor(() => expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('disabled'));
    await waitFor(() => expect(screen.getByTestId(`cap-${WS_B}`)).toHaveTextContent('disabled'));
    const before = bFetches;

    await screen.user.click(screen.getByRole('button', { name: 'operator-enable' }));

    await waitFor(() => expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('enabled'));
    expect(screen.getByTestId(`cap-${WS_B}`)).toHaveTextContent('disabled');
    expect(bFetches).toBe(before);
  });

  it('reflects an operator CLEAR in the same session', async () => {
    let enabled = true;
    server.use(
      http.get(CAP(WS_A), () => HttpResponse.json({ enabled })),
      http.delete(P('/internal/system/capabilities/overrides'), () => {
        enabled = false;
        return HttpResponse.json({
          capability: 'scout_scheduling',
          workspace_id: WS_A,
          enabled: null,
          changed: true,
          created: false,
          override_id: null,
        });
      }),
    );

    const screen = renderApp(
      <>
        <CapProbe workspaceId={WS_A} />
        <OperatorClear workspaceId={WS_A} />
      </>,
      { route: '/operations' },
    );

    // Precondition: cached ENABLED, so the flip below can only come from invalidation.
    await waitFor(() => expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('enabled'));

    await screen.user.click(screen.getByRole('button', { name: 'operator-clear' }));

    await waitFor(() => expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('disabled'));
  });

  it('keeps one workspace answer per workspace — never a shared cache entry', async () => {
    server.use(
      http.get(CAP(WS_A), () => HttpResponse.json({ enabled: true })),
      http.get(CAP(WS_B), () => HttpResponse.json({ enabled: false })),
    );

    const screen = renderApp(
      <>
        <CapProbe workspaceId={WS_A} />
        <CapProbe workspaceId={WS_B} />
      </>,
    );

    await waitFor(() => expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('enabled'));
    await waitFor(() => expect(screen.getByTestId(`cap-${WS_B}`)).toHaveTextContent('disabled'));
  });

  it('reports loading, not disabled, before the lookup settles', async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get(CAP(WS_A), async () => {
        await gate;
        return HttpResponse.json({ enabled: false });
      }),
    );

    const screen = renderApp(<CapProbe workspaceId={WS_A} />);

    expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('loading');
    release();
    await waitFor(() => expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('disabled'));
  });
});
