import { waitFor, within as domWithin } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { API_PREFIX } from '@/api/config';
import { useAuth } from '@/auth/AuthContext';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import {
  OpportunityFeedbackPanel,
  feedbackRoleState,
} from '@/pages/opportunities/FeedbackPanel';
import {
  useClearCapabilityOverride,
  useSetCapabilityOverride,
} from '@/pages/operations/useOperations';
import { useFeedbackCapability } from '@/pages/opportunities/useFeedback';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * Phase 6U-1H — P6-UI-005. The customer feedback UI must reflect the same
 * WORKSPACE-EFFECTIVE decision the backend feedback gate enforces, not the raw
 * global flag that `GET /system/capabilities` deliberately publishes.
 *
 * Wait discipline (the reason this file exists in this shape): an absence
 * assertion must be preceded by an awaited positive observable for every gate
 * above it. There are three — role, capability, history — and each renders
 * `null` independently. Before 6U-1H the capability gate had NO settled
 * observable, so "still loading" and "genuinely disabled" were the same
 * rendered state and every dark-state assertion held vacuously. `CapProbe`
 * renders the real hook's status unconditionally so a test can await
 * settlement rather than a tick count.
 *
 * WHY `settled()` IS NOT THE GUARANTEE. It tells you a PRECONDITION probe
 * settled. That implies the panel's own gate settled only if (a) the panel
 * actually mounted a capability observer and (b) the shared cache entry was
 * fresh at that moment. Four separate constructions have broken (a), each found
 * by a reviewer rather than by this file:
 *
 *   1. ERROR BRANCH — an errored query holds no data, is therefore always stale,
 *      and a later-mounting observer re-probes and reads `loading` again. F8 is
 *      the only row that crosses it, and is sound there because the panel treats
 *      `loading` and `disabled` identically.
 *   2. ROLE GATE — a non-editor panel returns null before `FeedbackInner`, so no
 *      capability observer is ever created.
 *   3. `/auth/me` TIMING — `AuthProvider` renders children with empty
 *      memberships, so on the first commit no caller is an editor yet.
 *   4. `GET /organizations` TIMING — the role needs `organizationId` from
 *      `WorkspaceContext`, whose query is `enabled: authed` and so cannot even
 *      start until `/auth/me` resolves. Three round-trips sit above the panel.
 *
 * Cases 2-4 are all "(a) fails by a different route in". That is the lesson:
 * enumerating the panel's INPUTS is unbounded, because any future provider or
 * query extends the list silently. `RolePrecondition` therefore observes the
 * panel's PREDICATE instead, which is bounded by construction — but it still
 * says nothing whatsoever about (b).
 *
 * THE ACTUAL GUARANTEE IS THE MUTATION, NOT THE WAIT. If a row's claim is about
 * the panel's gate, delete that gate and confirm the row dies. Every one of the
 * four cases above was found that way and none was found by reading. A row whose
 * claim is an ABSENCE should additionally carry a positive observable of its own
 * — a rendered control, a request count, an ordering — because the rows that
 * survived every adversarial construction to date are exactly the rows that do.
 */

const WS_A = 'ws-1';
const WS_B = 'ws-2';
const OPP = 'opp-loc-dallas-0';
const REC = 'rec-opp-loc-dallas-0';
const P = (path: string) => `*${API_PREFIX}${path}`;
const CAP = (ws: string) => P(`/workspaces/${ws}/feedback-capability`);
const FEEDBACK = (ws: string) => P(`/workspaces/${ws}/opportunities/${OPP}/feedback`);

/** Effective (resolver-backed) reflection for one workspace. */
function effective(ws: string, enabled: boolean) {
  server.use(http.get(CAP(ws), () => HttpResponse.json({ enabled })));
}

/** Stubs the liveness witness workspace: always enabled, history always empty. */
function liveWitness() {
  server.use(
    http.get(CAP(WS_LIVE), () => HttpResponse.json({ enabled: true })),
    http.get(FEEDBACK(WS_LIVE), () =>
      HttpResponse.json({ items: [], total: 0, limit: 20, offset: 0 }),
    ),
  );
}

