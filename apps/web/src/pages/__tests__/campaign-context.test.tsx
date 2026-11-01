import { waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import type { ContextKind, ContextPatchByKind } from '@/api/endpoints';
import {
  CONTEXT_FIELD_RULES,
  CONTEXT_ROW_DEFAULTS,
  contextModel,
  orgModel,
  type ContextFieldRule,
  type ModelRole,
} from '@/test/handlers';
import { renderApp } from '@/test/utils';
import { titleCase } from '@/lib/utils';

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

// =====================================================================================
// P6-PLAT-9 (customer half): edit a stored row of each of the nine collections through
// PATCH /workspaces/{workspace_id}/{kind}/{item_id}.
// =====================================================================================

// ---- 1. The backend model these tests run against is the generated contract --------
//
// apps/api/openapi.json is generated from the API (npm run gen:types; CI fails on drift),
// so checking the MSW model against it here means the model cannot quietly disagree with
// the API about which keys a row has, which take null, which refuse an empty string, or
// what a column holds when nobody set it.

interface JsonSchema {
  type?: string;
  format?: string;
  minLength?: number;
  maxItems?: number;
  items?: JsonSchema;
  anyOf?: JsonSchema[];
  default?: unknown;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  additionalProperties?: boolean;
  $ref?: string;
}
interface OpenApi {
  paths: Record<string, Record<string, { requestBody?: { content: Record<string, { schema: JsonSchema }> } }>>;
  components: { schemas: Record<string, JsonSchema> };
}

const openapiSources = import.meta.glob('../../../../api/openapi.json', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;
const OPENAPI = JSON.parse(Object.values(openapiSources)[0] ?? '{}') as OpenApi;

const KINDS = Object.keys(CONTEXT_FIELD_RULES) as ContextKind[];
/** Keys a JSON-Schema property may carry for `ruleOf` to classify it; anything else is unmodelled. */
const KNOWN_KEYS = new Set(['type', 'format', 'minLength', 'maxItems', 'items', 'anyOf', 'title', 'description', 'default', 'additionalProperties']);

/** The MSW rule a published property maps to, or a reason it maps to none. */
function ruleOf(property: JsonSchema): string {
  const unknown = Object.keys(property).filter((k) => !KNOWN_KEYS.has(k));
  if (unknown.length) return `unmodelled keys ${unknown.join(',')}`;
  const variants = property.anyOf ?? [property];
  const nullable = variants.some((v) => v.type === 'null');
  const [main, ...more] = variants.filter((v) => v.type !== 'null');
  if (!main || more.length) return `unmodelled union ${JSON.stringify(property)}`;
  let base: string;
  if (main.type === 'string') {
    if (main.format === 'date-time') base = 'date-time';
    else if (main.format !== undefined) return `unmodelled format ${main.format}`;
    else if (main.minLength === 1) base = 'string+';
    else if (main.minLength !== undefined) return `unmodelled minLength ${main.minLength}`;
    else base = 'string';
  } else if (main.type === 'array') {
    // maxItems (500 on the two location-id lists) is not modelled: the form never sends them.
    if (main.items?.type !== 'string' || Object.keys(main.items).some((k) => k !== 'type')) {
      return `unmodelled items ${JSON.stringify(main.items)}`;
    }
    base = 'string[]';
  } else if (main.type === 'object' && main.additionalProperties === true) {
    base = 'object';
  } else if (main.type === 'integer' || main.type === 'number' || main.type === 'boolean') {
    base = main.type;
  } else {
    return `unmodelled type ${JSON.stringify(main)}`;
  }
  return nullable ? `${base}?` : base;
}

function schemaOf(path: string, method: 'patch' | 'post'): { name: string; schema: JsonSchema } {
  const ref = OPENAPI.paths[path]?.[method]?.requestBody?.content['application/json']?.schema.$ref ?? '';
  const name = ref.replace('#/components/schemas/', '');
  return { name, schema: OPENAPI.components.schemas[name] ?? {} };
}

describe('the context model matches the generated contract (P6-PLAT-9)', () => {
  it('reads the contract', () => {
    expect(Object.keys(openapiSources)).toHaveLength(1);
    expect(Object.keys(OPENAPI.paths ?? {}).length).toBeGreaterThan(50);
  });

  it.each(KINDS)('%s: every PATCH field, its type, null and minimum length', (kind) => {
    const { name, schema } = schemaOf(`${API_PREFIX}/workspaces/{workspace_id}/${kind}/{item_id}`, 'patch');
    expect(name).toMatch(/Patch$/);
    // Nothing is required in a PATCH body: an omitted field is left unchanged.
    expect(schema.required).toBeUndefined();
    const published = Object.fromEntries(
      Object.entries(schema.properties ?? {}).map(([key, property]) => [key, ruleOf(property)]),
    );
    expect(published).toEqual(CONTEXT_FIELD_RULES[kind] as Record<string, ContextFieldRule>);
  });

  it.each(KINDS)('%s: a stored column nobody set holds the create schema default', (kind) => {
    const { schema } = schemaOf(`${API_PREFIX}/workspaces/{workspace_id}/${kind}`, 'post');
    const defaults = CONTEXT_ROW_DEFAULTS[kind] as Record<string, unknown>;
    expect(Object.keys(schema.properties ?? {}).sort()).toEqual(Object.keys(defaults).sort());
    for (const [key, property] of Object.entries(schema.properties ?? {})) {
      const rule = (CONTEXT_FIELD_RULES[kind] as Record<string, ContextFieldRule>)[key]!;
      if ((schema.required ?? []).includes(key)) {
        // A required create field has no default; the model's '' is only a placeholder
        // that every seed overwrites (contextModel.seed callers always pass it).
        expect(defaults[key], key).toBe('');
      } else if ('default' in property) {
        expect(defaults[key], key).toEqual(property.default);
      } else if (rule.endsWith('?')) {
        expect(defaults[key], key).toBeNull();
      } else if (rule === 'string[]') {
        expect(defaults[key], key).toEqual([]); // Field(default_factory=list)
      } else {
        expect(rule, key).toBe('object');
        expect(defaults[key], key).toEqual({}); // Field(default_factory=dict)
      }
    }
  });
});

// ---- 2. Editing each collection --------------------------------------------------------

type Screen = ReturnType<typeof renderApp>;

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const exactly = (s: string) => new RegExp(`^${escape(s)}$`, 'i');

async function openTab(screen: Screen, tab: string) {
  await screen.findByRole('heading', { name: /campaign context/i });
  await screen.user.click(screen.getByRole('tab', { name: exactly(tab) }));
  return screen.findByRole('tabpanel');
}

async function openEditor(screen: Screen, panel: HTMLElement, singular: string, title: string) {
  await within(panel).findByText(title);
  await screen.user.click(within(panel).getByRole('button', { name: exactly(`edit ${singular}`) }));
  const dialog = await screen.findByRole('dialog', { name: exactly(`edit ${singular}`) });
  return dialog;
}

async function retype(screen: Screen, dialog: HTMLElement, label: RegExp, value: string) {
  const input = within(dialog).getByLabelText(label);
  await screen.user.clear(input);
  if (value) await screen.user.type(input, value);
}

async function addTag(screen: Screen, dialog: HTMLElement, label: RegExp, tag: string) {
  await screen.user.type(within(dialog).getByLabelText(label), `${tag}{Enter}`);
}

async function choose(screen: Screen, dialog: HTMLElement, label: RegExp, option: RegExp) {
  await screen.user.click(within(dialog).getByRole('combobox', { name: label }));
  await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: option }));
}

