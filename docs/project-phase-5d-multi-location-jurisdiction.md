# Phase 5D — Multi-Location Campaign Context and Jurisdiction Propagation

**Parent:** `docs/project-phase-5-plan.md`. **Status:** PLANNED / DOCS-ONLY —
authorizes nothing. **Schedule slot (continuous round, master plan §5; O-11
RESOLVED 2026-09-01 — the fast-follow shape is rejected):** 5D's contracts are
part of the single consolidated **CF-3** freeze (shared-surface authoring D15 PM,
integration cycle **D16 AM**, after the **D10 AM** PR #34 adjacency check; reviewed
D16 PM); build **D17 AM–D18 AM** (four rebalanced lanes W1–W4, longest lane 1.30
against a 1.5-day window) with
**four** dedicated lanes (local float **+0.20**, spendable only inside 5D — an earlier
draft said "zero slack", contradicting §5); security-weighted review
wave **R-5D** on D19 AM (after the I-5D integration cycle at D18 PM). 5D builds **inside** the round, on the critical path
before 5E, and **remains an activation precondition for 5C and for Phase 5D
itself** — nothing can activate before the round completes (master plan §10).

## 1. Objective

Harden the campaign contract so campaign scoping is safe to be load-bearing, and
introduce a **normalized, fail-closed jurisdiction axis** that propagates
location → campaign → brief → creative → review. This is the largest genuinely new
design area of Project Phase 5: today no jurisdiction entity or column exists
anywhere, market matching is bidirectional substring comparison, and the connector
policy **fails open** when a market cannot be resolved. Global-first is the target
(location, market, jurisdiction, service area, radius, exclusions); the
Texas/US, London/UK, Lagos/NG, Nairobi/KE seed matrix is an isolation/localization
test matrix, **not** a launch-market assertion.

## 2. Frozen contracts (fixed at the consolidated CF-3 freeze — shared surface D15 PM, integration cycle D16 AM, reviewed D16 PM)

**Campaign hardening:**

- `Campaign.mode` and `Campaign.status` validated against the existing
  `CampaignMode` enum and a new closed `CampaignStatus` enum, at the schema layer
  and via portable check constraints (additive migration; existing free-string
  values enumerated and migrated).
- Campaign location membership per operator decision O-8 — RESOLVED 2026-09-01
  (`REC_campaign_locations_join_table_FKs_backfill`): new
  `campaign_locations` join table with real FKs + backfill from the JSON array,
  which is retained read-only for one release. **Backfill fail-closed rule:** the source array was written
  unvalidated, so the migration validates every id against the campaign's
  workspace, migrates only valid ids, **drops-and-reports** the rest in the
  migration output (never silently, never cross-tenant), and installs a
  workspace-consistency constraint on the join table; a cross-tenant negative
  test covers the class.
- Dedicated campaigns router: adds `GET /campaigns/{id}` and `PUT
  /campaigns/{id}` (currently only list/create/delete exist via the generic
  factory); response models projected — fixing the `_serialize` leak of
  `organization_id`/`workspace_id` **for campaigns only**. The same leak exists
  for the other eight context entities; leaving them is deliberate (the caller
  already knows both ids; low severity) and recorded here so the narrow scope
  is not mistaken for an oversight.
- **`campaign_id` workspace validation** everywhere it is accepted — closing the
  definition audit's isolation asymmetry on scout-request creation (location is
  validated; campaign is not) and applying the same check in the 5A/5C surfaces.

**Jurisdiction axis:**

- Normalized codes: `jurisdiction_code` (ISO 3166-1 alpha-2, optionally
  `-` + subdivision, e.g. `US-TX`, `GB`, `NG-LA`, `KE`) added to
  `business_locations` (additive column alongside the free-text `country`, which
  is retained) and to `geo_coverage_rules`.
- A pure `jurisdiction/` resolution module (pattern: `geography/engine.py`):
  `resolve_jurisdiction(location, coverage_rules) -> JurisdictionResolution`
  returning code(s) + confidence + evidence. **Fail-closed:** an unresolvable
  jurisdiction yields `UNRESOLVED`, and every consumer treats `UNRESOLVED` as
  **deny** for gating purposes.
- Exclusion semantics fixed at the engine level: exclusions are evaluated
  **before** any short-circuit (curing `online_global` bypassing exclusions), and
  matching uses normalized codes, not substrings. The existing substring paths
  remain for scoring display but are never used for gating.
