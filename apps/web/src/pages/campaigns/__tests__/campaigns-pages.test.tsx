/**
 * Project Phase 5D-W4 (phase 3): the campaign pages of lane 5D-W3 (not mounted until I-5D),
 * tested against the contract, not against what the pages happen to do.
 *
 * Contract: `docs/project-phase-5d-multi-location-jurisdiction.md` §2 "Campaign hardening"
 * (the dedicated campaigns router with GET one and an update; projected answers without
 * `organization_id` / `workspace_id`; `mode` and `status` from the closed `CampaignMode` and
 * `CampaignStatus`) and §4 exit criteria 2 and 3. The roles are the router's own
 * (`apps/api/app/campaign_context/campaigns.py`: `EDITORS = require_role(OWNER, ADMIN,
 * MARKETER)`, every member reads), written out here rather than read from `lib/roles.ts`, so a
 * wrong frontend helper cannot pass. The closed vocabularies are read from the generated
 * contract (`apps/api/openapi.json`), so a page that invents or drops a member fails.
 *
 * The API is a stateful MSW mock of the router that deliberately echoes fields the projection
 * removes (and a jurisdiction the campaign does not have), with distinctive values, so a page
 * that reaches for them is caught. Copy is asserted only as far as the contract and the lane
 * brief require: each refusal gets its own, specific message, and nothing a retry cannot fix
 * is offered as a retry. Exact wording is not pinned.
 */
import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it } from 'vitest';
import { API_PREFIX } from '@/api/config';
import type { CampaignOut, LocationOut } from '@/api/types';
import { campaignModeLabels, campaignStatusLabels } from '@/lib/labels';
import { orgModel, type ModelRole } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';
import { CampaignDetailPage } from '../CampaignDetailPage';
import { CampaignsPage } from '../CampaignsPage';

// ---- The contract -------------------------------------------------------------------------
interface EnumSchema {
  type?: string;
  enum?: string[];
}
const openapiSources = import.meta.glob('../../../../../api/openapi.json', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;
const OPENAPI = JSON.parse(Object.values(openapiSources)[0] ?? '{}') as {
  components: { schemas: Record<string, EnumSchema> };
};
const CONTRACT_MODES = OPENAPI.components.schemas.CampaignMode?.enum ?? [];
const CONTRACT_STATUSES = OPENAPI.components.schemas.CampaignStatus?.enum ?? [];

/** The router's write roles (EDITORS) and every role, from the contract's Role enum order. */
const ROLES: ModelRole[] = ['owner', 'admin', 'marketer', 'reviewer', 'compliance_reviewer', 'viewer'];
const EDITORS = new Set<ModelRole>(['owner', 'admin', 'marketer']);

// ---- A stateful mock of the dedicated campaigns router --------------------------------------
const LIST = `${API_PREFIX}/workspaces/:ws/campaigns`;
const ITEM = `${API_PREFIX}/workspaces/:ws/campaigns/:id`;

/** Fields the projection removes, and a jurisdiction no campaign has: echoed anyway. */
const ECHOED = {
  organization_id: 'org-echo-7f3a91',
  workspace_id: 'ws-echo-0c2d55',
  jurisdiction_code: 'ZZ-QQ',
  jurisdiction_codes: ['YY-RR'],
} as const;
const BRAND_ID = 'brand-echo-5a9e17';
/** Ids that are never shown to a person (a campaign is named, a location is named). */
const CAMPAIGN_A = 'cmp-id-4b7e2d';
const CAMPAIGN_B = 'cmp-id-9c1f06';
const LOC_UPTOWN = 'loc-id-a1f3c9';
const LOC_HARBOR = 'loc-id-b27e44';
const LOC_DEPOT = 'loc-id-c3d8b0'; // deactivated
const NEVER_RENDERED = [
  ...Object.values(ECHOED).flat(),
  BRAND_ID,
  CAMPAIGN_A,
  CAMPAIGN_B,
  LOC_UPTOWN,
  LOC_HARBOR,
  LOC_DEPOT,
];

function location(id: string, name: string, isActive: boolean): LocationOut {
  return {
    id,
    brand_id: BRAND_ID,
    workspace_id: ECHOED.workspace_id,
    is_active: isActive,
    name,
    address: null,
    city: name,
    state_province: null,
    country: null,
    postal_code: null,
    timezone: null,
    currency: null,
    latitude: null,
    longitude: null,
    languages: [],
    local_phone: null,
    local_website_page: null,
    local_social_links: {},
    google_business_profile: null,
    local_competitors: [],
    local_notes: null,
    overrides: {},
  } as unknown as LocationOut;
}
const LOCATIONS = [
  location(LOC_UPTOWN, 'Uptown Store', true),
  location(LOC_HARBOR, 'Harbor Kiosk', true),
  location(LOC_DEPOT, 'Old Depot', false),
];

type Row = CampaignOut & typeof ECHOED;
let rows: Row[] = [];
let writes: { method: string; id: string | null; body: unknown }[] = [];
let created = 0;

function campaign(id: string, name: string, fields: Partial<CampaignOut> = {}): Row {
  return {
    id,
    brand_id: BRAND_ID,
    name,
    goal: null,
    mode: 'per_location',
    status: 'active',
    location_ids: [],
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z',
    ...fields,
    ...ECHOED,
  };
}

const refusal = (status: number, code: string, message: string) =>
  HttpResponse.json({ error: { code, message } }, { status });
const notFound = () => refusal(404, 'not_found', 'Item not found in this workspace.');

const routerMock = [
  http.get(LIST, () => HttpResponse.json(rows)),
  http.get(ITEM, ({ params }) => {
    const row = rows.find((r) => r.id === params.id);
    return row ? HttpResponse.json(row) : notFound();
  }),
  http.post(LIST, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    writes.push({ method: 'POST', id: null, body });
    created += 1;
    const row = campaign(`cmp-new-${created}`, String(body.name), body as Partial<CampaignOut>);
    rows.push(row);
    return HttpResponse.json(row, { status: 201 });
  }),
  http.patch(ITEM, async ({ request, params }) => {
    const body = (await request.json()) as Record<string, unknown>;
    writes.push({ method: 'PATCH', id: String(params.id), body });
    const row = rows.find((r) => r.id === params.id);
    if (!row) return notFound();
    Object.assign(row, body, { location_ids: [...((body.location_ids as string[]) ?? row.location_ids)].sort() });
    return HttpResponse.json(row);
  }),
  http.delete(ITEM, ({ params }) => {
    writes.push({ method: 'DELETE', id: String(params.id), body: null });
    const before = rows.length;
    rows = rows.filter((r) => r.id !== params.id);
    return rows.length === before ? notFound() : new HttpResponse(null, { status: 204 });
  }),
  http.get(`${API_PREFIX}/workspaces/:ws/locations`, () => HttpResponse.json(LOCATIONS)),
];

