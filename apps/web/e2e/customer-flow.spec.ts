import { expect, test, type APIResponse, type Locator, type Page } from '@playwright/test';
import flow from './flow.json' with { type: 'json' };

/**
 * The minimum customer flow the Phase 6 plan requires a browser harness to cover
 * (§14, P6-CI-2): sign-up → onboarding → create location → create scout → run →
 * see opportunity → sign out.
 *
 * Everything the customer needs is created THROUGH THE UI by a synthetic identity
 * minted for this run: no seeded account, no pre-created workspace, location, scout or
 * opportunity, no direct database write and no mocked response. The API is read as
 * that same customer (bearer session) only to ASSERT what the UI did: that the session
 * belongs to the registered account, that onboarding persisted, and that the
 * opportunity shown was produced by this run for this scout and this location rather
 * than being an unrelated pre-existing record.
 *
 * Fixture boundary (stated, not hidden): the backend runs its deterministic modes —
 * fixture connectors in place of live sources, and the mock LLM. The location is placed
 * in Dallas, TX because the fixture library carries signals for that market and the
 * pipeline derives the market from the location's city and state; a location outside
 * the four fixture markets would complete with zero signals. Nothing here says
 * anything about live ingestion, real LLM behaviour, real mail or deployment.
 *
 * Registration creates an organization and an OWNER membership but no workspace; the
 * product's own path is Settings → Workspaces → New, and the onboarding page says so.
 * The flow follows that path through the UI (step 1a).
 */

const TOKEN_KEY = 'signalnest-token';
const API = '/api/v1';
/** Bound on the queued → completed transition of the one run (the worker polls every 0.5 s in the harness). */
const RUN_TIMEOUT_MS = 90_000;
/** Bound on any single navigation the app performs after a server answer. */
const NAV_TIMEOUT_MS = 15_000;

const runId = `${Date.now().toString(36)}-${process.pid.toString(36)}`;
const customer = {
  name: 'E2E Browser Customer',
  organization: `E2E Browser Org ${runId}`,
  // A syntactically ordinary, non-reserved domain: the API's e-mail validator refuses
  // special-use names such as .test and .example.
  email: `e2e-customer-${runId}@signalnest-e2e.net`,
  // Synthetic, minted per run; never a real credential.
  password: `e2e-browser-${runId}`,
};
const workspaceName = `E2E workspace ${runId}`;
const brandName = `E2E Coffee ${runId}`;
const industry = 'Specialty coffee';
/** The market the pipeline derives from the location (city, state) — the fixture library's key. */
const market = 'Dallas, TX';
/** A comma-free tag for the onboarding wizard's markets-served field (its tag input splits on commas). */
const marketTag = 'Dallas';
const locationName = `E2E Dallas flagship ${runId}`;
const scoutName = `E2E Dallas scout ${runId}`;
const keywords = ['coffee', 'latte'];

interface Membership {
  organization_id: string;
  organization_name: string;
  role: string;
}
interface Session {
  user: { email: string; full_name: string };
  memberships: Membership[];
}
interface Named {
  id: string;
  name: string;
}
interface Workspace extends Named {
  onboarding_completed: boolean;
}
interface BusinessProfile {
  company_name: string;
  industry: string | null;
  markets_served: string[];
}
interface ScoutRequest extends Named {
  location_id: string | null;
  status: string;
}
interface Opportunity {
  id: string;
  title: string;
  scout_request_id: string;
  location_id: string | null;
  resolved_market: string | null;
  is_simulated: boolean;
}
interface Run {
  id: string;
  status: string;
  trigger: string;
  stats: { opportunities: number } | null;
}
interface Job {
  id: string;
  status: string;
  scout_request_id: string | null;
  location_id: string | null;
  result_summary: { opportunities?: number } | null;
}
interface JobEvent {
  event_type: string;
  new_status: string | null;
}

/** Read the API as the signed-in customer (or anonymously when `token` is null). */
async function get(page: Page, token: string | null, path: string): Promise<APIResponse> {
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  return page.request.get(`${API}${path}`, { headers });
}

async function getJson<T>(page: Page, token: string, path: string): Promise<T> {
  const response = await get(page, token, path);
  expect(response.status(), `GET ${path}`).toBe(200);
  return (await response.json()) as T;
}

