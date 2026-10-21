import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import type { ContextKind } from '@/api/endpoints';
import { contextModel, orgModel, type ModelRole } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

const P = (path: string) => `*${API_PREFIX}${path}`;

describe('campaign context center', () => {
  beforeEach(() => contextModel.reset());

  it('shows the context tabs and an empty state for products', async () => {
    const screen = renderApp(<App />, { route: '/context' });
    expect(await screen.findByRole('heading', { name: /campaign context/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /products & services/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /claims library/i })).toBeInTheDocument();
    // Default (empty) product list renders an empty-state prompt.
    expect(await screen.findByText(/no products yet/i)).toBeInTheDocument();
  });

  it('opens the add-product dialog', async () => {
    const screen = renderApp(<App />, { route: '/context' });
    await screen.findByRole('heading', { name: /campaign context/i });
    const [addButton] = screen.getAllByRole('button', { name: /add product/i });
    await screen.user.click(addButton!);
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /add product/i })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------------
// P6-PLAT-9 (customer half): edit a stored row of each of the nine collections
// ---------------------------------------------------------------------------------

type Screen = ReturnType<typeof renderApp>;

interface EditCase {
  kind: ContextKind;
  tab: string;
  singular: string;
  /** Visible in the list before the edit (the row's title). */
  before: string;
  seed: () => void;
  edit: (screen: Screen, dialog: HTMLElement) => Promise<void>;
  /** Exactly the PATCH body the edit must send. */
  body: Record<string, unknown>;
  /** Visible in the list after the edit. */
  after: string | RegExp;
}

async function retype(screen: Screen, dialog: HTMLElement, label: RegExp, value: string) {
  const input = within(dialog).getByLabelText(label);
  await screen.user.clear(input);
  if (value) await screen.user.type(input, value);
}

const CASES: EditCase[] = [
  {
    kind: 'products',
    tab: 'Products & services',
    singular: 'product',
    before: 'Signature Cut',
    seed: () => contextModel.seed('products', 'row-products', { name: 'Signature Cut', keywords: ['cut'] }),
    edit: (s, d) => retype(s, d, /^name/i, 'Signature Cut Pro'),
    body: { name: 'Signature Cut Pro' },
    after: 'Signature Cut Pro',
  },
  {
    kind: 'audiences',
    tab: 'Audiences',
    singular: 'audience',
    before: 'New parents',
    seed: () => contextModel.seed('audiences', 'row-audiences', { label: 'New parents' }),
    edit: (s, d) => retype(s, d, /^label/i, 'Busy new parents'),
    body: { label: 'Busy new parents' },
    after: 'Busy new parents',
  },
  {
    kind: 'competitors',
    tab: 'Competitors',
    singular: 'competitor',
    before: 'Rival Co',
    // The founder's example: a typo in a competitor no longer means delete and recreate.
    seed: () => contextModel.seed('competitors', 'row-competitors', { name: 'Rival Co', website: 'https://rival.example' }),
    edit: (s, d) => retype(s, d, /^name/i, 'Rival Inc'),
    body: { name: 'Rival Inc' },
    after: 'Rival Inc',
  },
  {
    kind: 'brand-voice',
    tab: 'Brand voice',
    singular: 'voice profile',
    before: 'warm',
    seed: () => contextModel.seed('brand-voice', 'row-brand-voice', { tone: ['warm'] }),
    edit: (s, d) => retype(s, d, /^example copy/i, 'Hello, neighbour.'),
    body: { example_copy: 'Hello, neighbour.' },
    after: 'warm',
  },
  {
    kind: 'offers',
    tab: 'Offers',
    singular: 'offer',
    before: 'Spring',
    seed: () =>
      contextModel.seed('offers', 'row-offers', { name: 'Spring', eligible_location_ids: ['loc-dallas'] }),
    edit: (s, d) => retype(s, d, /^name/i, 'Spring sale'),
    body: { name: 'Spring sale' },
    after: 'Spring sale',
  },
  {
    kind: 'claims',
    tab: 'Claims library',
    singular: 'claim',
    before: 'Fastest in town',
    seed: () => contextModel.seed('claims', 'row-claims', { text: 'Fastest in town' }),
    edit: (s, d) => retype(s, d, /^claim text/i, 'Among the fastest in town'),
    body: { text: 'Among the fastest in town' },
    after: 'Among the fastest in town',
  },
  {
    kind: 'campaigns',
    tab: 'Campaigns',
    singular: 'campaign',
    before: 'Launch',
    seed: () => contextModel.seed('campaigns', 'row-campaigns', { name: 'Launch', location_ids: ['loc-dallas'] }),
    edit: (s, d) => retype(s, d, /^name/i, 'Launch v2'),
    body: { name: 'Launch v2' },
    after: 'Launch v2',
  },
  {
    kind: 'source-preferences',
    tab: 'Sources',
    singular: 'source preference',
    before: 'Enabled',
    seed: () => contextModel.seed('source-preferences', 'row-source-preferences', { source_type: 'manual' }),
    edit: async (s, d) => {
      await s.user.click(within(d).getByRole('switch', { name: /enabled/i }));
    },
    body: { enabled: false },
    after: 'Disabled',
  },
  {
    kind: 'channel-preferences',
    tab: 'Channels',
    singular: 'channel preference',
    before: 'Email',
    seed: () => contextModel.seed('channel-preferences', 'row-channel-preferences', { channel: 'Email' }),
    edit: (s, d) => retype(s, d, /^channel/i, 'Newsletter'),
    body: { channel: 'Newsletter' },
    after: 'Newsletter',
  },
];

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

async function openTab(screen: Screen, tab: string) {
  await screen.findByRole('heading', { name: /campaign context/i });
  await screen.user.click(screen.getByRole('tab', { name: new RegExp(`^${escape(tab)}$`, 'i') }));
  return screen.findByRole('tabpanel');
}

function patches() {
  return orgModel.requests().filter((r) => r.method === 'PATCH');
}

describe('editing campaign context (P6-PLAT-9)', () => {
  beforeEach(() => {
    orgModel.reset();
    contextModel.reset();
  });
  afterEach(() => {
    expect(orgModel.violations()).toEqual([]);
  });

  it.each(CASES)('$tab: sends only the changed field and keeps the row', async (c) => {
    c.seed();
    const stored = { ...contextModel.rows(c.kind)[0]! };
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, c.tab);
    await within(panel).findByText(c.before);

    await screen.user.click(within(panel).getByRole('button', { name: new RegExp(`^edit ${escape(c.singular)}$`, 'i') }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('heading', { name: new RegExp(`^edit ${escape(c.singular)}$`, 'i') })).toBeInTheDocument();
    await c.edit(screen, dialog);
    await screen.user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(await within(await screen.findByRole('tabpanel')).findByText(c.after)).toBeInTheDocument();

    // One PATCH, on this row's id, carrying exactly the changed field.
    const sent = patches();
    expect(sent).toHaveLength(1);
    expect(sent[0]!.path).toBe(`${API_PREFIX}/workspaces/ws-1/${c.kind}/${stored.id}`);
    expect(sent[0]!.body).toEqual(c.body);
    // The stored row kept its id and every field the form does not show.
    const row = contextModel.rows(c.kind)[0]!;
    expect(row.id).toBe(stored.id);
    for (const [key, value] of Object.entries(stored)) {
      if (!(key in c.body) && key !== 'updated_at') expect(row[key], key).toEqual(value);
    }
  });

  it('clearing an optional text field sends null, which clears it', async () => {
    contextModel.seed('products', 'row-p', { name: 'Signature Cut', description: 'Old description' });
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, 'Products & services');
    await within(panel).findByText('Old description');
    await screen.user.click(within(panel).getByRole('button', { name: /^edit product$/i }));
    const dialog = await screen.findByRole('dialog');
    await retype(screen, dialog, /^description/i, '');
    await screen.user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(patches()[0]!.body).toEqual({ description: null });
    expect(contextModel.rows('products')[0]!.description).toBeNull();
  });

  it('saving is disabled until something changes, and while a required field is empty', async () => {
    contextModel.seed('competitors', 'row-c', { name: 'Rival Co' });
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, 'Competitors');
    await within(panel).findByText('Rival Co');
    await screen.user.click(within(panel).getByRole('button', { name: /^edit competitor$/i }));
    const dialog = await screen.findByRole('dialog');
    const save = within(dialog).getByRole('button', { name: /^save changes$/i });
    expect(within(dialog).getByLabelText(/^name/i)).toHaveValue('Rival Co');
    expect(save).toBeDisabled();
    await retype(screen, dialog, /^name/i, '');
    expect(save).toBeDisabled();
    await retype(screen, dialog, /^name/i, 'Rival Co');
    expect(save).toBeDisabled(); // back to the stored value: nothing to send
    await retype(screen, dialog, /^name/i, 'Rival Ltd');
    expect(save).toBeEnabled();
    expect(patches()).toHaveLength(0);
  });

  it('shows the API refusal in the dialog and keeps it open', async () => {
    contextModel.seed('campaigns', 'row-k', { name: 'Launch' });
    server.use(
      http.patch(P('/workspaces/:ws/campaigns/:id'), () =>
        HttpResponse.json(
          { error: { code: 'not_found', message: 'Location not found in this workspace.', request_id: 'r' } },
          { status: 404 },
        ),
      ),
    );
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, 'Campaigns');
    await within(panel).findByText('Launch');
    await screen.user.click(within(panel).getByRole('button', { name: /^edit campaign$/i }));
    const dialog = await screen.findByRole('dialog');
    await retype(screen, dialog, /^name/i, 'Launch v2');
    await screen.user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Location not found in this workspace.');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(contextModel.rows('campaigns')[0]!.name).toBe('Launch');
  });

  it('opening Add after an Edit starts from an empty form', async () => {
    contextModel.seed('products', 'row-p', { name: 'Signature Cut' });
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, 'Products & services');
    await within(panel).findByText('Signature Cut');
    await screen.user.click(within(panel).getByRole('button', { name: /^edit product$/i }));
    let dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByLabelText(/^name/i)).toHaveValue('Signature Cut');
    await screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await screen.user.click(within(panel).getByRole('button', { name: /^add product$/i }));
    dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('heading', { name: /^add product$/i })).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/^name/i)).toHaveValue('');
  });
});

