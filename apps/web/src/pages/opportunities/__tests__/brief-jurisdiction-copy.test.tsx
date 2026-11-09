// Lane 5D-W4 (W21), phase Q-4: the brief and generation copy under operator rulings Q-4 and
// Q-1 (rulings record RULINGS-S1-5D-2026-11-08.md, "Rulings of the sixth instruction" and "of
// the fifth instruction"), tested against lane 5D-W3's pages, which W4 did not write.
//
// Q-4: "Allow an opportunity with no location to receive a recommendation brief with
// jurisdiction NULL. ... Item 31 remains enforced: no creative draft may be generated ... from
// that brief. ... explain the limitation clearly in the UI". The pins are on what must be said
// (no location; recommendations only; no drafts) and on where it must not be said (an
// opportunity that has a location, or is not known; creative drafts dark), not on the wording.
//
// The pages run inside the real App (OpportunityDetail at /opportunities/:id, BriefDraftsPage at
// /creative/:briefId) against MSW handlers typed by the generated contract.
import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import type {
  ApprovalHistoryOut,
  CreativeDraftListOut,
  OpportunityDetail,
  RecommendationBriefListOut,
  RecommendationBriefOut,
} from '@/api/types';
import { OpportunityBriefPanel } from '@/pages/opportunities/BriefPanel';
import { opportunityDetailFixture } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

const WS = 'ws-1';
const OPP = 'opp-loc-dallas-0'; // a fixture opportunity that has a location (loc-dallas)
const BRIEF = 'brief-1';
const P = (path: string) => `*${API_PREFIX}${path}`;
const DETAIL = P(`/workspaces/${WS}/opportunities/${OPP}`);
const BRIEFS = P(`/workspaces/${WS}/opportunities/${OPP}/briefs`);
const DRAFTS = P(`/workspaces/${WS}/briefs/${BRIEF}/drafts`);
const EDITORS = ['owner', 'admin', 'marketer'] as const;
const NON_EDITORS = ['reviewer', 'compliance_reviewer', 'viewer'] as const;

