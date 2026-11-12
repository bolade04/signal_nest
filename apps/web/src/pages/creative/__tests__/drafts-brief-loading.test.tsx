// W21-REV-5DW3 r4 F-6, as built by the 5D integration (CreativeDraftsPanel `briefLoading`): while
// the drafts page is still reading its brief for the first time, the generate path waits, so the
// Q-1 note, the approve-first note and the empty state's invitation never flash for a brief that
// turns out to have no location (operator ruling Q-4). Once the read ends, loaded or failed, the
// page shows what it showed before; a background refetch of a brief already loaded hides nothing.
//
// Committed at W21-REV-5D-INTEG r1 INFO-1, from the integration lane's probe; it kills that lane's
// mutants MF6a and MF6b and the review's RV-W4.
import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { act, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { useEffect } from 'react';
import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { queryKeys } from '@/api/queryKeys';
import type { ApprovalHistoryOut, CreativeDraftListOut, RecommendationBriefOut } from '@/api/types';
import { opportunityDetailFixture } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

const WS = 'ws-1';
const OPP = 'opp-loc-dallas-0';
const BRIEF = 'brief-1';
const P = (path: string) => `*${API_PREFIX}${path}`;
const BRIEF_URL = P(`/workspaces/${WS}/opportunities/${OPP}/briefs/${BRIEF}`);
const Q1 = /Drafts use the jurisdiction recorded/;
const APPROVE_FIRST = /approve this brief before generating drafts/i;
const INVITATION = /generate one from this brief/i;
const Q4 = /recommendations only/i;

function brief(located: boolean): RecommendationBriefOut {
  return {
    id: BRIEF,
    opportunity_id: OPP,
    intelligence_record_id: `rec-${OPP}`,
    location_id: located ? 'loc-dallas' : null,
    campaign_id: null,
    brief_version: 1,
    parent_brief_id: null,
    analysis_version: '3c',
    scoring_version: '3c.1',
    prompt_name: 'explain_opportunity',
    prompt_version: '1.2.0',
    model_identifier: 'mock-1',
    is_simulated: true,
    created_by: 'user-1',
    created_by_snapshot: 'user-1 Dana Marketer <dana@example.com>',
    created_at: '2026-11-01T09:00:00Z',
    summary: 'Summary of version 1.',
    rationale: 'Inference: commuters are choosing on speed.',
    recommended_actions: [],
    suggested_angles: [],
    risk_notes: [],
    evidence_refs: [],
  };
}

function history(approved: boolean): ApprovalHistoryOut {
  return {
    items: approved
      ? [
          {
            id: 'appr-1',
            workspace_id: WS,
            subject_type: 'recommendation_brief',
            subject_id: BRIEF,
            subject_version: 1,
            brief_id: BRIEF,
            draft_id: null,
            decision: 'approved',
            decision_seq: 1,
            reason_code: null,
            note: null,
            decided_by: 'user-reviewer',
            decided_by_snapshot: 'user-reviewer Rhea Reviewer <rhea@example.com>',
            decided_role: 'reviewer',
            is_simulated: true,
            created_at: '2026-11-01T09:00:00Z',
          },
        ]
      : [],
    total: approved ? 1 : 0,
    limit: 20,
    offset: 0,
  };
}

let cache: QueryClient | null = null;
function CacheProbe() {
  const client = useQueryClient();
  useEffect(() => {
    cache = client;
  }, [client]);
  return null;
}

/**
 * A marketer on the drafts page of brief 1. The brief read is held until `release()` is called
 * (or answers 404 when `briefStatus` is 404); every other read answers at once.
 */
function draftsPage({ located, approved, briefStatus = 200 }: { located: boolean; approved: boolean; briefStatus?: number }) {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  server.use(
    http.get(P('/auth/me'), () =>
      HttpResponse.json({
        access_token: 'test-token',
        token_type: 'bearer',
        user: { id: 'user-1', email: 'demo@signalnest.dev', full_name: 'Demo', is_operator: false },
        memberships: [{ organization_id: 'org-1', role: 'marketer' }],
      }),
    ),
    http.get(P(`/workspaces/${WS}/brief-capability`), () => HttpResponse.json({ enabled: true })),
    http.get(P(`/workspaces/${WS}/creative-capability`), () =>
      HttpResponse.json({ enabled: true, generation_configured: true }),
    ),
    http.get(P(`/workspaces/${WS}/opportunities/${OPP}`), () =>
      HttpResponse.json({ ...opportunityDetailFixture(OPP), ...(located ? {} : { location_id: null }) }),
    ),
    http.get(P(`/workspaces/${WS}/opportunities/${OPP}/briefs/${BRIEF}/approvals`), () =>
      HttpResponse.json(history(approved)),
    ),
    http.get(BRIEF_URL, async () => {
      await gate;
      return briefStatus === 200
        ? HttpResponse.json(brief(located))
        : HttpResponse.json({ error: { code: 'not_found', message: 'Brief not found.' } }, { status: briefStatus });
    }),
    http.get(P(`/workspaces/${WS}/briefs/${BRIEF}/drafts`), () =>
      HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 } satisfies CreativeDraftListOut),
    ),
  );
  const screen = renderApp(
    <>
      <App />
      <CacheProbe />
    </>,
    { route: `/creative/${BRIEF}?opportunity=${OPP}` },
  );
  return { screen, release };
}

