import { waitFor, within } from '@testing-library/react';
import { getResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { handlers, orgModel } from '@/test/handlers';
import { renderProductionApp } from '@/test/utils';

/**
 * UI-AUDIT F2 / P2: Geocode in the location dialog always failed with "Workspace Id:
 * Field required". POST /geocode resolves its tenant through get_tenant_context, so
 * `workspace_id` is a required query parameter (apps/api/openapi.json); the client sent
 * none and the old MSW handler accepted that. The handler now refuses it as the API does,
 * and the request must carry the ACTIVE workspace, including after a workspace switch.
 */

const SECOND_WORKSPACE = {
  id: 'ws-2',
  name: 'Second Workspace',
  slug: 'second-ws',
  onboarding_completed: true,
  created_at: '2026-01-01T00:00:00Z',
};

type Screen = ReturnType<typeof renderProductionApp>;

const geocodeRequests = () =>
  orgModel.requests().filter((r) => r.method === 'POST' && r.path === `${API_PREFIX}/geocode`);

async function geocodeDallas(screen: Screen) {
  const add = await screen.findAllByRole('button', { name: /^add location$/i });
  await screen.user.click(add[0]!);
  const dialog = await screen.findByRole('dialog', { name: /add location/i });
  await screen.user.type(within(dialog).getByLabelText(/street address/i), 'Dallas');
  await screen.user.click(within(dialog).getByRole('button', { name: /geocode/i }));
  return dialog;
}

async function switchWorkspace(screen: Screen, name: string, id: string) {
  const [trigger] = screen.getAllByRole('combobox', { name: /^workspace$/i });
  await screen.user.click(trigger!);
  await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name }));
  await waitFor(() => expect(localStorage.getItem('signalnest-active-workspace')).toBe(id));
}

beforeEach(() => {
  orgModel.reset();
});

describe('geocode carries the active workspace (UI-AUDIT P2)', () => {
  it('sends ?workspace_id= of the active workspace and fills the coordinates', async () => {
    const screen = renderProductionApp(<App />, '/locations', { token: 'test-token' });
    const dialog = await geocodeDallas(screen);

    expect(await within(dialog).findByText('32.7767, -96.7970')).toBeInTheDocument();
    expect(within(dialog).queryByRole('status')).toBeNull();
    expect(geocodeRequests().map((r) => r.search)).toEqual(['?workspace_id=ws-1']);
  });

  it('follows a workspace switch: the next lookup names the newly active workspace', async () => {
    orgModel.addWorkspace('org-1', SECOND_WORKSPACE);
    const screen = renderProductionApp(<App />, '/locations', { token: 'test-token' });
    let dialog = await geocodeDallas(screen);
    expect(await within(dialog).findByText('32.7767, -96.7970')).toBeInTheDocument();
    await screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /add location/i })).not.toBeInTheDocument());

    await switchWorkspace(screen, 'Second Workspace', 'ws-2');
    dialog = await geocodeDallas(screen);
    expect(await within(dialog).findByText('32.7767, -96.7970')).toBeInTheDocument();

    expect(geocodeRequests().map((r) => r.search)).toEqual(['?workspace_id=ws-1', '?workspace_id=ws-2']);
  });

  it('the fixture refuses a lookup without workspace_id exactly as the API does (422, query.workspace_id missing)', async () => {
    const response = await getResponse(
      handlers,
      new Request(`http://localhost${API_PREFIX}/geocode`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer test-token' },
        body: JSON.stringify({ query: 'Dallas' }),
      }),
    );
    expect(response?.status).toBe(422);
    expect(await response!.json()).toMatchObject({
      error: {
        code: 'validation_error',
        message: 'Request validation failed',
        details: [{ type: 'missing', loc: ['query', 'workspace_id'], msg: 'Field required' }],
      },
    });
  });

  it('the fixture refuses a workspace the caller is not a member of (403), like get_tenant_context', async () => {
    orgModel.addOrganization({ id: 'org-x', name: 'Other Org', slug: 'other-org' }, [
      { id: 'ws-x', name: 'Other Workspace', slug: 'other-ws', onboarding_completed: true, created_at: '2026-01-01T00:00:00Z' },
    ]);
    const response = await getResponse(
      handlers,
      new Request(`http://localhost${API_PREFIX}/geocode?workspace_id=ws-x`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer test-token' },
        body: JSON.stringify({ query: 'Dallas' }),
      }),
    );
    expect(response?.status).toBe(403);
  });
});
