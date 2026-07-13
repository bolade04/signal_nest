import { http, HttpResponse } from 'msw';
import { API_PREFIX } from '@/api/config';

// A small but realistic in-memory backend for component/integration tests.
// It models one org → one workspace → one brand → four independent city
// locations (Dallas, London, Lagos, Nairobi), each with its own scout request
// and opportunities. The opportunity endpoint honours the location_id and
// scout_request_id filters so tests can prove strict per-location isolation.

const P = (path: string) => `*${API_PREFIX}${path}`;

export const demoUser = { id: 'user-1', email: 'demo@signalnest.dev', full_name: 'Demo Marketer' };

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
  stats: { opportunities: 2, signals_processed: 20 },
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

export const handlers = [
  // ---- System (runtime introspection; secret-free) ----
  http.get(P('/system/capabilities'), () =>
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

  // ---- Auth ----
  http.get(P('/auth/me'), () =>
    HttpResponse.json({ access_token: 'test-token', token_type: 'bearer', user: demoUser, memberships: [{ organization_id: org.id, role: 'owner' }] }),
  ),
  http.post(P('/auth/login'), () =>
    HttpResponse.json({ access_token: 'test-token', token_type: 'bearer', user: demoUser, memberships: [{ organization_id: org.id, role: 'owner' }] }),
  ),
  http.post(P('/auth/register'), () =>
    HttpResponse.json({ access_token: 'test-token', token_type: 'bearer', user: demoUser, memberships: [{ organization_id: org.id, role: 'owner' }] }),
  ),

  // ---- Org / workspace / brand ----
  http.get(P('/organizations'), () => HttpResponse.json([org])),
  http.get(P('/organizations/:orgId/workspaces'), () => HttpResponse.json([workspace])),
  http.post(P('/organizations/:orgId/workspaces'), () => HttpResponse.json(workspace)),
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
    HttpResponse.json({ scout_request_id: String(params.id), status: 'completed', stats: { opportunities: 2, signals_processed: 20 } }),
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

  // ---- Campaign context (all kinds return an empty list by default) ----
  // Registered LAST so the specific single-segment routes above (locations,
  // scout-requests, opportunities) take precedence over this greedy :kind match.
  http.get(P('/workspaces/:ws/:kind'), ({ params }) => {
    const kinds = ['products', 'audiences', 'competitors', 'brand-voice', 'offers', 'claims', 'source-preferences', 'channel-preferences', 'campaigns'];
    if (kinds.includes(String(params.kind))) return HttpResponse.json([]);
    return HttpResponse.json({ detail: 'Not found' }, { status: 404 });
  }),
];
