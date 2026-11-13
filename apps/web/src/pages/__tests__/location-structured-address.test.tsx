import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import type { GeoCoverageOut } from '@/api/types';
import {
  ADDRESS_HELP,
  CONFIRM_LABEL,
  SAVE_FIRST,
  TOWN_DIFFERS,
  WITHDRAW_LABEL,
} from '@/pages/locations/addressCopy';
import { locationModel, orgModel } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * W21 ADDR: the structured business address on the Locations dialog's Service area tab, and the
 * customer's confirmation that the business town lies within the location's area.
 *
 * - W-1 / W-2: the five fields open empty for a new location, and for a stored rule without
 *   them, even when the location's own city, state and country are filled: nothing is copied
 *   from the location (a copy would make the address agree with it by construction).
 * - W-3 / W-4: the save sends exactly what was typed (trimmed; empty is null), and a reopening
 *   shows what is stored.
 * - W-5: the confirmation is offered only when the STORED town differs from the city, is
 *   disabled while the fields it depends on have unsaved edits, and confirms or withdraws
 *   through its own route.
 * - W-6: the copy says customer-confirmed and never verified.
 * - W-7: a member who may not edit sees values and status as text, with no control.
 * - W-8: the API's fixed field message is shown as the API words it.
 */

const P = (path: string) => `*${API_PREFIX}${path}`;
const DALLAS = 'loc-dallas';
const FIELDS = [
  'Business street',
  'Business town or locality',
  'Business state or province',
  'Business postcode',
  'Business country',
] as const;
const KEYS = {
  'Business street': 'business_address_street',
  'Business town or locality': 'business_address_locality',
  'Business state or province': 'business_address_region',
  'Business postcode': 'business_address_postcode',
  'Business country': 'business_address_country',
} as const;

function rule(fields: Partial<GeoCoverageOut> = {}): GeoCoverageOut {
  return {
    id: `geo-${DALLAS}`,
    location_id: DALLAS,
    coverage_type: 'radius',
    business_address: null,
    center_latitude: null,
    center_longitude: null,
    radius_miles: 25,
    country: null,
    state: null,
    included_markets: [],
    excluded_markets: [],
    online_global: false,
    business_address_street: null,
    business_address_locality: null,
    business_address_country: null,
    business_address_region: null,
    business_address_postcode: null,
    locality_matches_city: null,
    locality_confirmed_at: null,
    locality_confirmation_applies: false,
    ...fields,
  };
}

/** Irving, a town of Dallas County, stored and unconfirmed: the API answers "differs". */
const IRVING = rule({
  business_address_street: '600 E Las Colinas Blvd',
  business_address_locality: 'Irving',
  business_address_country: 'United States',
  business_address_region: 'Texas',
  business_address_postcode: '75039',
  locality_matches_city: false,
});

interface Store {
  rules: Map<string, GeoCoverageOut>;
  puts: Record<string, unknown>[];
  confirmations: boolean[];
}

function serve(initial: Record<string, GeoCoverageOut>): Store {
  const store: Store = { rules: new Map(Object.entries(initial)), puts: [], confirmations: [] };
  server.use(
    http.get(P('/workspaces/:ws/locations/:id/geo-coverage'), ({ params }) => {
      const found = store.rules.get(String(params.id));
      if (found) return HttpResponse.json(found);
      return HttpResponse.json(
        { error: { code: 'not_found', message: 'No Scout Reach configured for this location yet.', request_id: 'r' } },
        { status: 404 },
      );
    }),
    http.put(P('/workspaces/:ws/locations/:id/geo-coverage'), async ({ request, params }) => {
      const id = String(params.id);
      const body = (await request.json()) as Record<string, unknown>;
      store.puts.push(body);
      const saved = { ...(store.rules.get(id) ?? rule({ location_id: id, id: `geo-${id}` })), ...body } as GeoCoverageOut;
      store.rules.set(id, saved);
      return HttpResponse.json(saved);
    }),
    http.put(P('/workspaces/:ws/locations/:id/geo-coverage/locality-confirmation'), async ({ request, params }) => {
      const id = String(params.id);
      const { confirmed } = (await request.json()) as { confirmed: boolean };
      store.confirmations.push(confirmed);
      const current = store.rules.get(id)!;
      const next = {
        ...current,
        locality_confirmation_applies: confirmed,
        locality_confirmed_at: confirmed ? '2026-11-12T09:30:00Z' : null,
      };
      store.rules.set(id, next);
      return HttpResponse.json(next);
    }),
  );
  return store;
}

type Screen = ReturnType<typeof renderApp>;

async function openDallas(screen: Screen, button = /^edit & service area$/i, title = /edit location/i) {
  await screen.findByText('Dallas');
  await waitFor(() => expect(screen.getAllByRole('button', { name: button })).toHaveLength(4));
  await screen.user.click(screen.getAllByRole('button', { name: button })[0]!);
  return screen.findByRole('dialog', { name: title });
}