- Propagation: briefs and creative drafts snapshot the resolved
  `jurisdiction_code` at capture time; `content_reviews` record the jurisdiction
  they were evaluated under; **the `exports` table is created at the same CF-3
  freeze WITH its `jurisdiction_code` snapshot column native from birth** — in
  the adopted continuous round, 5D builds before the 5E export service, so
  every export row ever written carries a jurisdiction snapshot and **no
  NULL-jurisdiction export cohort exists** for rows created after CF-3 (an
  improvement over the rejected split, which disclosed such a cohort).
  **Nullability — reconciled decision (S21 second source; operator-confirmed
  2026-09-02).** `jurisdiction_code` remains **nullable** on the three *populated*
  tables — `briefs`, `creative_drafts` and `business_locations` — where `NULL` means
  *"no resolution was ever attempted or captured"* and `UNRESOLVED` means *"the resolver
  ran and failed"*. These are genuinely different states and a sentinel in the column
  would collapse them. The `UNRESOLVED` sentinel stays where this section already puts
  it: in the `JurisdictionResolution` **return value**, not in the column.

  **An earlier draft proposed `NOT NULL` + an `UNRESOLVED` sentinel on these populated
  tables. That is REFUTED and must not be reinstated.** 5E §2 states export precondition
  5 as literally `jurisdiction_code IS NOT NULL` — *"an explicit refusal, not an
  assumption"* — and master plan §10 makes verifying that refusal a
  `content_export_enabled` **activation gate**. If no row is ever NULL the refusal is
  vacuously true for every row and admits everything, including the residual pre-CF-3
  cohort it exists to block. It would also foreclose the recorded *"operator decision to
  block"* branch below by forcing the backfill.

  **`NOT NULL` IS adopted for the new `exports` table, with no sentinel in its allowed
  set.** That table is created at CF-3 with no pre-existing rows, so the constraint costs
  nothing and makes a NULL-jurisdiction export row **unrepresentable at the database
  layer** — defence in depth in the right direction. The CHECK enforcing its allowed set
  is covered by `PP5-CHK-001` (master plan §9.2), because Alembic does not compare check
  constraints at all.

  **Isolation is unaffected either way.** Workspace isolation is an equality on
  `workspace_id` derived from the authenticated context
  (`apps/api/app/auth/dependencies.py:81-99`), never on a jurisdiction value; a shared
  attribute value is structurally identical to `business_locations.country`, which many
  workspaces already share.

  **Residual pre-CF-3 cohort — disclosed, not assumed away:** `jurisdiction_code`
  is added **additively** to `briefs` and `creative_drafts`, which 5A and 5C
  created earlier in the round. Any row written before CF-3 — in particular
  briefs created if an operator enabled `recommendation_briefs_enabled` per
  workspace at or after the D8 recovery boundary — carries a NULL jurisdiction
  and is **not** cured by the table being born jurisdiction-aware. CF-3
  therefore requires **one of**: a backfill for those rows using the same
  drop-and-report discipline specified for `campaign_locations` (unresolvable
  rows reported, never silently defaulted), **or** a recorded operator decision
  to block generation and export for any row whose `jurisdiction_code` is NULL.
  The 5E export service enforces the NULL refusal independently as
  defence-in-depth (5E §2), and master plan §10 makes this a precondition of 5A
  activation. Jurisdiction-conditioned claim rules
  become possible via an additive `jurisdiction_codes` column on
  `claims_library` (empty = all).
  **Ownership:** the column additions on earlier milestones' tables land in the
  consolidated CF-3 migration payload authored by the integration owner
  (post-integration contract files are freeze-owned, master plan §6); the
  snapshot-capture **service** edits in `briefs/` and `creative/` are assigned
  to lane 5D-W3 below under the cross-milestone touch rule. No 5D lane touches
  `apps/api/app/exports/` — that service does not exist yet in this window and
  is born jurisdiction-aware in 5E (5E-W1).
- The 7-city fixture geocoder is retained for tests; the provider seam is
  unchanged (a real geocoding provider is an activation-time concern, not built
  here).

## 3. Entry criteria

1. I-5C integrated and R4 clean (the operator resolved O-11 on 2026-09-01 by
   adopting the single continuous round; there is no committed-round/fast-follow
   boundary).
2. CF-3 reviewed (**D16 PM**); operator decision O-8 RESOLVED 2026-09-01
   (join table); the PR #34 adjacency check (master plan §6) performed at **D10 AM**,
   which precedes the CF-3 integration cycle (**D16 AM**) as that rule requires.

## 4. Exit criteria (all TRUE with artifacts)