describe('the edit control follows the API role gate (P6-PLAT-9)', () => {
  // The backend gate on PATCH is require_role(OWNER, ADMIN, MARKETER), the same as POST
  // and DELETE; written out here rather than read from lib/roles.ts, as in
  // role-control-matrix.test.tsx, so a wrong frontend helper cannot pass this test.
  const ROLES: ModelRole[] = ['owner', 'admin', 'marketer', 'reviewer', 'compliance_reviewer', 'viewer'];
  const EDITOR = new Set<ModelRole>(['owner', 'admin', 'marketer']);

  beforeEach(() => {
    orgModel.reset();
    contextModel.reset();
    for (const c of CASES) c.seed();
  });

  it.each(ROLES)('%s', async (role) => {
    orgModel.setMembership('org-1', 'user-1', role);
    const screen = renderApp(<App />, { route: '/context' });
    for (const c of CASES) {
      const panel = await openTab(screen, c.tab);
      // The read surface renders for every role before the control is judged.
      await within(panel).findByText(c.before);
      const name = new RegExp(`^edit ${escape(c.singular)}$`, 'i');
      if (EDITOR.has(role)) {
        // Positive instance: the editor is actually offered the control (wait for the
        // role to resolve; until then every role is denied).
        await waitFor(() => expect(within(panel).getAllByRole('button', { name })).toHaveLength(1));
        expect(within(panel).getByRole('button', { name })).toBeEnabled();
      } else {
        expect(within(panel).queryAllByRole('button', { name }), c.tab).toHaveLength(0);
      }
    }
  });
});
