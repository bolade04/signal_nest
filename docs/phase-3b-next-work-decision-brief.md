# Phase 3B — post-Batch-4 next-work decision brief

**Purpose.** Record, from repository evidence only, what the *next* implementation
work item is after the completed Phase 3B Batch 4 closeout — and why it cannot start
without an owner/legal decision. This document **invents no product, scope, legal, or
business decision**; it surfaces the decisions the product owner must make so a future
implementation slice can begin. It changes no code, contract, migration, or runtime
behaviour.

_Independent-review status: NO INDEPENDENT THIRD-PARTY REVIEW COMPLETED._

## 1. Baseline (entry state)

| Item | Value |
| --- | --- |
| `main` SHA at authoring | `f4da3359da4b00623ec1f27358842e740652648e` |
| Working tree | clean; `main` == `origin/main` (ahead/behind 0/0) |
| Batch 4 (4A–4D) | **COMPLETE — merged and post-merge verified** (see `docs/phase-3b-implementation-plan.md` §17.1, §17.14, §17.22.21) |
| Ruleset `18820692` "main protection" | active, unchanged |
| Live RSS egress | disabled |

## 2. Candidate inventory (repository evidence only)

Every candidate below is drawn from an authoritative plan section or the current PR
list. No candidate is invented.

| # | Candidate | Source of record | State | Startable now? |
|---|-----------|------------------|-------|----------------|
| C1 | **Batch 2 — live HTTP RSS egress** | `phase-3b-implementation-plan.md` §13 (Batch plan); PR #34 (draft) | Draft PR open; risk-register rows **B-1** and **B-7** both `Open — needs owner/legal sign-off` (§15) | **No** — owner approval + legal/ToS sign-off outstanding; also explicitly out of current scope |
| C2 | **Phase 3C — scoring/evidence feedback loop** | `phase-3-plan.md` §"Phase 3C"; `phase-3b-implementation-plan.md` §17.19 ("Human feedback deferred to Phase 3C") | Deferred; **no** batch-level implementation-ready plan exists (`docs/phase-3c*` absent) | **No** — requires owner product decisions before it can even be scoped |
| C3 | **Connector-attribution frontend surface** | `phase-3b-implementation-plan.md` §13 ("Later"), §3 out-of-scope | Named as a "later, separately-gated UI batch"; no implementation-ready plan | **No** — no defined scope/acceptance/tests yet |
| C4 | **Batch 5** | Referenced only as "not started" across §17.6, §17.20, §17.22.x | Undefined — no scope in any document | **No** — scoping it would be pure invention |
| C5 | Dependency-maintenance PRs (#26–#29 dependabot; #6 TypeScript 7) | Open PR list; `phase-3-plan.md` maintenance queue | Maintenance, not phase feature work; #6 explicitly deferred/off-limits | Out of scope for "next phase" determination |

## 3. Classification

**`NEXT ITEM BLOCKED BY AMBIGUITY`.**

The literal next sequenced item (C1, Batch 2 live RSS) has a plan and a draft PR but is
**blocked on two outstanding owner/legal decisions** (risk rows B-1 and B-7) and is
explicitly excluded from the current working scope. Every candidate past it (C2 Phase
3C, C3 connector-attribution UI, C4 Batch 5) is deferred or undefined and **cannot be
planned or implemented without inventing business decisions**. Therefore there is no
repository-authoritative, implementation-ready, owner-approved next **code** item to
build at this time.

This is not a defect: the Phase 3B plan-of-record deliberately gates each next stage
behind an explicit owner checkpoint (§13, §15, §17.19, §17.20).

## 4. Owner/legal decisions required to unblock (nothing here is pre-decided)

To convert the next item into an implementation-ready slice, the product owner must
resolve **at least one** of the following. This brief takes no position on any of them.

1. **Batch 2 (live RSS) go/no-go** — approve a specific connector for live egress
   (resolves B-1) **and** confirm RSS/news ToS + legal feasibility in writing
   (resolves B-7). Only then can PR #34 move from draft toward review.
2. **Phase 3C scope** — decide what human feedback is captured, whether/how it may
   influence scoring or ranking, and its isolation/retention rules. These are product
   decisions, not repository-derivable; a Phase 3C implementation plan can be written
   only after they exist.
3. **Connector-attribution UI batch** — decide whether the deferred frontend
   attribution surface is the next slice, and its acceptance boundary.
4. **Batch 5** — define whether Batch 5 exists at all and, if so, its objective. It is
   currently undefined.

## 5. What this brief does and does not do

- **Does:** record the blocking analysis and the exact owner/legal decision points, so a
  future slice starts from an explicit approval rather than an assumption.
- **Does not:** implement any batch; assume Phase 3C or Batch 5 is next merely because it
  is named or next in sequence; touch PR #34, PR #6, the live-RSS branch, the safety
  branch, ruleset `18820692`, or any product runtime; enable live RSS; or make any
  product/legal decision on the owner's behalf.

## 6. Recommended next action for the owner

Choose one candidate from §4 and record the approval (and, for C1, the legal sign-off)
so the corresponding implementation-ready plan can be authored and a single focused
vertical slice can begin under the existing protected-PR process.