const save = (screen: Screen, dialog: HTMLElement) =>
  screen.user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));

const patches = () => orgModel.requests().filter((r) => r.method === 'PATCH');

interface EditCase<K extends ContextKind = ContextKind> {
  kind: K;
  tab: string;
  singular: string;
  /** Every field the form shows, plus fields it does not show, which must survive the edit. */
  stored: ContextPatchByKind[K];
  /** The row's title in the list before the edit. */
  title: string;
  /** The form opens on the stored values. */
  shows: (dialog: HTMLElement) => void;
  edit: (screen: Screen, dialog: HTMLElement) => Promise<void>;
  /** Exactly the PATCH body the edit must send, typed by the generated PATCH schema. */
  body: ContextPatchByKind[K];
  /** Visible in the list after the edit. */
  after: string;
}

const kase = <K extends ContextKind>(c: EditCase<K>): EditCase => c as unknown as EditCase;

const value = (dialog: HTMLElement, label: RegExp) => within(dialog).getByLabelText(label);
const tagShown = (dialog: HTMLElement, tag: string) =>
  expect(within(dialog).getByRole('button', { name: `Remove ${tag}` })).toBeInTheDocument();

const CASES: EditCase[] = [
  kase({
    kind: 'products',
    tab: 'Products & services',
    singular: 'product',
    stored: {
      name: 'Signature Cut',
      description: 'Our classic cut',
      audience: 'Busy professionals',
      keywords: ['cut'],
      pain_points: ['no time'],
      use_cases: ['before work'],
      // Not on the form: never sent, so never overwritten.
      relevance_weight: 2.5,
      claims_rules: { avoid: ['cheapest'] },
      exclusion_rules: ['kids'],
    },
    title: 'Signature Cut',
    shows: (d) => {
      expect(value(d, /^name/i)).toHaveValue('Signature Cut');
      expect(value(d, /^description/i)).toHaveValue('Our classic cut');
      expect(value(d, /^primary audience/i)).toHaveValue('Busy professionals');
      tagShown(d, 'cut');
      tagShown(d, 'no time');
      tagShown(d, 'before work');
    },
    edit: async (s, d) => {
      await retype(s, d, /^name/i, 'Signature Cut Pro');
      await addTag(s, d, /^keywords/i, 'trim');
    },
    body: { name: 'Signature Cut Pro', keywords: ['cut', 'trim'] },
    after: 'Signature Cut Pro',
  }),
  kase({
    kind: 'audiences',
    tab: 'Audiences',
    singular: 'audience',
    stored: {
      label: 'New parents',
      description: 'First child under two',
      motivations: ['sleep', 'time'],
      objections: ['price'],
      keywords: ['baby'],
      demographics: { age: '25-40' },
    },
    title: 'New parents',
    shows: (d) => {
      expect(value(d, /^label/i)).toHaveValue('New parents');
      expect(value(d, /^description/i)).toHaveValue('First child under two');
      tagShown(d, 'sleep');
      tagShown(d, 'price');
      tagShown(d, 'baby');
    },
    edit: async (s, d) => {
      await retype(s, d, /^description/i, '');
      await s.user.click(within(d).getByRole('button', { name: 'Remove sleep' }));
    },
    body: { description: null, motivations: ['time'] },
    after: 'New parents',
  }),
  kase({
    kind: 'competitors',
    tab: 'Competitors',
    singular: 'competitor',
    // The harm the row names: a typo in a competitor no longer means delete and recreate.
    stored: { name: 'Rivl Co', website: 'https://rival.example', known_weaknesses: ['slow'], notes: 'Downtown' },
    title: 'Rivl Co',
    shows: (d) => {
      expect(value(d, /^name/i)).toHaveValue('Rivl Co');
      expect(value(d, /^website/i)).toHaveValue('https://rival.example');
      expect(value(d, /^notes/i)).toHaveValue('Downtown');
      tagShown(d, 'slow');
    },
    edit: (s, d) => retype(s, d, /^name/i, 'Rival Co'),
    body: { name: 'Rival Co' },
    after: 'Rival Co',
  }),
  kase({
    kind: 'brand-voice',
    tab: 'Brand voice',
    singular: 'voice profile',
    stored: { tone: ['warm'], personality: ['playful'], do_use: ['fresh'], avoid: ['cheap'], example_copy: 'Hi there' },
    title: 'warm',
    shows: (d) => {
      tagShown(d, 'warm');
      tagShown(d, 'playful');
      tagShown(d, 'fresh');
      tagShown(d, 'cheap');
      expect(value(d, /^example copy/i)).toHaveValue('Hi there');
    },
    edit: async (s, d) => {
      await addTag(s, d, /^tone/i, 'direct');
      await retype(s, d, /^example copy/i, 'Hello, neighbour.');
    },
    body: { tone: ['warm', 'direct'], example_copy: 'Hello, neighbour.' },
    after: 'warm, direct',
  }),
  kase({
    kind: 'offers',
    tab: 'Offers',
    singular: 'offer',
    stored: {
      name: 'Spring',
      product_service: 'Haircut',
      promo_code: 'SPRING10',
      cta: 'Book now',
      terms: 'Weekdays only',
      required_disclaimer: 'While stocks last',
      // Not on the form: the location list, the discount and the flag stay as they are.
      eligible_location_ids: ['loc-dallas'],
      percentage_discount: 10,
      is_active: false,
    },
    title: 'Spring',
    shows: (d) => {
      expect(value(d, /^name/i)).toHaveValue('Spring');
      expect(value(d, /^product \/ service/i)).toHaveValue('Haircut');
      expect(value(d, /^promo code/i)).toHaveValue('SPRING10');
      expect(value(d, /^call to action/i)).toHaveValue('Book now');
      expect(value(d, /^terms/i)).toHaveValue('Weekdays only');
      expect(value(d, /^required disclaimer/i)).toHaveValue('While stocks last');
    },
    edit: async (s, d) => {
      await retype(s, d, /^promo code/i, 'SPRING15');
      await retype(s, d, /^call to action/i, '');
    },
    body: { promo_code: 'SPRING15', cta: null },
    after: 'Spring',
  }),
  kase({
    kind: 'claims',
    tab: 'Claims library',
    singular: 'claim',
    stored: { text: 'Fastest in town', kind: 'approved', risk_level: 'low', category: 'speed', notes: 'Checked' },
    title: 'Fastest in town',
    shows: (d) => {
      expect(value(d, /^claim text/i)).toHaveValue('Fastest in town');
      expect(within(d).getByRole('combobox', { name: /^kind/i })).toHaveTextContent('Approved');
      expect(within(d).getByRole('combobox', { name: /^risk level/i })).toHaveTextContent('Low');
      expect(value(d, /^category/i)).toHaveValue('speed');
      expect(value(d, /^notes/i)).toHaveValue('Checked');
    },
    edit: async (s, d) => {
      await choose(s, d, /^kind/i, /^restricted$/i);
      await choose(s, d, /^risk level/i, /^high$/i);
    },
    body: { kind: 'restricted', risk_level: 'high' },
    after: 'Restricted · High risk',
  }),
  kase({
    kind: 'campaigns',
    tab: 'Campaigns',
    singular: 'campaign',
    stored: {
      name: 'Launch',
      goal: 'Awareness',
      mode: 'per_location',
      // Not on the form.
      location_ids: ['loc-dallas', 'loc-london'],
      status: 'paused',
    },
    title: 'Launch',
    shows: (d) => {
      expect(value(d, /^name/i)).toHaveValue('Launch');
      expect(value(d, /^goal/i)).toHaveValue('Awareness');
      expect(within(d).getByRole('combobox', { name: /^location mode/i })).toHaveTextContent('Independent per location');
    },
    edit: async (s, d) => {
      await retype(s, d, /^goal/i, 'Repeat visits');
      await choose(s, d, /^location mode/i, /^grouped sets$/i);
    },
    body: { goal: 'Repeat visits', mode: 'grouped' },
    after: 'Grouped sets',
  }),
  kase({
    kind: 'source-preferences',
    tab: 'Sources',
    singular: 'source preference',
    stored: { source_type: 'reddit', enabled: true, config: { subreddits: ['dallas'] } },
    title: 'Reddit',
    shows: (d) => {
      expect(within(d).getByRole('combobox', { name: /^source/i })).toHaveTextContent('Reddit');
      expect(within(d).getByRole('switch', { name: /^enabled$/i })).toBeChecked();
    },
    edit: async (s, d) => {
      await s.user.click(within(d).getByRole('switch', { name: /^enabled$/i }));
    },
    body: { enabled: false },
    after: 'Disabled',
  }),
  kase({
    kind: 'channel-preferences',
    tab: 'Channels',
    singular: 'channel preference',
    stored: { channel: 'Email', enabled: true, weekly_volume: 5 },
    title: 'Email',
    shows: (d) => {
      expect(value(d, /^channel/i)).toHaveValue('Email');
      expect(within(d).getByRole('switch', { name: /^enabled$/i })).toBeChecked();
      expect(value(d, /^weekly volume/i)).toHaveValue(5);
    },
    edit: async (s, d) => {
      await retype(s, d, /^channel/i, 'Newsletter');
      await retype(s, d, /^weekly volume/i, '7');
    },
    body: { channel: 'Newsletter', weekly_volume: 7 },
    after: 'Newsletter',
  }),
];

