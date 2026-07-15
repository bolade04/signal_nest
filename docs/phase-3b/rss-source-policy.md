# RSS/news source policy (Phase 3B Batch 2)

**Status: BLOCKED — no source is legally or owner-approved. The approved-source
registry ships empty / all-disabled.** This document defines *how* a source would
be onboarded and governed; it does **not** approve any source.

## 1. Allowlist-only principle

A live fetch is permitted **only** when all of the following hold:

- the target matches an **explicitly approved source** (canonical scheme + host and
  an exact feed URL or approved path), and
- the source's `enabled` flag is on, and
- the source has both `legal_review = approved` and `owner_approval = approved`, and
- the global `connector_rss_live_enabled` flag is on and the kill switch is off, and
- the request's market is admitted by the source's jurisdiction allowlist, and
- the request's tenant/workspace is admitted by the source's eligibility.

If any condition is false, the connector **fails closed** to the fixture path.
Arbitrary customer-defined URLs are **never** accepted in Batch 2.

## 2. Approved-source schema

Each source is an immutable record (`app/connectors/sources.py :: ApprovedSource`):

| Field | Meaning |
|-------|---------|
| `source_id` | Stable, immutable identifier (bounded, safe for metrics labels) |
| `display_name` | Human-readable name |
| `scheme` | Must be `https` (an `http` exception requires a documented note) |
| `host` | Canonical host (exact match; no wildcards) |
| `allowed_ports` | Approved ports (default `{443}`) |
| `feed_urls` | Exact approved feed URL(s) |
| `allowed_redirect_hosts` | Hosts a redirect may target (subset, explicit) |
| `enabled` | Off by default |
| `environments` | Where eligible (e.g. `staging` only for canary) |
| `tenants` / `workspaces` | Eligibility allowlists (empty ⇒ none until set) |
| `markets` | Location/market eligibility |
| `jurisdictions` | Jurisdiction allowlist |
| `fetch_interval_seconds` | Minimum spacing between fetches |
| `burst_limit` | Token-bucket capacity |
| `daily_limit` | Max fetches / day |
| `max_response_bytes` | Hard response cap |
| `retention` | `metadata_only` \| `excerpt` (never `full_article`) |
| `attribution_required` | Always true |
| `legal_review` | `pending` \| `approved` \| `rejected` |
| `legal_reference` | Ticket/URL + date of the review |
| `owner_approval` | `pending` \| `approved` \| `rejected` |
| `notes` | Free text |

## 3. Source onboarding checklist

1. Identify publisher/provider and domain ownership.
2. Confirm the feed is public and requires no authentication.
3. Record commercial-use terms, attribution requirements, republishing and
   excerpt/retention restrictions, and any geographic restrictions.
4. Record rate-limit expectations and `robots.txt` relevance.
5. Capture the Terms-of-Service URL/reference.
6. Obtain **legal review** → set `legal_review` + `legal_reference`.
7. Obtain **product-owner approval** → set `owner_approval`.
8. Set conservative `burst_limit`, `daily_limit`, `fetch_interval_seconds`,
   `max_response_bytes`.
9. Add the record **disabled**; enable only for canary environment/tenant first.

A source whose usage rights are unknown is **never** hard-coded or enabled.

## 4. Disabling / revocation

- Flip `enabled = False` (or trip `connector_rss_kill_switch`) → immediate stop.
- Remove the record to permanently revoke.
- Revocation requires no migration and no redeploy of application logic.

## 5. Legal & ToS status (current)

| Source | Legal review | Owner approval | Notes |
|--------|-------------|----------------|-------|
| _(none)_ | — | — | Registry intentionally empty pending sign-off |

**No source may be marked legally approved without documented evidence.**

## 6. Attribution policy

Every emitted signal carries source title, source URL, license descriptor and
retrieved-at timestamp. Attribution is non-secret provenance only.

## 7. Storage & retention policy

Persist **metadata or short excerpts only** — never full articles. Retention is
bounded by the source's `retention` field and the platform's data-retention
foundations. No credentials or secrets are ever stored on a signal.

## 8. Jurisdiction controls

A source serves a request only if the request's market is in the source's
`jurisdictions` allowlist. This is enforced in addition to the pipeline's existing
per-location/market isolation.

## 9. Rate-limit policy

Per-source token bucket (`burst_limit` + refill from `fetch_interval_seconds`) plus
a global `connector_rss_live_max_concurrency`. `Retry-After` is honored within
bounded limits with jitter to prevent retry storms.

## 10. Incident escalation

On anomalous behavior (validation-rejection spike, source poisoning, unexpected
redirects): trip the kill switch, disable the source, and follow
`docs/operations/rss-connector-operations.md` → Incident response.
