import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * P6-DATA-3: an address lookup that finds no single place is not a dead end. The
 * API answers with a distinct error code and a message saying how to continue; the
 * dialog keeps that message on screen next to the coordinates, and the location can
 * still be saved with its city, state / province and country typed in.
 */

const P = (path: string) => `*${API_PREFIX}${path}`;

const UNRESOLVABLE =
  "We couldn't find that place. Check the spelling, or add the city and country (for example \"London, Ontario, Canada\"). You can still save this location: enter its city, state or province and country, which is what scouting uses.";
const NOT_CONFIGURED =
  'Address lookup is not available in this environment. You can still save this location: enter its city, state or province and country, which is what scouting uses.';

function failGeocode(status: number, code: string, message: string) {
  server.use(
    http.post(P('/geocode'), () =>
      HttpResponse.json({ error: { code, message, request_id: 'req-1' } }, { status }),
    ),
  );
}

async function openAddDialog() {
  const screen = renderApp(<App />, { route: '/locations' });
  const add = await screen.findAllByRole('button', { name: /^add location$/i });
  await screen.user.click(add[0]!);
  const dialog = await screen.findByRole('dialog', { name: /add location/i });
  return { screen, dialog };
}

describe('location dialog — an unresolvable address is not a dead end', () => {
  it('shows why the lookup failed and how to continue, and still saves the location', async () => {
    failGeocode(422, 'location_unresolvable', UNRESOLVABLE);
    let created: Record<string, unknown> | null = null;
    server.use(
      http.post(P('/workspaces/:ws/locations'), async ({ request }) => {
        created = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          id: 'loc-new',
          brand_id: 'brand-1',
          workspace_id: 'ws-1',
          is_active: true,
          languages: [],
          local_social_links: {},
          local_competitors: [],
          overrides: {},
          ...created,
        });
      }),
    );
    const { screen, dialog } = await openAddDialog();
    await screen.user.type(within(dialog).getByLabelText(/location name/i), 'London Ontario shop');
    await screen.user.type(within(dialog).getByLabelText(/street address/i), '1 Nowhere Lane');
    await screen.user.click(within(dialog).getByRole('button', { name: /geocode/i }));

    const note = await within(dialog).findByRole('status');
    expect(note).toHaveTextContent(UNRESOLVABLE);
    // No coordinates were invented.
    expect(within(dialog).getByText('Not set — enter an address and geocode.')).toBeInTheDocument();

    // The way on: type the place and save.
    await screen.user.type(within(dialog).getByLabelText(/^city/i), 'London');
    await screen.user.type(within(dialog).getByLabelText(/state \/ province/i), 'Ontario');
    await screen.user.type(within(dialog).getByLabelText(/^country/i), 'Canada');
    await screen.user.click(within(dialog).getByRole('button', { name: /^add location$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(created).toMatchObject({ city: 'London', state_province: 'Ontario', country: 'Canada' });
  });

  it('says so when address lookup is not configured', async () => {
    failGeocode(503, 'geocoder_not_configured', NOT_CONFIGURED);
    const { screen, dialog } = await openAddDialog();
    await screen.user.type(within(dialog).getByLabelText(/street address/i), 'Dallas');
    await screen.user.click(within(dialog).getByRole('button', { name: /geocode/i }));
    expect(await within(dialog).findByRole('status')).toHaveTextContent(NOT_CONFIGURED);
  });

  it('clears the note once a later lookup succeeds', async () => {
    failGeocode(422, 'location_ambiguous', 'That matches more than one place.');
    const { screen, dialog } = await openAddDialog();
    await screen.user.type(within(dialog).getByLabelText(/street address/i), 'London');
    await screen.user.click(within(dialog).getByRole('button', { name: /geocode/i }));
    expect(await within(dialog).findByRole('status')).toHaveTextContent('more than one place');

    server.use(
      http.post(P('/geocode'), () =>
        HttpResponse.json({
          latitude: 32.7767,
          longitude: -96.797,
          city: 'Dallas',
          state_province: 'Texas',
          country: 'United States',
          timezone: 'America/Chicago',
          confidence: 0.95,
          country_code: 'US',
          region_code: 'US-TX',
          label: 'Dallas, Texas, United States',
          provider: 'fixture',
        }),
      ),
    );
    await screen.user.click(within(dialog).getByRole('button', { name: /geocode/i }));
    expect(await within(dialog).findByText('32.7767, -96.7970')).toBeInTheDocument();
    expect(within(dialog).queryByRole('status')).not.toBeInTheDocument();
    expect(within(dialog).getByLabelText(/^country/i)).toHaveValue('United States');
  });
});