/** Count every feedback request. Zero is the claim a pre-request gate makes. */
function spyFeedback(ws: string, status = 503) {
  const calls = { get: 0, post: 0 };
  const body =
    status === 200
      ? { items: [], total: 0, limit: 20, offset: 0 }
      : status === 403
        ? { error: { code: 'forbidden', message: 'You do not have access to this workspace.' } }
        : { error: { code: 'capability_unavailable', message: 'dark' } };
  server.use(
    http.get(FEEDBACK(ws), () => {
      calls.get += 1;
      return HttpResponse.json(body, { status });
    }),
    http.post(FEEDBACK(ws), () => {
      calls.post += 1;
      return HttpResponse.json(body, { status });
    }),
  );
  return calls;
}

function asRole(role: string) {
  server.use(
    http.get(P('/auth/me'), () =>
      HttpResponse.json({
        access_token: 'test-token',
        token_type: 'bearer',
        user: { id: 'user-1', email: 'demo@signalnest.dev', full_name: 'Demo', is_operator: true },
        memberships: [{ organization_id: 'org-1', role }],
      }),
    ),
  );
}

/**
 * Test-only probe. It imports the PRODUCTION hook — a re-implementation would
 * be a second construction, not a second witness, and could agree with the
 * panel while both were wrong. It renders the status unconditionally, including
 * while loading: rendering nothing until settled would rebuild the very
 * ambiguity this probe exists to remove, one layer out.
 */
function CapProbe({ workspaceId }: { workspaceId: string }) {
  const { status } = useFeedbackCapability(workspaceId);
  return <span data-testid={`cap-${workspaceId}`}>{status}</span>;
}

/**
 * Renders the panel's OWN role precondition, importing the production predicate.
 *
 * Deliberately NOT a probe over the panel's *inputs*. Three earlier versions of
 * this file probed inputs — first the role, then `/auth/me`, then both — and each
 * was shown incomplete, because the set of async dependencies above the panel is
 * a property of the component tree that any future provider or query silently
 * extends. Probing the predicate is bounded by construction: a fourth input is
 * covered automatically.
 *
 * It observes precondition (a) — that the panel can mount its capability
 * observer — and says NOTHING about (b), cache freshness.
 */
function RolePrecondition() {
  const { memberships } = useAuth();
  const { organizationId } = useWorkspace();
  return (
    <span data-testid="role-precondition">
      {feedbackRoleState(memberships, organizationId)}
    </span>
  );
}

/**
 * Workspace used only by the liveness witness. Always effectively enabled, and
 * never the subject of an assertion about darkness.
 */
const WS_LIVE = 'ws-live';

/**
 * Renders the subject panel and, when `witness` is set, a SECOND panel bound to
 * an always-enabled workspace.
 *
 * Why: an absence assertion is satisfied by the component not existing at all.
 * Deleting `OpportunityFeedbackPanel`'s body left every dark-state row green,
 * because "renders nothing" and "is nothing" are indistinguishable from outside.
 * The witness closes that: its Useful button is output THE COMPONENT PRODUCED,
 * so a dead component removes it and the row fails.
 *
 * It must not be a wrapper, slot, marker or probe the test renders — those are
 * scaffolding, and scaffolding witnesses the test's own render, which was never
 * in doubt. Only the subject's own output can witness the subject.
 *
 * NOT COMPOSABLE with a row whose positive assertions are document-wide: the
 * witness renders its own Useful button, so an unscoped `findByRole` will match
 * two elements and fail even unmutated. Scope such assertions with `subject()`.
 *
 * Residual, stated rather than glossed: this proves the component can render in
 * this tree. It does not prove the DARK instance mounted its own capability
 * observer for its own workspace. F2/F7/F16/F17 carry that per-instance, via
 * output from the very instance under test.
 */
function render(
  workspaceId = WS_A,
  probes: string[] = [workspaceId],
  { witness = false }: { witness?: boolean } = {},
) {
  return renderApp(
    <>
      <RolePrecondition />
      {probes.map((ws) => (
        <CapProbe key={ws} workspaceId={ws} />
      ))}
      <div data-testid="subject">
        <OpportunityFeedbackPanel
          workspaceId={workspaceId}
          opportunityId={OPP}
          intelligenceRecordId={REC}
        />
      </div>
      {witness ? (
        <div data-testid="liveness">
          <OpportunityFeedbackPanel
            workspaceId={WS_LIVE}
            opportunityId={OPP}
            intelligenceRecordId={REC}
          />
        </div>
      ) : null}
    </>,
    { route: `/opportunities/${OPP}` },
  );
}

