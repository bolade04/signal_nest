import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import type { GeoCoverageBase, GeoCoverageOut } from '@/api/types';
import { locationModel, orgModel } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * F-1: the business address on a location's service-area rule is the one the API stores and
 * the jurisdiction resolver reads. The dialog used to send `coverage.business_address ??
 * form.address`, so after the first save nothing in the product could change it. It is now
 * a field on the Service area tab, prefilled from the stored rule (or, with no stored rule,
 * from the street address it follows), and the save sends exactly what it shows.
 *
 * The coverage handlers below behave as the API does (apps/api/app/locations/routes.py):
 * GET answers the stored rule or 404, and PUT replaces the whole rule with the body, every
 * omitted field taking its schema default (GeoCoverageBase in app/locations/schemas.py).
 */

const P = (path: string) => `*${API_PREFIX}${path}`;

/** The fixture Dallas location (test/handlers.ts): listed first, street address "1 Dallas Ave". */
const DALLAS = 'loc-dallas';
const STREET = '1 Dallas Ave';
const CORRECTED = '1 Dallas Ave, Dallas, TX';
const HELP = 'Keep it in the same country and state or province as this location.';

/** A stored rule whose business address names another country, with every other field set. */
const LOADED: GeoCoverageOut = {
  id: 'geo-loc-dallas',
  location_id: DALLAS,
  coverage_type: 'radius',
  business_address: 'Toronto, Ontario, Canada',
  center_latitude: 32.7767,
  center_longitude: -96.797,
  radius_miles: 40,
  country: 'United States',
  state: 'Texas',
  included_markets: ['Dallas', 'Fort Worth'],
  excluded_markets: ['Houston'],
  online_global: true,
};

/** The rule's writable fields, exactly as loaded: what an untouched save must send back. */
function asLoaded(rule: GeoCoverageOut): GeoCoverageBase {
  const { id: _id, location_id: _location, ...fields } = rule;
  return fields;
}

/** GeoCoverageBase's defaults: what the API stores for a field the PUT body omits. */
const SCHEMA_DEFAULTS: GeoCoverageBase = {
  coverage_type: 'radius',
  business_address: null,
  center_latitude: null,
  center_longitude: null,
  radius_miles: null,
  country: null,
  state: null,
  included_markets: [],
  excluded_markets: [],
  online_global: false,
};

interface CoverageStore {
  rules: Map<string, GeoCoverageOut>;
  puts: { locationId: string; body: Record<string, unknown> }[];
}

