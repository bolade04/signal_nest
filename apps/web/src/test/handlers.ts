import { http, HttpResponse } from 'msw';
import { API_PREFIX } from '@/api/config';

// A small but realistic in-memory backend for component/integration tests.
// It models one org → one workspace → one brand → four independent city
// locations (Dallas, London, Lagos, Nairobi), each with its own scout request
// and opportunities. The opportunity endpoint honours the location_id and
// scout_request_id filters so tests can prove strict per-location isolation.

const P = (path: string) => `*${API_PREFIX}${path}`;

export const demoUser = {
  id: 'user-1',
  email: 'demo@signalnest.dev',
  full_name: 'Demo Marketer',
  is_operator: true,
  // The seeded account is verified, so the verification banner stays out of every
  // page test that does not ask for it (6B-4B).
  email_verified: true,
};

const org = { id: 'org-1', name: 'Demo Org', slug: 'demo-org' };
const workspace = {
  id: 'ws-1',
  organization_id: org.id,
  name: 'Demo Workspace',
  slug: 'demo-workspace',
  onboarding_completed: true,
  created_at: '2026-01-01T00:00:00Z',
};
const brand = { id: 'brand-1', name: 'Demo Brand', industry: 'Retail', business_type: 'B2C' };

interface City {
  id: string;
  name: string;
  market: string;
  country: string;
}

export const CITIES: City[] = [
  { id: 'loc-dallas', name: 'Dallas', market: 'Dallas, TX', country: 'United States' },
  { id: 'loc-london', name: 'London', market: 'London, UK', country: 'United Kingdom' },
  { id: 'loc-lagos', name: 'Lagos', market: 'Lagos, NG', country: 'Nigeria' },
  { id: 'loc-nairobi', name: 'Nairobi', market: 'Nairobi, KE', country: 'Kenya' },
];

const locations = CITIES.map((c) => ({
  id: c.id,
  name: c.name,
  address: `1 ${c.name} Ave`,
  city: c.name,
  state_province: '',
  country: c.country,
  postal_code: '',
  timezone: 'UTC',
  currency: 'USD',
  latitude: 0,
  longitude: 0,
  local_competitors: [],
  local_notes: '',
}));

const scoutRequests = CITIES.map((c, i) => ({
  id: `scout-${c.id}`,
  organization_id: org.id,
  workspace_id: workspace.id,
  brand_id: brand.id,
  location_id: c.id,
  campaign_id: null,
  name: `${c.name} demand scan`,
  status: i === 1 ? 'paused' : 'completed',
  source_types: ['manual', 'website_scan', 'rss_news'],
  keywords: [c.name.toLowerCase(), 'demand'],
  product_profile_id: null,
  resolved_market: c.market,
  notes: null,
  last_run_at: '2026-06-01T12:00:00Z',
  // The four keys apps/api/app/jobs/pipeline.py actually persists. The previous
  // fixture invented `signals_processed`, which is why the wrong-key defect passed CI.
  stats: { scanned: 24, noise_filtered: 4, signals_analyzed: 20, opportunities: 2 },
  created_at: '2026-05-01T00:00:00Z',
  updated_at: '2026-06-01T12:00:00Z',
}));

interface Opp {
  id: string;
  title: string;
  classification: string;
  decision: string;
  opportunity_score: number;
  confidence_score: number;
  confidence_level: string;
  priority_score: number;
  relevance_score: number;
  risk_level: string;
  resolved_market: string | null;
  inside_scout_area: boolean;
  why_it_matters: string | null;
  recommended_action: string | null;
  audience_fit: string | null;
  urgency: string | null;
  commercial_value: string | null;
  source_summary: string[];
  status: string;
  location_id: string | null;
  campaign_id: string | null;
  scout_request_id: string;
  is_simulated: boolean;
  created_at: string;
}

const opportunities: Opp[] = CITIES.flatMap((c, ci) =>
  [0, 1].map((n) => ({
    id: `opp-${c.id}-${n}`,
    title: `${c.name} customers want faster delivery ${n + 1}`,
    classification: n === 0 ? 'validated' : 'early',
    decision: n === 0 ? 'act_now' : 'monitor',
    opportunity_score: 80 - ci * 5 - n * 10,
    confidence_score: 70 - n * 15,
    confidence_level: n === 0 ? 'high' : 'medium',
    priority_score: 75 - ci * 4 - n * 8,
    relevance_score: 78 - n * 12,
    risk_level: n === 0 ? 'low' : 'medium',
    resolved_market: c.market,
    inside_scout_area: true,
    why_it_matters: `People in ${c.market} are actively discussing this need.`,
    recommended_action: 'Publish a locally targeted response.',
    audience_fit: 'Time-poor urban households',
    urgency: n === 0 ? 'High' : 'Medium',
    commercial_value: 'Medium',
    source_summary: ['manual', 'rss_news'],
    status: 'new',
    location_id: c.id,
    campaign_id: null,
    scout_request_id: `scout-${c.id}`,
    is_simulated: true,
    created_at: `2026-06-0${ci + 1}T09:0${n}:00Z`,
  })),
);

function detailFor(o: Opp) {
  return {
    ...o,
    who_cares: 'Local growth marketers for this market.',
    observed_evidence: [
      {
        source_type: 'rss_news',
        excerpt: `${o.resolved_market}: reports of rising demand.`,
        author: 'Local Post',
        timestamp: o.created_at,
        source_url: 'https://example.com/article',
      },
    ],
    ai_inference: 'The signal suggests an unmet, localized need worth a timely response.',
    suggested_angles: ['Speed-focused messaging', 'Local trust signals'],
    risk_note: null,
    claims_warnings: [],
    brand_id: brand.id,
    scores: [
      { kind: 'opportunity', total: o.opportunity_score, breakdown: { relevance: 25, trend: 15 } },
      { kind: 'confidence', total: o.confidence_score, breakdown: { evidence_quantity: 20, diversity: 15 } },
    ],
    validation_evidence: [
      { source_type: 'rss_news', detail: 'Corroborated by a local news mention.', weight: 2, source_url: 'https://example.com/article' },
    ],
  };
}

// One opportunity deliberately has no persisted intelligence so tests can prove
// the neutral empty state (HTTP 200 with intelligence: null).
export const noIntelOpportunityId = 'opp-loc-london-1';

// A per-opportunity intelligence payload. The excerpt/quote text is intentionally
// IDENTICAL across every market so isolation tests must rely on scoped fetches,
// not on distinct free text, to tell markets apart.
const SHARED_EXCERPT = 'Customers keep asking for faster delivery windows.';

function intelligenceFor(o: Opp) {
  return {
    // Customer-safe opaque record id (3C-C.1). Distinct per opportunity so
    // four-market feedback isolation can be proven against the real id.
    intelligence_record_id: `rec-${o.id}`,
    classification: o.classification,
    decision: o.decision,
    is_simulated: true,
    rationale: `Localized demand detected for ${o.resolved_market}.`,
    created_at: o.created_at,
    facts: {
      source_type: 'rss_news',
      market: o.resolved_market,
      language: 'en',
      published_days_ago: 3,
      char_count: 480,
      word_count: 82,
      excerpt: SHARED_EXCERPT,
      distinct_source_types: 2,
      duplicate_count: 1,
      engagement: 12,
    },
    inference: {
      signal_type: { value: 'demand_signal', confidence: 0.82, method: 'lexical_match' },
      pain_point_dna: { value: 'slow_delivery', confidence: 0.71, method: 'phrase_cluster' },
      sentiment: { value: 'frustrated', confidence: 0.64, method: 'lexicon' },
      has_buying_intent: true,
      has_competitor_dissatisfaction: false,
    },
    relevance: {
      score: o.relevance_score,
      below_action_floor: false,
      keyword_hits: ['delivery', 'fast'],
      pain_point_hits: ['slow delivery'],
      audience_hits: ['urban households'],
      competitor_hits: [],
    },
    score: {
      total: o.opportunity_score,
      classification: o.classification,
      version: '3b.1',
      factors: {
        relevance: { weight: 0.4, value: 0.8, points: 32 },
        recency: { weight: 0.2, value: 0.6, points: 12 },
      },
    },
    evidence: [
      { quote: SHARED_EXCERPT, method: 'span_match', start: 0, end: 48 },
      { quote: 'Second corroborating mention.', method: 'span_match', start: 60, end: 89 },
      { quote: 'Third corroborating mention.', method: 'span_match', start: 100, end: 128 },
      { quote: 'Fourth corroborating mention.', method: 'span_match', start: 140, end: 169 },
    ],
    provenance: {
      enricher: 'deterministic',
      analysis_version: '3b',
      scoring_version: '3b.1',
    },
    version: {
      analysis_version: '3b',
      scoring_version: '3b.1',
    },
  };
}