/**
 * Bounds an absence query to the subject panel.
 *
 * This wrapper is a QUERY BOUNDARY, not evidence. It cannot show the subject
 * exists — the test renders it either way. What shows the component is alive is
 * `expectPanelAlive`, whose witness is output the component itself produced.
 * The boundary only stops the witness's own affordance from satisfying a
 * document-wide `queryByRole` in a row asserting the subject is dark.
 */
function subject(screen: ReturnType<typeof render>) {
  return domWithin(screen.getByTestId('subject'));
}

/** The witness panel must have rendered a real affordance. */
async function expectPanelAlive(screen: ReturnType<typeof render>) {
  const live = domWithin(screen.getByTestId('liveness'));
  expect(await live.findByRole('button', { name: /^useful$/i })).toBeInTheDocument();
}

/**
 * The panel is hidden: no verdict affordance AND no feedback section at all.
 *
 * Both limbs are load-bearing and neither implies the other. Asserting only the
 * Useful button admits a defect that renders the heading, the separator and
 * "No feedback recorded yet." while omitting the buttons — a customer would see
 * a visible Feedback section for a workspace where feedback is OFF, which is
 * exactly what the rollout runbook promises cannot happen.
 *
 * Measured before this helper existed: returning <FeedbackHeading /> from the
 * dark branch of FeedbackPanel killed 0 of 177 rows, and returning the WHOLE
 * section killed 1 — in a different file. All five capability-dark rows passed
 * while the customer saw a Feedback section. The predicate, not the witness,
 * was too narrow.
 *
 * Scoped with subject(): rows that mount the liveness witness have a second,
 * legitimately-rendered panel whose heading must never be read as the subject's.
 */
function expectPanelHidden(screen: ReturnType<typeof render>) {
  const s = subject(screen);
  expect(s.queryByRole('button', { name: /^useful$/i })).not.toBeInTheDocument();
  expect(s.queryByRole('heading', { name: /feedback/i })).not.toBeInTheDocument();
}

/** Await the capability gate SETTLING — never a tick count, and never `enabled` alone. */
async function settled(screen: ReturnType<typeof render>, ws = WS_A) {
  // SETTLEMENT ONLY. This says the probes resolved. It does NOT say the panel
  // mounted a capability observer — a row that depends on that must assert
  // `role-precondition` reads `editor` IN ITS OWN BODY. Keeping that in here is
  // what let five successive rows depend on a precondition none of them stated.
  // Awaits a role-bearing answer. NOTE: `no-role` is deliberately excluded —
  // it conflates "inputs not loaded" with "user has no membership here", so a
  // row about a non-member must observe the inputs directly rather than wait
  // here; this helper would spin to timeout on a state that is already settled.
  await waitFor(() =>
    expect(screen.getByTestId('role-precondition')).toHaveTextContent(/^(editor|not-editor)$/),
  );
  await waitFor(() =>
    expect(screen.getByTestId(`cap-${ws}`)).toHaveTextContent(/^(enabled|disabled)$/),
  );
}

/** Asserts the panel's gate is open — required by every row claiming the
 *  CAPABILITY gate did the hiding, so the dependency is visible at the site. */
function expectEditor(screen: ReturnType<typeof render>) {
  // Anchored: `toHaveTextContent` is a SUBSTRING match, and 'not-editor'
  // contains 'editor'. The un-anchored form passes in precisely the state this
  // assertion exists to catch.
  expect(screen.getByTestId('role-precondition')).toHaveTextContent(/^editor$/);
}

/** Drives the real operator mutation, so the coherence claim is not asserted about
 *  a hand-rolled invalidation but about the one the operations UI actually calls. */
function OperatorClearControl({ workspaceId }: { workspaceId: string }) {
  const clearOverride = useClearCapabilityOverride('org-1', workspaceId);
  return (
    <button type="button" onClick={() => clearOverride.mutate('opportunity_feedback')}>
      operator-clear
    </button>
  );
}

function OperatorControl({ workspaceId }: { workspaceId: string }) {
  const setOverride = useSetCapabilityOverride('org-1', workspaceId);
  return (
    <button
      type="button"
      onClick={() =>
        setOverride.mutate({
          organization_id: 'org-1',
          workspace_id: workspaceId,
          capability: 'opportunity_feedback',
          enabled: true,
        })
      }
    >
      operator-enable
    </button>
  );
}