async function serviceArea(screen: Screen, dialog: HTMLElement) {
  await screen.user.click(within(dialog).getByRole('tab', { name: /service area/i }));
}

async function save(screen: Screen, dialog: HTMLElement, label = /^save changes$/i) {
  const button = within(dialog).getByRole('button', { name: label });
  await waitFor(() => expect(button).toBeEnabled());
  await screen.user.click(button);
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
}

beforeEach(() => {
  orgModel.reset();
  locationModel.reset();
});

describe('location dialog: the structured business address (W21 ADDR)', () => {
  it('opens empty for a new location even when its city, state and country are filled', async () => {
    const store = serve({});
    const screen = renderApp(<App />, { route: '/locations' });
    await screen.findByText('Dallas');
    await screen.user.click(screen.getAllByRole('button', { name: /^add location$/i })[0]!);
    const dialog = await screen.findByRole('dialog', { name: /add location/i });
    await screen.user.type(within(dialog).getByLabelText(/^location name/i), 'Plano');
    await screen.user.type(within(dialog).getByLabelText(/^city/i), 'Plano');
    await screen.user.type(within(dialog).getByLabelText(/^state \/ province/i), 'TX');
    await screen.user.type(within(dialog).getByLabelText(/^country/i), 'US');
    await serviceArea(screen, dialog);
    for (const label of FIELDS) expect(within(dialog).getByLabelText(label)).toHaveValue('');
    expect(within(dialog).getByText(ADDRESS_HELP)).toBeInTheDocument();
    expect(ADDRESS_HELP).toMatch(/Provided by your team\.$/);
    expect(dialog.textContent ?? '').not.toMatch(/verif/i);
    await save(screen, dialog, /^add location$/i);
    expect(store.puts).toHaveLength(1);
    for (const key of Object.values(KEYS)) expect(store.puts[0]![key]).toBeNull();
  });

  it('opens empty for a stored rule without them; nothing is copied from the location', async () => {
    serve({ [DALLAS]: rule() });
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen);
    await screen.user.click(within(dialog).getByRole('tab', { name: /details/i }));
    expect(within(dialog).getByLabelText(/^city/i)).toHaveValue('Dallas');
    await serviceArea(screen, dialog);
    for (const label of FIELDS) expect(within(dialog).getByLabelText(label)).toHaveValue('');
  });

  it('saves exactly what was typed, trimmed, and an emptied field as null', async () => {
    const store = serve({ [DALLAS]: IRVING });
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen);
    await serviceArea(screen, dialog);
    const town = within(dialog).getByLabelText('Business town or locality');
    await waitFor(() => expect(town).toHaveValue('Irving'));
    await screen.user.clear(town);
    await screen.user.type(town, '  Dallas  ');
    await screen.user.clear(within(dialog).getByLabelText('Business postcode'));
    await save(screen, dialog);
    expect(store.puts).toHaveLength(1);
    expect(store.puts[0]).toMatchObject({
      business_address_street: '600 E Las Colinas Blvd',
      business_address_locality: 'Dallas',
      business_address_region: 'Texas',
      business_address_country: 'United States',
      business_address_postcode: null,
    });
  });

  it('shows the stored values when reopened', async () => {
    serve({ [DALLAS]: IRVING });
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen);
    await serviceArea(screen, dialog);
    await waitFor(() => expect(within(dialog).getByLabelText('Business street')).toHaveValue('600 E Las Colinas Blvd'));
    expect(within(dialog).getByLabelText('Business state or province')).toHaveValue('Texas');
    expect(within(dialog).getByLabelText('Business country')).toHaveValue('United States');
    expect(within(dialog).getByLabelText('Business postcode')).toHaveValue('75039');
  });

  it('shows the API\'s fixed field message for an unreadable country', async () => {
    serve({ [DALLAS]: rule() });
    server.use(
      http.put(P('/workspaces/:ws/locations/:id/geo-coverage'), () =>
        HttpResponse.json(
          {
            error: {
              code: 'validation_error',
              message: 'Request validation failed',
              request_id: 'r',
              details: [
                {
                  type: 'value_error',
                  loc: ['body', 'business_address_country'],
                  msg: 'Value error, Enter a country by name or two-letter code, such as Nigeria or NG.',
                },
              ],
            },
          },
          { status: 422 },
        ),
      ),
    );
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen);
    await serviceArea(screen, dialog);
    await screen.user.type(within(dialog).getByLabelText('Business country'), 'Narnia');
    const button = within(dialog).getByRole('button', { name: /^save changes$/i });
    await waitFor(() => expect(button).toBeEnabled());
    await screen.user.click(button);
    expect(
      await within(dialog).findByText(
        'Business address country: Value error, Enter a country by name or two-letter code, such as Nigeria or NG.',
      ),
    ).toBeInTheDocument();
    expect(within(dialog).queryByText(/Narnia/)).not.toBeInTheDocument();
  });
});

