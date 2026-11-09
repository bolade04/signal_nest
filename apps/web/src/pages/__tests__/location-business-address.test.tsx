import { act, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import type { GeoCoverageBase, GeoCoverageOut, LocationOut } from '@/api/types';
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
const UNREADABLE = "Could not load this location's service area. Close and reopen the location to try again before saving.";

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
  return openLocation(screen, 0, button, title);
}

/** Open the `index`-th listed location (fixtures: Dallas, London, Lagos, Nairobi). */
async function openLocation(
  screen: Screen,
  index: number,
  button = /^edit & service area$/i,
  title = /edit location/i,
  listed = 4,
) {
  await screen.findByText('Dallas');
  await waitFor(() => expect(screen.getAllByRole('button', { name: button })).toHaveLength(listed));
  await screen.user.click(screen.getAllByRole('button', { name: button })[index]!);
  return screen.findByRole('dialog', { name: title });
}

async function cancel(screen: Screen, dialog: HTMLElement) {
  await screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
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
  // Save is enabled once the location's stored rule is known (loaded, or answered 404).
  const button = within(dialog).getByRole('button', { name: label });
  await waitFor(() => expect(button).toBeEnabled());
  await screen.user.click(button);
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
    const created: Record<string, unknown>[] = [];
    server.use(
      http.post(P('/workspaces/:ws/locations'), async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        created.push(body);
        return HttpResponse.json({ ...body, id: 'loc-new', brand_id: 'brand-1', workspace_id: 'ws-1', is_active: true });
      }),
    );
    const screen = renderApp(<App />, { route: '/locations' });
    await screen.findByText('Dallas');
    await screen.user.click(screen.getAllByRole('button', { name: /^add location$/i })[0]!);
    const dialog = await screen.findByRole('dialog', { name: /add location/i });
    await screen.user.type(within(dialog).getByLabelText(/location name/i), 'Elm Street shop');
    await screen.user.type(within(dialog).getByLabelText(/street address/i), '500 Elm St, Dallas, TX');
    expect(await businessField(screen, dialog)).toHaveValue('500 Elm St, Dallas, TX');
    await save(screen, dialog, /^add location$/i);

    // The create body is the dialog's own fields only, as before (none of the six it does not edit).
    expect(created).toEqual([
      {
        name: 'Elm Street shop',
        address: '500 Elm St, Dallas, TX',
        city: '',
        state_province: '',
        country: '',
        postal_code: '',
        timezone: '',
        currency: '',
        local_competitors: [],
        local_notes: '',
      },
    ]);
    // The new location has no coordinates.
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
    await waitFor(() => expect(within(dialog).getByRole('button', { name: /^save changes$/i })).toBeEnabled());
    expect(within(dialog).queryByText(UNREADABLE)).not.toBeInTheDocument();
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

