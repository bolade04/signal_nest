import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { locationModel, orgModel, type ModelRole } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

const P = (path: string) => `*${API_PREFIX}${path}`;

describe('locations', () => {
  beforeEach(() => {
    orgModel.reset();
    locationModel.reset();
  });

  it('populates the edit dialog from the selected location', async () => {
    // Regression guard for the effect→render-phase migration of the form-reset
    // logic in LocationDialog. Opening the dialog for a specific location must
    // seed the form with that location's data via the guarded render-phase reset
    // (now keyed on the location id rather than the object reference).
    const screen = renderApp(<App />, { route: '/locations', activeLocation: 'loc-dallas' });

    const editButtons = await screen.findAllByRole('button', { name: /edit & service area/i });
    // Fixtures list Dallas first (see test/handlers CITIES order).
    await screen.user.click(editButtons[0]!);

    const dialog = await screen.findByRole('dialog', { name: /edit location/i });
    expect(within(dialog).getByLabelText(/location name/i)).toHaveValue('Dallas');
  });

  it('shows an active location as Active (the API always sends is_active)', async () => {
    const screen = renderApp(<App />, { route: '/locations' });
    await screen.findByText('Dallas');
    expect(screen.getAllByText(/^Active$/)).toHaveLength(4);
    expect(screen.queryByText(/^Deactivated$/)).not.toBeInTheDocument();
  });
});

describe('locations: deactivate and reactivate (P6-UI-023)', () => {
  beforeEach(() => {
    orgModel.reset();
    locationModel.reset();
  });

  it('deactivates after confirmation, keeps the location listed, and reactivates', async () => {
    const screen = renderApp(<App />, { route: '/locations' });
    await screen.findByText('Dallas');
    await screen.user.click(screen.getAllByRole('button', { name: /^deactivate$/i })[0]!);

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/deactivate dallas\?/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/opportunities, scout history and service area are kept/i)).toBeInTheDocument();
    await screen.user.click(within(dialog).getByRole('button', { name: /^deactivate$/i }));

    await waitFor(() => expect(locationModel.isActive('loc-dallas')).toBe(false));
    expect(await screen.findByText(/^Deactivated$/)).toBeInTheDocument();
    expect(screen.getByText('Dallas')).toBeInTheDocument(); // still listed
    expect(screen.getByText(/no new scouts run here/i)).toBeInTheDocument();

    await screen.user.click(await screen.findByRole('button', { name: /^reactivate$/i }));
    await waitFor(() => expect(locationModel.isActive('loc-dallas')).toBe(true));
    await waitFor(() => expect(screen.queryByText(/^Deactivated$/)).not.toBeInTheDocument());
    expect(screen.getAllByRole('button', { name: /^deactivate$/i })).toHaveLength(4);
  });

  it('shows the server refusal and leaves the location active', async () => {
    server.use(
      http.post(P('/workspaces/:ws/locations/:id/deactivate'), () =>
        HttpResponse.json(
          {
            error: {
              code: 'conflict',
              message: "Pause this location's scheduled scouts before deactivating it.",
              request_id: 'req-test',
            },
          },
          { status: 409 },
        ),
      ),
    );
    const screen = renderApp(<App />, { route: '/locations' });
    await screen.findByText('Dallas');
    await screen.user.click(screen.getAllByRole('button', { name: /^deactivate$/i })[0]!);
    const dialog = await screen.findByRole('dialog');
    await screen.user.click(within(dialog).getByRole('button', { name: /^deactivate$/i }));

    expect(
      await screen.findByText(/pause this location's scheduled scouts before deactivating it/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^Deactivated$/)).not.toBeInTheDocument();
    expect(screen.getAllByText(/^Active$/)).toHaveLength(4);
  });

  const ROLES: ModelRole[] = ['owner', 'admin', 'marketer', 'reviewer', 'compliance_reviewer', 'viewer'];
  const EDITOR = new Set<ModelRole>(['owner', 'admin', 'marketer']);

  it.each(ROLES)('offers the controls only to editors: %s', async (role) => {
    // The backend gate is require_role(OWNER, ADMIN, MARKETER), written out here rather
    // than read from lib/roles.ts, as in role-control-matrix.test.tsx.
    orgModel.setMembership('org-1', 'user-1', role);
    locationModel.deactivate('loc-london');
    const screen = renderApp(<App />, { route: '/locations' });
    await screen.findByText('Dallas');
    // The read surface is the same for everyone: the state of every location is shown.
    expect(await screen.findByText(/^Deactivated$/)).toBeInTheDocument();
    // Wait for the role to resolve: the edit/view label is itself role-gated.
    const label = EDITOR.has(role) ? /^edit & service area$/i : /^view details & service area$/i;
    await waitFor(() => expect(screen.getAllByRole('button', { name: label })).toHaveLength(4));
    if (EDITOR.has(role)) {
      // Positive instance: an editor is offered the control on every location.
      expect(screen.getAllByRole('button', { name: /^deactivate$/i })).toHaveLength(3);
      expect(screen.getAllByRole('button', { name: /^reactivate$/i })).toHaveLength(1);
    } else {
      expect(screen.queryByRole('button', { name: /^deactivate$/i })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /^reactivate$/i })).not.toBeInTheDocument();
    }
  });
});