beforeEach(() => {
  orgModel.reset();
  rows = [
    campaign(CAMPAIGN_A, 'Spring opening', {
      goal: 'More weekend visits',
      mode: 'grouped',
      status: 'draft',
      location_ids: [LOC_HARBOR, LOC_UPTOWN],
    }),
    campaign(CAMPAIGN_B, 'Autumn fair', { mode: 'same_for_all', status: 'paused' }),
  ];
  writes = [];
  created = 0;
  server.use(...routerMock);
});

function renderAt(route: string) {
  return renderApp(
    <Routes>
      <Route path="/campaigns" element={<CampaignsPage />} />
      <Route path="/campaigns/:campaignId" element={<CampaignDetailPage />} />
    </Routes>,
    { route },
  );
}
type Screen = ReturnType<typeof renderAt>;

const asRole = (role: string) => orgModel.setMembership('org-1', 'user-1', role as ModelRole);
const shown = (screen: Screen) => screen.container.textContent ?? '';

function expectNothingInternalShown(screen: Screen) {
  const text = shown(screen) + (document.body.textContent ?? '');
  for (const value of NEVER_RENDERED) expect(text, value).not.toContain(value);
}

async function openDetail(screen: Screen, name: string) {
  return screen.findByRole('heading', { name });
}

async function choose(screen: Screen, scope: HTMLElement, label: RegExp, option: string) {
  await screen.user.click(within(scope).getByRole('combobox', { name: label }));
  await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: option }));
}

async function optionsOf(screen: Screen, scope: HTMLElement, label: RegExp): Promise<string[]> {
  await screen.user.click(within(scope).getByRole('combobox', { name: label }));
  const listbox = await screen.findByRole('listbox');
  const names = within(listbox)
    .getAllByRole('option')
    .map((o) => o.textContent?.trim() ?? '');
  await screen.user.keyboard('{Escape}');
  await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument());
  return names;
}