const emptyProfile = {
  company_name: 'Demo Brand',
  industry: 'Retail',
  business_type: 'B2C',
  website: 'https://demo.example',
  social_links: {},
  marketplace_links: [],
  markets_served: [],
  customer_pain_points: [],
  common_objections: [],
  campaign_goals: [],
  preferred_platforms: [],
  sensitive_topics: [],
  id: 'bp-1',
  brand_id: brand.id,
  workspace_id: workspace.id,
};

// ---- Operator observability + capability governance (4A-D) ----
// The registry the operator console renders its capability controls from; a static
// mirror of app/capabilities/registry.py. Not a store and not reset by anything —
// the stateful override store is `capabilityOverrideRows` further below.
const capabilityRegistryItems = [
  {
    capability: 'opportunity_feedback',
    label: 'Opportunity Feedback',
    global_flag_attr: 'opportunity_feedback_enabled',
    workspace_enableable: true,
    workspace_disableable: true,
    future_activation_phase: '4B',
  },
  {
    capability: 'scout_scheduling',
    label: 'Scout Scheduling',
    global_flag_attr: 'scout_scheduling_enabled',
    workspace_enableable: true,
    workspace_disableable: true,
    future_activation_phase: '4B',
  },
  {
    capability: 'connector_rss',
    label: 'RSS Connector',
    global_flag_attr: 'connector_rss_enabled',
    workspace_enableable: false,
    workspace_disableable: true,
    future_activation_phase: '4B',
  },
] as const;

interface OverrideRow {
  id: string;
  organization_id: string;
  workspace_id: string;
  capability: string;
  enabled: boolean;
  reason: string | null;
  set_by_user_id: string;
  created_at: string;
  updated_at: string;
}

// A stateful in-memory override store so tests can exercise the full
// set → resolve → clear loop against the same deny-biased precedence the real
// resolver applies. Operations tests call resetCapabilityOverrides() in their
// beforeEach; no other suite touches this state.
const capabilityOverrideRows = new Map<string, OverrideRow>();
const overrideKey = (ws: string, cap: string) => `${ws}:${cap}`;

export function resetCapabilityOverrides(): void {
  capabilityOverrideRows.clear();
}

// Mirrors the backend resolver's honored-override semantics: an enable is
// honored only for an enableable capability; a disable for a disableable one;
// otherwise the deny-biased secure default applies. All global flags are False
// (dark), so with no override the global configuration decides "disabled".
function resolveEffective(ws: string, item: (typeof capabilityRegistryItems)[number]) {
  const row = capabilityOverrideRows.get(overrideKey(ws, item.capability));
  if (row) {
    if (row.enabled && item.workspace_enableable) {
      return {
        capability: item.capability,
        workspace_id: ws,
        effective_enabled: true,
        decided_by: 'workspace_override',
        global_flag: false,
        has_override: true,
        override_value: true,
      };
    }
    if (!row.enabled && item.workspace_disableable) {
      return {
        capability: item.capability,
        workspace_id: ws,
        effective_enabled: false,
        decided_by: 'workspace_override',
        global_flag: false,
        has_override: true,
        override_value: false,
      };
    }
    return {
      capability: item.capability,
      workspace_id: ws,
      effective_enabled: false,
      decided_by: 'secure_default',
      global_flag: false,
      has_override: false,
      override_value: null,
    };
  }
  return {
    capability: item.capability,
    workspace_id: ws,
    effective_enabled: false,
    decided_by: 'global_configuration',
    global_flag: false,
    has_override: false,
    override_value: null,
  };
}

// The capability endpoints are tenant-scoped and deliberately non-enumerating:
// a scope that is not the demo org/workspace 404s, mirroring the backend. A
// test that renders the page successfully therefore proves the frontend sent
// the active workspace's real scope.
function scopeInvalid(url: URL): boolean {
  return (
    url.searchParams.get('organization_id') !== org.id ||
    url.searchParams.get('workspace_id') !== workspace.id
  );
}

// ---- Organizations, members, invitations and sessions (6B-3A authority) ----
// A stateful model of the backend's organization administration so a test can
// drive the real customer flow end to end: an administrator creates an
// invitation, the one-time token comes back once, the invitee previews and
// registers or accepts, and the SessionOut that comes back carries the new
// membership. Sessions are derived from the bearer token, so a role or a second
// organization is configured in the model, never by stubbing /auth/me.
//
// Every rule below mirrors a backend line (cited), because a model that is
// kinder than the server would let the UI pass for the wrong reason. The model
// is module state: vitest isolates modules per test file, and files that drive
// it call `orgModel.reset()` in their beforeEach. Files that never touch it
// see the default below, which serves exactly what the fixed handlers served
// before 6B-3B.

export type ModelRole = 'owner' | 'admin' | 'marketer' | 'reviewer' | 'viewer' | 'compliance_reviewer';
type InvitableRole = Exclude<ModelRole, 'owner'>;

// apps/api/app/auth/dependencies.py:26-33.
const ROLE_RANK: Record<ModelRole, number> = {
  viewer: 0,
  reviewer: 1,
  compliance_reviewer: 1,
  marketer: 2,
  admin: 3,
  owner: 4,
};
const ORG_ADMINS = new Set<ModelRole>(['owner', 'admin']);
const INVITABLE = new Set<string>(['admin', 'marketer', 'reviewer', 'compliance_reviewer', 'viewer']);
const INVITATION_TTL_MS = 72 * 3600 * 1000;
// apps/api/app/core/config.py:200-205 (defaults).
const PASSWORD_RESET_TTL_MS = 60 * 60 * 1000;
const EMAIL_VERIFICATION_TTL_MS = 48 * 3600 * 1000;
const AUTH_MAIL_COOLDOWN_MS = 120 * 1000;

interface ModelUser {
  id: string;
  email: string;
  full_name: string;
  password: string | null;
  is_operator: boolean;
  email_verified: boolean;
}
interface ModelOrg {
  id: string;
  name: string;
  slug: string;
}
interface ModelWorkspace {
  id: string;
  organization_id: string;
  name: string;
  slug: string;
  onboarding_completed: boolean;
  created_at: string;
}
interface ModelMembership {
  organization_id: string;
  user_id: string;
  role: ModelRole;
  created_at: string;
  /** 'invitation' only when created by the register/accept handlers below. */
  source: 'fixture' | 'invitation';
}
interface ModelInvitation {
  id: string;
  organization_id: string;
  email: string;
  role: InvitableRole;
  token: string;
  created_at: string;
  expires_at: string;
  invited_by_user_id: string;
  revoked_at: string | null;
  accepted_at: string | null;
}
/** A password-reset or email-verification token (apps/api/app/auth/account_tokens.py). */
type AccountTokenKind = 'password_reset' | 'email_verification';
interface ModelAccountToken {
  kind: AccountTokenKind;
  token: string;
  user_id: string;
  /** The address the token was minted for; a verification token is void once it changes. */
  email: string;
  issued_at: number;
  expires_at: number;
  used_at: number | null;
  revoked_at: number | null;
}
/** A message the backend would have mailed: the link is `<origin><path>#token=<token>`. */
export interface ModelMail {
  kind: AccountTokenKind;
  to: string;
  path: '/reset-password' | '/verify-email';
  token: string;
}
export interface ModelRequest {
  method: string;
  path: string;
  search: string;
  body: unknown;
  userId: string | null;
}
/** A server-side session (P6-AUTH-4 `auth_sessions`): every access token belongs to one. */
interface ModelSession {
  user_id: string;
  revoked: boolean;
}
/** How a sign-out request arrived: its bearer, and what this browser still stored then. */
export interface ModelSignOut {
  path: '/auth/logout' | '/auth/logout-all';
  authorization: string | null;
  storedToken: string | null;
}

const model = {
  users: new Map<string, ModelUser>(),
  orgs: new Map<string, ModelOrg>(),
  workspaces: new Map<string, ModelWorkspace[]>(),
  memberships: [] as ModelMembership[],
  invitations: [] as ModelInvitation[],
  accountTokens: [] as ModelAccountToken[],
  outbox: [] as ModelMail[],
  /** access token -> the id of the session it belongs to. */
  accessTokens: new Map<string, string>(),
  sessions: new Map<string, ModelSession>(),
  sessionSeq: 0,
  reissueSeq: 0,
  signOuts: [] as ModelSignOut[],
  requests: [] as ModelRequest[],
  violations: [] as string[],
  nextInvitationToken: null as string | null,
  seq: 0,
};

function nextId(prefix: string): string {
  model.seq += 1;
  return `${prefix}-${model.seq}`;
}

