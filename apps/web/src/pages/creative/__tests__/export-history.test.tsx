import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { API_PREFIX } from '@/api/config';
import type { DraftExportSummary } from '@/api/types';
import { DraftExportControls } from '@/pages/creative/ExportDialog';
import { ExportHistory } from '@/pages/creative/ExportHistory';
import { orgModel } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

// Project Phase 5E (lane 5E-W4): a draft version's export history (5E-W3's ExportHistory.tsx).
//
// - Each record names the exporter from `exported_by_snapshot` (never the raw snapshot with its
//   id and address), the format, the version, the jurisdiction and whether it was simulated;
//   the rendered text is never listed.
// - Empty says so; 503, 403 and 404 are static lines, never server text; any other failure
//   offers a retry.
// - `enabled={false}` renders nothing and sends no request, and a VIEWER's controls never ask.

const WS = 'ws-1';
const DRAFT = 'd-1';
const P = (path: string) => `*${API_PREFIX}${path}`;
const EXPORTS = P(`/workspaces/${WS}/drafts/${DRAFT}/exports`);
const seen: string[] = [];

function record({ request }: { request: Request }) {
  const url = new URL(request.url);
  seen.push(`${request.method} ${url.pathname}`);
}

beforeEach(() => {
  orgModel.reset();
  seen.length = 0;
  server.events.on('request:start', record);
});
afterEach(() => {
  server.events.removeListener('request:start', record);
});

const settle = () => new Promise((resolve) => setTimeout(resolve, 60));

function item(over: Partial<DraftExportSummary> = {}): DraftExportSummary {
  return {
    id: 'e-1',
    draft_id: DRAFT,
    draft_version: 2,
    approval_id: 'a-1',
    format: 'text/plain',
    jurisdiction_code: 'US-TX',
    exported_by_snapshot: 'user-9 Erin Exporter <erin@example.test>',
    is_simulated: true,
    created_at: '2026-11-14T10:00:00Z',
    ...over,
  };
}

function history(items: DraftExportSummary[] | { status: number; code: string }) {
  let calls = 0;
  server.use(
    http.get(EXPORTS, () => {
      calls += 1;
      if (!Array.isArray(items)) {
        return HttpResponse.json(
          { error: { code: items.code, message: 'SERVER TEXT', request_id: 'r' } },
          { status: items.status },
        );
      }
      return HttpResponse.json({ items, total: items.length, limit: 20, offset: 0 });
    }),
  );
  return () => calls;
}

describe('export history — records', () => {
  it('names the exporter, format, version and jurisdiction, never the snapshot or the text', async () => {
    history([
      item(),
      item({
        id: 'e-2',
        format: 'json',
        jurisdiction_code: 'GB-ENG',
        is_simulated: false,
        exported_by_snapshot: 'user-7 Mo Marketer <mo@example.test>',
      }),
      item({ id: 'e-3', format: 'text/markdown', exported_by_snapshot: '[erased]' }),
    ]);
    renderApp(<ExportHistory workspaceId={WS} draftId={DRAFT} />);
    const list = await screen.findByRole('list', { name: 'Draft export history' });
    const rows = within(list).getAllByRole('listitem');
    expect(rows).toHaveLength(3);
    expect(within(rows[0]!).getByText('Plain text')).toBeInTheDocument();
    expect(within(rows[0]!).getByText('Version 2')).toBeInTheDocument();
    expect(within(rows[0]!).getByText(/^by Erin Exporter · /)).toBeInTheDocument();
    expect(within(rows[0]!).getByText('Jurisdiction US-TX')).toBeInTheDocument();
    expect(within(rows[0]!).getByText('Simulated')).toBeInTheDocument();
    expect(within(rows[1]!).getByText('JSON')).toBeInTheDocument();
    expect(within(rows[1]!).getByText(/^by Mo Marketer · /)).toBeInTheDocument();
    expect(within(rows[1]!).getByText('Jurisdiction GB-ENG')).toBeInTheDocument();
    expect(within(rows[1]!).queryByText('Simulated')).not.toBeInTheDocument();
    expect(within(rows[2]!).getByText('Markdown')).toBeInTheDocument();
    expect(within(rows[2]!).getByText(/^by Erased account · /)).toBeInTheDocument();
    // The snapshot's id and address never reach the screen.
    expect(screen.queryByText(/user-9|erin@example\.test|mo@example\.test|\[erased\]/)).not.toBeInTheDocument();
  });

  it('an empty history says the draft has not been exported', async () => {
    history([]);
    renderApp(<ExportHistory workspaceId={WS} draftId={DRAFT} />);
    expect(await screen.findByText("This draft hasn't been exported yet.")).toBeInTheDocument();
    expect(screen.queryByRole('list', { name: 'Draft export history' })).not.toBeInTheDocument();
  });

  it('enabled={false} renders nothing and sends no request', async () => {
    const calls = history([item()]);
    renderApp(
      <div data-testid="host">
        <ExportHistory workspaceId={WS} draftId={DRAFT} enabled={false} />
      </div>,
    );
    await settle();
    expect(screen.getByTestId('host')).toBeEmptyDOMElement();
    expect(calls()).toBe(0);
    expect(seen.filter((s) => s.includes('/exports'))).toEqual([]);
  });
});

describe('export history — refusals', () => {
  it.each([
    [503, 'capability_unavailable', "Export isn't available in this workspace."],
    [403, 'permission_denied', "Your role can't see this draft's export history."],
    [404, 'not_found', "This draft isn't available in this workspace."],
  ] as const)('%i is a static line, asked once, never the server text', async (status, code, line) => {
    const calls = history({ status, code });
    renderApp(<ExportHistory workspaceId={WS} draftId={DRAFT} />);
    expect(await screen.findByRole('status')).toHaveTextContent(line);
    expect(screen.queryByText('SERVER TEXT')).not.toBeInTheDocument();
    await settle();
    expect(calls()).toBe(1); // a deterministic refusal is never retried
  });

  it('another deterministic failure offers a retry that asks again', async () => {
    const calls = history({ status: 422, code: 'validation_error' });
    const { user } = renderApp(<ExportHistory workspaceId={WS} draftId={DRAFT} />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('The export history could not be loaded.');
    expect(screen.queryByText('SERVER TEXT')).not.toBeInTheDocument();
    await user.click(within(alert).getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(calls()).toBe(2));
  });
});

describe('export history — who asks', () => {
  it('a viewer’s controls never ask for the history', async () => {
    orgModel.setMembership('org-1', 'user-1', 'viewer');
    server.use(http.get(P(`/workspaces/${WS}/export-capability`), () => HttpResponse.json({ enabled: true })));
    const calls = history([item()]);
    renderApp(<DraftExportControls workspaceId={WS} draftId={DRAFT} draftVersion={2} />);
    await settle();
    await settle();
    expect(calls()).toBe(0);
  });

  it('a compliance reviewer’s controls show the history (item 32)', async () => {
    orgModel.setMembership('org-1', 'user-1', 'compliance_reviewer');
    server.use(http.get(P(`/workspaces/${WS}/export-capability`), () => HttpResponse.json({ enabled: true })));
    history([item()]);
    renderApp(<DraftExportControls workspaceId={WS} draftId={DRAFT} draftVersion={2} />);
    expect(await screen.findByRole('list', { name: 'Draft export history' })).toBeInTheDocument();
  });
});
