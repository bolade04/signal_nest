import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { API_PREFIX } from '@/api/config';
import type { DraftExport, DraftExportSummary } from '@/api/types';
import { DraftExportControls, ExportDialog } from '@/pages/creative/ExportDialog';
import { orgModel, type ModelRole } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

// Project Phase 5E (lane 5E-W4): the export dialog and the controls that open it (5E-W3's
// ExportDialog.tsx, adopted at I-5E onto 5E-W1's routes and codes).
//
// - While the export reflection is not literally `{enabled: true}` the controls render
//   nothing and send no export or history request.
// - Export is offered only to OWNER, ADMIN and MARKETER; the history to those plus REVIEWER
//   and COMPLIANCE_REVIEWER (item 32); a VIEWER is offered neither and sends nothing.
// - Success: the chosen format is posted once, the rendered text is shown read-only, and the
//   history refreshes.
// - Every refusal is a static line, never the server's text: 503, 403, 404, and each of the
//   four 409 codes of the export service, with a generic line for any other code.

const WS = 'ws-1';
const DRAFT = 'd-1';
const P = (path: string) => `*${API_PREFIX}${path}`;
const EXPORTS = P(`/workspaces/${WS}/drafts/${DRAFT}/exports`);
const seen: string[] = [];
const posted: unknown[] = [];

function record({ request }: { request: Request }) {
  const url = new URL(request.url);
  seen.push(`${request.method} ${url.pathname}`);
}

beforeEach(() => {
  orgModel.reset();
  seen.length = 0;
  posted.length = 0;
  server.events.on('request:start', record);
});
afterEach(() => {
  server.events.removeListener('request:start', record);
});

const settle = () => new Promise((resolve) => setTimeout(resolve, 60));
const exportRequests = () => seen.filter((s) => s.includes('/exports'));
const reflection = (body: unknown, status = 200) =>
  http.get(P(`/workspaces/${WS}/export-capability`), () =>
    HttpResponse.json(body as object, { status }),
  );

const summary: DraftExportSummary = {
  id: 'e-1',
  draft_id: DRAFT,
  draft_version: 2,
  approval_id: 'a-1',
  format: 'text/plain',
  jurisdiction_code: 'US-TX',
  exported_by_snapshot: 'user-9 Erin Exporter <erin@example.test>',
  is_simulated: true,
  created_at: '2026-11-14T10:00:00Z',
};

function backend({ post }: { post?: { status: number; body: unknown } } = {}) {
  let history: DraftExportSummary[] = [summary];
  server.use(
    reflection({ enabled: true }),
    http.get(EXPORTS, () =>
      HttpResponse.json({ items: history, total: history.length, limit: 20, offset: 0 }),
    ),
    http.post(EXPORTS, async ({ request }) => {
      posted.push(await request.json());
      if (post) return HttpResponse.json(post.body as object, { status: post.status });
      const made: DraftExport = {
        ...summary,
        id: `e-${history.length + 1}`,
        format: 'text/markdown',
        content: '# Weekend parking\n\nEasy weekend visits.',
      };
      history = [made, ...history];
      return HttpResponse.json(made, { status: 201 });
    }),
  );
}

/** The controls inside a host element, so "renders nothing" means the host stays empty. */
function controls() {
  const rendered = renderApp(
    <div data-testid="host">
      <DraftExportControls workspaceId={WS} draftId={DRAFT} draftVersion={2} />
    </div>,
  );
  return { ...rendered, host: () => screen.getByTestId('host') };
}

async function exportOnce(user: ReturnType<typeof renderApp>['user']) {
  await user.click(await screen.findByRole('button', { name: 'Export' }));
  const dialog = await screen.findByRole('dialog');
  await user.click(within(dialog).getByRole('button', { name: 'Export' }));
  return dialog;
}