function modelError(status: number, code: string, message: string) {
  return HttpResponse.json({ error: { code, message, request_id: 'req-model' } }, { status });
}

function invitationState(inv: ModelInvitation): 'pending' | 'accepted' | 'revoked' | 'expired' {
  // apps/api/app/organizations/invitations.py:111-120 (revoked, then accepted, then expired).
  if (inv.revoked_at) return 'revoked';
  if (inv.accepted_at) return 'accepted';
  if (Date.parse(inv.expires_at) <= Date.now()) return 'expired';
  return 'pending';
}

const STATE_CONFLICT = {
  accepted: ['invitation_already_used', 'This invitation has already been used.'],
  revoked: ['invitation_revoked', 'This invitation has been revoked.'],
  expired: ['invitation_expired', 'This invitation has expired.'],
} as const;

function membershipOf(orgId: string, userId: string): ModelMembership | undefined {
  return model.memberships.find((m) => m.organization_id === orgId && m.user_id === userId);
}

/**
 * The live session a request's bearer belongs to, and its user (P6-AUTH-4: a token of a
 * revoked session authenticates nothing, whatever it says).
 */
function sessionOf(request: Request): { id: string; user: ModelUser } | null {
  const header = request.headers.get('authorization') ?? '';
  const token = header.startsWith('Bearer ') ? header.slice('Bearer '.length) : '';
  const id = model.accessTokens.get(token);
  const session = id ? model.sessions.get(id) : undefined;
  const user = session && !session.revoked ? model.users.get(session.user_id) : undefined;
  return id && user ? { id, user } : null;
}

function userOf(request: Request): ModelUser | null {
  return sessionOf(request)?.user ?? null;
}

/** A token inside an existing session: the /auth/me and invitation-accept re-issue. */
function mintInSession(sessionId: string, user: ModelUser): string {
  // The seeded demo session keeps the fixed token every existing test seeds.
  const token = user.id === demoUser.id ? 'test-token' : `access-${user.id}-${nextId('t')}`;
  model.accessTokens.set(token, sessionId);
  return token;
}

/**
 * The /auth/me re-issue: a NEW token string inside the presented session. Its own counter
 * keeps every other generated id (invitations, users, fresh-session tokens) numbered as
 * before; the demo session keeps its fixed token.
 */
function reissueInSession(sessionId: string, user: ModelUser): string {
  if (user.id === demoUser.id) return 'test-token';
  model.reissueSeq += 1;
  const token = `access-${user.id}-me${model.reissueSeq}`;
  model.accessTokens.set(token, sessionId);
  return token;
}

/** A fresh sign-in (login, register, invitation register): a NEW session and its token. */
function issueAccessToken(user: ModelUser): string {
  model.sessionSeq += 1;
  const sessionId = `session-${model.sessionSeq}`;
  model.sessions.set(sessionId, { user_id: user.id, revoked: false });
  return mintInSession(sessionId, user);
}

function sessionFor(user: ModelUser, accessToken: string) {
  return {
    access_token: accessToken,
    token_type: 'bearer',
    user: {
      id: user.id,
      email: user.email,
      full_name: user.full_name,
      is_operator: user.is_operator,
      email_verified: user.email_verified,
    },
    memberships: model.memberships
      .filter((m) => m.user_id === user.id)
      .map((m) => ({
        organization_id: m.organization_id,
        organization_name: model.orgs.get(m.organization_id)?.name ?? '',
        role: m.role,
      })),
  };
}

function liveTokens(): string[] {
  return [...model.invitations.map((i) => i.token), ...model.accountTokens.map((t) => t.token)];
}