/** What the Q-4 limitation must say: no location, recommendations only, no drafts. */
const RECOMMENDATIONS_ONLY = /recommendations only/i;
const NO_LOCATION = /no location|without a location/i;
const NO_DRAFTS = /drafts? can(?:[’']t|not) be generated/i;
/** A jurisdiction code shape: briefs and drafts never display one (no contract exposes it). */
const CODE = /\b(?:[A-Z]{2}-[A-Z0-9]{1,3}|US|GB|KE|NG|CA|AU)\b/;

function brief(version: number, overrides: Partial<RecommendationBriefOut> = {}): RecommendationBriefOut {
  return {
    id: version === 1 ? BRIEF : `brief-${version}`,
    opportunity_id: OPP,
    intelligence_record_id: `rec-${OPP}`,
    location_id: null,
    campaign_id: null,
    brief_version: version,
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
    summary: `Summary of version ${version}.`,
    rationale: 'Inference: commuters are choosing on speed.',
    recommended_actions: [],
    suggested_angles: [],
    risk_notes: [],
    evidence_refs: [],
    ...overrides,
  };
}

const page = (items: RecommendationBriefOut[]): RecommendationBriefListOut => ({
  items,
  total: items.length,
  limit: 20,
  offset: 0,
});

function asRole(role: string) {
  server.use(
    http.get(P('/auth/me'), () =>
      HttpResponse.json({
        access_token: 'test-token',
        token_type: 'bearer',
        user: { id: 'user-1', email: 'demo@signalnest.dev', full_name: 'Demo', is_operator: false },
        memberships: [{ organization_id: 'org-1', role }],
      }),
    ),
  );
}

/** Briefs on; creative drafts on (generation configured) or dark; the opportunity's location. */
function world({
  creative = true,
  located,
  briefs,
}: {
  creative?: boolean;
  located: boolean;
  briefs: RecommendationBriefOut[];
}) {
  const detail: OpportunityDetail = {
    ...opportunityDetailFixture(OPP),
    ...(located ? {} : { location_id: null }),
  };
  expect(detail.location_id === null).toBe(!located);
  server.use(
    http.get(P(`/workspaces/${WS}/brief-capability`), () => HttpResponse.json({ enabled: true })),
    http.get(P(`/workspaces/${WS}/creative-capability`), () =>
      HttpResponse.json({ enabled: creative, generation_configured: creative }),
    ),
    http.get(DETAIL, () => HttpResponse.json(detail)),
    http.get(BRIEFS, () => HttpResponse.json(page(briefs))),
  );
}

async function briefPanel(screen: ReturnType<typeof renderApp>) {
  const panel = await screen.findByRole('region', { name: /recommendation brief/i });
  await within(panel).findByText(/Summary of version 1\.|No brief has been generated yet/);
  return panel;
}

/** The block that holds "Open drafts" (the link itself, as its button renders it) and the
 *  note beside it. */
function openDraftsBlock(panel: HTMLElement): HTMLElement {
  return within(panel).getByRole('link', { name: 'Open drafts' }).parentElement as HTMLElement;
}

// --------------------------------------------------------------------------------------------
// Q-4 in the brief panel: the limitation, said only where it is true
// --------------------------------------------------------------------------------------------
describe('Q-4: an opportunity with no location', () => {
  it('says beside "Open drafts" that its brief gives recommendations only and no drafts', async () => {
    asRole('viewer');
    world({ located: false, briefs: [brief(1)] });
    const screen = renderApp(<App />, { route: `/opportunities/${OPP}` });
    const panel = await briefPanel(screen);
    const note = await within(panel).findByText(RECOMMENDATIONS_ONLY);
    expect(note).toHaveTextContent(NO_LOCATION);
    expect(note).toHaveTextContent(NO_DRAFTS);
    expect(openDraftsBlock(panel)).toContainElement(note);
    // No sentence may claim a jurisdiction was recorded for a brief that has none.
    expect(panel).not.toHaveTextContent(/jurisdiction recorded when/i);
    expect(note.textContent ?? '').not.toMatch(CODE);
  });

  it('says it where the first brief is generated, before any brief exists', async () => {
    asRole('marketer');
    world({ located: false, briefs: [] });
    const screen = renderApp(<App />, { route: `/opportunities/${OPP}` });
    const panel = await briefPanel(screen);
    const note = await within(panel).findByText(RECOMMENDATIONS_ONLY);
    expect(note).toHaveTextContent(NO_LOCATION);
    expect(note).toHaveTextContent(NO_DRAFTS);
    expect(within(panel).getByRole('button', { name: /^generate brief$/i })).toBeInTheDocument();
  });

  it('says it to a member who cannot create a brief, before any brief exists', async () => {
    asRole('viewer');
    world({ located: false, briefs: [] });
    const screen = renderApp(<App />, { route: `/opportunities/${OPP}` });
    const panel = await briefPanel(screen);
    const note = await within(panel).findByText(RECOMMENDATIONS_ONLY);
    expect(note).toHaveTextContent(NO_LOCATION);
    expect(note).toHaveTextContent(NO_DRAFTS);
    expect(within(panel).queryByRole('button', { name: /generate/i })).toBeNull();
  });

  it('says it for every version shown, once', async () => {
    asRole('marketer');
    world({ located: false, briefs: [brief(2), brief(1)] });
    const screen = renderApp(<App />, { route: `/opportunities/${OPP}` });
    const panel = await screen.findByRole('region', { name: /recommendation brief/i });
    await within(panel).findByText('Summary of version 2.');
    expect(await within(panel).findAllByText(RECOMMENDATIONS_ONLY)).toHaveLength(1);
    const versions = within(panel).getByRole('navigation', { name: /brief versions/i });
    await screen.user.click(within(versions).getByRole('button', { name: 'Version 1' }));
    await within(panel).findByText('Summary of version 1.');
    const notes = within(panel).getAllByText(RECOMMENDATIONS_ONLY);
    expect(notes).toHaveLength(1);
    expect(openDraftsBlock(panel)).toContainElement(notes[0] ?? null);
    expect(panel).not.toHaveTextContent(/jurisdiction recorded when/i);
  });

  for (const briefs of [[brief(1)], []]) {
    it(`says nothing of it for an opportunity that has a location (${briefs.length ? 'a brief' : 'no brief'})`, async () => {
      asRole('owner');
      world({ located: true, briefs });
      const screen = renderApp(<App />, { route: `/opportunities/${OPP}` });
      const panel = await briefPanel(screen);
      // The page's opportunity is loaded (the panel reads it from the page's cache).
      await screen.findByRole('heading', { name: /Dallas customers want faster delivery 1/i });
      expect(within(panel).queryByText(RECOMMENDATIONS_ONLY)).toBeNull();
      expect(panel).not.toHaveTextContent(NO_LOCATION);
    });
  }

  it('says nothing of it while the opportunity is not known (unknown is not "no location")', async () => {
    asRole('owner');
    world({ located: false, briefs: [brief(1)] });
    // The panel alone: the opportunity is never loaded into the page's cache.
    const screen = renderApp(<OpportunityBriefPanel workspaceId={WS} opportunityId={OPP} />, {
      route: `/opportunities/${OPP}`,
    });
    const panel = await briefPanel(screen);
    await within(panel).findByRole('link', { name: 'Open drafts' });
    expect(within(panel).queryByText(RECOMMENDATIONS_ONLY)).toBeNull();
    expect(panel).not.toHaveTextContent(NO_LOCATION);
  });

  for (const briefs of [[brief(1)], []]) {
    it(`says nothing about drafts while creative drafts are dark (${briefs.length ? 'a brief' : 'no brief'})`, async () => {
      asRole('owner');
      world({ creative: false, located: false, briefs });
      const screen = renderApp(<App />, { route: `/opportunities/${OPP}` });
      const panel = await briefPanel(screen);
      await screen.findByRole('heading', { name: /Dallas customers want faster delivery 1/i });
      await new Promise((r) => setTimeout(r, 50));
      // Not one word of drafts ("AI-drafted" in the panel's description is not about them).
      expect(panel).not.toHaveTextContent(/\bdrafts?\b/i);
      expect(within(panel).queryByText(RECOMMENDATIONS_ONLY)).toBeNull();
    });
  }
});

// --------------------------------------------------------------------------------------------
// Q-4: the jurisdiction_unresolved message is about an existing location only
// --------------------------------------------------------------------------------------------
const SERVER_UNRESOLVED = 'SERVER-TEXT the jurisdiction could not be determined';

function captureRefused(code: string, message: string) {
  server.use(
    http.post(BRIEFS, () => HttpResponse.json({ error: { code, message } }, { status: 422 })),
  );
}

async function generateBrief(screen: ReturnType<typeof renderApp>) {
  const generate = await screen.findByRole('button', { name: /^generate (brief|a new version)$/i });
  await waitFor(() => expect(generate).toBeEnabled());
  await screen.user.click(generate);
}

describe('Q-4: a capture refused for an existing location (jurisdiction_unresolved)', () => {
  it('asks for the location to be completed or corrected, and no longer speaks of no location', async () => {
    asRole('marketer');
    world({ located: true, briefs: [] });
    captureRefused('jurisdiction_unresolved', SERVER_UNRESOLVED);
    const screen = renderApp(<App />, { route: `/opportunities/${OPP}` });
    const panel = await briefPanel(screen);
    await generateBrief(screen);
    const alert = await within(panel).findByRole('alert');
    expect(alert).toHaveTextContent(/location/i);
    expect(alert).toHaveTextContent(/complete or correct/i);
    expect(alert).not.toHaveTextContent(/no location|without a location|workspace-wide/i);
    expect(alert).not.toHaveTextContent(/try again/i);
    expect(screen.queryByText(new RegExp(SERVER_UNRESOLVED))).toBeNull();
  });
});

// --------------------------------------------------------------------------------------------
// Item 31 on the generate path (BriefDraftsPage / CreativeDraftsPanel)
// --------------------------------------------------------------------------------------------
const SERVER_MISSING = 'SERVER-TEXT no jurisdiction';

function approvedHistory() {
  server.use(
    http.get(P(`/workspaces/${WS}/opportunities/${OPP}/briefs/${BRIEF}/approvals`), () =>
      HttpResponse.json({
        items: [
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
        ],
        total: 1,
        limit: 20,
        offset: 0,
      } satisfies ApprovalHistoryOut),
    ),
  );
}

function draftsPage(
  role: string,
  refusal?: { code: string; message: string; status?: number },
  located = false,
) {
  asRole(role);
  // The brief's location follows the opportunity's: the capture copies the record's (the
  // scout's) location onto both.
  const shown = brief(1, { location_id: located ? 'loc-dallas' : null });
  world({ located, briefs: [shown] });
  approvedHistory();
  const posts: unknown[] = [];
  server.use(
    http.get(P(`/workspaces/${WS}/opportunities/${OPP}/briefs/${BRIEF}`), () => HttpResponse.json(shown)),
    http.get(DRAFTS, () =>
      HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 } satisfies CreativeDraftListOut),
    ),
    http.post(DRAFTS, async ({ request }) => {
      posts.push(await request.json());
      const r = refusal ?? { code: 'brief_jurisdiction_missing', message: SERVER_MISSING };
      return HttpResponse.json({ error: { code: r.code, message: r.message } }, { status: r.status ?? 422 });
    }),
  );
  const screen = renderApp(<App />, { route: `/creative/${BRIEF}?opportunity=${OPP}` });
  return { screen, posts };
}