describe('location dialog: the save preserves untouched fields (F-1)', () => {
  /** Dallas with every LocationOut field set, including the six the dialog does not edit. */
  const DALLAS_FULL: LocationOut = {
    id: DALLAS,
    brand_id: 'brand-1',
    workspace_id: 'ws-1',
    is_active: true,
    name: 'Dallas',
    address: STREET,
    city: 'Dallas',
    state_province: 'TX',
    country: 'United States',
    postal_code: '75201',
    timezone: 'America/Chicago',
    currency: 'USD',
    latitude: 32.7767,
    longitude: -96.797,
    local_competitors: ['Rival Dallas'],
    local_notes: 'Downtown',
    languages: ['en', 'es'],
    local_phone: '+1 214 555 0100',
    local_website_page: 'https://example.com/dallas',
    local_social_links: { instagram: 'https://instagram.com/example' },
    google_business_profile: 'https://maps.example.com/?cid=1',
    overrides: { tone: 'formal' },
  };

  it('a cancelled Service area edit does not carry into the next opening', async () => {
    const store = serveCoverage({ [DALLAS]: LOADED });
    const screen = renderApp(<App />, { route: '/locations' });
    let dialog = await openDallas(screen);
    const field = await businessField(screen, dialog);
    await waitFor(() => expect(field).toHaveValue('Toronto, Ontario, Canada'));
    await replace(screen, field, 'Cancelled edit');
    await screen.user.type(within(dialog).getByLabelText(/included markets/i), 'Austin{Enter}');
    await screen.user.click(within(dialog).getByRole('switch', { name: /serve online/i }));
    expect(within(dialog).getByText('Austin')).toBeInTheDocument();
    await cancel(screen, dialog);

    dialog = await openDallas(screen);
    expect(await businessField(screen, dialog)).toHaveValue('Toronto, Ontario, Canada');
    expect(within(dialog).queryByText('Austin')).not.toBeInTheDocument();
    expect(within(dialog).getByText('Fort Worth')).toBeInTheDocument();
    expect(within(dialog).getByRole('switch', { name: /serve online/i })).toBeChecked();
    await save(screen, dialog);

    expect(store.puts).toEqual([{ locationId: DALLAS, body: asLoaded(LOADED) }]);
  });

  it("a location with no stored rule opens with the defaults, never another location's values", async () => {
    const store = serveCoverage({ [DALLAS]: LOADED }); // London has none: its GET answers 404
    const screen = renderApp(<App />, { route: '/locations' });
    let dialog = await openDallas(screen);
    await tab(screen, dialog, /service area/i);
    expect(await within(dialog).findByText('Service radius — 40 miles')).toBeInTheDocument();
    await cancel(screen, dialog);

    dialog = await openLocation(screen, 1); // London: street "1 London Ave", coordinates 0, 0
    expect(await businessField(screen, dialog)).toHaveValue('1 London Ave');
    expect(within(dialog).getByText('Service radius — 25 miles')).toBeInTheDocument();
    expect(within(dialog).queryByText('Houston')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Fort Worth')).not.toBeInTheDocument();
    expect(within(dialog).getByRole('switch', { name: /serve online/i })).not.toBeChecked();
    await save(screen, dialog);

    expect(store.puts).toEqual([
      {
        locationId: 'loc-london',
        body: {
          coverage_type: 'radius',
          radius_miles: 25,
          included_markets: [],
          excluded_markets: [],
          online_global: false,
          center_latitude: 0,
          center_longitude: 0,
          business_address: '1 London Ave',
        },
      },
    ]);
  });

  it('the location PUT carries the six fields the dialog does not edit, exactly as loaded', async () => {
    serveCoverage({ [DALLAS]: LOADED });
    const updates: Record<string, unknown>[] = [];
    server.use(
      http.get(P('/workspaces/:ws/locations'), () => HttpResponse.json([DALLAS_FULL])),
      http.put(P('/workspaces/:ws/locations/:id'), async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        updates.push(body);
        return HttpResponse.json({ ...DALLAS_FULL, ...body });
      }),
    );
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openLocation(screen, 0, /^edit & service area$/i, /edit location/i, 1);
    const name = within(dialog).getByLabelText(/location name/i);
    await replace(screen, name, 'Dallas Flagship');
    await save(screen, dialog);

    const { id: _id, brand_id: _brand, workspace_id: _ws, is_active: _active, ...loaded } = DALLAS_FULL;
    expect(updates).toEqual([{ ...loaded, name: 'Dallas Flagship' }]);
  });

  it('an existing location saves only once its stored rule has arrived', async () => {
    const store = serveCoverage({ [DALLAS]: LOADED });
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get(P('/workspaces/:ws/locations/:id/geo-coverage'), async () => {
        await held;
        return undefined; // then answered by serveCoverage's handler
      }),
    );
    const screen = renderApp(<App />, { route: '/locations' });
    const dialog = await openDallas(screen);
    const button = within(dialog).getByRole('button', { name: /^save changes$/i });
    expect(button).toBeDisabled();
    await screen.user.click(button);
    expect(store.puts).toEqual([]);

    release();
    await waitFor(() => expect(button).toBeEnabled());
    await save(screen, dialog);
    expect(store.puts).toEqual([{ locationId: DALLAS, body: asLoaded(LOADED) }]);
  });

  it('a stored rule that cannot be read blocks the save until a reopening reads it', async () => {
    const store = serveCoverage({ [DALLAS]: LOADED });
    let failures = 0;
    server.use(
      http.get(P('/workspaces/:ws/locations/:id/geo-coverage'), () => {
        if (failures >= 1) return undefined;
        failures += 1;
        return HttpResponse.json(
          { error: { code: 'internal_error', message: 'Temporarily unavailable.', request_id: 'req-test' } },
          { status: 500 },
        );
      }),
    );
    const screen = renderApp(<App />, { route: '/locations' });
    let dialog = await openDallas(screen);
    expect(await within(dialog).findByText(UNREADABLE)).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: /^save changes$/i })).toBeDisabled();
    await cancel(screen, dialog);

    dialog = await openDallas(screen);
    await save(screen, dialog);
    expect(failures).toBe(1);
    expect(store.puts).toEqual([{ locationId: DALLAS, body: asLoaded(LOADED) }]);
  });

  it('a loaded copy of the rule may be saved when its refetch fails, with no alert', async () => {
    const store = serveCoverage({ [DALLAS]: LOADED });
    const screen = renderApp(<App />, { route: '/locations' });
    let dialog = await openDallas(screen);
    await tab(screen, dialog, /service area/i);
    expect(await within(dialog).findByText('Service radius — 40 miles')).toBeInTheDocument();
    await cancel(screen, dialog);

    // The reopening's refetch of the cached rule answers 500 once.
    let failures = 0;
    server.use(
      http.get(P('/workspaces/:ws/locations/:id/geo-coverage'), () => {
        if (failures >= 1) return undefined;
        failures += 1;
        return HttpResponse.json(
          { error: { code: 'internal_error', message: 'Temporarily unavailable.', request_id: 'req-test' } },
          { status: 500 },
        );
      }),
    );
    dialog = await openDallas(screen);
    await waitFor(() => expect(failures).toBe(1));
    // The failed refetch is processed within these ticks; the alert must never appear.
    for (let tick = 0; tick < 10; tick += 1) {
      await act(() => new Promise((resolve) => setTimeout(resolve, 20)));
      expect(within(dialog).queryByText(UNREADABLE)).not.toBeInTheDocument();
    }
    expect(within(dialog).getByRole('button', { name: /^save changes$/i })).toBeEnabled();
    await save(screen, dialog);

    expect(store.puts).toEqual([{ locationId: DALLAS, body: asLoaded(LOADED) }]);
  });

  it('on a cached reopen, the business address follows the street as last saved', async () => {
    const store = serveCoverage({ [DALLAS]: { ...LOADED, business_address: STREET } });
    const screen = renderApp(<App />, { route: '/locations' });
    let dialog = await openDallas(screen);
    await tab(screen, dialog, /service area/i);
    expect(await within(dialog).findByText('Service radius — 40 miles')).toBeInTheDocument();
    await setStreet(screen, dialog, 'Cancelled Street');
    await cancel(screen, dialog);

    dialog = await openDallas(screen);
    await setStreet(screen, dialog, '9 Elm Street');
    await save(screen, dialog);

    expect(store.puts).toEqual([
      { locationId: DALLAS, body: { ...asLoaded(LOADED), business_address: '9 Elm Street' } },
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
