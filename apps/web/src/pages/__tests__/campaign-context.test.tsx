import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { orgModel, type ModelRole } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

describe('campaign context center', () => {
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

// W21 CF-2: the claims library is a protected safety control. Its writes are exactly
// owner, admin and compliance_reviewer (`CLAIMS_STEWARDS`,
// apps/api/app/campaign_context/routes.py); the other eight kinds keep the editors
// (owner, admin, marketer). The page must offer each role exactly the controls the API
// lets it use: never a control that 403s, never a missing control the role holds.
describe('claims library controls are steward-only (W21 CF-2)', () => {
  const P = (path: string) => `*${API_PREFIX}${path}`;
  const SEEDED_CLAIM = http.get(P('/workspaces/:ws/claims'), () =>
    HttpResponse.json([{ id: 'claim-1', text: 'Seeded blocked claim', kind: 'blocked', risk_level: 'high' }]),
  );

  beforeEach(() => {
    orgModel.reset();
  });

  afterEach(() => {
    expect(orgModel.violations()).toEqual([]);
  });

  async function renderAs(role: ModelRole) {
    orgModel.setMembership('org-1', 'user-1', role);
    const screen = renderApp(<App />, { route: '/context' });
    await screen.findByRole('heading', { name: /campaign context/i });
    await screen.findByText(/no products yet/i);
    return screen;
  }

  async function openClaims(screen: Awaited<ReturnType<typeof renderAs>>) {
    await screen.user.click(screen.getByRole('tab', { name: /^claims library$/i }));
    return screen.findByRole('tabpanel');
  }

  it('a marketer edits products but is offered no claims control, and still reads the library', async () => {
    server.use(SEEDED_CLAIM);
    const screen = await renderAs('marketer');
    // Witness that the role has resolved: the marketer is offered the products controls.
    await waitFor(() => expect(screen.getAllByRole('button', { name: /^add product$/i })).toHaveLength(2));

    const panel = await openClaims(screen);
    expect(await within(panel).findByText('Seeded blocked claim')).toBeInTheDocument();
    expect(within(panel).queryAllByRole('button', { name: /^add claim$/i })).toHaveLength(0);
    expect(within(panel).queryAllByRole('button', { name: /^remove claim$/i })).toHaveLength(0);
  });

  it('a marketer is offered no Add claim on an empty claims library', async () => {
    const screen = await renderAs('marketer');
    await waitFor(() => expect(screen.getAllByRole('button', { name: /^add product$/i })).toHaveLength(2));

    const panel = await openClaims(screen);
    expect(await within(panel).findByText(/no claims yet/i)).toBeInTheDocument();
    expect(within(panel).queryAllByRole('button', { name: /^add claim$/i })).toHaveLength(0);
  });

  it('a compliance reviewer is offered every claims control and no products control', async () => {
    server.use(SEEDED_CLAIM);
    const screen = await renderAs('compliance_reviewer');

    const panel = await openClaims(screen);
    expect(await within(panel).findByText('Seeded blocked claim')).toBeInTheDocument();
    // Positive witness: the role resolved and holds the claims controls.
    const [add] = await within(panel).findAllByRole('button', { name: /^add claim$/i });
    expect(add).toBeEnabled();
    const remove = within(panel).getAllByRole('button', { name: /^remove claim$/i });
    expect(remove).toHaveLength(1);
    expect(remove[0]).toBeEnabled();
    await screen.user.click(add!);
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('heading', { name: /^add claim$/i })).toBeInTheDocument();
    await screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    // A compliance reviewer is no kind of editor: the products tab offers nothing.
    await screen.user.click(screen.getByRole('tab', { name: /^products & services$/i }));
    const products = await screen.findByRole('tabpanel');
    await within(products).findByText(/no products yet/i);
    expect(within(products).queryAllByRole('button', { name: /^add product$/i })).toHaveLength(0);
  });

  it('a compliance reviewer is offered both Add claim controls on an empty claims library', async () => {
    const screen = await renderAs('compliance_reviewer');

    const panel = await openClaims(screen);
    await within(panel).findByText(/no claims yet/i);
    // Header + empty-state Add claim.
    await waitFor(() => expect(within(panel).getAllByRole('button', { name: /^add claim$/i })).toHaveLength(2));
    for (const add of within(panel).getAllByRole('button', { name: /^add claim$/i })) expect(add).toBeEnabled();
  });
});
