import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { createElement, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { ApiError, setAuthToken } from '@/api/client';
import { API_PREFIX } from '@/api/config';
import { getOpportunityIntelligence } from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import { useOpportunityIntelligence } from '@/pages/opportunities/useOpportunityIntelligence';
import { server } from '@/test/server';

const WS = 'ws-1';
const OPP = 'opp-loc-dallas-0';
const P = (path: string) => `*${API_PREFIX}${path}`;

// Record every request the client makes so we can prove no N+1 fan-out and that
// exactly one intelligence call is issued per (workspace, opportunity).
let requested: string[] = [];
function onRequestStart({ request }: { request: Request }) {
  requested.push(new URL(request.url).pathname);
}

beforeEach(() => {
  requested = [];
  setAuthToken('test-token');
  server.events.on('request:start', onRequestStart);
});

afterEach(() => {
  server.events.removeListener('request:start', onRequestStart);
  setAuthToken(null);
});

function wrapper() {
  // Each hook test gets an isolated cache so keys never collide across tests.
  const client = new QueryClient({
    defaultOptions: { queries: { gcTime: 0, staleTime: 0 } },
  });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
}

describe('getOpportunityIntelligence (endpoint)', () => {
  it('requests the workspace- and opportunity-scoped intelligence path', async () => {
    const result = await getOpportunityIntelligence(WS, OPP);
    expect(requested).toContain(`${API_PREFIX}/workspaces/${WS}/opportunities/${OPP}/intelligence`);
    expect(result.opportunity_id).toBe(OPP);
    expect(result.intelligence).not.toBeNull();
  });

  it('treats a 200 with intelligence:null as a successful empty result, not an error', async () => {
    const result = await getOpportunityIntelligence(WS, 'opp-loc-london-1');
    expect(result.intelligence).toBeNull();
  });

  it('surfaces a server error as an ApiError with its status', async () => {
    server.use(
      http.get(P('/workspaces/:ws/opportunities/:id/intelligence'), () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 }),
      ),
    );
    await expect(getOpportunityIntelligence(WS, OPP)).rejects.toMatchObject({
      name: 'ApiError',
      status: 500,
    });
  });

  it('rejects an already-aborted request rather than resolving with data', async () => {
    const controller = new AbortController();
    controller.abort();
    // An aborted signal must never yield a resolved payload; the exact rejection
    // shape (AbortError vs. wrapped network error) is environment-dependent.
    await expect(getOpportunityIntelligence(WS, OPP, controller.signal)).rejects.toThrow();
  });
});

describe('useOpportunityIntelligence (query hook)', () => {
  it('embeds both the workspace and opportunity in the query key', () => {
    expect(queryKeys.opportunityIntelligence(WS, OPP)).toEqual([
      'workspaces',
      WS,
      'opportunities',
      'detail',
      OPP,
      'intelligence',
    ]);
    // Switching either dimension yields a distinct key (no cross-tenant leakage).
    expect(queryKeys.opportunityIntelligence(WS, OPP)).not.toEqual(
      queryKeys.opportunityIntelligence('ws-2', OPP),
    );
    expect(queryKeys.opportunityIntelligence(WS, OPP)).not.toEqual(
      queryKeys.opportunityIntelligence(WS, 'opp-loc-london-0'),
    );
  });

  it('stays disabled and issues no request until both IDs are present', async () => {
    const { result } = renderHook(
      () => useOpportunityIntelligence({ workspaceId: '', opportunityId: OPP }),
      { wrapper: wrapper() },
    );
    expect(result.current.fetchStatus).toBe('idle');
    expect(requested).toHaveLength(0);
  });

  it('fetches exactly once and exposes the payload (no N+1)', async () => {
    const { result } = renderHook(
      () => useOpportunityIntelligence({ workspaceId: WS, opportunityId: OPP }),
      { wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.intelligence).not.toBeNull();
    const intelCalls = requested.filter((p) => p.endsWith('/intelligence'));
    expect(intelCalls).toHaveLength(1);
  });

  it('returns a null payload as success, never as an error', async () => {
    const { result } = renderHook(
      () => useOpportunityIntelligence({ workspaceId: WS, opportunityId: 'opp-loc-london-1' }),
      { wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.intelligence).toBeNull();
    expect(result.current.isError).toBe(false);
  });

  it('does not retry on a client (4xx) error', async () => {
    server.use(
      http.get(P('/workspaces/:ws/opportunities/:id/intelligence'), () =>
        HttpResponse.json({ detail: 'gone' }, { status: 404 }),
      ),
    );
    const { result } = renderHook(
      () => useOpportunityIntelligence({ workspaceId: WS, opportunityId: OPP }),
      { wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(ApiError);
    const intelCalls = requested.filter((p) => p.endsWith('/intelligence'));
    expect(intelCalls).toHaveLength(1);
  });
});