describe('Item 31 on generate: brief_jurisdiction_missing', () => {
  it('is true for both kinds of brief without a jurisdiction, and never a retry', async () => {
    const { screen, posts } = draftsPage('marketer');
    await screen.findByText(/No drafts yet/);
    await screen.user.click(await screen.findByRole('button', { name: 'Generate draft' }));
    const alert = await screen.findByRole('alert');
    expect(posts).toHaveLength(1);
    // Neither "created before jurisdictions were recorded" (false for a Q-4 brief) nor a retry.
    expect(alert).toHaveTextContent(/no jurisdiction was recorded/i);
    expect(alert).not.toHaveTextContent(/before jurisdictions were recorded/i);
    expect(alert).not.toHaveTextContent(/try again/i);
    // Both remedies, each with its condition: a new version where there is a location, and
    // recommendations only where there is none.
    expect(alert).toHaveTextContent(/if the opportunity has a location.*new brief version/i);
    expect(alert).toHaveTextContent(/without a location.*recommendations only/i);
    // Static copy: never the server's text, never a code.
    expect(screen.queryByText(new RegExp(SERVER_MISSING))).toBeNull();
    expect(alert.textContent ?? '').not.toMatch(CODE);
  });
});

// --------------------------------------------------------------------------------------------
// F-5 (review W21-REV-5DW3 r3, queued to lane 5D-W4): the phase-C copy, committed. Ruling Q-1
// (2026-11-08): "A draft inherits its brief’s captured jurisdiction. Editing the location does
// not alter existing brief, draft or review snapshots. A new brief version is needed to use the
// changed location’s jurisdiction. Make this relationship clear wherever the customer chooses
// the brief used for generation." Those places are the brief panel's version choice with "Open
// drafts" and the generate controls (review r3 §2). The reviewed visibility: the remedy only to
// members who can create a brief version (copy C-a); the generate-path note only with Generate.
// --------------------------------------------------------------------------------------------
const Q1_INHERITS = /jurisdiction recorded when this (?:brief )?version was created/i;
const Q1_SNAPSHOTS = /editing the location does not change existing briefs, drafts or reviews/i;
const Q1_REMEDY = /to use a changed location, generate a new (?:brief )?version/i;