/** Every optional text or number field the form shows, per collection: emptied, it is sent as null. */
const CLEARABLE: { kind: ContextKind; tab: string; singular: string; title: string; labels: RegExp[]; body: Record<string, null> }[] = [
  { kind: 'products', tab: 'Products & services', singular: 'product', title: 'Signature Cut', labels: [/^description/i, /^primary audience/i], body: { description: null, audience: null } },
  { kind: 'audiences', tab: 'Audiences', singular: 'audience', title: 'New parents', labels: [/^description/i], body: { description: null } },
  { kind: 'competitors', tab: 'Competitors', singular: 'competitor', title: 'Rivl Co', labels: [/^website/i, /^notes/i], body: { website: null, notes: null } },
  { kind: 'brand-voice', tab: 'Brand voice', singular: 'voice profile', title: 'warm', labels: [/^example copy/i], body: { example_copy: null } },
  {
    kind: 'offers',
    tab: 'Offers',
    singular: 'offer',
    title: 'Spring',
    labels: [/^product \/ service/i, /^promo code/i, /^call to action/i, /^terms/i, /^required disclaimer/i],
    body: { product_service: null, promo_code: null, cta: null, terms: null, required_disclaimer: null },
  },
  { kind: 'claims', tab: 'Claims library', singular: 'claim', title: 'Fastest in town', labels: [/^category/i, /^notes/i], body: { category: null, notes: null } },
  { kind: 'campaigns', tab: 'Campaigns', singular: 'campaign', title: 'Launch', labels: [/^goal/i], body: { goal: null } },
  { kind: 'channel-preferences', tab: 'Channels', singular: 'channel preference', title: 'Email', labels: [/^weekly volume/i], body: { weekly_volume: null } },
];