1. Migrations round-trip; single head; the `location_ids` backfill (O-8
   resolved: join table) is idempotent, **fail-closed per the §2 rule** (invalid/
   cross-workspace ids dropped-and-reported, consistency constraint installed),
   and verified.
2. Campaign mode/status invalid values are rejected at schema and DB layers;
   campaign get/update work; campaign responses no longer leak tenant columns.
3. `campaign_id` cross-workspace injection is rejected everywhere it is accepted
   (regression test reproducing the audit's asymmetry, now failing closed).
4. Jurisdiction resolution: the four-market matrix resolves to `US-TX`, `GB`,
   `NG-LA`, `KE` (or documented equivalents); an unknown city yields `UNRESOLVED`;
   a gating consumer given `UNRESOLVED` **denies** (explicit fail-closed tests).
5. Exclusion tests: an excluded jurisdiction is denied even under `online_global`
   coverage; normalized-code matching has no substring false-positives
   ("York" ≠ "New York" class regression test); and the connector-policy cure is
   pinned by its own explicit criterion — `ConnectorPolicy.permits` with a
   non-empty `allowed_markets` and an unresolved (`None`) market **denies**
   (today it skips the check entirely).
6. Propagation tests: a brief/draft/review row snapshots the jurisdiction; a
   jurisdiction-restricted claims-library rule fires only in its jurisdictions.
7. Location isolation regression suite still green (feed superset behavior for
   omitted `location_id` unchanged — location remains a filter, not an authz
   boundary, per current product semantics; any change to that is a future
   decision, not 5D).
8. **Honest behavior-change ledger (replaces any blanket dark-state claim):**
   four 5D changes land on **live, un-flagged paths** and are named as such —
   (a) `campaign_id` workspace validation on scout-request creation (a live
   endpoint today; the change narrows accepted input); (b) the
   exclusions-before-short-circuit fix in `market_in_coverage`, which runs in
   the live scout pipeline — bounded by a scoring-snapshot comparison test over
   the seeded four-market fixtures proving no unrelated scoring output moves;
   (c) the campaign response projection, which removes
   `organization_id`/`workspace_id` from live campaign list/read responses; and
   (d) `CampaignMode`/`CampaignStatus` validation, which rejects free-string
   values the live create endpoint previously accepted (existing rows are
   enumerated and migrated first, so the constraint cannot strand data). The
   connector-policy change is genuinely dark-path today (the fail-open branch
   requires a non-empty `allowed_markets`, which only the dark RSS connector
   carries). Everything else in 5D is inert for existing flows.

## 5. Builder lanes and exclusive path ownership (rebalanced W1–W4, D17 AM–D18 AM)

| Lane | Exclusive owned paths (exact) | Delivers | Lane-days |
|---|---|---|---|
| **5D-W1** Primary domain | `apps/api/app/campaign_context/` (all files — sequential-touch: 5C-W2 touched `routes.py` for claims gating in **D11 PM–D13 AM**), `apps/api/app/scouting_requests/routes.py` (campaign-validation change only). **Cross-authored slice — tests W2's engine code:** `apps/api/app/tests/test_jurisdiction_resolution.py`. **Cross-authored slice — tests W3's propagation code (exit criterion 6, which previously had no owning file):** `apps/api/app/tests/test_jurisdiction_propagation.py` — a brief/draft/review row snapshots the jurisdiction, and a jurisdiction-restricted claims-library rule fires only in its jurisdictions. **W3 authors no tests**, so this criterion could not be owned by the lane that writes the code (+0.10) | campaign hardening, `campaign_locations` join table, dedicated campaigns router, validation closure, **plus the jurisdiction-propagation behaviour battery** | **1.30** |
| **5D-W2** Seam | `apps/api/app/jurisdiction/` (new), `apps/api/app/geography/` (all files), **Model moved to the freeze payload (master plan §6.2.0).** ~~`apps/api/app/locations/models.py`~~ (**→ CF-3 payload**, master plan §6.2.0: the CF-3 migration adds `jurisdiction_code` at **D16 AM**, one day before this lane's D17 AM–D18 AM window, and `business_locations` is a pre-Phase-5 table the *earlier-milestones* carve-out does not reach) + `locations/schemas.py` (additive columns only), `apps/api/app/connectors/policy.py`. **Cross-authored slice — tests W1's domain code:** `apps/api/app/tests/test_campaign_router.py` | normalized fail-closed jurisdiction resolution, exclusion-ordering fix, connector fail-open cure | **1.12** |
| **5D-W3** Primary UI | `apps/api/app/briefs/service.py`, `apps/api/app/creative/service.py` (jurisdiction snapshot capture, assigned at CF-3; both originate in earlier windows and are touched again by 5E-W3 in **D20 AM–D22 PM**. **These two files are a direct-edit collision with 5E-W3 — two shared files, not three: 5E-W3 additionally owns `apps/api/app/approvals/service.py`, which this lane does not touch. The two windows are strictly separated by I-5D, R-5D and an integration-overflow slot, so the lanes are never concurrent; overlapping them would be prohibited by master plan §6 regardless of any review gate**); `apps/web/src/pages/CampaignContext.tsx`, `apps/web/src/pages/campaigns/` (new, **excluding** its `__tests__/`). Authors **no** tests | jurisdiction propagation into briefs and drafts, campaign UI | **1.19** |
| **5D-W4** Adversarial + UI verification | `apps/api/app/tests/test_jurisdiction_failclosed.py`, `test_jurisdiction_exclusions.py`, `test_campaign_hardening.py`, `test_campaign_backfill.py`; **W3's UI tests:** `apps/web/src/pages/campaigns/__tests__/`, `apps/web/src/pages/__tests__/campaign-context.test.tsx` | **the whole adversarial battery** (four-market matrix, `UNRESOLVED` denies, exclusion under `online_global`, `"York"` ≠ `"New York"`, connector-policy denial, backfill idempotence + drop-and-report) plus verification of W3's surfaces | **1.18** |

**Longest lane 1.30 against a 1.5-day window (local float +0.20, spendable only inside
5D).** This is the operative statement; the header block restates it as a **derived**
summary and must be propagated in the same edit. An earlier draft carried this paragraph
twice, claimed it was stated only once, and left the §5 heading scheduling the build
overlapping the CF-3 review wave, which would have started builder work inside an open
review wave in violation of §7.2. Under the operative table the CF-3 review occupies
**D16 PM** and the 5D build starts **D17 AM**, so no overlap exists. Per-lane figures indicative
within ±0.1. Utilisation against the 1.5-day window is **87/75/79/79%** (W1 1.30,
W2 1.12, W3 1.19, W4 1.18) — an earlier draft quoted 100/90/99/75%, which reconciled
against neither the window nor the longest lane.

**W2 is deliberately NOT split**: the jurisdiction resolver and the geography exclusion
engine that consumes it are the round's most safety-critical fail-closed path, and
splitting them across two lanes in one window would create a tight intra-window
interface dependency on exactly the code that must not fail open. The ~0.11 lane-day
gain is not worth it. **5D gains no calendar time from the rebalance** — 1.50 and 1.20
both round up to 1.5 — but the gain is real as risk margin.

**5D's share of the CF-3 shared frontend contract surface — authored D15 PM by the
integration owner (master plan §6.3).** 5D-W3 creates the new top-level page directory
`apps/web/src/pages/campaigns/` and 5D adds the dedicated campaigns routes, which
cannot be reached without freeze-owned entries this sub-plan previously named **zero
times**: `apps/web/src/api/types.ts` aliases, `apps/web/src/api/endpoints.ts` wrappers,
`apps/web/src/api/queryKeys.ts` entries, a
**`apps/web/src/components/layout/nav.ts` entry** and a matching
**`apps/web/src/App.tsx` route**. Master plan §6.2 records that a route without a
`nav.ts` entry is unreachable and its breadcrumb silently degrades. All are
freeze-owned, authored inside the CF-3 payload, never by a builder lane, and they are
part of the 0.38 CF-3 share in the master plan §5 charge table.

Freeze-owned files per master plan §6 apply unchanged in this window (the
consolidated CF-3 payload carries the migrations, enums, OpenAPI/types
regeneration, and the column additions on post-integration contract files).

## 6. Out of scope

Real geocoding provider; per-location authorization boundaries; retention; any
activation. **Activation coupling (binding):** Phase 5D activation — and 5C
activation — require the fail-closed jurisdiction resolution of this milestone to
be merged and verified (master plan §10).

## 7. Rollback

Additive migrations downgrade cleanly. Behavior changes are classified per exit
criterion 8's four-item live-path ledger: the `campaign_id` validation, the
geography exclusion-ordering fix (with the scoring-snapshot bound), the
campaign response projection, and the `CampaignMode`/`CampaignStatus`
validation narrowing — each called out in its PR description with dedicated
tests; the connector-policy change is dark-path today. Reverting any PR
restores prior behavior.