describe('F-5: Q-1 where the brief version for drafts is chosen ("Open drafts")', () => {
  for (const role of [...EDITORS, ...NON_EDITORS]) {
    const editor = (EDITORS as readonly string[]).includes(role);
    it(`${role}: drafts keep the version's recorded jurisdiction; the remedy ${editor ? 'is' : 'is not'} offered`, async () => {
      asRole(role);
      world({ located: true, briefs: [brief(2), brief(1)] });
      const screen = renderApp(<App />, { route: `/opportunities/${OPP}` });
      const panel = await screen.findByRole('region', { name: /recommendation brief/i });
      const link = await within(panel).findByRole('link', { name: 'Open drafts' });
      const block = link.parentElement as HTMLElement;
      expect(block).toHaveTextContent(Q1_INHERITS);
      expect(block).toHaveTextContent(Q1_SNAPSHOTS);
      if (editor) expect(block).toHaveTextContent(Q1_REMEDY);
      else expect(block).not.toHaveTextContent(/to use a changed location/i);
      expect(block.textContent ?? '').not.toMatch(CODE);
      // The note goes with the version chosen: "Open drafts" follows the selector.
      expect(link).toHaveAttribute('href', expect.stringContaining('/creative/brief-2'));
      const versions = within(panel).getByRole('navigation', { name: /brief versions/i });
      await screen.user.click(within(versions).getByRole('button', { name: 'Version 1' }));
      await waitFor(() =>
        expect(within(panel).getByRole('link', { name: 'Open drafts' })).toHaveAttribute(
          'href',
          expect.stringContaining(`/creative/${BRIEF}`),
        ),
      );
      expect(within(panel).getByRole('link', { name: 'Open drafts' }).parentElement).toHaveTextContent(
        Q1_INHERITS,
      );
    });
  }
});