function seedCase(c: EditCase, id = `row-${c.kind}`) {
  return contextModel.seed(c.kind, id, c.stored);
}

describe('editing campaign context (P6-PLAT-9)', () => {
  beforeEach(() => {
    orgModel.reset();
    contextModel.reset();
  });
  afterEach(() => {
    expect(orgModel.violations()).toEqual([]);
  });

  it.each(CASES)('$tab: opens on the stored row, sends only what changed, keeps the row', async (c) => {
    const before = seedCase(c);
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, c.tab);
    const dialog = await openEditor(screen, panel, c.singular, c.title);
    c.shows(dialog);
    await c.edit(screen, dialog);
    await save(screen, dialog);

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(await within(panel).findByText(c.after)).toBeInTheDocument();
    expect(await screen.findByText(`${titleCase(c.singular)} updated`)).toBeInTheDocument();

    // One PATCH, on this row's id, carrying exactly the changed fields.
    const sent = patches();
    expect(sent).toHaveLength(1);
    expect(sent[0]!.path).toBe(`${API_PREFIX}/workspaces/ws-1/${c.kind}/${before.id}`);
    expect(sent[0]!.body).toEqual(c.body);
    // Still one row, with the same id; every field the edit did not send is as it was.
    const rows = contextModel.rows(c.kind);
    expect(rows).toHaveLength(1);
    const after = rows[0]!;
    expect(after.id).toBe(before.id);
    for (const [key, stored] of Object.entries(before)) {
      if (key === 'updated_at') continue;
      if (key in c.body) expect(after[key], key).toEqual((c.body as Record<string, unknown>)[key]);
      else expect(after[key], key).toEqual(stored);
    }
  });

  it.each(CLEARABLE)('$tab: emptying every optional field sends null for each, which clears it', async (c) => {
    seedCase(CASES.find((x) => x.kind === c.kind)!);
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, c.tab);
    const dialog = await openEditor(screen, panel, c.singular, c.title);
    for (const label of c.labels) await retype(screen, dialog, label, '');
    await save(screen, dialog);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(patches()).toHaveLength(1);
    expect(patches()[0]!.body).toEqual(c.body);
    for (const key of Object.keys(c.body)) expect(contextModel.rows(c.kind)[0]![key], key).toBeNull();
  });

  it('saving is disabled until something changes, and while a required field is empty', async () => {
    contextModel.seed('competitors', 'row-c', { name: 'Rival Co' });
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, 'Competitors');
    const dialog = await openEditor(screen, panel, 'competitor', 'Rival Co');
    const button = within(dialog).getByRole('button', { name: /^save changes$/i });
    expect(button).toBeDisabled();
    await retype(screen, dialog, /^name/i, '');
    expect(button).toBeDisabled(); // the API refuses an empty name (min_length=1)
    await retype(screen, dialog, /^name/i, 'Rival Co');
    expect(button).toBeDisabled(); // back to the stored value: nothing to send
    await retype(screen, dialog, /^name/i, 'Rival Ltd');
    expect(button).toBeEnabled();
    expect(patches()).toHaveLength(0);
  });

  it('Cancel sends nothing, and opening the row again starts from the stored values', async () => {
    contextModel.seed('products', 'row-p', { name: 'Signature Cut' });
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, 'Products & services');
    let dialog = await openEditor(screen, panel, 'product', 'Signature Cut');
    await retype(screen, dialog, /^name/i, 'Abandoned draft');
    await screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    dialog = await openEditor(screen, panel, 'product', 'Signature Cut');
    expect(within(dialog).getByLabelText(/^name/i)).toHaveValue('Signature Cut');
    expect(patches()).toHaveLength(0);
  });

  it('opening Add after an Edit starts from an empty form', async () => {
    contextModel.seed('products', 'row-p', { name: 'Signature Cut' });
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, 'Products & services');
    let dialog = await openEditor(screen, panel, 'product', 'Signature Cut');
    expect(within(dialog).getByLabelText(/^name/i)).toHaveValue('Signature Cut');
    await screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await screen.user.click(within(panel).getByRole('button', { name: /^add product$/i }));
    dialog = await screen.findByRole('dialog', { name: /^add product$/i });
    expect(within(dialog).getByLabelText(/^name/i)).toHaveValue('');
  });

  it('a stored value the list does not offer is shown as stored, and kept unless changed', async () => {
    // The demo seed stores claim kind and risk "blocked" (apps/api/app/db/seed.py); the API
    // accepts any string for both, and the form offers neither.
    contextModel.seed('claims', 'row-blocked', { text: 'Better than everyone', kind: 'blocked', risk_level: 'blocked' });
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, 'Claims library');
    const dialog = await openEditor(screen, panel, 'claim', 'Better than everyone');
    expect(within(dialog).getByRole('combobox', { name: /^kind/i })).toHaveTextContent('Blocked');
    expect(within(dialog).getByRole('combobox', { name: /^risk level/i })).toHaveTextContent('Blocked');
    await retype(screen, dialog, /^notes/i, 'Never use');
    await save(screen, dialog);
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(patches()[0]!.body).toEqual({ notes: 'Never use' });
    expect(contextModel.rows('claims')[0]).toMatchObject({ kind: 'blocked', risk_level: 'blocked', notes: 'Never use' });
  });
});