// =============================================================================================
// 1. List, create, view, edit, delete
// =============================================================================================
describe('the campaigns list', () => {
  it('lists every campaign by name with its mode and status read through lib/labels.ts', async () => {
    const screen = renderAt('/campaigns');
    const list = await screen.findByRole('list', { name: /campaigns/i });
    const items = within(list).getAllByRole('listitem');
    expect(items).toHaveLength(2);
    for (const [item, row] of items.map((item, i) => [item, rows[i]!] as const)) {
      expect(within(item).getByRole('link', { name: row.name })).toBeInTheDocument();
      expect(item.textContent).toContain(campaignModeLabels[row.mode]);
      expect(item.textContent).toContain(campaignStatusLabels[row.status]);
    }
    expectNothingInternalShown(screen);
  });

  it('an editor adds a campaign: the closed fields, the active locations, one POST', async () => {
    const screen = renderAt('/campaigns');
    await screen.findByRole('list', { name: /campaigns/i });
    await screen.user.click(screen.getByRole('button', { name: /new campaign/i }));
    const dialog = await screen.findByRole('dialog');
    await screen.user.type(within(dialog).getByRole('textbox', { name: /name/i }), '  Summer launch  ');
    await choose(screen, dialog, /location mode/i, campaignModeLabels.recommend!);
    await choose(screen, dialog, /status/i, campaignStatusLabels.draft!);
    // Only active locations are offered to a new campaign; a deactivated one cannot be added.
    expect(within(dialog).queryByRole('checkbox', { name: /old depot/i })).not.toBeInTheDocument();
    await screen.user.click(within(dialog).getByRole('checkbox', { name: /uptown store/i }));
    await screen.user.click(within(dialog).getByRole('checkbox', { name: /harbor kiosk/i }));
    await screen.user.click(within(dialog).getByRole('button', { name: /add campaign/i }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toEqual({
      method: 'POST',
      id: null,
      body: {
        name: 'Summer launch',
        goal: null,
        mode: 'recommend',
        status: 'draft',
        location_ids: [LOC_UPTOWN, LOC_HARBOR].sort(),
      },
    });
    expect(await screen.findByRole('link', { name: 'Summer launch' })).toBeInTheDocument();
    expectNothingInternalShown(screen);
  });
});

describe('one campaign', () => {
  it('shows its goal, mode, status and member locations by name, never by id', async () => {
    const screen = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await openDetail(screen, 'Spring opening');
    const text = shown(screen);
    expect(text).toContain('More weekend visits');
    expect(text).toContain(campaignModeLabels.grouped);
    expect(text).toContain(campaignStatusLabels.draft);
    await waitFor(() => expect(shown(screen)).toContain('Uptown Store'));
    expect(shown(screen)).toContain('Harbor Kiosk');
    expectNothingInternalShown(screen);
  });

  it('the edit form shows no internal field either, and every control is labelled', async () => {
    const screen = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await openDetail(screen, 'Spring opening');
    await screen.user.click(screen.getByRole('button', { name: /edit/i }));
    await screen.findByRole('combobox', { name: /status/i });
    expectNothingInternalShown(screen);
  });
});

describe('editing sends only what changed (PATCH)', () => {
  async function edit(screen: Screen) {
    await openDetail(screen, 'Spring opening');
    await screen.user.click(screen.getByRole('button', { name: /edit/i }));
    await screen.findByRole('combobox', { name: /status/i });
  }
  const save = (screen: Screen) => screen.user.click(screen.getByRole('button', { name: /save/i }));

  it('a renamed campaign sends the name alone', async () => {
    const screen = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await edit(screen);
    const name = screen.getByRole('textbox', { name: /name/i });
    await screen.user.clear(name);
    await screen.user.type(name, 'Spring reopening');
    await save(screen);
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toEqual({ method: 'PATCH', id: CAMPAIGN_A, body: { name: 'Spring reopening' } });
  });

  it('a new status sends the status alone, chosen from the closed vocabulary', async () => {
    const screen = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await edit(screen);
    await choose(screen, screen.container, /status/i, campaignStatusLabels.archived!);
    await save(screen);
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]!.body).toEqual({ status: 'archived' });
  });

  it('location_ids is sent only when the set changed, and then whole', async () => {
    const screen = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await edit(screen);
    const uptown = await screen.findByRole('checkbox', { name: /uptown store/i });
    // Untick and tick again: the set is the same, so there is nothing to save.
    await screen.user.click(uptown);
    await screen.user.click(uptown);
    expect(screen.getByRole('button', { name: /save/i })).toBeDisabled();
    expect(writes).toHaveLength(0);
    // Untick one member: the whole new set is sent, and nothing else.
    await screen.user.click(screen.getByRole('checkbox', { name: /harbor kiosk/i }));
    await save(screen);
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]!.body).toEqual({ location_ids: [LOC_UPTOWN] });
  });

  it('a change that is undone sends nothing', async () => {
    const screen = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await edit(screen);
    await choose(screen, screen.container, /location mode/i, campaignModeLabels.recommend!);
    await choose(screen, screen.container, /location mode/i, campaignModeLabels.grouped!);
    expect(screen.getByRole('button', { name: /save/i })).toBeDisabled();
    expect(writes).toHaveLength(0);
  });

  it('a cleared goal is sent as null, which clears it', async () => {
    // `CampaignPatch.goal` is nullable: null clears it (P6-PLAT-9); an empty string would be
    // stored as a goal.
    const screen = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await edit(screen);
    await screen.user.clear(screen.getByRole('textbox', { name: /goal/i }));
    await save(screen);
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toEqual({ method: 'PATCH', id: CAMPAIGN_A, body: { goal: null } });
  });

  it('a deactivated location that is already a member is offered, ticked and removable; a deactivated non-member is not offered', async () => {
    // W1's rule (campaign_context/location_refs.py, validate_location_ids): a deactivated
    // location may stay a member of a campaign, but cannot be newly added to one.
    rows[1]!.location_ids = [LOC_DEPOT];
    const screen = renderAt(`/campaigns/${CAMPAIGN_B}`);
    await openDetail(screen, 'Autumn fair');
    await screen.user.click(screen.getByRole('button', { name: /edit/i }));
    const depot = await screen.findByRole('checkbox', { name: /old depot/i });
    expect(depot).toBeChecked();
    await screen.user.click(depot);
    await save(screen);
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toEqual({ method: 'PATCH', id: CAMPAIGN_B, body: { location_ids: [] } });
    screen.unmount();

    const other = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await edit(other);
    await other.findByRole('checkbox', { name: /uptown store/i });
    expect(other.queryByRole('checkbox', { name: /old depot/i })).not.toBeInTheDocument();
  });
});

