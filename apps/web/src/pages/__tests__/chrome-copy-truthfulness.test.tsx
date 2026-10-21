import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { SidebarNav } from '@/components/layout/sidebar';
import { CampaignContextPage } from '@/pages/CampaignContext';
import { SignInPage } from '@/pages/auth/SignIn';
import { renderApp } from '@/test/utils';

// P6-UI-010. Customer-visible chrome must not name internal roadmap phases, and must
// not promise a feature the product does not have. Two distinct defects were fixed:
//
//   1. Promise of an unbuilt feature — "Creative generation arrives in Phase 3" on the
//      sidebar card and the opportunity-detail angles caption. No creative-generation
//      code exists in apps/api.
//   2. Unsupported present-tense claim — the brand-voice and channel-preference blurbs
//      claimed to guide reasoning tone / be referenced by recommended actions. Neither
//      BrandVoiceProfile nor ChannelPreference is read outside its own CRUD routes and
//      the seed; build_business_context reads only product/audience/competitor/business
//      profiles, and no brand-voice or channel data reaches app/llm/prompts.py.
//
// Two traps this file is written around:
//   - The source spelled the ampersand as the `&amp;` entity; the rendered DOM holds a
//     literal "Phase 1 & 2". Asserting the source spelling would pass vacuously.
//   - app-shell.tsx mounts SidebarNav twice (desktop + mobile), so the sidebar limb
//     renders the component directly. A failure here must mean stale copy came back,
//     never that a query matched two nodes.
//
// Every absence assertion is paired with a liveness control, because "the stale string
// is absent" is also true of a component that rendered nothing at all.

describe('chrome copy is roadmap-free and promises nothing unbuilt (P6-UI-010)', () => {
  it('sidebar scope card states current capability only', async () => {
    const screen = renderApp(<SidebarNav />);

    // Liveness: the nav actually rendered.
    expect(await screen.findByRole('link', { name: /opportunities/i })).toBeInTheDocument();

    const text = document.body.textContent ?? '';
    // Rendered DOM spelling, not the `&amp;` source spelling.
    expect(text).not.toContain('Phase 1 & 2');
    expect(text).not.toContain('Creative generation arrives in Phase 3');
    expect(text).not.toMatch(/Phase \d/);
    expect(text).not.toMatch(/creative generation/i);

    // The replacement copy is present, not merely the stale copy absent.
    expect(text).toContain('Current scope');
    expect(text).toContain('Scouting to explainable, scored opportunities.');
  });

  it('sign-in marketing panel footer names no roadmap phase', async () => {
    const screen = renderApp(<SignInPage />, { route: '/sign-in', authed: false });

    // Liveness: the sign-in surface actually rendered.
    expect(await screen.findByRole('button', { name: /^sign in$/i })).toBeInTheDocument();

    const text = document.body.textContent ?? '';
    expect(text).not.toContain('Phase 1 & 2');
    expect(text).not.toMatch(/Phase \d/);
    expect(text).not.toMatch(/creative generation/i);

    // Deliberately NOT "...explainable, scored opportunities": auth.test.tsx:15 uses
    // /scored opportunities/i as its sentinel that the protected Opportunities page did
    // not render for an anonymous visitor. Putting that phrase in the sign-in chrome
    // makes that guard fire on this footer instead of on a real leak.
    expect(text).toContain('Scouting workflow to explainable opportunities.');
    // The truthful headline above the footer is untouched.
    expect(text).toContain('Turn scattered market signals into explainable, ranked opportunities.');
  });

  // This limb needs a real route param, so the smallest parent that renders the page
  // with `useParams` populated is <App/> at the detail route.
  it('opportunity angles caption labels inference instead of promising creative', async () => {
    const screen = renderApp(<App />, { route: '/opportunities/opp-loc-dallas-0' });

    // Liveness: the angles list this caption belongs to actually rendered.
    expect(await screen.findByText(/suggested creative angles/i)).toBeInTheDocument();
    expect(screen.getByText('Speed-focused messaging')).toBeInTheDocument();

    const text = document.body.textContent ?? '';
    // Variant wording — the sidebar literals would not have caught this one.
    expect(text).not.toContain('Creative generation from these angles arrives in Phase 3');
    expect(text).not.toMatch(/Phase \d/);
    expect(text).not.toMatch(/creative generation/i);

    expect(text).toContain('AI-suggested starting points, not finished creative.');
  });

  it('campaign-context blurbs claim no use the backend does not make', async () => {
    const screen = renderApp(<CampaignContextPage />, { route: '/context' });

    // Liveness: the tab strip actually rendered before anything is clicked.
    const brandVoiceTab = await screen.findByRole('tab', { name: /brand voice/i });
    await screen.user.click(brandVoiceTab);

    // KindPanel renders the blurb twice — once as the panel header (CampaignContext
    // ~:421) and again as the empty-state description (~:462) — so this must be an
    // "all" query. A singular query fails on the match count, which would hide
    // whether the copy itself is right.
    expect((await screen.findAllByText(/scouting does not use it yet/i)).length).toBeGreaterThan(0);
    let text = document.body.textContent ?? '';
    expect(text).toContain('How your brand sounds.');
    expect(text).not.toContain('Guides reasoning tone');
    expect(text).not.toMatch(/Phase \d/);
    expect(text).not.toMatch(/creative generation/i);

    await screen.user.click(screen.getByRole('tab', { name: /^channels$/i }));

    expect((await screen.findAllByText(/scouting does not use them yet/i)).length).toBeGreaterThan(
      0,
    );
    text = document.body.textContent ?? '';
    expect(text).toContain('Marketing channels you use.');
    expect(text).not.toContain('Referenced by recommended actions');
    expect(text).not.toMatch(/Phase \d/);
  });
});