// ---- 3. Refusals the API can give an edit, shown in the dialog -------------------------
//
// At N the context PATCH answers 200, 401, 403 (not a member; role outside EDITORS), 404
// (no such row in this workspace) and 422 (body validation; a deactivated location on the
// two location lists, which this form never sends). It has no 409: no context table has a
// uniqueness constraint and the route raises no ConflictError.

describe('an edit the API refuses (P6-PLAT-9)', () => {
  beforeEach(() => {
    orgModel.reset();
    contextModel.reset();
  });
  afterEach(() => {
    expect(orgModel.violations()).toEqual([]);
  });

  it('422: a fractional weekly volume shows the field error, the dialog stays open, the row is unchanged', async () => {
    contextModel.seed('channel-preferences', 'row-ch', { channel: 'Email', weekly_volume: 5 });
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, 'Channels');
    const dialog = await openEditor(screen, panel, 'channel preference', 'Email');
    await retype(screen, dialog, /^weekly volume/i, '2.5');
    await save(screen, dialog);
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'Weekly volume: Input should be a valid integer, got a number with a fractional part',
    );
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(patches()[0]!.body).toEqual({ weekly_volume: 2.5 });
    expect(contextModel.rows('channel-preferences')[0]!.weekly_volume).toBe(5);
  });

  it('404: a row removed meanwhile shows the refusal and leaves the list', async () => {
    contextModel.seed('competitors', 'row-gone', { name: 'Rival Co' });
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, 'Competitors');
    const dialog = await openEditor(screen, panel, 'competitor', 'Rival Co');
    contextModel.removeBehindTheUi('competitors', 'row-gone');
    await retype(screen, dialog, /^name/i, 'Rival Inc');
    await save(screen, dialog);
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Item not found in this workspace.');
    // The list was refreshed: the row it offered is gone.
    expect(await within(panel).findByText(/no competitors yet/i)).toBeInTheDocument();
    expect(contextModel.rows('competitors')).toHaveLength(0);
  });

  it('403: a role lowered behind the screen shows the refusal and changes nothing', async () => {
    contextModel.seed('offers', 'row-o', { name: 'Spring' });
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, 'Offers');
    const dialog = await openEditor(screen, panel, 'offer', 'Spring');
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'viewer');
    await retype(screen, dialog, /^name/i, 'Spring sale');
    await save(screen, dialog);
    expect(await within(dialog).findByRole('alert')).toHaveTextContent("Role 'viewer' is not permitted for this action.");
    expect(contextModel.rows('offers')[0]!.name).toBe('Spring');
  });
});