describe('location dialog: the locality confirmation (W21 ADDR)', () => {
  it('is offered only when the stored town differs from the city', async () => {
    serve({ [DALLAS]: rule({ business_address_locality: 'Dallas', locality_matches_city: true }) });
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen);
    await serviceArea(screen, dialog);
    await waitFor(() => expect(within(dialog).getByLabelText('Business town or locality')).toHaveValue('Dallas'));
    expect(within(dialog).queryByText(TOWN_DIFFERS)).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: CONFIRM_LABEL })).not.toBeInTheDocument();
  });

  it('confirms the saved town, says customer-confirmed and never verified, and withdraws', async () => {
    const store = serve({ [DALLAS]: IRVING });
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen);
    await serviceArea(screen, dialog);
    expect(await within(dialog).findByText(TOWN_DIFFERS)).toBeInTheDocument();
    await screen.user.click(within(dialog).getByRole('button', { name: CONFIRM_LABEL }));
    expect(await within(dialog).findByText(/^Confirmed by your team on /)).toBeInTheDocument();
    expect(store.confirmations).toEqual([true]);
    // Customer data is labelled as provided or confirmed by the customer, never as verified.
    expect(dialog.textContent ?? '').not.toMatch(/verif/i);
    expect(store.puts).toEqual([]); // the confirmation is its own request, of the saved values

    await screen.user.click(within(dialog).getByRole('button', { name: WITHDRAW_LABEL }));
    await waitFor(() => expect(store.confirmations).toEqual([true, false]));
    expect(await within(dialog).findByRole('button', { name: CONFIRM_LABEL })).toBeInTheDocument();
    expect(within(dialog).queryByText(/^Confirmed by your team on /)).not.toBeInTheDocument();
  });

  it('is disabled while the town, the state, the country or the city has unsaved edits', async () => {
    const store = serve({ [DALLAS]: IRVING });
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen);
    await serviceArea(screen, dialog);
    const confirm = await within(dialog).findByRole('button', { name: CONFIRM_LABEL });
    expect(confirm).toBeEnabled();
    const town = within(dialog).getByLabelText('Business town or locality');
    await screen.user.type(town, 'x');
    expect(confirm).toBeDisabled();
    expect(within(dialog).getByText(SAVE_FIRST)).toBeInTheDocument();
    await screen.user.type(town, '{Backspace}');
    expect(confirm).toBeEnabled();
    await screen.user.click(within(dialog).getByRole('tab', { name: /details/i }));
    await screen.user.type(within(dialog).getByLabelText(/^city/i), 'x');
    await serviceArea(screen, dialog);
    expect(within(dialog).getByRole('button', { name: CONFIRM_LABEL })).toBeDisabled();
    expect(store.confirmations).toEqual([]);
  });

  it('shows the API refusal as worded', async () => {
    serve({ [DALLAS]: IRVING });
    const refusal =
      "This town or locality cannot be confirmed: the business country, or the business state or province, is not this location's own. Correct it first.";
    server.use(
      http.put(P('/workspaces/:ws/locations/:id/geo-coverage/locality-confirmation'), () =>
        HttpResponse.json(
          { error: { code: 'locality_confirmation_refused', message: refusal, request_id: 'r' } },
          { status: 422 },
        ),
      ),
    );
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen);
    await serviceArea(screen, dialog);
    await screen.user.click(await within(dialog).findByRole('button', { name: CONFIRM_LABEL }));
    expect(await within(dialog).findByText(refusal)).toBeInTheDocument();
  });
});

describe('location dialog: the structured address, read-only (W21 ADDR)', () => {
  let writes: string[] = [];
  const onRequest = ({ request }: { request: Request }) => {
    if (request.method !== 'GET') writes.push(`${request.method} ${new URL(request.url).pathname}`);
  };
  beforeEach(() => {
    writes = [];
    server.events.on('request:start', onRequest);
  });
  afterEach(() => {
    server.events.removeListener('request:start', onRequest);
  });

  it('shows values and the confirmation status as text, with no control and no write', async () => {
    orgModel.setMembership('org-1', 'user-1', 'viewer');
    serve({
      [DALLAS]: { ...IRVING, locality_confirmation_applies: true, locality_confirmed_at: '2026-11-12T09:30:00Z' },
    });
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen, /^view details & service area$/i, /location details/i);
    await serviceArea(screen, dialog);
    expect(await within(dialog).findByText('Irving')).toBeInTheDocument();
    expect(within(dialog).getByText('75039')).toBeInTheDocument();
    expect(within(dialog).getByText(/^Confirmed by your team on /)).toBeInTheDocument();
    expect(within(dialog).queryByRole('textbox', { name: 'Business town or locality' })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: CONFIRM_LABEL })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: WITHDRAW_LABEL })).not.toBeInTheDocument();
    expect(within(dialog).queryByText(ADDRESS_HELP)).not.toBeInTheDocument();
    expect(dialog.textContent ?? '').not.toMatch(/verif/i);
    expect(writes).toEqual([]);
  });
});