// --------------------------------------------------------------------------------------
// The gate: reflection and role
// --------------------------------------------------------------------------------------
describe('export controls — gated', () => {
  it.each([
    ['off', { enabled: false }, 200],
    ['the string "true"', { enabled: 'true' }, 200],
    ['failed', { error: { code: 'internal', message: 'x' } }, 500],
  ] as const)('render nothing and send no export request while the reflection is %s', async (_n, body, status) => {
    server.use(reflection(body, status));
    const { host } = controls();
    await waitFor(() => expect(seen.some((s) => s.endsWith('/export-capability'))).toBe(true));
    await settle();
    expect(exportRequests()).toEqual([]);
    expect(host()).toBeEmptyDOMElement();
  });

  it('the shipped default (MSW dark) renders nothing', async () => {
    const { host } = controls();
    await waitFor(() => expect(seen.some((s) => s.endsWith('/export-capability'))).toBe(true));
    await settle();
    expect(host()).toBeEmptyDOMElement();
    expect(exportRequests()).toEqual([]);
  });

  it('a viewer is offered nothing and sends no request at all', async () => {
    orgModel.setMembership('org-1', 'user-1', 'viewer');
    backend();
    const { host } = controls();
    await settle();
    await settle();
    expect(host()).toBeEmptyDOMElement();
    expect(seen.filter((s) => s.includes('export'))).toEqual([]);
  });

  it.each(['reviewer', 'compliance_reviewer'] as ModelRole[])(
    '%s sees the history but is not offered Export',
    async (role) => {
      orgModel.setMembership('org-1', 'user-1', role);
      backend();
      controls();
      expect(await screen.findByRole('list', { name: 'Draft export history' })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Export' })).not.toBeInTheDocument();
    },
  );

  it.each(['owner', 'admin', 'marketer'] as ModelRole[])('%s is offered Export and the history', async (role) => {
    orgModel.setMembership('org-1', 'user-1', role);
    backend();
    controls();
    expect(await screen.findByRole('button', { name: 'Export' })).toBeInTheDocument();
    expect(await screen.findByRole('list', { name: 'Draft export history' })).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------------------
// Success
// --------------------------------------------------------------------------------------
describe('export dialog — success', () => {
  it('posts the chosen format once, shows the text read-only, and refreshes the history', async () => {
    backend();
    const { user } = controls();
    const list = await screen.findByRole('list', { name: 'Draft export history' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(1);
    await user.click(await screen.findByRole('button', { name: 'Export' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('heading', { name: 'Export draft version 2' })).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Markdown' }));
    expect(within(dialog).getByRole('button', { name: 'Markdown' })).toHaveAttribute('aria-pressed', 'true');
    await user.click(within(dialog).getByRole('button', { name: 'Export' }));
    const text = await within(dialog).findByRole('textbox');
    expect(text).toHaveValue('# Weekend parking\n\nEasy weekend visits.');
    expect(text).toHaveAttribute('readonly');
    expect(within(dialog).getByText(/Markdown export of version/)).toBeInTheDocument();
    expect(within(dialog).getByText('Generated by the simulated model.')).toBeInTheDocument();
    expect(posted).toEqual([{ format: 'text/markdown' }]);
    expect(within(dialog).queryByRole('button', { name: 'Export' })).not.toBeInTheDocument();
    // The modal hides the page behind it from the accessibility tree, hence `hidden: true`.
    await waitFor(() => expect(within(list).getAllByRole('listitem', { hidden: true })).toHaveLength(2));
  });

  it('plain text is the default format', async () => {
    backend();
    const { user } = controls();
    await exportOnce(user);
    await waitFor(() => expect(posted).toEqual([{ format: 'text/plain' }]));
  });

  it('copy writes the text to the clipboard, or says it could not', async () => {
    backend();
    const { user } = controls();
    const dialog = await exportOnce(user);
    await within(dialog).findByRole('textbox');
    const written: string[] = [];
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: async (t: string) => void written.push(t) },
      configurable: true,
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Copy' }));
    expect(await within(dialog).findByRole('button', { name: 'Copied' })).toBeInTheDocument();
    expect(written).toEqual(['# Weekend parking\n\nEasy weekend visits.']);
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Copied' }));
    expect(await within(dialog).findByText(/could not be copied/)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------------------
// Refusals: static lines, never server text
// --------------------------------------------------------------------------------------
const REFUSALS: [string, number, string, string][] = [
  ['dark', 503, 'capability_unavailable', "Export isn't available in this workspace."],
  ['wrong role', 403, 'permission_denied', "Your role can't export drafts."],
  ['unknown or foreign draft', 404, 'not_found', "This draft isn't available in this workspace."],
  [
    'no jurisdiction',
    409,
    'export_jurisdiction_missing',
    "This draft or its brief has no recorded jurisdiction, so it can't be exported.",
  ],
  [
    'jurisdiction mismatch',
    409,
    'jurisdiction_mismatch',
    "This draft's jurisdiction doesn't match its brief's, so it can't be exported.",
  ],
  [
    'not approved',
    409,
    'export_not_approved',
    "This version isn't approved. Only a version whose current decision is an approval can be exported.",
  ],
  [
    'review blocked',
    409,
    'export_review_blocked',
    "This draft's content review doesn't allow export, or the claims library changed since it was reviewed.",
  ],
  ['another conflict', 409, 'conflict', "This draft can't be exported in its current state."],
  ['an inherited key', 409, 'toString', "This draft can't be exported in its current state."],
];

describe('export dialog — refusals', () => {
  it.each(REFUSALS)('%s (%i %s) shows its static line and never the server text', async (_n, status, code, line) => {
    backend({ post: { status, body: { error: { code, message: 'SERVER TEXT', request_id: 'r' } } } });
    const { user } = controls();
    const dialog = await exportOnce(user);
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(line);
    expect(within(dialog).getByRole('alert').textContent).toBe(line);
    expect(within(dialog).queryByText(/SERVER TEXT/)).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('textbox')).not.toBeInTheDocument();
    expect(posted).toHaveLength(1); // never retried: an audited write is not duplicated
  });

  it('each 409 code has its own line', () => {
    const lines = REFUSALS.filter(([, s, c]) => s === 409 && c.includes('_')).map(([, , , l]) => l);
    expect(new Set(lines).size).toBe(lines.length);
  });

  it('a network failure is a generic line', async () => {
    server.use(
      reflection({ enabled: true }),
      http.get(EXPORTS, () => HttpResponse.json({ items: [], total: 0, limit: 20, offset: 0 })),
      http.post(EXPORTS, () => HttpResponse.error()),
    );
    const { user } = controls();
    const dialog = await exportOnce(user);
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'The export could not be created. Try again.',
    );
  });
});

describe('export dialog — standalone', () => {
  it('cancel closes without a request', async () => {
    backend();
    let closed = 0;
    const { user } = renderApp(
      <ExportDialog target={{ workspaceId: WS, draftId: DRAFT, draftVersion: 2 }} onClose={() => (closed += 1)} />,
    );
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(closed).toBe(1);
    expect(posted).toEqual([]);
  });
});