// ---- 4. The edit control follows the API role gate ------------------------------------

describe('the edit control follows the API role gate (P6-PLAT-9)', () => {
  // The backend gate on PATCH is require_role(OWNER, ADMIN, MARKETER) (EDITORS in
  // apps/api/app/campaign_context/routes.py), the same as POST and DELETE. Written out
  // here rather than read from lib/roles.ts, as in role-control-matrix.test.tsx, so a
  // wrong frontend helper cannot pass this test.
  const ROLES: ModelRole[] = ['owner', 'admin', 'marketer', 'reviewer', 'compliance_reviewer', 'viewer'];
  const EDITOR = new Set<ModelRole>(['owner', 'admin', 'marketer']);

  beforeEach(() => {
    orgModel.reset();
    contextModel.reset();
    for (const c of CASES) seedCase(c);
  });

  it.each(ROLES)('%s', async (role) => {
    orgModel.setMembership('org-1', 'user-1', role);
    const screen = renderApp(<App />, { route: '/context' });
    for (const c of CASES) {
      const panel = await openTab(screen, c.tab);
      // The read surface renders for every role before the control is judged.
      await within(panel).findByText(c.title);
      const name = exactly(`edit ${c.singular}`);
      if (EDITOR.has(role)) {
        // Positive instance: the editor is actually offered the control (wait for the
        // role to resolve; until then every role is denied).
        await waitFor(() => expect(within(panel).getAllByRole('button', { name })).toHaveLength(1));
        expect(within(panel).getByRole('button', { name })).toBeEnabled();
      } else {
        expect(within(panel).queryAllByRole('button', { name }), c.tab).toHaveLength(0);
      }
    }
    expect(patches()).toHaveLength(0);
  });
});