/** Long enough for every read but the held brief to have answered and rendered. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 150));

describe('F-6: the drafts page waits for its brief before the generate path', () => {
  for (const located of [false, true]) {
    for (const approved of [true, false]) {
      it(`${located ? 'a located' : 'a no-location'} brief, ${approved ? 'approved' : 'not approved'}: nothing it replaces is shown while it loads`, async () => {
        const { screen, release } = draftsPage({ located, approved });
        await screen.findByText(/No drafts yet/);
        await settle(); // the decision history has answered; the brief has not
        expect(screen.queryByText(Q1)).toBeNull();
        expect(screen.queryByText(APPROVE_FIRST)).toBeNull();
        expect(screen.getByText(/No drafts yet/)).not.toHaveTextContent(INVITATION);
        expect(screen.queryByRole('button', { name: 'Generate draft' })).toBeNull();

        release();
        if (!located) {
          expect(await screen.findByText(Q4)).toBeInTheDocument();
          await settle();
          expect(screen.queryByText(Q1)).toBeNull();
          expect(screen.queryByText(APPROVE_FIRST)).toBeNull();
          expect(screen.getByText(/No drafts yet/)).not.toHaveTextContent(INVITATION);
        } else if (approved) {
          expect(await screen.findByText(Q1)).toBeInTheDocument();
          await waitFor(() => expect(screen.getByText(/No drafts yet/)).toHaveTextContent(INVITATION));
          expect(screen.queryByText(Q4)).toBeNull();
        } else {
          expect(await screen.findByText(APPROVE_FIRST)).toBeInTheDocument();
          expect(screen.queryByText(Q4)).toBeNull();
        }
      });
    }
  }

  it('a brief that fails to load ends the wait and is not "no location": the Q-1 note and Generate come back', async () => {
    const { screen, release } = draftsPage({ located: false, approved: true, briefStatus: 404 });
    await screen.findByText(/No drafts yet/);
    await settle();
    expect(screen.queryByText(Q1)).toBeNull();
    release();
    expect(await screen.findByRole('button', { name: 'Generate draft' })).toBeInTheDocument();
    expect(screen.getByText(Q1)).toBeInTheDocument();
    expect(screen.queryByText(Q4)).toBeNull();
  });

  it('a background refetch of a brief already loaded hides nothing', async () => {
    cache = null;
    const { screen, release } = draftsPage({ located: true, approved: true });
    release();
    expect(await screen.findByRole('button', { name: 'Generate draft' })).toBeInTheDocument();
    expect(screen.getByText(Q1)).toBeInTheDocument();

    // The next read of the brief is held, and the page's brief is refetched in the background.
    let releaseRefetch!: () => void;
    const refetchGate = new Promise<void>((resolve) => {
      releaseRefetch = resolve;
    });
    server.use(
      http.get(BRIEF_URL, async () => {
        await refetchGate;
        return HttpResponse.json(brief(true));
      }),
    );
    const key = queryKeys.opportunityBrief(WS, OPP, BRIEF);
    await act(async () => {
      void cache!.invalidateQueries({ queryKey: key });
    });
    await waitFor(() => expect(cache!.isFetching({ queryKey: key })).toBeGreaterThan(0));
    await settle();
    // Still loaded while it refetches: the generate path stays as it was.
    expect(screen.getByRole('button', { name: 'Generate draft' })).toBeInTheDocument();
    expect(screen.getByText(Q1)).toBeInTheDocument();
    expect(screen.getByText(/No drafts yet/)).toHaveTextContent(INVITATION);
    releaseRefetch();
    await waitFor(() => expect(cache!.isFetching({ queryKey: key })).toBe(0));
  });
});