function serveCoverage(initial: Record<string, GeoCoverageOut>): CoverageStore {
  const store: CoverageStore = { rules: new Map(Object.entries(initial)), puts: [] };
  server.use(
    http.get(P('/workspaces/:ws/locations/:id/geo-coverage'), ({ params }) => {
      const rule = store.rules.get(String(params.id));
      if (rule) return HttpResponse.json(rule);
      return HttpResponse.json(
        { error: { code: 'not_found', message: 'No Scout Reach configured for this location yet.', request_id: 'req-test' } },
        { status: 404 },
      );
    }),
    http.put(P('/workspaces/:ws/locations/:id/geo-coverage'), async ({ request, params }) => {
      const locationId = String(params.id);
      const body = (await request.json()) as Record<string, unknown>;
      store.puts.push({ locationId, body });
      const saved = { ...SCHEMA_DEFAULTS, ...body, id: `geo-${locationId}`, location_id: locationId } as GeoCoverageOut;
      store.rules.set(locationId, saved);
      return HttpResponse.json(saved);
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

async function tab(screen: Screen, dialog: HTMLElement, name: RegExp) {
  await screen.user.click(within(dialog).getByRole('tab', { name }));
}

async function businessField(screen: Screen, dialog: HTMLElement) {
  await tab(screen, dialog, /service area/i);
  return within(dialog).getByLabelText(/^business address/i);
}

async function setStreet(screen: Screen, dialog: HTMLElement, value: string) {
  await tab(screen, dialog, /details/i);
  const street = within(dialog).getByLabelText(/street address/i);
  await screen.user.clear(street);
  await screen.user.type(street, value);
}

async function replace(screen: Screen, field: HTMLElement, value: string) {
  await screen.user.clear(field);
  if (value) await screen.user.type(field, value);
}

async function save(screen: Screen, dialog: HTMLElement, label = /^save changes$/i) {
  await screen.user.click(within(dialog).getByRole('button', { name: label }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
}

beforeEach(() => {
  orgModel.reset();
  locationModel.reset();
});

describe('location dialog: the business address (F-1)', () => {
  it('sends the corrected business address, and every other field exactly as loaded', async () => {
    const store = serveCoverage({ [DALLAS]: LOADED });
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen);
    const field = await businessField(screen, dialog);
    await waitFor(() => expect(field).toHaveValue('Toronto, Ontario, Canada'));
    expect(field).toHaveAccessibleDescription(HELP);

    await replace(screen, field, CORRECTED);
    await save(screen, dialog);

    expect(store.puts).toEqual([
      { locationId: DALLAS, body: { ...asLoaded(LOADED), business_address: CORRECTED } },
    ]);
  });

  it('shows the corrected address when the location is reopened, and in a new session', async () => {
    const store = serveCoverage({ [DALLAS]: LOADED });
    const first = renderApp(<App />, { route: '/locations' });
    let dialog = await openDallas(first);
    let field = await businessField(first, dialog);
    await waitFor(() => expect(field).toHaveValue('Toronto, Ontario, Canada'));
    await replace(first, field, CORRECTED);
    await save(first, dialog);

    // Reopened in the same session: the dialog stays mounted and the rule is cached.
    dialog = await openDallas(first);
    field = await businessField(first, dialog);
    await waitFor(() => expect(field).toHaveValue(CORRECTED));
    expect(within(dialog).getByText('Service radius — 40 miles')).toBeInTheDocument();
    first.unmount();

    // A new session reads the saved rule back from the API.
    expect(store.rules.get(DALLAS)).toEqual({ ...LOADED, business_address: CORRECTED });
    const second = renderApp(<App />, { route: '/locations' });
    dialog = await openDallas(second);
    field = await businessField(second, dialog);
    await waitFor(() => expect(field).toHaveValue(CORRECTED));
  });

  it('moves the business address with the street address while they are equal', async () => {
    const store = serveCoverage({ [DALLAS]: { ...LOADED, business_address: STREET } });
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen);
    let field = await businessField(screen, dialog);
    expect(await within(dialog).findByText('Service radius — 40 miles')).toBeInTheDocument(); // loaded
    expect(field).toHaveValue(STREET);

    await setStreet(screen, dialog, '2 Dallas Ave');
    field = await businessField(screen, dialog);
    expect(field).toHaveValue('2 Dallas Ave');
    await save(screen, dialog);

    expect(store.puts).toEqual([
      { locationId: DALLAS, body: { ...asLoaded(LOADED), business_address: '2 Dallas Ave' } },
    ]);
  });

  it('keeps a business address set separately when the street address changes', async () => {
    const store = serveCoverage({ [DALLAS]: { ...LOADED, business_address: STREET } });
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen);
    let field = await businessField(screen, dialog);
    expect(await within(dialog).findByText('Service radius — 40 miles')).toBeInTheDocument();
    await replace(screen, field, CORRECTED);

    await setStreet(screen, dialog, '3 Dallas Ave');
    field = await businessField(screen, dialog);
    expect(field).toHaveValue(CORRECTED);
    await save(screen, dialog);

    expect(store.puts).toEqual([
      { locationId: DALLAS, body: { ...asLoaded(LOADED), business_address: CORRECTED } },
    ]);
  });

  it('does not move a stored business address that differs from the street address', async () => {
    const store = serveCoverage({ [DALLAS]: LOADED });
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen);
    let field = await businessField(screen, dialog);
    await waitFor(() => expect(field).toHaveValue('Toronto, Ontario, Canada'));

    await setStreet(screen, dialog, '4 Dallas Ave');
    field = await businessField(screen, dialog);
    expect(field).toHaveValue('Toronto, Ontario, Canada');
    await save(screen, dialog);

    expect(store.puts).toEqual([{ locationId: DALLAS, body: asLoaded(LOADED) }]);
  });

  it('a new location (no stored rule) sends its street address, as the first save always has', async () => {
    const store = serveCoverage({});
    const screen = renderApp(<App />, { route: '/locations' });
    await screen.findByText('Dallas');
    await screen.user.click(screen.getAllByRole('button', { name: /^add location$/i })[0]!);
    const dialog = await screen.findByRole('dialog', { name: /add location/i });
    await screen.user.type(within(dialog).getByLabelText(/location name/i), 'Elm Street shop');
    await screen.user.type(within(dialog).getByLabelText(/street address/i), '500 Elm St, Dallas, TX');
    expect(await businessField(screen, dialog)).toHaveValue('500 Elm St, Dallas, TX');
    await save(screen, dialog, /^add location$/i);

    // POST /locations answers id "loc-new" (test/handlers.ts); the new location has no coordinates.
    expect(store.puts).toEqual([
      {
        locationId: 'loc-new',
        body: {
          coverage_type: 'radius',
          radius_miles: 25,
          included_markets: [],
          excluded_markets: [],
          online_global: false,
          center_latitude: null,
          center_longitude: null,
          business_address: '500 Elm St, Dallas, TX',
        },
      },
    ]);
  });

  it('an existing location with no stored rule sends its street address', async () => {
    const store = serveCoverage({});
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen);
    expect(await businessField(screen, dialog)).toHaveValue(STREET);
    await save(screen, dialog);

    expect(store.puts).toHaveLength(1);
    expect(store.puts[0]).toMatchObject({ locationId: DALLAS, body: { business_address: STREET } });
  });

  it('an emptied field saves null, never the street address', async () => {
    const store = serveCoverage({ [DALLAS]: LOADED });
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen);
    const field = await businessField(screen, dialog);
    await waitFor(() => expect(field).toHaveValue('Toronto, Ontario, Canada'));
    await replace(screen, field, '');
    await save(screen, dialog);

    expect(store.puts).toEqual([
      { locationId: DALLAS, body: { ...asLoaded(LOADED), business_address: null } },
    ]);
  });

  it('a stored rule with no business address opens empty and saves null', async () => {
    const store = serveCoverage({ [DALLAS]: { ...LOADED, business_address: null } });
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen);
    const field = await businessField(screen, dialog);
    expect(await within(dialog).findByText('Service radius — 40 miles')).toBeInTheDocument();
    expect(field).toHaveValue('');
    await save(screen, dialog);

    expect(store.puts).toEqual([
      { locationId: DALLAS, body: { ...asLoaded(LOADED), business_address: null } },
    ]);
  });
});

describe('location dialog: the business address, read-only (F-1)', () => {
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

  it('shows the stored value as text, with no input, no instruction and no write', async () => {
    orgModel.setMembership('org-1', 'user-1', 'viewer');
    const store = serveCoverage({ [DALLAS]: LOADED });
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen, /^view details & service area$/i, /location details/i);
    await tab(screen, dialog, /service area/i);

    expect(await within(dialog).findByText('Toronto, Ontario, Canada')).toBeInTheDocument();
    expect(within(dialog).getByText('Business address')).toBeInTheDocument();
    expect(within(dialog).queryByRole('textbox', { name: /business address/i })).not.toBeInTheDocument();
    expect(within(dialog).queryByText(HELP)).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: /save/i })).not.toBeInTheDocument();

    const footerClose = within(dialog)
      .getAllByRole('button', { name: /^close$/i })
      .find((button) => button.textContent?.trim() === 'Close');
    await screen.user.click(footerClose!);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(store.puts).toEqual([]);
    expect(writes).toEqual([]);
  });
});