// ---- 5. Accessibility of the edit path ---------------------------------------------------

describe('the edit path is accessible (P6-PLAT-9)', () => {
  beforeEach(() => {
    orgModel.reset();
    contextModel.reset();
  });
  afterEach(() => {
    expect(orgModel.violations()).toEqual([]);
  });

  it('each Edit button is named for the action and described by its own row', async () => {
    contextModel.seed('competitors', 'row-1', { name: 'Rival Co' });
    contextModel.seed('competitors', 'row-2', { name: 'Other Ltd' });
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, 'Competitors');
    await within(panel).findByText('Other Ltd');
    const buttons = within(panel).getAllByRole('button', { name: /^edit competitor$/i });
    expect(buttons).toHaveLength(2);
    expect(buttons[0]).toHaveAccessibleDescription('Rival Co');
    expect(buttons[1]).toHaveAccessibleDescription('Other Ltd');
  });

  it('the dialog is named and described, every control has a label, and focus moves in and back', async () => {
    contextModel.seed('channel-preferences', 'row-ch', { channel: 'Email', weekly_volume: 5 });
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, 'Channels');
    await within(panel).findByText('Email');
    const opener = within(panel).getByRole('button', { name: /^edit channel preference$/i });
    await screen.user.click(opener);
    const dialog = await screen.findByRole('dialog', { name: /^edit channel preference$/i });
    expect(dialog).toHaveAccessibleDescription(/Editing “Email”\. Only the fields you change are saved/);
    const controls = [
      ...within(dialog).getAllByRole('textbox'),
      ...within(dialog).getAllByRole('spinbutton'),
      ...within(dialog).getAllByRole('switch'),
    ];
    expect(controls).toHaveLength(3);
    for (const control of controls) expect(control).toHaveAccessibleName();
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    await screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(opener).toHaveFocus();
  });

  it('works from the keyboard: Enter opens, Escape closes without sending, focus returns', async () => {
    contextModel.seed('products', 'row-p', { name: 'Signature Cut' });
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, 'Products & services');
    await within(panel).findByText('Signature Cut');
    const opener = within(panel).getByRole('button', { name: /^edit product$/i });
    opener.focus();
    await screen.user.keyboard('{Enter}');
    const dialog = await screen.findByRole('dialog', { name: /^edit product$/i });
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    await screen.user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(opener).toHaveFocus();
    expect(patches()).toHaveLength(0);
  });

  it('after a save, focus returns to the row it edited', async () => {
    contextModel.seed('products', 'row-p', { name: 'Signature Cut' });
    const screen = renderApp(<App />, { route: '/context' });
    const panel = await openTab(screen, 'Products & services');
    const dialog = await openEditor(screen, panel, 'product', 'Signature Cut');
    await retype(screen, dialog, /^name/i, 'Signature Cut Pro');
    await save(screen, dialog);
    await within(panel).findByText('Signature Cut Pro');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const opener = within(panel).getByRole('button', { name: /^edit product$/i });
    expect(opener).toHaveAccessibleDescription('Signature Cut Pro');
    expect(opener).toHaveFocus();
  });
});