describe('deleting', () => {
  it('requires confirmation: nothing is sent until the confirm is pressed, once', async () => {
    const screen = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await openDetail(screen, 'Spring opening');
    await screen.user.click(screen.getByRole('button', { name: /delete/i }));
    const confirm = await screen.findByRole('dialog');
    expect(writes).toHaveLength(0);
    await screen.user.click(within(confirm).getByRole('button', { name: /cancel/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(writes).toHaveLength(0);
    expect(rows.map((r) => r.id)).toContain(CAMPAIGN_A);

    await screen.user.click(screen.getByRole('button', { name: /delete/i }));
    const again = await screen.findByRole('dialog');
    await screen.user.click(within(again).getByRole('button', { name: /delete/i }));
    await waitFor(() => expect(writes).toEqual([{ method: 'DELETE', id: CAMPAIGN_A, body: null }]));
    // Deleted: back on the list, which no longer has it.
    const list = await screen.findByRole('list', { name: /campaigns/i });
    await waitFor(() => expect(within(list).queryByRole('link', { name: 'Spring opening' })).not.toBeInTheDocument());
  });
});

// =============================================================================================
// 2. Role gating: the router's EDITORS write, every member reads, an unresolved role is denied
// =============================================================================================
describe('role gating', () => {
  it.each(ROLES)('%s', async (role) => {
    asRole(role);
    const list = renderAt('/campaigns');
    await list.findByRole('link', { name: 'Spring opening' });
    if (EDITORS.has(role)) {
      await waitFor(() => expect(list.getByRole('button', { name: /new campaign/i })).toBeEnabled());
    } else {
      expect(list.queryByRole('button', { name: /new campaign/i })).not.toBeInTheDocument();
    }
    list.unmount();

    const detail = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await openDetail(detail, 'Spring opening');
    expect(shown(detail)).toContain('More weekend visits'); // every member reads
    if (EDITORS.has(role)) {
      await waitFor(() => expect(detail.getByRole('button', { name: /edit/i })).toBeEnabled());
      expect(detail.getByRole('button', { name: /delete/i })).toBeEnabled();
    } else {
      expect(detail.queryByRole('button', { name: /edit/i })).not.toBeInTheDocument();
      expect(detail.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument();
      expect(detail.queryByRole('combobox')).not.toBeInTheDocument();
      expect(detail.queryByRole('textbox')).not.toBeInTheDocument();
    }
    expect(writes).toHaveLength(0);
  });

  it('a role this build does not recognise is denied: read-only, no write control', async () => {
    asRole('superuser');
    const list = renderAt('/campaigns');
    await list.findByRole('link', { name: 'Spring opening' });
    expect(list.queryByRole('button', { name: /new campaign/i })).not.toBeInTheDocument();
    list.unmount();
    const detail = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await openDetail(detail, 'Spring opening');
    expect(detail.queryByRole('button', { name: /edit/i })).not.toBeInTheDocument();
    expect(detail.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument();
  });
});

// =============================================================================================
// 3. The closed vocabularies
// =============================================================================================
describe('the closed vocabularies', () => {
  it('the contract declares both enums (the test reads them, it does not assume them)', () => {
    expect(CONTRACT_MODES.length).toBeGreaterThan(1);
    expect(CONTRACT_STATUSES.length).toBeGreaterThan(1);
  });

  it('mode and status are closed selects offering exactly the contract members, nothing typed', async () => {
    const screen = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await openDetail(screen, 'Spring opening');
    await screen.user.click(screen.getByRole('button', { name: /edit/i }));
    await screen.findByRole('combobox', { name: /status/i });
    for (const [label, members, labels] of [
      [/location mode/i, CONTRACT_MODES, campaignModeLabels],
      [/status/i, CONTRACT_STATUSES, campaignStatusLabels],
    ] as const) {
      expect(screen.queryByRole('textbox', { name: label })).not.toBeInTheDocument();
      const offered = await optionsOf(screen, screen.container, label);
      expect([...offered].sort()).toEqual(members.map((m) => labels[m]!).sort());
    }
  });
});

// =============================================================================================
// 4. Every refusal gets its own, specific message; a 404 is never a generic retry
// =============================================================================================
const network = () => HttpResponse.error();
const STATUSES: [string, () => Response][] = [
  ['403', () => refusal(403, 'permission_denied', 'Insufficient role.')],
  ['404', () => notFound()],
  ['409', () => refusal(409, 'conflict', 'Conflict.')],
  ['422', () => refusal(422, 'validation_error', 'Name must not be blank (from the API).')],
  ['429', () => refusal(429, 'rate_limited', 'Too many requests.')],
  ['500', () => refusal(500, 'internal_error', 'Internal server error.')],
  ['network', network],
];
const RETRYABLE = new Set(['429', '500', 'network']);

function alertText(screen: Screen): string {
  return screen
    .getAllByRole('alert')
    .map((a) => a.textContent ?? '')
    .join(' ')
    .trim();
}

describe('reading one campaign', () => {
  it('a 404 is a not-found state with no retry; a 500 is retryable and says something else', async () => {
    server.use(http.get(ITEM, () => notFound()));
    const missing = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await missing.findByRole('heading', { name: /not found/i });
    expect(missing.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument();
    const notFoundText = shown(missing);
    missing.unmount();

    server.use(http.get(ITEM, () => refusal(500, 'internal_error', 'Internal server error.')));
    const broken = renderAt(`/campaigns/${CAMPAIGN_A}`);
    expect(await broken.findByRole('button', { name: /try again/i })).toBeInTheDocument();
    expect(broken.queryByRole('heading', { name: /not found/i })).not.toBeInTheDocument();
    expect(alertText(broken)).not.toBe('');
    expect(notFoundText).not.toContain(alertText(broken));
  });
});

describe('reading the list', () => {
  it('each failure has its own message; only a repeatable failure offers a retry', async () => {
    const messages = new Map<string, string>();
    for (const [status, respond] of STATUSES.filter(([s]) => !['409', '422'].includes(s))) {
      server.use(http.get(LIST, respond));
      const screen = renderAt('/campaigns');
      await waitFor(() => expect(screen.getAllByRole('alert').length).toBeGreaterThan(0), { timeout: 4000 });
      messages.set(status, alertText(screen));
      const retry = screen.queryByRole('button', { name: /try again/i });
      if (RETRYABLE.has(status)) expect(retry, status).toBeInTheDocument();
      else expect(retry, status).not.toBeInTheDocument();
      screen.unmount();
    }
    expect(new Set(messages.values()).size).toBe(messages.size);
  });
});

describe('saving a change', () => {
  it('each refusal has its own message; the 422 carries the API’s reason; none but a repeatable one says try again', async () => {
    const messages = new Map<string, string>();
    for (const [status, respond] of STATUSES) {
      server.use(http.patch(ITEM, respond));
      const screen = renderAt(`/campaigns/${CAMPAIGN_A}`);
      await openDetail(screen, 'Spring opening');
      await screen.user.click(screen.getByRole('button', { name: /edit/i }));
      const name = await screen.findByRole('textbox', { name: /name/i });
      await screen.user.type(name, ' two');
      await screen.user.click(screen.getByRole('button', { name: /save/i }));
      await waitFor(() => expect(screen.getAllByRole('alert').length).toBeGreaterThan(0));
      const text = alertText(screen);
      messages.set(status, text);
      if (!RETRYABLE.has(status)) expect(text, status).not.toMatch(/try again/i);
      if (status === '422') expect(text).toContain('Name must not be blank (from the API).');
      screen.unmount();
    }
    expect(new Set(messages.values()).size).toBe(messages.size);
  });

  it('a create refused with 422 says why, in the dialog, and adds nothing', async () => {
    server.use(http.post(LIST, () => refusal(422, 'validation_error', 'A deactivated location cannot be added.')));
    const screen = renderAt('/campaigns');
    await screen.findByRole('list', { name: /campaigns/i });
    await screen.user.click(screen.getByRole('button', { name: /new campaign/i }));
    const dialog = await screen.findByRole('dialog');
    await screen.user.type(within(dialog).getByRole('textbox', { name: /name/i }), 'Refused');
    await screen.user.click(within(dialog).getByRole('button', { name: /add campaign/i }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('A deactivated location cannot be added.');
    expect(rows).toHaveLength(2);
  });

  it('a create refused with 403 or 404 says something specific in the dialog, never a retry', async () => {
    const messages = new Map<string, string>();
    for (const [status, respond] of [
      ['403', () => refusal(403, 'permission_denied', 'Insufficient role.')],
      ['404', () => refusal(404, 'not_found', 'Location not found in this workspace.')],
    ] as const) {
      server.use(http.post(LIST, respond));
      const screen = renderAt('/campaigns');
      await screen.findByRole('list', { name: /campaigns/i });
      await screen.user.click(screen.getByRole('button', { name: /new campaign/i }));
      const dialog = await screen.findByRole('dialog');
      await screen.user.type(within(dialog).getByRole('textbox', { name: /name/i }), 'Refused');
      await screen.user.click(within(dialog).getByRole('button', { name: /add campaign/i }));
      const text = (await within(dialog).findByRole('alert')).textContent ?? '';
      expect(text, status).not.toMatch(/try again/i);
      messages.set(status, text);
      screen.unmount();
    }
    // A create names no campaign: its 404 is the workspace's or a location's, so the API's
    // own reason is what tells the person which.
    expect(messages.get('404')).toContain('Location not found in this workspace.');
    expect(messages.get('403')).not.toBe(messages.get('404'));
    expect(rows).toHaveLength(2);
  });

  it('a delete refused with 403 says so, not as a missing campaign and not as a retry', async () => {
    server.use(http.delete(ITEM, () => refusal(403, 'permission_denied', 'Insufficient role.')));
    const screen = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await openDetail(screen, 'Spring opening');
    await screen.user.click(screen.getByRole('button', { name: /delete/i }));
    await screen.user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: /delete/i }));
    await waitFor(() => expect(screen.getAllByRole('alert').length).toBeGreaterThan(0));
    const forbidden = alertText(screen);
    expect(forbidden).not.toMatch(/try again/i);
    screen.unmount();

    server.use(http.delete(ITEM, () => notFound()));
    const gone = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await openDetail(gone, 'Spring opening');
    await gone.user.click(gone.getByRole('button', { name: /delete/i }));
    await gone.user.click(within(await gone.findByRole('dialog')).getByRole('button', { name: /delete/i }));
    await waitFor(() => expect(gone.getAllByRole('alert').length).toBeGreaterThan(0));
    expect(alertText(gone)).not.toBe(forbidden);
    expect(rows.map((r) => r.id)).toContain(CAMPAIGN_A);
  });

  it('a delete of a campaign already gone says so, not a generic failure', async () => {
    const screen = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await openDetail(screen, 'Spring opening');
    server.use(http.delete(ITEM, () => notFound()));
    await screen.user.click(screen.getByRole('button', { name: /delete/i }));
    await screen.user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: /delete/i }));
    await waitFor(() => expect(screen.getAllByRole('alert').length).toBeGreaterThan(0));
    const gone = alertText(screen);
    expect(gone).not.toMatch(/try again/i);
    screen.unmount();

    const other = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await openDetail(other, 'Spring opening');
    server.use(http.delete(ITEM, () => refusal(500, 'internal_error', 'Internal server error.')));
    await other.user.click(other.getByRole('button', { name: /delete/i }));
    await other.user.click(within(await other.findByRole('dialog')).getByRole('button', { name: /delete/i }));
    await waitFor(() => expect(other.getAllByRole('alert').length).toBeGreaterThan(0));
    expect(alertText(other)).not.toBe(gone);
  });
});

// =============================================================================================
// 5. D-N4 item 37 (#318): a campaign a brief or draft refers to cannot be deleted (409)
// =============================================================================================
// The router refuses that delete with 409 `campaign_referenced_by_brief_or_draft` and a fixed
// reason (apps/api/app/campaign_context/campaigns.py `CAMPAIGN_IN_USE`, coordinator ruling
// 37-R-1): briefs and drafts are append-only, so their campaign cannot be taken from them. The
// page says that reason as static copy keyed by the code, never the server's text, and the
// confirmation says it before anything is sent.
const IN_USE = /cannot be deleted because a recommendation brief or creative draft refers to it/i;
const KEPT_AS_WRITTEN = /briefs and drafts are kept as they were written/i;

describe('deleting a campaign that a brief or draft refers to (D-N4 item 37)', () => {
  it('the confirmation says such a campaign cannot be deleted, and no longer that briefs and drafts lose it', async () => {
    const screen = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await openDetail(screen, 'Spring opening');
    await screen.user.click(screen.getByRole('button', { name: /delete/i }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent(/a campaign that a recommendation brief or creative draft refers to cannot be deleted/i);
    expect(dialog).toHaveTextContent(KEPT_AS_WRITTEN);
    expect(dialog).toHaveTextContent(/scout requests and opportunities that used it are kept, without the campaign/i);
    expect(dialog).not.toHaveTextContent(/briefs and drafts that used it are kept/i);
    expect(writes).toHaveLength(0);
  });

  it('the 409 says the API’s fixed reason as static copy: no server text, no retry, the campaign stays', async () => {
    const SERVER = 'SERVER-TEXT campaign in use';
    server.use(http.delete(ITEM, () => refusal(409, 'campaign_referenced_by_brief_or_draft', SERVER)));
    const screen = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await openDetail(screen, 'Spring opening');
    await screen.user.click(screen.getByRole('button', { name: /delete/i }));
    await screen.user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: /delete/i }));
    await waitFor(() => expect(screen.getAllByRole('alert').length).toBeGreaterThan(0));
    const text = alertText(screen);
    expect(text).toMatch(/^The campaign was not deleted\./);
    expect(text).toMatch(IN_USE);
    expect(text).toMatch(KEPT_AS_WRITTEN);
    expect(text).not.toMatch(/changed at the same time|try again/i);
    expect(document.body.textContent ?? '').not.toContain(SERVER);
    expect(document.body.innerHTML).not.toContain(SERVER); // nor in an attribute (r2 INFO-2)
    // Still on the campaign, which is still listed.
    expect(screen.getByRole('heading', { name: 'Spring opening' })).toBeInTheDocument();
    expect(rows.map((r) => r.id)).toContain(CAMPAIGN_A);
  });

  it('any other 409 keeps the generic conflict message, not the in-use reason', async () => {
    server.use(http.delete(ITEM, () => refusal(409, 'conflict', 'Conflict.')));
    const screen = renderAt(`/campaigns/${CAMPAIGN_A}`);
    await openDetail(screen, 'Spring opening');
    await screen.user.click(screen.getByRole('button', { name: /delete/i }));
    await screen.user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: /delete/i }));
    await waitFor(() => expect(screen.getAllByRole('alert').length).toBeGreaterThan(0));
    const text = alertText(screen);
    expect(text).toMatch(/changed at the same time/i);
    expect(text).not.toMatch(IN_USE);
  });
});