describe('F-5: Q-1 on the generate controls', () => {
  it('is said with Generate to an editor who can generate', async () => {
    const { screen } = draftsPage('marketer', undefined, true);
    const generate = await screen.findByRole('button', { name: 'Generate draft' });
    const section = generate.closest('section') as HTMLElement;
    expect(section).toHaveTextContent(Q1_INHERITS);
    expect(section).toHaveTextContent(Q1_SNAPSHOTS);
    expect(section).toHaveTextContent(Q1_REMEDY);
    expect(section.textContent ?? '').not.toMatch(CODE);
  });

  for (const role of NON_EDITORS) {
    it(`is not said to a ${role}, who is offered no Generate`, async () => {
      const { screen } = draftsPage(role, undefined, true);
      await screen.findByText(/No drafts yet/);
      expect(screen.queryByRole('button', { name: 'Generate draft' })).toBeNull();
      expect(screen.queryByText(Q1_INHERITS)).toBeNull();
    });
  }
});

describe('F-5: the phase-C refusal messages', () => {
  it('jurisdiction_unresolved is said once, inline: no toast', async () => {
    asRole('owner');
    world({ located: true, briefs: [] });
    captureRefused('jurisdiction_unresolved', SERVER_UNRESOLVED);
    const screen = renderApp(<App />, { route: `/opportunities/${OPP}` });
    await briefPanel(screen);
    await generateBrief(screen);
    expect(await screen.findByRole('alert')).toHaveTextContent(/complete or correct/i);
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText('No brief was saved')).toBeNull();
  });

  it('every other capture failure keeps its toast with the server message, and no location message', async () => {
    asRole('owner');
    world({ located: true, briefs: [] });
    captureRefused('brief_withheld', 'The generated brief did not pass the safety screen, so nothing was stored.');
    const screen = renderApp(<App />, { route: `/opportunities/${OPP}` });
    const panel = await briefPanel(screen);
    await generateBrief(screen);
    expect(await screen.findByText('No brief was saved')).toBeInTheDocument();
    expect(screen.getByText(/did not pass the safety screen/i)).toBeInTheDocument();
    expect(within(panel).queryByText(/complete or correct/i)).toBeNull();
  });

  it('the inline message goes when the editor tries again and another refusal arrives', async () => {
    asRole('marketer');
    world({ located: true, briefs: [] });
    let posts = 0;
    server.use(
      http.post(BRIEFS, () => {
        posts += 1;
        return posts === 1
          ? HttpResponse.json({ error: { code: 'jurisdiction_unresolved', message: SERVER_UNRESOLVED } }, { status: 422 })
          : HttpResponse.json(
              { error: { code: 'brief_withheld', message: 'The generated brief did not pass the safety screen.' } },
              { status: 422 },
            );
      }),
    );
    const screen = renderApp(<App />, { route: `/opportunities/${OPP}` });
    const panel = await briefPanel(screen);
    await generateBrief(screen);
    expect(await within(panel).findByRole('alert')).toHaveTextContent(/complete or correct/i);
    await generateBrief(screen);
    expect(await screen.findByText('No brief was saved')).toBeInTheDocument();
    await waitFor(() => expect(within(panel).queryByText(/complete or correct/i)).toBeNull());
    expect(posts).toBe(2);
  });

  for (const [code, status, expected] of [
    ['brief_not_approved', 422, 'Approve this brief before generating drafts from it.'],
    ['creative_generation_unconfigured', 503, 'Draft generation is not available right now.'],
  ] as const) {
    it(`a generate refused with ${code} keeps its own message, not the jurisdiction one`, async () => {
      const { screen } = draftsPage('admin', { code, message: `SERVER-TEXT ${code}`, status }, true);
      await screen.findByText(/No drafts yet/);
      await screen.user.click(await screen.findByRole('button', { name: 'Generate draft' }));
      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent(expected);
      expect(alert).not.toHaveTextContent(/jurisdiction/i);
      expect(screen.queryByText(new RegExp(`SERVER-TEXT ${code}`))).toBeNull();
    });
  }
});