/** The payload claims of a JWT, decoded (not verified) to compare session identity. */
function claims(jwt: string): { sid?: string; sub?: string } {
  const payload = jwt.split('.')[1] ?? '';
  return JSON.parse(Buffer.from(payload, 'base64url').toString('utf8')) as { sid?: string; sub?: string };
}

async function storedToken(page: Page): Promise<string | null> {
  return page.evaluate((key) => localStorage.getItem(key), TOKEN_KEY);
}

/** Type a value into a tag input and commit it with Enter. */
async function addTag(input: Locator, value: string): Promise<void> {
  await input.fill(value);
  await input.press('Enter');
}

// The title and the step titles come from flow.json, which the report gate also reads: a
// report that passes the gate is a report of THIS flow with every step executed.
const STEP = flow.steps as [string, string, string, string, string, string, string, string];

test(flow.title, async ({ page }) => {
  let token = '';
  let organizationId = '';
  let workspaceId = '';
  let locationId = '';
  let scoutId = '';
  let opportunities: Opportunity[] = [];

  await test.step(STEP[0], async () => {
    await page.goto('/register');
    await page.getByLabel(/^Full name/).fill(customer.name);
    await page.getByLabel(/^Organization name/).fill(customer.organization);
    await page.getByLabel(/^Work email/).fill(customer.email);
    await page.getByLabel(/^Password/).fill(customer.password);
    await page.getByRole('button', { name: 'Create account' }).click();

    // The register page itself navigates to onboarding once the session is applied.
    await expect(page).toHaveURL(/\/onboarding$/, { timeout: NAV_TIMEOUT_MS });
    const stored = await storedToken(page);
    expect(stored, 'a session token is stored after registration').toBeTruthy();
    token = stored as string;

    // The session the browser holds is the registered account's, with one OWNER
    // membership of the organization typed into the form.
    const session = await getJson<Session>(page, token, '/auth/me');
    expect(session.user.email).toBe(customer.email);
    expect(session.user.full_name).toBe(customer.name);
    expect(session.memberships).toHaveLength(1);
    const membership = session.memberships[0]!;
    expect(membership.organization_name).toBe(customer.organization);
    expect(membership.role).toBe('owner');
    organizationId = membership.organization_id;

    // The app shell renders the signed-in customer, not a placeholder.
    await expect(page.getByRole('button', { name: 'Account menu' })).toContainText(customer.name);
    // A fresh organization has no workspace yet; the product says where to create one.
    await expect(page.getByText('No workspace available')).toBeVisible();
  });

  await test.step(STEP[1], async () => {
    await page.goto('/settings');
    const workspacesCard = page.getByText('Workspaces', { exact: true }).locator('..');
    await workspacesCard.getByRole('button', { name: 'New' }).click();
    const dialog = page.getByRole('dialog', { name: 'Create workspace' });
    await dialog.getByLabel(/^Workspace name/).fill(workspaceName);
    await dialog.getByRole('button', { name: 'Create' }).click();
    await expect(page.getByText('Workspace created', { exact: true })).toBeVisible();

    const workspaces = await getJson<Workspace[]>(page, token, `/organizations/${organizationId}/workspaces`);
    expect(workspaces.map((w) => w.name)).toEqual([workspaceName]);
    workspaceId = workspaces[0]!.id;
    expect(workspaces[0]!.onboarding_completed).toBe(false);
  });

  await test.step(STEP[2], async () => {
    await page.goto('/onboarding');
    await expect(page.getByRole('heading', { name: 'Guided onboarding' })).toBeVisible();
    const next = page.getByRole('button', { name: 'Save & continue' });

    // Presence
    await page.getByRole('button', { name: /^A website/ }).click();
    await expect(page.getByRole('button', { name: /^A website/ })).toHaveAttribute('aria-pressed', 'true');
    await next.click();
    // Business
    await page.getByLabel(/^Business \/ brand name/).fill(brandName);
    await page.getByLabel(/^Industry/).fill(industry);
    await next.click();
    // Reach
    await page.getByLabel(/^Website/).fill('https://signalnest-e2e.net');
    await next.click();
    // Value & audience
    await addTag(page.getByLabel(/^Markets served/), marketTag);
    await next.click();
    // Sources (the defaults are kept; the page itself states they are fixture connectors)
    await expect(page.getByText(/Fixture connectors are used in this build/)).toBeVisible();
    await next.click();
    // Review
    await expect(page.getByRole('heading', { name: 'Review' })).toBeVisible();
    await expect(page.getByText(brandName)).toBeVisible();
    await page.getByRole('button', { name: 'Finish onboarding' }).click();
    await expect(page.getByText('Onboarding complete', { exact: true })).toBeVisible();
    await expect(page).toHaveURL(/\/$/, { timeout: NAV_TIMEOUT_MS });

    // Persisted on the server, under this workspace.
    const profile = await getJson<BusinessProfile>(page, token, `/workspaces/${workspaceId}/business-profile`);
    expect(profile.company_name).toBe(brandName);
    expect(profile.industry).toBe(industry);
    expect(profile.markets_served).toEqual([marketTag]);
    const workspace = await getJson<Workspace>(page, token, `/workspaces/${workspaceId}`);
    expect(workspace.onboarding_completed).toBe(true);

    // And visible to the customer: the overview's setup card counts exactly one of its
    // four steps done, and with no location, scout or opportunity yet that step is
    // the business profile.
    await expect(page.getByText('Finish setting up (1/4)')).toBeVisible();
  });

  await test.step(STEP[3], async () => {
    await page.goto('/locations');
    await page.getByRole('button', { name: 'Add location' }).first().click();
    const dialog = page.getByRole('dialog', { name: 'Add location' });
    await dialog.getByLabel(/^Location name/).fill(locationName);
    await dialog.getByLabel(/^City/).fill('Dallas');
    await dialog.getByLabel(/^State \/ province/).fill('TX');
    await dialog.getByLabel(/^Country/).fill('US');
    await dialog.getByRole('button', { name: 'Add location' }).click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText(locationName)).toBeVisible();

    const locations = await getJson<Named[]>(page, token, `/workspaces/${workspaceId}/locations`);
    expect(locations.map((l) => l.name)).toEqual([locationName]);
    locationId = locations[0]!.id;
  });

  await test.step(STEP[4], async () => {
    await page.goto('/scout-requests');
    await page.getByRole('button', { name: 'New scout request' }).first().click();
    const dialog = page.getByRole('dialog', { name: 'New scout request' });
    await dialog.getByLabel(/^Name/).fill(scoutName);
    await dialog.getByLabel(/^Location/).click();
    await page.getByRole('option', { name: locationName }).click();
    for (const keyword of keywords) await addTag(dialog.getByLabel(/^Keywords & topics/), keyword);
    await dialog.getByRole('button', { name: 'Create scout request' }).click();
    await expect(page.getByText('Scout request created', { exact: true })).toBeVisible();
    await expect(dialog).toBeHidden();

    const scouts = await getJson<ScoutRequest[]>(page, token, `/workspaces/${workspaceId}/scout-requests`);
    expect(scouts.map((s) => s.name)).toEqual([scoutName]);
    expect(scouts[0]!.location_id).toBe(locationId);
    scoutId = scouts[0]!.id;

    // Nothing exists yet: the workspace holds no opportunity before the run.
    const before = await getJson<Opportunity[]>(page, token, `/workspaces/${workspaceId}/opportunities?limit=100`);
    expect(before).toEqual([]);
  });

  await test.step(STEP[5], async () => {
    await page.getByRole('link', { name: scoutName }).click();
    await expect(page).toHaveURL(new RegExp(`/scout-requests/${scoutId}$`), { timeout: NAV_TIMEOUT_MS });
    const header = page.getByRole('heading', { level: 1, name: scoutName }).locator('..');
    await expect(header.getByText(locationName, { exact: true })).toBeVisible();
    // Never run yet: the page renders the em-dash form, not a zero.
    await expect(header.getByText(/^— opportunities · — signals · never run$/)).toBeVisible();

    // The page offers Run now twice (header action and empty-state card); use the header's.
    await page.getByRole('button', { name: 'Run now' }).first().click();
    await expect(page.getByText('Scout queued', { exact: true })).toBeVisible();
    // Bounded, observable wait: the page polls the request while it is queued/running
    // and the badge settles on Completed only after the worker finished the job.
    await expect(header.getByText('Completed', { exact: true })).toBeVisible({ timeout: RUN_TIMEOUT_MS });
    const statLine = await header.getByText(/opportunities ·/).innerText();
    const shownCount = Number(/^(\d+) opportunities/.exec(statLine)?.[1]);
    expect(shownCount, `opportunity count shown: ${statLine}`).toBeGreaterThanOrEqual(1);

    // One manual run, completed, with exactly the count the page shows.
    const runs = await getJson<{ items: Run[]; total: number }>(page, token, `/workspaces/${workspaceId}/scout-requests/${scoutId}/runs`);
    expect(runs.total).toBe(1);
    const run = runs.items[0]!;
    expect(run.status).toBe('succeeded');
    expect(run.trigger).toBe('manual');
    expect(run.stats?.opportunities).toBe(shownCount);

    // The run is a durable job that a worker claimed and completed for THIS scout and
    // THIS location (the API never executes the pipeline inside the request).
    const job = await getJson<Job>(page, token, `/workspaces/${workspaceId}/jobs/${run.id}`);
    expect(job.status).toBe('succeeded');
    expect(job.scout_request_id).toBe(scoutId);
    expect(job.location_id).toBe(locationId);
    expect(job.result_summary?.opportunities).toBe(shownCount);
    const events = await getJson<JobEvent[]>(page, token, `/workspaces/${workspaceId}/jobs/${run.id}/events`);
    const types = events.map((e) => e.event_type);
    expect(types).toContain('claimed');
    expect(types).toContain('succeeded');

    // Every opportunity in the workspace now belongs to this scout and this location.
    opportunities = await getJson<Opportunity[]>(page, token, `/workspaces/${workspaceId}/opportunities?limit=100`);
    expect(opportunities).toHaveLength(shownCount);
    for (const o of opportunities) {
      expect(o.scout_request_id).toBe(scoutId);
      expect(o.location_id).toBe(locationId);
      expect(o.resolved_market).toBe(market);
      expect(o.is_simulated).toBe(true);
    }
  });

  await test.step(STEP[6], async () => {
    const first = opportunities[0]!;
    // The card under the scout links to the opportunity detail.
    await page.getByRole('link', { name: first.title, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/opportunities/${first.id}$`), { timeout: NAV_TIMEOUT_MS });
    await expect(page.getByRole('heading', { level: 1, name: first.title })).toBeVisible();
    // The market line: the location-derived market, flagged inside the scout area.
    const marketLine = page.getByText('Inside scout area', { exact: true }).locator('..');
    await expect(marketLine).toContainText(market);
    await expect(page.getByText('Simulated opportunity.')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open' })).toHaveAttribute('href', `/scout-requests/${scoutId}`);

    // It is listed on the opportunities page, and the overview no longer asks for setup.
    await page.goto('/opportunities');
    await expect(page.getByRole('link', { name: first.title, exact: true })).toBeVisible();
    await page.goto('/');
    await expect(page.getByText(/^Finish setting up/)).toHaveCount(0);
  });

  await test.step(STEP[7], async () => {
    // The product re-issues the access token on session re-reads (same session, new
    // token), so the token stored now may differ from the registration token: both
    // belong to one session, and both must be refused once that session is revoked.
    const before = await storedToken(page);
    expect(before, 'a session token is still stored before sign-out').toBeTruthy();
    const current = before as string;
    expect(claims(current).sid).toBe(claims(token).sid);
    expect(claims(current).sub).toBe(claims(token).sub);
    await page.getByRole('button', { name: 'Account menu' }).click();
    await page.getByRole('menuitem', { name: 'Sign out', exact: true }).click();
    await expect(page).toHaveURL(/\/sign-in$/, { timeout: NAV_TIMEOUT_MS });

    // Locally forgotten and revoked on the server: the old token is refused.
    expect(await storedToken(page)).toBeNull();
    expect((await get(page, token, '/auth/me')).status()).toBe(401);
    expect((await get(page, current, '/auth/me')).status()).toBe(401);
    expect((await get(page, token, `/workspaces/${workspaceId}/locations`)).status()).toBe(401);
    expect((await get(page, null, `/workspaces/${workspaceId}/opportunities`)).status()).toBe(401);

    // Protected navigation bounces to sign-in and renders none of the customer's data.
    for (const path of ['/locations', `/scout-requests/${scoutId}`, '/opportunities', '/']) {
      await page.goto(path);
      await expect(page).toHaveURL(/\/sign-in$/, { timeout: NAV_TIMEOUT_MS });
      await expect(page.getByRole('button', { name: 'Account menu' })).toHaveCount(0);
      await expect(page.getByText(locationName)).toHaveCount(0);
      await expect(page.getByText(scoutName)).toHaveCount(0);
      await expect(page.getByText(customer.name)).toHaveCount(0);
      expect(await storedToken(page)).toBeNull();
    }
  });
});