describe('P6-UI-005 — workspace-effective feedback reflection', () => {
  it('F1 stays dark when the workspace is not effectively enabled', async () => {
    effective(WS_A, false);
    const calls = spyFeedback(WS_A);
    liveWitness();
    const screen = render(undefined, undefined, { witness: true });
    // Liveness: the witness panel must produce a real affordance. A deleted or
    // dead component removes it, and this row's absence claim then fails —
    // which is what distinguishes 'hides correctly' from 'is not there'.
    await expectPanelAlive(screen);

    await settled(screen);
    expectEditor(screen); // this row's claim is about the CAPABILITY gate, not the role gate
    expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('disabled');
    expectPanelHidden(screen);
    expect(calls.get).toBe(0);
  });

  it('F2 shows feedback when the workspace override enables it despite a dark global flag', async () => {
    // The central P6-UI-005 case. `/system/capabilities` keeps reporting the raw
    // global flag as false; the workspace-effective reflection says true.
    server.use(
      http.get(P('/system/capabilities'), () =>
        HttpResponse.json({
          app_mode: 'local',
          environment: 'development',
          is_local_mode: true,
          all_configured: true,
          features: {
            opportunity_feedback_enabled: false,
            scout_scheduling_enabled: false,
            connector_rss_enabled: false,
          },
        }),
      ),
    );
    effective(WS_A, true);
    const calls = spyFeedback(WS_A, 200);
    const screen = render();

    await settled(screen);
    expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('enabled');
    expect(await screen.findByRole('button', { name: /^useful$/i })).toBeInTheDocument();
    await waitFor(() => expect(calls.get).toBe(1));
  });

  it('F5 does not serve one workspace the cached answer of another', async () => {
    effective(WS_A, true);
    effective(WS_B, false);
    const bCalls = spyFeedback(WS_B);
    liveWitness();
    const screen = render(WS_B, [WS_A, WS_B], { witness: true });
    // Liveness: the witness panel must produce a real affordance. A deleted or
    // dead component removes it, and this row's absence claim then fails —
    // which is what distinguishes 'hides correctly' from 'is not there'.
    await expectPanelAlive(screen);

    await settled(screen, WS_A);
    await settled(screen, WS_B);
    expectEditor(screen); // this row's claim is about the CAPABILITY gate, not the role gate
    // Distinct cache entries: the two scopes settle to different values at once.
    expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('enabled');
    expect(screen.getByTestId(`cap-${WS_B}`)).toHaveTextContent('disabled');
    // The panel is bound to B, so it must stay dark.
    expectPanelHidden(screen);
    // The panel must consult ITS OWN workspace's entry. Absence alone cannot see
    // a panel that read a sibling workspace and was then hidden by the 503
    // fallback instead of by the gate; a zero request count can.
    expect(bCalls.get).toBe(0);
  });

  it('F6 resolves each workspace from its own response, not the first one seen', async () => {
    effective(WS_A, false);
    effective(WS_B, true);
    const screen = render(WS_B, [WS_A, WS_B]);

    await settled(screen, WS_A);
    await settled(screen, WS_B);
    expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('disabled');
    expect(screen.getByTestId(`cap-${WS_B}`)).toHaveTextContent('enabled');
  });

  it('F7 issues the feedback request strictly after the capability resolves', async () => {
    // Ordering recorded at the handlers CORROBORATES this row; it does not carry
    // it. See the load-bearing assertion below.
    // A handler-entry log is tick-free and fails if the feedback request is ever
    // issued before the capability response lands.
    liveWitness();
    const order: string[] = [];
    let release: (() => void) | undefined;
    const gate = new Promise<void>((r) => {
      release = r;
    });
    server.use(
      http.get(CAP(WS_A), async () => {
        order.push('cap-start');
        await gate;
        order.push('cap-end');
        return HttpResponse.json({ enabled: true });
      }),
      http.get(FEEDBACK(WS_A), () => {
        order.push('feedback');
        return HttpResponse.json({ items: [], total: 0, limit: 20, offset: 0 });
      }),
    );
    const screen = render(undefined, undefined, { witness: true });

    // Deliberately asserted BEFORE settling: this is the one row whose claim is
    // about the unresolved window itself.
    expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('loading');

    // THE LOAD-BEARING ASSERTION. The trailing `toEqual` below cannot carry this
    // row: `release()` fires at tick 0, before auth and the org query have let
    // any panel mount, so `cap-end` precedes `feedback` in every source state —
    // including one with no gating at all. That ordering is scheduled by this
    // test, not produced by the subject.
    //
    // Instead: at a moment when a panel demonstrably EXISTS (witness output, not
    // scaffolding) and the capability is demonstrably UNRESOLVED (the gate is
    // still held), zero feedback requests have been issued.
    await expectPanelAlive(screen);
    // Moved here from above the witness. Asserted at tick 0 it was INERT: no
    // panel instance existed yet, so it claimed the absence of a button that
    // nothing could have rendered. Proof: in that position F7 SURVIVED M16
    // (deleting the `!capability.isEnabled` render gate) even though M16 kills
    // F1/F5/F8/F9. Below the witness, the identical line kills M16.
    // Position, not wording, is what makes this assertion evidence.
    expectPanelHidden(screen);
    expect(order).not.toContain('feedback');

    release?.();
    await settled(screen);
    expect(await subject(screen).findByRole('button', { name: /^useful$/i })).toBeInTheDocument();

    await waitFor(() => expect(order).toContain('feedback'));
    expect(order).toEqual(['cap-start', 'cap-end', 'feedback']);
  });

  it('F8 fails closed when the reflection request errors', async () => {
    server.use(
      http.get(CAP(WS_A), () =>
        HttpResponse.json({ error: { code: 'server_error', message: 'boom' } }, { status: 500 }),
      ),
    );
    const calls = spyFeedback(WS_A, 200);
    liveWitness();
    const screen = render(undefined, undefined, { witness: true });
    // Liveness: the witness panel must produce a real affordance. A deleted or
    // dead component removes it, and this row's absence claim then fails —
    // which is what distinguishes 'hides correctly' from 'is not there'.
    await expectPanelAlive(screen);

    await settled(screen);
    expectEditor(screen); // this row's claim is about the CAPABILITY gate, not the role gate
    expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('disabled');
    expectPanelHidden(screen);
    expect(calls.get).toBe(0);
  });

  it('F9 fails closed on a non-boolean value', async () => {
    // `=== true`, not a truthy check: a mis-serialised "true" must not enable.
    server.use(http.get(CAP(WS_A), () => HttpResponse.json({ enabled: 'true' })));
    const calls = spyFeedback(WS_A, 200);
    liveWitness();
    const screen = render(undefined, undefined, { witness: true });
    // Liveness: the witness panel must produce a real affordance. A deleted or
    // dead component removes it, and this row's absence claim then fails —
    // which is what distinguishes 'hides correctly' from 'is not there'.
    await expectPanelAlive(screen);

    await settled(screen);
    expectEditor(screen); // this row's claim is about the CAPABILITY gate, not the role gate
    expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('disabled');
    // Request suppression survives deletion of the render gate, so the count
    // alone would still hold while the panel rendered its heading and both
    // verdict buttons for a capability that came back as the string 'true'.
    expectPanelHidden(screen);
    expect(calls.get).toBe(0);
  });

  it('F11 hides the panel from a viewer even when the workspace is effectively enabled', async () => {
    // The probe sits outside the role gate, so it settles regardless — which is
    // what lets this prove the ROLE gate did the hiding rather than the
    // capability gate or an unresolved query.
    asRole('viewer');
    effective(WS_A, true);
    const calls = spyFeedback(WS_A, 200);
    // NO liveness witness here, deliberately. F11's premise is a VIEWER, and the
    // witness panel is role-gated exactly as the subject is — a viewer sees
    // neither, so no panel-produced output exists to witness liveness for this
    // user. Its discriminator is different and stronger: the capability reads
    // `enabled` while the role precondition reads `not-editor`, so the ROLE gate
    // is demonstrably what hid the panel. Removing the role gate kills this row.
    const screen = render();

    await settled(screen);
    // The role gate did the hiding — and the probe proves the role RESOLVED first.
    expect(screen.getByTestId('role-precondition')).toHaveTextContent(/^not-editor$/);
    expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('enabled');
    expectPanelHidden(screen);
    expect(calls.get).toBe(0);
  });

  it('F18 hides the panel from an editor of a DIFFERENT organization', async () => {
    // M28: `memberships.find(m => m.organization_id === organizationId)?.role`
    // mutated to `memberships[0]?.role` killed 0 of 177. Every other fixture
    // gives the user exactly ONE membership, in the resolved organization, so
    // the two forms cannot disagree and the org-scoping of the lookup is never
    // exercised. This row is the only one where they diverge.
    //
    // The user is an OWNER of org-other (first in the list) and a VIEWER of the
    // resolved org-1. Reading index 0 yields 'owner' -> editor -> panel shown;
    // reading by organization_id yields 'viewer' -> not-editor -> panel hidden.
    server.use(
      http.get(P('/auth/me'), () =>
        HttpResponse.json({
          access_token: 'test-token',
          token_type: 'bearer',
          user: { id: 'user-1', email: 'demo@signalnest.dev', full_name: 'Demo', is_operator: true },
          memberships: [
            { organization_id: 'org-other', role: 'owner' },
            { organization_id: 'org-1', role: 'viewer' },
          ],
        }),
      ),
    );
    effective(WS_A, true);
    const calls = spyFeedback(WS_A, 200);
    // No liveness witness, for F11's reason: the witness panel is role-gated
    // exactly as the subject is, so this user sees neither.
    const screen = render();

    await settled(screen);
    // The capability is ENABLED, so only the role gate can be doing the hiding —
    // and the precondition proves it resolved to the RESOLVED org's role.
    expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('enabled');
    expect(screen.getByTestId('role-precondition')).toHaveTextContent(/^not-editor$/);
    expectPanelHidden(screen);
    expect(calls.get).toBe(0);
  });

  it('F16 keeps the server 503 as defence-in-depth for a stale client', async () => {
    // The reflection says enabled; the backend has since rolled back. The panel
    // must still hide. The evidence is the COUNT: a pre-request gate that
    // believed `true` does issue the probe — asserting only "hidden" would pass
    // even with no gate at all.
    effective(WS_A, true);
    const calls = spyFeedback(WS_A, 503);
    const screen = render();

    await settled(screen);
    expectEditor(screen); // this row's claim is about the CAPABILITY gate, not the role gate
    expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('enabled');
    await waitFor(() => expect(calls.get).toBe(1));
    // The heading, not the button, is what discriminates: the button is equally
    // absent while the history query is still loading, so asserting only its
    // absence would not prove the 503 hid anything. The heading IS rendered
    // during that loading window.
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: /feedback/i })).not.toBeInTheDocument(),
    );
    expect(subject(screen).queryByRole('button', { name: /^useful$/i })).not.toBeInTheDocument();
    expect(calls.post).toBe(0);
  });

  it('F17 hides the panel entirely when the feedback read is forbidden', async () => {
    // The 403 branch had no coverage anywhere: deleting it left the whole suite
    // green. 6U-1H makes it reachable — the reflection now says `enabled` for
    // workspaces where it previously said `disabled`, which is exactly when the
    // history query fires and can meet a server-side refusal that the client's
    // cached memberships do not know about (a mid-session role downgrade).
    //
    // Without the branch the user gets a Feedback heading, the backend's
    // forbidden message in an error state, and a "Try again" button wired to a
    // refetch that is guaranteed to fail again — a permission denial dressed up
    // as a transient fault.
    effective(WS_A, true);
    const calls = spyFeedback(WS_A, 403);
    const screen = render();

    await settled(screen);
    expectEditor(screen); // this row's claim is about the CAPABILITY gate, not the role gate
    expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('enabled');
    // The count is what makes this non-vacuous: it proves the panel is hidden
    // because of the 403, not because the capability gate hid it first.
    await waitFor(() => expect(calls.get).toBe(1));

    expect(subject(screen).queryByRole('button', { name: /^useful$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /feedback/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument();
  });

  it('F12 reflects an operator enable in the same session, without waiting out staleTime', async () => {
    // staleTime is 60s. If the operator mutation does not invalidate the
    // customer key, this cannot pass inside a test — which is the point: the
    // operator view and the customer view must not disagree about the same
    // workspace in the same session.
    let enabled = false;
    server.use(
      http.get(CAP(WS_A), () => HttpResponse.json({ enabled })),
      http.put(P('/internal/system/capabilities/overrides'), async () => {
        enabled = true;
        return HttpResponse.json({
          id: 'ovr-1',
          organization_id: 'org-1',
          workspace_id: WS_A,
          capability: 'opportunity_feedback',
          enabled: true,
          reason: null,
          set_by_user_id: 'user-1',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
        });
      }),
    );
    spyFeedback(WS_A, 200);

    const screen = renderApp(
      <>
        <CapProbe workspaceId={WS_A} />
        <OperatorControl workspaceId={WS_A} />
      </>,
      { route: '/operations' },
    );

    await waitFor(() =>
      expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent(/^(enabled|disabled)$/),
    );
    expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('disabled');

    await screen.user.click(screen.getByRole('button', { name: 'operator-enable' }));

    await waitFor(() => expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('enabled'));
  });

  it('F14 does not disturb another workspace when one workspace is mutated', async () => {
    let bFetches = 0;
    server.use(
      http.get(CAP(WS_A), () => HttpResponse.json({ enabled: false })),
      http.get(CAP(WS_B), () => {
        bFetches += 1;
        return HttpResponse.json({ enabled: false });
      }),
      http.put(P('/internal/system/capabilities/overrides'), async () =>
        HttpResponse.json({
          id: 'ovr-1',
          organization_id: 'org-1',
          workspace_id: WS_A,
          capability: 'opportunity_feedback',
          enabled: true,
          reason: null,
          set_by_user_id: 'user-1',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
        }),
      ),
    );

    const screen = renderApp(
      <>
        <CapProbe workspaceId={WS_A} />
        <CapProbe workspaceId={WS_B} />
        <OperatorControl workspaceId={WS_A} />
      </>,
      { route: '/operations' },
    );

    await waitFor(() => expect(screen.getByTestId(`cap-${WS_B}`)).toHaveTextContent('disabled'));
    const before = bFetches;

    await screen.user.click(screen.getByRole('button', { name: 'operator-enable' }));
    await waitFor(() => expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('disabled'));

    // B's entry was never invalidated, so it was never refetched.
    expect(bFetches).toBe(before);
  });

  it('F19 reflects an operator CLEAR in the same session, and only for that workspace', async () => {
    // The CLEAR path, not the set path. F12 proves the ENABLE direction only.
    // Nothing proved REVOCATION — which is the direction the rollout runbook is
    // written about. Measured before this row existed: removing the customer-key
    // invalidation from the clear path while leaving the set path intact killed
    // 0 of 178 rows. The production code was correct; the evidence was absent.
    //
    // This row cannot be satisfied by any of the alternatives it must exclude:
    //   - set-path invalidation — no set mutation is issued anywhere here;
    //   - a blanket cache reset — WS_B's fetch count must not move;
    //   - a test-owned refetch — nothing here calls refetch();
    //   - staleTime expiry — staleTime is 60s, and staleness is permission to
    //     refetch, not a trigger; a mounted observer is never refreshed by its
    //     passage, so inside a test only an invalidation can move this value.
    let enabled = true;
    let bFetches = 0;
    server.use(
      http.get(CAP(WS_A), () => HttpResponse.json({ enabled })),
      http.get(CAP(WS_B), () => {
        bFetches += 1;
        return HttpResponse.json({ enabled: false });
      }),
      http.delete(P('/internal/system/capabilities/overrides'), () => {
        enabled = false;
        return HttpResponse.json({
          capability: 'opportunity_feedback',
          workspace_id: WS_A,
          enabled: null,
          changed: true,
          created: false,
          override_id: null,
        });
      }),
    );
    spyFeedback(WS_A, 200);

    const screen = renderApp(
      <>
        <CapProbe workspaceId={WS_A} />
        <CapProbe workspaceId={WS_B} />
        <OperatorClearControl workspaceId={WS_A} />
      </>,
      { route: '/operations' },
    );

    // Precondition: A is cached ENABLED. Without this the row could pass on a
    // value that was never enabled in the first place.
    await waitFor(() => expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('enabled'));
    await waitFor(() => expect(screen.getByTestId(`cap-${WS_B}`)).toHaveTextContent('disabled'));
    const before = bFetches;

    await screen.user.click(screen.getByRole('button', { name: 'operator-clear' }));

    await waitFor(() => expect(screen.getByTestId(`cap-${WS_A}`)).toHaveTextContent('disabled'));
    // Scope: the clear targeted WS_A, so B's entry must not have been refetched.
    expect(bFetches).toBe(before);
  });
});