async function readJson(request: Request): Promise<Record<string, unknown>> {
  try {
    const body = (await request.clone().json()) as unknown;
    return body && typeof body === 'object' && !Array.isArray(body) ? (body as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

/** Log a model request and flag any invitation or account token that reached the URL. */
async function record(request: Request): Promise<{ user: ModelUser | null; body: Record<string, unknown> }> {
  const url = new URL(request.url);
  const body = request.method === 'GET' || request.method === 'DELETE' ? {} : await readJson(request);
  const user = userOf(request);
  model.requests.push({ method: request.method, path: url.pathname, search: url.search, body, userId: user?.id ?? null });
  for (const token of liveTokens()) {
    if (request.url.includes(token) || request.url.includes(encodeURIComponent(token))) {
      model.violations.push(`token in request URL: ${request.method} ${url.pathname}${url.search}`);
    }
  }
  return { user, body };
}

/** Pydantic `extra="forbid"`: the exact key set, else a 422 and a recorded violation. */
function exactKeys(route: string, body: Record<string, unknown>, keys: string[]) {
  const got = Object.keys(body).sort();
  const want = [...keys].sort();
  if (got.length === want.length && got.every((k, i) => k === want[i])) return null;
  model.violations.push(`${route} body keys ${JSON.stringify(got)} != ${JSON.stringify(want)}`);
  return modelError(422, 'validation_error', 'Request validation failed');
}

function tokenOnlyInBody(route: string, request: Request) {
  const url = new URL(request.url);
  if (url.search) model.violations.push(`${route} carried a query string: ${url.search}`);
}

/** apps/api/app/organizations/invitations.py:274-290 — unknown → 404, not pending → its 409. */
function resolveToken(token: unknown): ModelInvitation | Response {
  const inv = typeof token === 'string' ? model.invitations.find((i) => i.token === token) : undefined;
  if (!inv) return modelError(404, 'invitation_invalid', 'This invitation link is invalid.');
  const state = invitationState(inv);
  if (state !== 'pending') {
    const [code, message] = STATE_CONFLICT[state];
    return modelError(409, code, message);
  }
  return inv;
}

/** invitations.py:293-319 — the inviter must still be able to issue this invitation. */
function inviterStillAuthorized(inv: ModelInvitation): boolean {
  const m = membershipOf(inv.organization_id, inv.invited_by_user_id);
  return Boolean(m && ORG_ADMINS.has(m.role) && ROLE_RANK[inv.role] <= ROLE_RANK[m.role]);
}

// ---- Password reset and email verification (6B-4A, account_tokens.py) ----

function accountTokenOpen(t: ModelAccountToken): boolean {
  return t.used_at === null && t.revoked_at === null && t.expires_at > Date.now();
}

/** Revoke the account's other open tokens of a kind (issue, reset and verify all do). */
function revokeOpenAccountTokens(kind: AccountTokenKind, userId: string, except?: ModelAccountToken) {
  const now = Date.now();
  for (const t of model.accountTokens) {
    if (t.kind === kind && t.user_id === userId && t !== except && accountTokenOpen(t)) t.revoked_at = now;
  }
}

function mintAccountToken(kind: AccountTokenKind, user: ModelUser, token?: string): ModelAccountToken {
  revokeOpenAccountTokens(kind, user.id);
  const now = Date.now();
  const minted: ModelAccountToken = {
    kind,
    token:
      token ??
      (kind === 'password_reset'
        ? `Rs${model.seq + 1}_mail-reset${model.seq + 1}Q`
        : `Vf${model.seq + 1}_mail-verify${model.seq + 1}Q`),
    user_id: user.id,
    email: user.email,
    issued_at: now,
    expires_at: now + (kind === 'password_reset' ? PASSWORD_RESET_TTL_MS : EMAIL_VERIFICATION_TTL_MS),
    used_at: null,
    revoked_at: null,
  };
  model.seq += 1;
  model.accountTokens.push(minted);
  return minted;
}

/**
 * A request-driven issue, as `_issue` does it: within the cooldown nothing is minted
 * or mailed, yet the route still answers 204 — a 204 never proves a message went out.
 */
function mailAccountToken(kind: AccountTokenKind, user: ModelUser): void {
  const now = Date.now();
  const cooling = model.accountTokens.some(
    (t) => t.kind === kind && t.user_id === user.id && now - t.issued_at < AUTH_MAIL_COOLDOWN_MS,
  );
  if (cooling) return;
  const minted = mintAccountToken(kind, user);
  model.outbox.push({
    kind,
    to: user.email,
    path: kind === 'password_reset' ? '/reset-password' : '/verify-email',
    token: minted.token,
  });
}

function findAccountToken(kind: AccountTokenKind, token: unknown): ModelAccountToken | undefined {
  return typeof token === 'string' ? model.accountTokens.find((t) => t.kind === kind && t.token === token) : undefined;
}

/** A pydantic `str` field with `min_length` / `max_length`. */
function boundedString(value: unknown, min: number, max: number): value is string {
  return typeof value === 'string' && value.length >= min && value.length <= max;
}

/** OrganizationContext: a non-member is refused before any role check (403). */
function orgActor(orgId: string, user: ModelUser | null): ModelMembership | Response {
  if (!user) return modelError(401, 'unauthorized', 'Not authenticated.');
  if (!model.orgs.has(orgId)) return modelError(404, 'not_found', 'Organization not found.');
  const m = membershipOf(orgId, user.id);
  if (!m) return modelError(403, 'permission_denied', 'You are not a member of this organization.');
  return m;
}

function requireOrgAdmin(actor: ModelMembership): Response | null {
  return ORG_ADMINS.has(actor.role)
    ? null
    : modelError(403, 'permission_denied', `Role '${actor.role}' is not permitted for this action.`);
}

function ownerCount(orgId: string): number {
  return model.memberships.filter((m) => m.organization_id === orgId && m.role === 'owner').length;
}

function memberRow(m: ModelMembership) {
  const u = model.users.get(m.user_id)!;
  return { user_id: u.id, email: u.email, full_name: u.full_name, role: m.role, created_at: m.created_at };
}

function invitationOut(inv: ModelInvitation) {
  return {
    id: inv.id,
    email: inv.email,
    role: inv.role,
    expires_at: inv.expires_at,
    created_at: inv.created_at,
    invited_by_user_id: inv.invited_by_user_id,
  };
}

/** The organization that owns a workspace the model knows, else null. */
function workspaceOrganization(workspaceId: string): string | null {
  for (const [orgId, list] of model.workspaces) {
    if (list.some((w) => w.id === workspaceId)) return orgId;
  }
  return null;
}

/**
 * get_tenant_context (apps/api/app/auth/dependencies.py:147-163): a workspace-scoped
 * route answers only members of the workspace's organization, else 403. The fixed
 * fixture handlers below know nothing of sessions, so this gate runs first and then
 * falls through to them. A workspace the model does not know keeps their behaviour.
 */
async function tenantGate({ request, params }: { request: Request; params: Record<string, unknown> }) {
  const { user } = await record(request);
  const orgId = workspaceOrganization(String(params.ws));
  if (!orgId || !user) return undefined;
  if (!membershipOf(orgId, user.id)) {
    return modelError(403, 'permission_denied', 'You are not a member of this organization.');
  }
  return undefined;
}

/** Test-facing control surface for the model. */
export const orgModel = {
  reset(): void {
    model.users.clear();
    model.orgs.clear();
    model.workspaces.clear();
    model.memberships = [];
    model.invitations = [];
    model.accountTokens = [];
    model.outbox = [];
    model.accessTokens.clear();
    model.sessions.clear();
    model.sessionSeq = 0;
    model.reissueSeq = 0;
    model.signOuts = [];
    model.requests = [];
    model.violations = [];
    model.nextInvitationToken = null;
    model.seq = 0;
    model.users.set(demoUser.id, { ...demoUser, password: null });
    // The demo account's seeded session, behind the fixed token every test seeds.
    model.sessions.set('session-demo', { user_id: demoUser.id, revoked: false });
    model.accessTokens.set('test-token', 'session-demo');
    model.orgs.set(org.id, org);
    model.workspaces.set(org.id, [workspace]);
    model.memberships.push({
      organization_id: org.id,
      user_id: demoUser.id,
      role: 'owner',
      created_at: '2026-01-01T00:00:00Z',
      source: 'fixture',
    });
  },
  /**
   * An account that exists before the test. Verified unless `email_verified: false`:
   * like the seeded demo account, so the verification banner appears only where a
   * test asks for an unverified account.
   */
  addUser(user: {
    id: string;
    email: string;
    full_name: string;
    password?: string;
    is_operator?: boolean;
    email_verified?: boolean;
  }): void {
    model.users.set(user.id, {
      id: user.id,
      email: user.email,
      full_name: user.full_name,
      password: user.password ?? 'correct horse battery',
      is_operator: user.is_operator ?? false,
      email_verified: user.email_verified ?? true,
    });
  },
  addOrganization(o: ModelOrg, workspaces: Omit<ModelWorkspace, 'organization_id'>[] = []): void {
    model.orgs.set(o.id, o);
    model.workspaces.set(
      o.id,
      workspaces.map((w) => ({ ...w, organization_id: o.id })),
    );
  },
  /**
   * A PRE-INVITATION fixture membership (the actor's role, a roster). Refuses to
   * create or change a membership for any email that holds an invitation to that
   * organization: an invitee's membership may only come from the register/accept
   * handlers (§46: no test inserts the second membership directly).
   */
  setMembership(orgId: string, userId: string, role: ModelRole, createdAt = '2026-01-02T00:00:00Z'): void {
    const user = model.users.get(userId);
    if (!user) throw new Error(`orgModel.setMembership: unknown user ${userId}`);
    if (model.invitations.some((i) => i.organization_id === orgId && i.email === user.email)) {
      throw new Error(`orgModel.setMembership: ${user.email} was invited to ${orgId}; its membership must come from the flow`);
    }
    const existing = membershipOf(orgId, userId);
    if (existing) existing.role = role;
    else model.memberships.push({ organization_id: orgId, user_id: userId, role, created_at: createdAt, source: 'fixture' });
  },
  /** A concurrent change made by someone else (the §79 stale race), bypassing the UI. */
  changeRoleBehindTheUi(orgId: string, userId: string, role: ModelRole): void {
    const existing = membershipOf(orgId, userId);
    if (!existing) throw new Error(`orgModel.changeRoleBehindTheUi: ${userId} is not in ${orgId}`);
    existing.role = role;
  },
  /** Someone else created a pending invitation meanwhile (the screen has not seen it). */
  invitationCreatedBehindTheUi(orgId: string, email: string, role: InvitableRole, invitedBy: string): void {
    const now = Date.now();
    model.invitations.push({
      id: nextId('inv'),
      organization_id: orgId,
      email,
      role,
      token: `Bx${model.seq}_behind-${model.seq}`,
      created_at: new Date(now).toISOString(),
      expires_at: new Date(now + INVITATION_TTL_MS).toISOString(),
      invited_by_user_id: invitedBy,
      revoked_at: null,
      accepted_at: null,
    });
  },
  /** Someone else revoked an invitation meanwhile. */
  invitationRevokedBehindTheUi(email: string): void {
    const inv = model.invitations.find((i) => i.email === email && invitationState(i) === 'pending');
    if (!inv) throw new Error(`orgModel.invitationRevokedBehindTheUi: no pending invitation for ${email}`);
    inv.revoked_at = new Date().toISOString();
  },
  /**
   * The session behind an access token ends (expired or revoked): that token and every
   * other token of the same session now answer 401.
   */
  expireSession(accessToken: string): void {
    const id = model.accessTokens.get(accessToken);
    const session = id ? model.sessions.get(id) : undefined;
    if (session) session.revoked = true;
    model.accessTokens.delete(accessToken);
  },
  /** Whether the server still accepts `accessToken` (its session live, not revoked). */
  accepts(accessToken: string): boolean {
    const id = model.accessTokens.get(accessToken);
    const session = id ? model.sessions.get(id) : undefined;
    return Boolean(session && !session.revoked);
  },
  /** Every sign-out request, oldest first, as it arrived. */
  signOuts: (): readonly ModelSignOut[] => model.signOuts,
  /** Six-role roster: the demo actor plus one other member per role. */
  seedRoster(orgId: string): void {
    const roster: [string, string, string, ModelRole][] = [
      ['user-owner-2', 'olivia.owner@example.com', 'Olivia Owner', 'owner'],
      ['user-admin', 'adam.admin@example.com', 'Adam Admin', 'admin'],
      ['user-marketer', 'mara.marketer@example.com', 'Mara Marketer', 'marketer'],
      ['user-reviewer', 'rhea.reviewer@example.com', 'Rhea Reviewer', 'reviewer'],
      ['user-compliance', 'cora.compliance@example.com', 'Cora Compliance', 'compliance_reviewer'],
      ['user-viewer', 'vic.viewer@example.com', 'Vic Viewer', 'viewer'],
    ];
    roster.forEach(([id, email, name, role], i) => {
      if (!model.users.has(id)) orgModel.addUser({ id, email, full_name: name });
      orgModel.setMembership(orgId, id, role, `2026-02-0${i + 1}T00:00:00Z`);
    });
  },
  /** An access token for a model user, as if they had signed in earlier. */
  signIn(userId: string): string {
    const user = model.users.get(userId);
    if (!user) throw new Error(`orgModel.signIn: unknown user ${userId}`);
    return issueAccessToken(user);
  },
  /**
   * A password-reset token for a model user, as if the reset mail had just been sent.
   * The page under test only ever sees it through the `#token=` fragment the test opens.
   */
  issuePasswordResetToken(userId: string, token?: string): string {
    const user = model.users.get(userId);
    if (!user) throw new Error(`orgModel.issuePasswordResetToken: unknown user ${userId}`);
    return mintAccountToken('password_reset', user, token).token;
  },
  /** An email-verification token for a model user's current address (see above). */
  issueEmailVerificationToken(userId: string, token?: string): string {
    const user = model.users.get(userId);
    if (!user) throw new Error(`orgModel.issueEmailVerificationToken: unknown user ${userId}`);
    return mintAccountToken('email_verification', user, token).token;
  },
  /** The address was verified elsewhere meanwhile (another tab or device); the screen has not seen it. */
  emailVerifiedBehindTheUi(userId: string): void {
    const user = model.users.get(userId);
    if (!user) throw new Error(`orgModel.emailVerifiedBehindTheUi: unknown user ${userId}`);
    user.email_verified = true;
  },
  expireAccountToken(token: string): void {
    const t = model.accountTokens.find((row) => row.token === token);
    if (!t) throw new Error('orgModel.expireAccountToken: unknown token');
    t.expires_at = Date.now() - 60_000;
  },
  /** The raw token the NEXT created invitation will carry (else a generated one). */
  setNextInvitationToken(token: string): void {
    model.nextInvitationToken = token;
  },
  expireInvitation(invitationId: string): void {
    const inv = model.invitations.find((i) => i.id === invitationId);
    if (!inv) throw new Error(`orgModel.expireInvitation: unknown invitation ${invitationId}`);
    inv.expires_at = new Date(Date.now() - 60_000).toISOString();
  },
  /** Another workspace in an existing organization. */
  addWorkspace(orgId: string, w: Omit<ModelWorkspace, 'organization_id'>): void {
    model.workspaces.set(orgId, [...(model.workspaces.get(orgId) ?? []), { ...w, organization_id: orgId }]);
  },
  workspacesOf: (orgId: string): readonly ModelWorkspace[] => model.workspaces.get(orgId) ?? [],
  memberships: (): readonly ModelMembership[] => model.memberships,
  invitations: (): readonly ModelInvitation[] => model.invitations,
  users: (): readonly ModelUser[] => [...model.users.values()],
  user: (userId: string): ModelUser | undefined => model.users.get(userId),
  /** Every reset / verification message the backend would have mailed, oldest first. */
  outbox: (): readonly ModelMail[] => model.outbox,
  requests: (): readonly ModelRequest[] => model.requests,
  violations: (): readonly string[] => model.violations,
  count(method: string, pathPattern: RegExp): number {
    return model.requests.filter((r) => r.method === method && pathPattern.test(r.path)).length;
  },
};

orgModel.reset();

export const handlers = [
  // ---- Tenant gate for every workspace-scoped route (runs first, then falls through) ----
  http.all(P('/workspaces/:ws'), tenantGate),
  http.all(P('/workspaces/:ws/*'), tenantGate),

  // ---- Operator observability + capability governance (4A-D; operator-only) ----
  http.get(P('/internal/system/overview'), () =>
    HttpResponse.json({
      as_of: '2026-06-01T12:00:00Z',
      stale_after_seconds: 120,
      jobs: {
        total: 6,
        stuck_count: 1,
        dead_letter_count: 2,
        status_counts: { queued: 1, running: 2, succeeded: 1, dead_letter: 2 },
      },
      workers: { active_count: 3, stale_count: 1, status_counts: { active: 3, stale: 1 } },
      schedules: { total: 4, state_counts: { active: 3, paused: 1 } },
    }),
  ),
  http.get(P('/internal/system/telemetry'), () =>
    HttpResponse.json({
      logging_format: 'json',
      metrics_enabled: false,
      exporter_status: 'noop',
      telemetry_failures: 0,
      trace_export_failures: 0,
      tracing_enabled: false,
      tracing_exporter: 'none',
      tracing_sample_ratio: 0,
      tracing_status: 'disabled',
      redaction_enabled: true,
      correlation_enabled: true,
    }),
  ),
  http.get(P('/internal/system/capabilities/registry'), () =>
    HttpResponse.json({ items: capabilityRegistryItems }),
  ),
  http.get(P('/internal/system/capabilities/effective'), ({ request }) => {
    const url = new URL(request.url);
    if (scopeInvalid(url)) return HttpResponse.json({ detail: 'not_found' }, { status: 404 });
    return HttpResponse.json({
      items: capabilityRegistryItems.map((item) => resolveEffective(workspace.id, item)),
    });
  }),
  http.get(P('/internal/system/capabilities/overrides'), ({ request }) => {
    const url = new URL(request.url);
    if (scopeInvalid(url)) return HttpResponse.json({ detail: 'not_found' }, { status: 404 });
    const items = [...capabilityOverrideRows.values()];
    return HttpResponse.json({ items, total: items.length, limit: 50, offset: 0 });
  }),
  http.put(P('/internal/system/capabilities/overrides'), async ({ request }) => {
    const body = (await request.json()) as {
      organization_id: string;
      workspace_id: string;
      capability: string;
      enabled: boolean;
      reason?: string | null;
    };
    if (body.organization_id !== org.id || body.workspace_id !== workspace.id) {
      return HttpResponse.json({ detail: 'not_found' }, { status: 404 });
    }
    const item = capabilityRegistryItems.find((c) => c.capability === body.capability);
    if (!item) return HttpResponse.json({ detail: 'unknown_capability' }, { status: 422 });
    if (body.enabled && !item.workspace_enableable) {
      return HttpResponse.json(
        { detail: 'capability_override_not_permitted' },
        { status: 422 },
      );
    }
    const key = overrideKey(body.workspace_id, body.capability);
    const existing = capabilityOverrideRows.get(key);
    const row: OverrideRow = {
      id: existing?.id ?? `ovr-${key}`,
      organization_id: body.organization_id,
      workspace_id: body.workspace_id,
      capability: body.capability,
      enabled: body.enabled,
      reason: body.reason ?? null,
      set_by_user_id: demoUser.id,
      created_at: existing?.created_at ?? '2026-06-01T12:00:00Z',
      updated_at: '2026-06-01T12:00:00Z',
    };
    capabilityOverrideRows.set(key, row);
    return HttpResponse.json({
      capability: body.capability,
      workspace_id: body.workspace_id,
      enabled: body.enabled,
      changed: existing?.enabled !== body.enabled,
      created: !existing,
      override_id: row.id,
    });
  }),
  http.delete(P('/internal/system/capabilities/overrides'), ({ request }) => {
    const url = new URL(request.url);
    if (scopeInvalid(url)) return HttpResponse.json({ detail: 'not_found' }, { status: 404 });
    const capability = url.searchParams.get('capability') ?? '';
    const key = overrideKey(workspace.id, capability);
    const existing = capabilityOverrideRows.get(key);
    capabilityOverrideRows.delete(key);
    // Mirrors the real clear service: the mutation result never carries the
    // cleared row's id (override_id is always null on DELETE).
    return HttpResponse.json({
      capability,
      workspace_id: workspace.id,
      enabled: null,
      changed: Boolean(existing),
      created: false,
      override_id: null,
    });
  }),

  // ---- System (runtime introspection; secret-free) ----
  // Workspace-effective feedback availability (P6-UI-005). Dark by default, in
  // step with the shipped global default; tests that need it enabled install a
  // per-test `server.use(...)` override for the specific workspace.
  //
  // The trade: a default handler means a test that never reaches the capability
  // layer looks the same as one that does, because `onUnhandledRequest: 'error'`
  // can no longer flag the missing request. Fail-closed defaults are worth more
  // here, but a dark-state assertion must prove it settled rather than rely on
  // an unstubbed request throwing.
  http.get(P('/workspaces/:workspaceId/feedback-capability'), () =>
    HttpResponse.json({ enabled: false }),
  ),

  // Coarse summary for any authenticated caller (no per-capability topology).
  // Raw global flags only — NOT the workspace-effective answer above.
  http.get(P('/system/capabilities'), () =>
    HttpResponse.json({
      app_mode: 'local',
      environment: 'development',
      is_local_mode: true,
      all_configured: true,
      // Every registered capability ships dark by default, mirroring the server
      // flags (`config.py`: all three default False). Tests that need a
      // capability ON install a per-test `server.use(...)` override rather than
      // changing this shared default — so the dark path is what renders unless a
      // test explicitly opts out of it.
      features: {
        opportunity_feedback_enabled: false,
        scout_scheduling_enabled: false,
        connector_rss_enabled: false,
      },
    }),
  ),
  // Detailed backend topology — operator-only in the real API.
  http.get(P('/internal/system/capabilities'), () =>
    HttpResponse.json({
      app_mode: 'local',
      environment: 'development',
      llm_provider: 'mock',
      is_local_mode: true,
      all_configured: true,
      capabilities: [
        { name: 'database', backend: 'sqlite', configured: true, is_local: true, requires_external: false, detail: null },
        { name: 'queue', backend: 'inprocess', configured: true, is_local: true, requires_external: false, detail: null },
        { name: 'cache', backend: 'memory', configured: true, is_local: true, requires_external: false, detail: null },
        { name: 'vector', backend: 'bruteforce', configured: true, is_local: true, requires_external: false, detail: null },
        { name: 'storage', backend: 'local', configured: true, is_local: true, requires_external: false, detail: null },
        { name: 'llm', backend: 'mock', configured: true, is_local: true, requires_external: false, detail: null },
      ],
    }),
  ),

  // ---- Auth (sessions derived from the bearer token; see orgModel) ----
  // P6-AUTH-4: login, register and invitation register each open a NEW session; /auth/me
  // and invitation accept re-issue inside the presented one; logout and logout-all revoke.
  http.get(P('/auth/me'), async ({ request }) => {
    const { user } = await record(request);
    const session = sessionOf(request);
    if (!user || !session) return modelError(401, 'unauthorized', 'Not authenticated.');
    // Re-issue inside the presented session: never a new session, never past its end.
    return HttpResponse.json(sessionFor(user, reissueInSession(session.id, user)));
  }),
  http.post(P('/auth/login'), async ({ request }) => {
    const { body } = await record(request);
    const known = [...model.users.values()].find((u) => u.email === body.email && u.id !== demoUser.id);
    // Any other address signs in as the seeded demo account, exactly as the fixed
    // handler always did (auth.test.tsx relies on it).
    const demo = model.users.get(demoUser.id)!;
    if (!known) return HttpResponse.json(sessionFor(demo, issueAccessToken(demo)));
    if (known.password !== body.password) return modelError(401, 'unauthorized', 'Invalid credentials');
    return HttpResponse.json(sessionFor(known, issueAccessToken(known)));
  }),
  http.post(P('/auth/register'), async ({ request }) => {
    await record(request);
    const demo = model.users.get(demoUser.id)!;
    return HttpResponse.json(sessionFor(demo, issueAccessToken(demo)), { status: 201 });
  }),
  http.post(P('/auth/logout'), async ({ request }) => {
    await record(request);
    model.signOuts.push({
      path: '/auth/logout',
      authorization: request.headers.get('authorization'),
      storedToken: localStorage.getItem('signalnest-token'),
    });
    const session = sessionOf(request);
    if (!session) return modelError(401, 'unauthorized', 'Invalid or expired token.');
    model.sessions.get(session.id)!.revoked = true;
    return new HttpResponse(null, { status: 204 });
  }),
  http.post(P('/auth/logout-all'), async ({ request }) => {
    await record(request);
    model.signOuts.push({
      path: '/auth/logout-all',
      authorization: request.headers.get('authorization'),
      storedToken: localStorage.getItem('signalnest-token'),
    });
    const session = sessionOf(request);
    if (!session) return modelError(401, 'unauthorized', 'Invalid or expired token.');
    for (const other of model.sessions.values()) {
      if (other.user_id === session.user.id) other.revoked = true;
    }
    return new HttpResponse(null, { status: 204 });
  }),

  // ---- Password reset and email verification (auth/routes.py:198-287) ----
  // Every success is an empty 204. A token rides only in the JSON body.
  http.post(P('/auth/password-reset/request'), async ({ request }) => {
    const { body } = await record(request);
    const bad = exactKeys('password-reset request', body, ['email']);
    if (bad) return bad;
    if (typeof body.email !== 'string' || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(body.email)) {
      return modelError(422, 'validation_error', 'Request validation failed');
    }
    // The same answer for every address, known or not (anti-enumeration).
    const user = [...model.users.values()].find((u) => u.email === body.email);
    if (user) mailAccountToken('password_reset', user);
    return new HttpResponse(null, { status: 204 });
  }),
  http.post(P('/auth/password-reset/confirm'), async ({ request }) => {
    const { body } = await record(request);
    tokenOnlyInBody('password-reset confirm', request);
    const bad = exactKeys('password-reset confirm', body, ['token', 'new_password']);
    if (bad) return bad;
    if (!boundedString(body.token, 1, 128) || !boundedString(body.new_password, 8, 128)) {
      return modelError(422, 'validation_error', 'Request validation failed');
    }
    // account_tokens.py:341-381: every token-state failure is the same 404.
    const row = findAccountToken('password_reset', body.token);
    const user = row ? model.users.get(row.user_id) : undefined;
    if (!row || !accountTokenOpen(row) || !user) {
      return modelError(404, 'password_reset_invalid', 'This password reset link is invalid or has expired.');
    }
    row.used_at = Date.now();
    user.password = body.new_password;
    // FD-7: a completed reset verifies the address.
    user.email_verified = true;
    // auth_epoch + 1: every access token issued to the account before now stops working.
    // Its sessions are not revoked (P6-AUTH-4): the epoch alone refuses those tokens.
    for (const [accessToken, sessionId] of [...model.accessTokens]) {
      if (model.sessions.get(sessionId)?.user_id === user.id) model.accessTokens.delete(accessToken);
    }
    revokeOpenAccountTokens('password_reset', user.id, row);
    return new HttpResponse(null, { status: 204 });
  }),
  http.post(P('/auth/email-verification/request'), async ({ request }) => {
    const { user } = await record(request);
    if (!user) return modelError(401, 'unauthorized', 'Not authenticated.');
    // EmailVerificationRequest is the empty object and nothing else: no body at all
    // is a 422 as well (readJson would have read it as {}).
    let raw: unknown;
    try {
      raw = JSON.parse(await request.clone().text());
    } catch {
      raw = undefined;
    }
    if (!raw || typeof raw !== 'object' || Array.isArray(raw) || Object.keys(raw).length) {
      model.violations.push(`email-verification request body ${JSON.stringify(raw ?? null)} != {}`);
      return modelError(422, 'validation_error', 'Request validation failed');
    }
    if (user.email_verified) {
      return modelError(409, 'email_already_verified', 'This email address is already verified.');
    }
    mailAccountToken('email_verification', user);
    return new HttpResponse(null, { status: 204 });
  }),
  http.post(P('/auth/email-verification/confirm'), async ({ request }) => {
    const { user, body } = await record(request);
    tokenOnlyInBody('email-verification confirm', request);
    if (!user) return modelError(401, 'unauthorized', 'Not authenticated.');
    const bad = exactKeys('email-verification confirm', body, ['token']);
    if (bad) return bad;
    if (!boundedString(body.token, 1, 128)) return modelError(422, 'validation_error', 'Request validation failed');
    // account_tokens.py:424-469, in order: unknown 404, another account's 403 (nothing
    // spent), then not open or minted for an address the account no longer has 404.
    const row = findAccountToken('email_verification', body.token);
    if (!row) return modelError(404, 'email_verification_invalid', 'This verification link is invalid or has expired.');
    if (row.user_id !== user.id) {
      return modelError(403, 'email_verification_wrong_account', 'This verification link was issued to a different account.');
    }
    if (!accountTokenOpen(row) || row.email !== user.email) {
      return modelError(404, 'email_verification_invalid', 'This verification link is invalid or has expired.');
    }
    row.used_at = Date.now();
    user.email_verified = true;
    revokeOpenAccountTokens('email_verification', user.id, row);
    return new HttpResponse(null, { status: 204 });
  }),

  // ---- Invitations: the token holder's side (auth/routes.py:156-195) ----
  // The token rides only in the JSON body; no request here accepts the email,
  // organization or role.
  http.post(P('/auth/invitations/preview'), async ({ request }) => {
    const { body } = await record(request);
    tokenOnlyInBody('preview', request);
    const bad = exactKeys('preview', body, ['token']);
    if (bad) return bad;
    const inv = resolveToken(body.token);
    if (inv instanceof Response) return inv;
    return HttpResponse.json({
      organization_id: inv.organization_id,
      organization_name: model.orgs.get(inv.organization_id)!.name,
      email: inv.email,
      role: inv.role,
      expires_at: inv.expires_at,
    });
  }),
  http.post(P('/auth/invitations/register'), async ({ request }) => {
    const { body } = await record(request);
    tokenOnlyInBody('register', request);
    const bad = exactKeys('register', body, ['token', 'full_name', 'password']);
    if (bad) return bad;
    const inv = resolveToken(body.token);
    if (inv instanceof Response) return inv;
    // invitations.py:563-600: an existing account for the invited email is a 409.
    if ([...model.users.values()].some((u) => u.email === inv.email)) {
      return modelError(409, 'invitation_account_exists', 'An account already exists for this email address.');
    }
    if (!inviterStillAuthorized(inv)) {
      return modelError(409, 'invitation_inviter_not_authorized', 'The person who sent this invitation can no longer grant this access.');
    }
    const user: ModelUser = {
      id: nextId('user-invited'),
      email: inv.email,
      full_name: String(body.full_name),
      password: String(body.password),
      is_operator: false,
      // FD-5 / FD-6: holding an invitation proves nothing about the mailbox.
      email_verified: false,
    };
    model.users.set(user.id, user);
    inv.accepted_at = new Date().toISOString();
    model.memberships.push({
      organization_id: inv.organization_id,
      user_id: user.id,
      role: inv.role,
      created_at: inv.accepted_at,
      source: 'invitation',
    });
    return HttpResponse.json(sessionFor(user, issueAccessToken(user)), { status: 201 });
  }),
  http.post(P('/auth/invitations/accept'), async ({ request }) => {
    const { user, body } = await record(request);
    tokenOnlyInBody('accept', request);
    const session = sessionOf(request);
    if (!user || !session) return modelError(401, 'unauthorized', 'Not authenticated.');
    const bad = exactKeys('accept', body, ['token']);
    if (bad) return bad;
    const inv = resolveToken(body.token);
    if (inv instanceof Response) return inv;
    // invitations.py:532-560, in order.
    if (user.email !== inv.email) {
      return modelError(403, 'permission_denied', 'This invitation was issued to a different email address.');
    }
    if (membershipOf(inv.organization_id, user.id)) {
      return modelError(409, 'invitation_already_member', 'You are already a member of this organization.');
    }
    if (!inviterStillAuthorized(inv)) {
      return modelError(409, 'invitation_inviter_not_authorized', 'The person who sent this invitation can no longer grant this access.');
    }
    inv.accepted_at = new Date().toISOString();
    model.memberships.push({
      organization_id: inv.organization_id,
      user_id: user.id,
      role: inv.role,
      created_at: inv.accepted_at,
      source: 'invitation',
    });
    // Re-issue inside the presented session: accepting never forks a new one (P6-AUTH-4).
    return HttpResponse.json(sessionFor(user, mintInSession(session.id, user)));
  }),

  // ---- Org / workspace / brand ----
  http.get(P('/organizations'), async ({ request }) => {
    const { user } = await record(request);
    if (!user) return modelError(401, 'unauthorized', 'Not authenticated.');
    const ids = new Set(model.memberships.filter((m) => m.user_id === user.id).map((m) => m.organization_id));
    return HttpResponse.json([...model.orgs.values()].filter((o) => ids.has(o.id)));
  }),
  http.get(P('/organizations/:orgId/workspaces'), async ({ request, params }) => {
    const { user } = await record(request);
    const actor = orgActor(String(params.orgId), user);
    if (actor instanceof Response) return actor;
    return HttpResponse.json(model.workspaces.get(String(params.orgId)) ?? []);
  }),
  // organizations/routes.py:75-97 — exact OWNER/ADMIN.
  http.post(P('/organizations/:orgId/workspaces'), async ({ request, params }) => {
    const { user, body } = await record(request);
    const orgId = String(params.orgId);
    const actor = orgActor(orgId, user);
    if (actor instanceof Response) return actor;
    const denied = requireOrgAdmin(actor);
    if (denied) return denied;
    const created: ModelWorkspace = {
      id: nextId('ws'),
      organization_id: orgId,
      name: String(body.name),
      slug: String(body.name).toLowerCase().replace(/[^a-z0-9]+/g, '-'),
      onboarding_completed: false,
      created_at: new Date().toISOString(),
    };
    model.workspaces.set(orgId, [...(model.workspaces.get(orgId) ?? []), created]);
    return HttpResponse.json(created, { status: 201 });
  }),

  // ---- Invitations: the administrator's side (organizations/routes.py:118-161) ----
  http.post(P('/organizations/:orgId/invitations'), async ({ request, params }) => {
    const { user, body } = await record(request);
    const orgId = String(params.orgId);
    const actor = orgActor(orgId, user);
    if (actor instanceof Response) return actor;
    const denied = requireOrgAdmin(actor);
    if (denied) return denied;
    const bad = exactKeys('create invitation', body, ['email', 'role']);
    if (bad) return bad;
    // InvitationCreate.role's enum has no owner, so the schema refuses it (422)
    // before invitations.py:384-388 ever could.
    if (!INVITABLE.has(String(body.role))) return modelError(422, 'validation_error', 'Request validation failed');
    const role = body.role as InvitableRole;
    const email = String(body.email);
    if (ROLE_RANK[role] > ROLE_RANK[actor.role]) {
      return modelError(403, 'invitation_role_forbidden', 'You cannot invite a member with a role ranked above your own.');
    }
    if (model.memberships.some((m) => m.organization_id === orgId && model.users.get(m.user_id)?.email === email)) {
      return modelError(409, 'invitation_already_member', 'This email address already belongs to a member of the organization.');
    }
    const open = model.invitations.find(
      (i) => i.organization_id === orgId && i.email === email && !i.revoked_at && !i.accepted_at,
    );
    if (open && invitationState(open) === 'pending') {
      return modelError(409, 'invitation_pending_exists', 'A pending invitation already exists for this email address.');
    }
    if (open) open.revoked_at = new Date().toISOString(); // an expired open invitation is superseded
    const now = Date.now();
    const inv: ModelInvitation = {
      id: nextId('inv'),
      organization_id: orgId,
      email,
      role,
      token: model.nextInvitationToken ?? `Ix${model.seq + 1}_q7Rz-Tk${model.seq + 1}vW`,
      created_at: new Date(now).toISOString(),
      expires_at: new Date(now + INVITATION_TTL_MS).toISOString(),
      invited_by_user_id: user!.id,
      revoked_at: null,
      accepted_at: null,
    };
    model.nextInvitationToken = null;
    model.invitations.push(inv);
    return HttpResponse.json({ ...invitationOut(inv), token: inv.token }, { status: 201 });
  }),
  http.get(P('/organizations/:orgId/invitations'), async ({ request, params }) => {
    const { user } = await record(request);
    const orgId = String(params.orgId);
    const actor = orgActor(orgId, user);
    if (actor instanceof Response) return actor;
    const denied = requireOrgAdmin(actor);
    if (denied) return denied;
    return HttpResponse.json(
      model.invitations
        .filter((i) => i.organization_id === orgId && invitationState(i) === 'pending')
        .map(invitationOut),
    );
  }),
  http.delete(P('/organizations/:orgId/invitations/:invitationId'), async ({ request, params }) => {
    const { user } = await record(request);
    const orgId = String(params.orgId);
    const actor = orgActor(orgId, user);
    if (actor instanceof Response) return actor;
    const denied = requireOrgAdmin(actor);
    if (denied) return denied;
    const inv = model.invitations.find((i) => i.id === params.invitationId && i.organization_id === orgId);
    if (!inv) return modelError(404, 'invitation_not_found', 'Invitation not found.');
    const state = invitationState(inv);
    if (state !== 'pending') {
      const [code, message] = STATE_CONFLICT[state];
      return modelError(409, code, message);
    }
    inv.revoked_at = new Date().toISOString();
    return new HttpResponse(null, { status: 204 });
  }),

  // ---- Members (organizations/routes.py:164-205; members.py) ----
  http.get(P('/organizations/:orgId/members'), async ({ request, params }) => {
    const { user } = await record(request);
    const orgId = String(params.orgId);
    const actor = orgActor(orgId, user);
    if (actor instanceof Response) return actor;
    return HttpResponse.json(
      model.memberships
        .filter((m) => m.organization_id === orgId)
        .sort((a, b) => a.created_at.localeCompare(b.created_at) || a.user_id.localeCompare(b.user_id))
        .map(memberRow),
    );
  }),
  http.put(P('/organizations/:orgId/members/:userId/role'), async ({ request, params }) => {
    const { user, body } = await record(request);
    const orgId = String(params.orgId);
    const actor = orgActor(orgId, user);
    if (actor instanceof Response) return actor;
    const denied = requireOrgAdmin(actor);
    if (denied) return denied;
    const bad = exactKeys('change role', body, ['role']);
    if (bad) return bad;
    if (!(String(body.role) in ROLE_RANK)) return modelError(422, 'validation_error', 'Request validation failed');
    const next = body.role as ModelRole;
    // members.py:195-251, in order.
    if (params.userId === user!.id) {
      return modelError(403, 'member_self_change_forbidden', 'You cannot change your own role.');
    }
    const target = membershipOf(orgId, String(params.userId));
    if (!target) return modelError(404, 'member_not_found', 'Member not found.');
    if ((target.role === 'owner' || next === 'owner') && actor.role !== 'owner') {
      return modelError(403, 'member_owner_required', 'Only an owner can grant, change or remove the owner role.');
    }
    if (ROLE_RANK[next] > ROLE_RANK[actor.role] || ROLE_RANK[target.role] > ROLE_RANK[actor.role]) {
      return modelError(403, 'member_role_ceiling', 'You cannot manage a member or assign a role ranked above your own.');
    }
    if (target.role === 'owner' && next !== 'owner' && ownerCount(orgId) < 2) {
      return modelError(409, 'organization_last_owner', "The organization's last owner cannot be demoted or removed.");
    }
    target.role = next;
    return HttpResponse.json(memberRow(target));
  }),
  http.delete(P('/organizations/:orgId/members/:userId'), async ({ request, params }) => {
    const { user } = await record(request);
    const orgId = String(params.orgId);
    const actor = orgActor(orgId, user);
    if (actor instanceof Response) return actor;
    const denied = requireOrgAdmin(actor);
    if (denied) return denied;
    // members.py:253-295, in order.
    if (params.userId === user!.id) {
      return modelError(403, 'member_self_removal_forbidden', 'You cannot remove yourself from the organization.');
    }
    const target = membershipOf(orgId, String(params.userId));
    if (!target) return modelError(404, 'member_not_found', 'Member not found.');
    if (target.role === 'owner' && actor.role !== 'owner') {
      return modelError(403, 'member_owner_required', 'Only an owner can grant, change or remove the owner role.');
    }
    if (ROLE_RANK[target.role] > ROLE_RANK[actor.role]) {
      return modelError(403, 'member_role_ceiling', 'You cannot manage a member or assign a role ranked above your own.');
    }
    if (target.role === 'owner' && ownerCount(orgId) < 2) {
      return modelError(409, 'organization_last_owner', "The organization's last owner cannot be demoted or removed.");
    }
    model.memberships = model.memberships.filter((m) => m !== target);
    return new HttpResponse(null, { status: 204 });
  }),
  http.get(P('/workspaces/:ws'), () => HttpResponse.json(workspace)),
  http.get(P('/workspaces/:ws/brands'), () => HttpResponse.json([brand])),
  http.get(P('/workspaces/:ws/business-profile'), () => HttpResponse.json(emptyProfile)),
  http.put(P('/workspaces/:ws/business-profile'), () => HttpResponse.json(emptyProfile)),
  http.post(P('/workspaces/:ws/onboarding'), () =>
    HttpResponse.json({ brand, business_profile: emptyProfile, workspace_id: workspace.id, onboarding_completed: true }),
  ),

  // ---- Locations & geo ----
  http.get(P('/workspaces/:ws/locations'), () => HttpResponse.json(locations)),
  http.post(P('/workspaces/:ws/locations'), async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({ ...locations[0], ...body, id: 'loc-new' });
  }),
  http.put(P('/workspaces/:ws/locations/:id'), async ({ request, params }) => {
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({ ...locations[0], ...body, id: params.id });
  }),
  http.get(P('/workspaces/:ws/locations/:id/geo-coverage'), ({ params }) =>
    HttpResponse.json({ coverage_type: 'radius', radius_miles: 25, included_markets: [], excluded_markets: [], online_global: false, id: 'geo-1', location_id: params.id }),
  ),
  http.put(P('/workspaces/:ws/locations/:id/geo-coverage'), async ({ request, params }) => {
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({ ...body, id: 'geo-1', location_id: params.id });
  }),
  http.post(P('/geocode'), () =>
    HttpResponse.json({ latitude: 32.7, longitude: -96.8, city: 'Dallas', state_province: 'TX', country: 'United States', timezone: 'America/Chicago', confidence: 0.95 }),
  ),

  // ---- Scout requests ----
  http.get(P('/workspaces/:ws/scout-requests'), () => HttpResponse.json(scoutRequests)),
  http.get(P('/workspaces/:ws/scout-requests/:id'), ({ params }) => {
    const r = scoutRequests.find((s) => s.id === params.id);
    return r ? HttpResponse.json(r) : HttpResponse.json({ detail: 'Not found' }, { status: 404 });
  }),
  http.post(P('/workspaces/:ws/scout-requests'), async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({ ...scoutRequests[0], ...body, id: 'scout-new', status: 'draft' });
  }),
  http.post(P('/workspaces/:ws/scout-requests/:id/pause'), ({ params }) => {
    const r = scoutRequests.find((s) => s.id === params.id)!;
    return HttpResponse.json({ ...r, status: 'paused' });
  }),
  http.post(P('/workspaces/:ws/scout-requests/:id/resume'), ({ params }) => {
    const r = scoutRequests.find((s) => s.id === params.id)!;
    return HttpResponse.json({ ...r, status: 'completed' });
  }),
  http.post(P('/workspaces/:ws/scout-requests/:id/run'), ({ params }) =>
    HttpResponse.json({
      scout_request_id: String(params.id),
      status: 'queued',
      stats: { job_id: 'job-1', job_status: 'pending' },
    }),
  ),

  // ---- Durable jobs ----
  http.get(P('/workspaces/:ws/jobs'), () =>
    HttpResponse.json({ items: [], total: 0, limit: 10, offset: 0 }),
  ),

  // ---- Opportunities ----
  http.get(P('/workspaces/:ws/opportunities'), ({ request }) => {
    const url = new URL(request.url);
    const locationId = url.searchParams.get('location_id');
    const scoutId = url.searchParams.get('scout_request_id');
    const classification = url.searchParams.get('classification');
    const status = url.searchParams.get('status');
    const search = url.searchParams.get('search');
    const minScore = url.searchParams.get('min_score');

    let rows = opportunities.slice();
    if (locationId) rows = rows.filter((o) => o.location_id === locationId);
    if (scoutId) rows = rows.filter((o) => o.scout_request_id === scoutId);
    if (classification) rows = rows.filter((o) => o.classification === classification);
    if (status) rows = rows.filter((o) => o.status === status);
    if (minScore) rows = rows.filter((o) => o.opportunity_score >= Number(minScore));
    if (search) {
      const q = search.toLowerCase();
      rows = rows.filter((o) => o.title.toLowerCase().includes(q));
    }
    return HttpResponse.json(rows);
  }),
  http.get(P('/workspaces/:ws/opportunities/:id'), ({ params }) => {
    const o = opportunities.find((x) => x.id === params.id);
    return o ? HttpResponse.json(detailFor(o)) : HttpResponse.json({ detail: 'Not found' }, { status: 404 });
  }),
  http.put(P('/workspaces/:ws/opportunities/:id/status'), async ({ request, params }) => {
    const body = (await request.json()) as { status: string };
    const o = opportunities.find((x) => x.id === params.id)!;
    return HttpResponse.json({ ...o, status: body.status });
  }),

  // ---- Opportunity intelligence (Batch 4B read-only) ----
  // Returns a per-opportunity payload so four-market isolation is provable, and
  // ``{ intelligence: null }`` for opportunities without a persisted record.
  http.get(P('/workspaces/:ws/opportunities/:id/intelligence'), ({ params }) => {
    const o = opportunities.find((x) => x.id === params.id);
    if (!o) return HttpResponse.json({ detail: 'Not found' }, { status: 404 });
    return HttpResponse.json({
      opportunity_id: o.id,
      intelligence: o.id === noIntelOpportunityId ? null : intelligenceFor(o),
    });
  }),

  // ---- Opportunity feedback (3C-C; dark by default) ----
  // The feature ships dark, so both the read and the write answer 503
  // (capability_unavailable) unless a test explicitly enables them via
  // server.use(...). This keeps the feedback UI hidden by default.
  http.get(P('/workspaces/:ws/opportunities/:id/feedback'), () =>
    HttpResponse.json(
      { error: { code: 'capability_unavailable', message: 'Opportunity feedback is not available yet.' } },
      { status: 503 },
    ),
  ),
  http.post(P('/workspaces/:ws/opportunities/:id/feedback'), () =>
    HttpResponse.json(
      { error: { code: 'capability_unavailable', message: 'Opportunity feedback is not available yet.' } },
      { status: 503 },
    ),
  ),

  // ---- Campaign context (all kinds return an empty list by default) ----
  // Registered LAST so the specific single-segment routes above (locations,
  // scout-requests, opportunities) take precedence over this greedy :kind match.
  http.get(P('/workspaces/:ws/:kind'), ({ params }) => {
    const kinds = ['products', 'audiences', 'competitors', 'brand-voice', 'offers', 'claims', 'source-preferences', 'channel-preferences', 'campaigns'];
    if (kinds.includes(String(params.kind))) return HttpResponse.json([]);
    return HttpResponse.json({ detail: 'Not found' }, { status: 404 });
  }),
];
