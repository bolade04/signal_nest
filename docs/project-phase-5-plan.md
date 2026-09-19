# Project Phase 5 — Option B "Guided Action" — Master Plan

**Status:** PLANNED / DOCS-ONLY. This document authorizes nothing. It was authored
under a docs-only operator authorization (retained verbatim, SHA-256
`5257cc80b6cb60d878382d22f49cdaca650ff384b57ec3342bf567156bed87b0`, 5894 bytes)
against the clean baseline HEAD `5d978d8d59be425444ba1196217be62857705090`, tree
`68b4e6a6c6dec11b0fe5e53246f2325d0d96d812`. **Project Phase 5 implementation is
authorized by nothing in this repository** and remains blocked until Phase 4B-B,
INFRA-9 live deployment, and Phase 4B-C are completed or formally dispositioned
under separate authority.

**Authored:** 2026-09-01. **Source of truth for feasibility:** the Project Phase 5
definition audit (verdict `PROJECT_PHASE_5_DEFINED__OPTION_B_GUIDED_ACTION__PLAN_AUTHORING_READY`,
feasibility `FEASIBLE_WITH_NAMED_CORRECTIONS`), whose two report-scope corrections
were re-adjudicated `DEFINITION_AUDIT_CORRECTIONS_CLEAN/PASS` before this plan was
written (record: operator files, `CORRECTIONS-REVIEW.PP5-PLAN.md`, SHA-256
`2f67ba9c32c6276a63c558936bbc32b1bbe4e5f9e21912e20dc9f4b14db77966`).

**Reconciled:** 2026-09-01, under the operator's continuous-plan reconciliation
authorization (retained verbatim, `AUTHORIZATION.PP5-CONT.verbatim.md`, SHA-256
`bf7179c8877af9b2f4104e3a1f5d5437c803031d79b65e917fad31d2bace83c7`, 7230 bytes).
All eleven operator decisions O-1…O-11 are now RESOLVED and recorded in the
immutable decision record `DECISIONS.PP5-O1-O11.md` (SHA-256
`170547271258fa00199f20139c81bef1e5b8a9e4c5b2b039cf7a8ba00de7f103`, 6080 bytes;
operator round directory). **O-11 was REJECTED: the split schedule is not
operative.** The operative schedule is one continuous round with a **maximum
ceiling of 26 working days for the complete Phase 5A–5E scope** (§5), closing
earlier if every gate passes earlier. **No full-scope path fits 10–15
working days**; that figure applied only to the rejected split's committed-round
subset and appears below solely inside the clearly labeled rejected alternative.

---

## 1. Naming (binding)

- **Project Phase 5** is the product phase. Milestones are **Phase 5A**–**Phase 5E**.
- **Bare `P5` must never denote the product phase.** The identifier `P5` is a live
  governance predicate in `scripts/completeness_framework.py` (a P1–P9 series);
  the collision is real and the prohibition exists because of it.
- **"Batch 5" is retired as a roadmap label.** The string has three distinct senses
  in this repository: (1) the delivered Phase 3A.4b Batch 5 — historical records,
  never to be edited; (2) the formerly-undefined Phase 3B-track roadmap successor —
  now defined as Project Phase 5; forward-looking occurrences were corrected in the
  same round that authored this plan; (3) one infra-local occurrence of unresolved
  provenance (`infra/aws/modules/ecs/migration_contract.tftest.hcl:2`) — left
  untouched pending provenance. Rule for any future edit: correct a sense-2
  occurrence only when it is a forward-looking status/steering assertion; never
  edit sense-1 records, signed acceptance/gate-evidence rows, or sense 3.

## 2. Binding product loop and exclusions

**The loop (binding):**

```
signal → evidence-backed recommendation → human approval → text generation
       → content review → audited manual export
```

**Hard exclusions (all of Project Phase 5):** no live ad-platform integration; no
automatic publishing; no ad spend; no billing, subscriptions, payments, or monetized
entitlements (safety quotas and LLM cost ceilings only); no image, video, or audio
generation (text-first); no new external connector; no production activation. PR #34
(live RSS) remains untouched and off-limits. All new **capabilities** ship dark: every
new feature flag defaults `False` globally and fails closed.

**Content-safety ownership (binding planning rule):** the customer workspace's
authorized reviewer/approver owns final approval of generated marketing material;
SignalNest supplies automated claim-safety, policy, provenance, isolation, quota,
and content-review gates; SignalNest operators govern systemwide safety policy and
kill switches; SignalNest personnel do not manually approve every customer draft.

**Governance-plane note (recorded interpretation):** the exact-S9 capsule's ledger
and locator contain a permission field `"phase_5": false` in the capsule's own
permission vocabulary. That field governs **capsule-plane authority only** (the D8
value-authority state machine). It does not govern, and is not contradicted by,
repository planning or future separately-authorized Project Phase 5 implementation,
whose authority derives from operator authorizations, not from the capsule. This
interpretation is recorded here deliberately so it is never silently assumed.

## 3. Milestones

| Milestone | Deliverable | Sub-plan |
|---|---|---|
| **Phase 5A** | Recommendation foundation: immutable recommendation-brief entity with persisted LLM provenance; LLM service corrections; `recommendation_briefs_enabled` (dark) | `docs/project-phase-5a-recommendation-foundation.md` |
| **Phase 5B** | Approval workflow + audit spine: append-only approvals, exact-set approver gate, membership/role assignment, closed audit-action vocabulary, typed audit read API | `docs/project-phase-5b-approval-audit-spine.md` |
| **Phase 5C** | Text creative generation: immutable creative drafts, generation job, output-side content review, LLM tenant identity + sanitization + cost ceilings + quotas; `creative_generation_enabled` (dark) | `docs/project-phase-5c-text-creative-generation.md` |
| **Phase 5D** | Multi-location campaign context + jurisdiction propagation: campaign contract hardening, normalized jurisdiction resolution (fail-closed) | `docs/project-phase-5d-multi-location-jurisdiction.md` |
| **Phase 5E** | Notifications (in-app), status analytics (derived, read-only), audited manual export; `content_export_enabled` (dark); round closeout | `docs/project-phase-5e-notifications-analytics-export.md` |

**Phase 5A + Phase 5B form a sanctioned early stopping point** (§9) if later
milestones cannot safely proceed. The early-stop checkpoint is a **recovery
boundary only** — it is never Phase 5 closeout (§9).

**Operator decision O-11 — RESOLVED 2026-09-01: split REJECTED; single
continuous round ADOPTED** (`DECISIONS.PP5-O1-O11.md`). One continuous round of
**at most 26 working days** delivers all of Phase 5A–5E under identical lane,
freeze, review, and exact-stop machinery. Complete Project Phase 5 closeout
occurs **only** at the end-of-round closeout, when every 5A–5E exit criterion
is TRUE with a cited artifact and the fresh final-review verdict is recorded;
no earlier record — including the D8 5A+5B early-stop checkpoint — counts as
Phase 5 closeout; **and nothing built activates under this selection.** The
previously recommended split (≤15-day committed round +
6-day fast-follow, 21 working days total) is retained in §5 **only as a
clearly labeled rejected alternative**. 5D remains an **activation**
precondition for 5C and for Phase 5D itself regardless of schedule shape (§10).

## 4. Dependency graph and critical path

```
CF-1 (5A+5B contracts freeze; O-1/2/3/4/6/10 resolved 2026-09-01)
  └─► R1 (contracts review) ─► 5A build ─► I-5A ─► R2
        └─► 5B build ─► I-5B ─► R3 ─► EARLY-STOP CHECKPOINT (recovery
              │                       boundary only — never Phase 5 closeout)
              │                       + operator continue-window
              └─► CF-2 (5C contracts) ─► CF-2 review ─► 5C build ─► I-5C ─► R4
                    └─► CF-3 (consolidated freeze: 5D campaign/jurisdiction
                          + export + notifications + analytics + retention
                          contracts) ─► CF-3 review ─► 5D build ─► I-5D ─► R-5D
                                └─► 5E build (export + notifications +
                                      analytics, one four-lane window) ─► I-5E
                                      └─► R-5E + R-final (one frozen snapshot)
                                            └─► PHASE 5 CLOSEOUT (end of round)
```

- 5B depends on 5A (approvals reference briefs; the approver gate must exist before
  anything can be "approved").
- 5C depends on 5B (generation is gated behind approval; content review blocks the
  approval transition) and on CF-2.
- 5D depends on I-5C and R4 clean (its lanes touch files 5C's window owns —
  `campaign_context/routes.py`, `creative/service.py` — and its contracts
  freeze CF-3 merges only after R4 closes).
- 5E depends on 5D (briefs/drafts/reviews/exports snapshot the jurisdiction
  axis 5D builds; in this continuous shape the `exports` table and service are
  born jurisdiction-aware — no NULL-jurisdiction export cohort exists, an
  improvement over the rejected split) and on 5C (an export binds an exact
  approved creative version); 5E-notifications/analytics also consume 5A/5B
  events.
- 5D's fail-closed jurisdiction resolution remains an **activation**
  precondition for 5C and for Phase 5D itself (§10). Nothing may activate
  before the round completes anyway (Phase 4 remainder, §10).

**Critical path (continuous round) — gates included, because gate days
serialize onto the path (§5):** CF-1 → R1 → 5A → I-5A → R2 → 5B → I-5B → R3 →
early-stop checkpoint / operator continue-window → CF-2 → CF-2 review → 5C →
I-5C → R4 → CF-3 → CF-3 review → 5D → I-5D → R-5D → 5E → I-5E → R-5E ∥ R-final
→ Phase 5 closeout.

Every review gate, every integration merge, and the operator continue-window
sit **on** the critical path. Omitting them would understate the round's
central measured constraint, which is precisely that gates cannot overlap
builder work (§7.2).

## 5. Execution schedule — one continuous round, maximum 26 working days for the complete Phase 5A–5E scope

**26 working days is a ceiling, not a duration.** Closeout is declared on the
earliest day every 5A–5E exit criterion, integration, required CI check,
independent review, remediation obligation and closeout condition is satisfied. No
step may be skipped, weakened, improperly overlapped or artificially delayed to
reach day 26; equally, no gate may be compressed to reach an earlier day.

**Ceiling provenance (26 working days, selected 2026-09-02).** The operator selected
26.0 after three independent derivations of the pessimistic schedule returned 25.29,
25.85 and 25.10 working days. **The adopted measured minimum is 25.10.** The ceiling
was set above all three so that the most conservative charging of every unsourced
allowance still leaves the required margin. Earlier ceilings of 21 and 23 are
historical and superseded; see §12 (O-11) for the full supersession chain.

**No full-scope path fits 10–15 working days**, and none fits 17–18 either. Both
figures appear in this plan's history and both are superseded. The 10–15-day figure
belonged only to the rejected split's committed-round *subset* (see the labeled
rejected alternative at the end of this section). The 17–18-day figure was measured
with the integration-owner merge cycles **omitted from the calendar**; charging them
raises the round above 21 days at the former lane structure, which is why §6's lane
rebalance is a precondition of this schedule rather than an optimisation of it.

The schedule is expressed in working days D1–D26 from the first working day after a
future implementation authorization (which does not exist yet). It assumes the
disciplined multi-agent workflow of §6 — four concurrent builder lanes, one
integration owner, dedicated reviewer waves — not solo sequential work.

**Measured scheduling constraints (independently re-derived three ways).** Under the
mandated review rule — no workspace edits by anyone while a review wave is open
(§7.2) — gate days serialize: a wave, its fixes, and a contracts freeze cannot
overlap. A four-lane integration merge cannot overlap the builder work it merges.
The integration owner is one serial identity at all nine merge points. Under those
rules:

| Charge | Days | Basis |
|---|---|---|
| Builder windows (rebalanced, §6) | **9.50** | longest-lane duration per milestone, half-day grid; 5A 1.5, 5B 1.5, 5C 2.0, 5D 1.5, **5E 3.0** (`docs/project-phase-5e-notifications-analytics-export.md` §5) |
| Review waves — **nine** waves in **8** calendar slots × 0.5 | 4.00 | R-5E ∥ R-final run concurrently and share the D23 PM slot |
| **Integration cycles, 9** | **5.78** | **not 9 × 0.50 — a cycle does not fit a half-day slot. See below.** |
| CF-1 authoring | 1.00 | 19 audit call sites into a new registry |
| CF-2 authoring | 0.50 | |
| Early-stop checkpoint record | 0.50 | must follow R3 closure — cites its verdict and post-merge SHA |
| **Operator continue-window** | **0.50** | exogenous decision on the critical path, formerly charged zero |
| PR #34 adjacency check | 0.25 | co-located with the CF-2 shared surface at **D10 AM** (§6.3) |
| Closeout record | 0.50 | |
| **Shared frontend contract surface** (freeze-owned, §6.3) | **0.75** | **three** freezes: CF-1 0.27 + CF-2 0.10 + CF-3 0.38. The *surface* is 0.70; the 0.75 total adds the 0.05 `notifications.tsx` shell |
| **Total charged (adopted)** | **23.28** | pessimistic **25.37** — derived below |
| **Placed contingency** | **0.50** | D15 AM — transferable reserve, before the CF-3 freeze |
| Disclosed grid slack | 0.72 | unused fractions of the three shared-surface slots (0.23 + 0.15 + 0.12 = 0.50) and of the three integration-overflow slots (0.219) |
| **Tail spare** | **1.50** | D25 PM–D26 PM — **reachable**, see the ledger below |
| **Ceiling (superseded)** | ~~26.00~~ | 23.28 + 0.50 + 0.72 + 1.50 — **withdrawn**; see the operative ceiling below |
| **OPERATIVE CEILING (operator-selected)** | **35.00** | maximum, not a hold. Measured minimum **33.92** = pessimistic work **33.42** + mandatory contingency **0.50**. Residual headroom **1.08**. Derivation in §5.1 |

*Not an additive row:* the three **integration-overflow** slots at D14 PM, D19 PM and
D24 AM hold 1.50 of grid, but they carry **no separate charge** — the 1.281 of
per-cycle spill they absorb is already inside the 5.78 integration charge above, and
their 0.219 residue is already inside the 0.72 grid slack. They are a **placement** of
work already charged, and adding them to this column would double-count them.

**Derivation of the pessimistic 25.37**, so no term is unsourced:

| Term | Δ | Source |
|---|---|---|
| adopted charged | 23.28 | the table above |
| shared contract surface 0.75 → **1.75** | +1.00 | §6.3 — the plan's own 2.333× pessimistic ratio (1.40 ÷ 0.60), applied to the corrected 0.75. **Unsourced allowance, charged conservatively** |
| CI p50 → p90 across nine cycles | **+0.09** | measured: (66.38 − 61.57) min × 9 = 0.72 h = 0.09 day |
| one substantive review finding | +1.00 | the plan's own per-finding cost (mechanics 1 below), at the minimum non-zero rate |
| **pessimistic charged** | **25.37** | |

**Plan-internal ceiling test: 25.37 + 0.50 required reachable margin = 25.87.** Note
carefully that **25.87 is *pessimistic + contingency*, not a pessimistic figure**; using
it as a pessimistic base double-counts the 0.50. The plan-internal pessimistic is
**25.37**.

### 5.1 Operative ceiling — 35.0 working days

The 26.00 ceiling above is **superseded**. It reflected only the charges this document
carried before the S19–S22 evidence chain identified the remaining registration,
governance and congruence work. The operative derivation, each term charged exactly
once:

| Term | Days |
|---|---|
| deterministic charged work (§5 ledger, re-derived five times) | **23.28** |
| plan's own pessimistic loading (surface 0.75→1.75, CI p50→p90, one substantive finding) | **+2.09** |
| **plan pessimistic** | **25.37** |
| S19–S21 reconciled remediation set (22 non-overlapping lines) | **+7.45** |
| less the contract-equality double-count against the plan's own surface loading | **−0.70** |
| S21 falsifier lines N1–N5 (capability pin pair, requirement-key closure, CHECK partition + discovery, checker placement, `badges.tsx`) | **+1.30** |
| **PESSIMISTIC WORK** | **33.42** |
| mandatory contingency, counted **once** | **+0.50** |
| **MEASURED MINIMUM** | **33.92** |
| **OPERATOR CEILING** | **35.00** |
| **residual headroom** | **1.08** |

**Adopted case: 23.28 + 3.60 + 0.55 = 27.43**, +0.50 = **27.93** — comfortably inside
35.0. §7's requirement is tested against the **pessimistic** case, which is 33.92.

**The ceiling is a maximum, not a hold.** If every adoption gate passes earlier, the
round closes earlier; no work may be added to consume the remainder.

**O-9 contributes 0.00 days to every line above** — see §9 O-9.

**Why the integration charge is 5.78 and not 4.50 — the cycle does not fit its slot.**
The former 9 × 0.50 rested on measured p50 last-push-to-merge quantized up to a
half-day. That measurement **begins at the last push** and therefore excludes every
pre-push task: four-lane reconciliation, the exact-filename-list disjointness check
(§6.1), `scripts/gen-types.sh` and `openapi-typescript` regeneration, contract-drift
verification, and hermetic affected-test execution (§7.4). Charging the same interval
as the whole cycle repeats, one level up, precisely the error corrected when the
figure was raised from 0.25 to 0.50. Measured arithmetic: a 0.50-day slot is 4.0000 h;
measured last-push-to-merge p50 is **3.7635 h**; the residual left for **all** active
integration-owner work is **0.2365 h — 14.2 minutes per cycle**. That is not credible.
Charged honestly by merge-point class — freeze merges 0.595, four-lane merges 0.689,
the closeout merge 0.551 — nine cycles total **5.78**. Per-cycle spill above the
half-day slot (0.095 × 3, 0.189 × 5, 0.051 × 1 = **1.281**) is absorbed by the three
placed integration-overflow slots rather than hidden.

**Active integration-owner work per cycle is NOT RECOVERABLE from repository or
GitHub data**, because the measurement window opens at the last push. Three
independent derivations produced 5.19, 5.50 and 5.78 for the nine cycles, differing
only in that term; **the most conservative is charged.**

**Merge-cycle provenance (re-measured 2026-09-02, read-only GitHub, GET only).**
Last-push-to-merge across **n=12, PRs #144–#158** — the sample this plan has always
cited — gives p50 **3.7635 h**, p75 5.42, p90 8.99, max 11.65. This **confirms** the
previously unverified 3.77 h figure to within 23.4 seconds on the plan's own stated
anchor. It does **not** support charging that interval as the whole cycle: decomposed,
it is CI **1.026 h** + approval window **2.704 h** + merge action **0.033 h**, and the
active-versus-idle split *inside* the approval window is **NOT RECOVERABLE** from
timestamps. No working-hours normalization is applied or permitted: merges in the
sample land at 01:51, 02:46, 04:04, 05:21 and 22:50 local and across weekends, so
there is no diurnal or weekly pattern to subtract.

**No approval wait is treated as free.** Every one of the nine approval waits is
charged in full as strictly serial dependency time. **None is classified as
parallelizable in the baseline schedule**, because each downstream milestone's own
entry criterion requires its predecessor's review wave clean — see the 5B, 5D and 5E
sub-plans §3 — and *"integrated"* additionally requires the merge commit itself.
Eight of the nine merge points are immediately succeeded by a formal review wave and
the ninth has no successor at all. The one unconditional recovery available is
**packing, not overlap**: the PR #34 adjacency check (0.25) and the CF-2 shared-surface
work (0.10) together charge 0.35 and fit the single D10 AM slot, and the adjacency
check still precedes the CF-3 cycle as §6 requires.

**CI facts (re-measured, n=29 successful post-regime runs).** The
`Revision reader (unit, IaC contract, in-image)` job dominates and is effectively the
whole run: min **42.72**, p50 **61.57**, p75 62.52, p90 **66.38**, max **68.82** min,
mean 59.26; every other job is **≤2.80 min** (measured: `Backend quality` 2.80,
`Frontend quality` 1.08, `Container build and security` 1.00, `Migrations and API
contract` 0.80, `Integration smoke` 0.55). An earlier figure of "mean 61.6, p50 65.4,
max 68.8, n=10" is **not fabricated** — the max is exact and the triple is reproducible
from ten-run subsets of this sample — but **the exact ten runs behind it are not
recoverable from repository bytes**, and its p50 is 3.83 min high against the larger
sample. That is disclosed rather than silently replaced.

Ruleset 18820692 (verified live): **active**, **zero bypass actors**, **four** required
contexts — `Frontend quality`, `Backend quality`, `Migrations and API contract`,
`Integration smoke` — with `require_last_push_approval`,
`dismiss_stale_reviews_on_push`, `required_review_thread_resolution` and
`strict_required_status_checks_policy` all true and
`require_extra_approval_for_unattributed_changes` **true**. **`Container build and
security` and `Revision reader (unit, IaC contract, in-image)` exist as jobs but are
NOT required**, so O-9 is correctly recorded as decided-but-unperformed; the ~1-hour
CI budget above assumes O-9 is performed before D1, which §5's condition (iii)
requires. **Inside every merge cycle the order is forced: regenerate → CI → approve →
merge, with no push after approval** — a post-approval push dismisses the approval and
restarts the full required-check run. Note also that the n=12 baseline was produced
under a **one-approver** regime; with
`require_extra_approval_for_unattributed_changes` true, a squashed four-lane
integration containing any unattributed commit needs **two** approvals, so that
baseline may understate the four-lane cycles. The magnitude is **NOT RECOVERABLE**
and the precondition is unresolved (below).
### Operative continuous round (maximum ceiling: 26 working days; half-day granularity throughout)

**The 26-day figure is an upper bound, not a target or a waiting period.** If every
deliverable, integration, required CI check, independent review, remediation
obligation and closeout condition passes earlier, closeout is declared **immediately
on that earlier day**. No step may be skipped, weakened, improperly overlapped, or
artificially delayed to reach day 26. Equally, no gate may be compressed to reach an
earlier day. **Earlier closeout is mandatory, not optional**, when every gate has
passed.

**The ceiling is conditional**, and the conditions are named rather than implied:
(i) the operator's continue decision is recorded within its allocated **D9 AM** window;
(ii) lanes are staffed so they may straddle the frontend/backend boundary (§6). Derived
from §6.2's measured pure-stack figures against the **operative** window set: pure-stack
5B is 1.63, which needs a **2.0**-day window where straddling fits 1.38 into 1.5;
pure-stack 5E is 2.39 + 0.52 = 2.91, which **does** fit the operator's 3.0-day window.
Pure-stack total = 1.5 + **2.0** + 2.0 + 1.5 + 3.0 = **10.0 builder lane-windows against
the 9.50 this grid allocates — a 0.50-day shortfall, concentrated entirely in 5B — and
it does not fit.** (The 9.5-vs-8.5 pair quoted in §6.2 is the *pre-increment*
comparison and is retained there for that purpose; it must not be read against the
9.50 operative budget, which it coincidentally equals.);
(iii) **O-9 is performed before D1** — promoting the exact emitted status-check contexts
`Container build and security` and `Revision reader (unit, IaC contract, in-image)` to
required contexts. **These are the jobs' `name:` values, never their YAML keys**; a
required context matches a check-run name, so promoting `container-build` or
`revision-reader` — or the reader's name truncated of its
` (unit, IaC contract, in-image)` parenthetical — names a string no check run ever
emits and would leave every PR permanently unmergeable (§9 O-9). Currently recorded but
**not** performed. **O-9 carries a measured schedule effect of 0.00 working days**, so
its non-performance is **not** a §8 breach;
(v) **the pre-existing 87→88 secret-inventory drift is dispositioned before D1.** §14
records this as *"blocking CF-1"*, and CF-1 is authored **D1 AM** — so it is a pre-D1
external precondition of exactly the same kind as (iii), which this plan cannot itself
clear because the inventory document is freeze-owned and outside the Phase 5
documentation surface. Its failure carries the same §8 breach classification as (ii).
**It does not share (iii)'s classification, because (iii) now carries no charge.** It
was previously recorded only in §14 and absent from this list;
(iv) the **adopted** total of 23.28 assumes every one of the nine review waves — held in
eight calendar slots, since R-5E and R-final share D23 PM — returns
clean on first pass — which this plan does **not** treat as a base case: the pessimistic
total charges **one** substantive finding at its full 1.0-day cost (below). Operator
latency beyond (i) shifts the remainder day-for-day and does **not** draw on
contingency. Failure of (ii) is a schedule breach under §8 — report and stop; **failure
of (iii) is not**, because O-9 is measured at 0.00 days and adds no charge to any cycle;
never compress a review gate to absorb it. A **second** substantive finding, beyond the
one the pessimistic case carries, is likewise a §8 breach.

| Day | AM | PM |
|---|---|---|
| **D1** | CF-1 authoring — 5A+5B contracts freeze (enums, models, migrations, `recommendation_briefs_enabled` flag + capability member, `require_exact_roles`, `approvals` + `AuditAction` registry). Encodes resolved O-1/O-2/O-3/O-4/O-6/O-10; per O-4 the briefs flag joins `FeatureFlagsOut` with the exact-equality reflection test updated in the same commit | CF-1 authoring (cont.) — its 5B payload enumerates 19 audit call sites into the registry |
| **D2** | **CF-1 shared-surface authoring** (§6.3, 0.27) — the integration owner authors this freeze's endpoint wrappers, type aliases, query keys and API-surface stubs, so `gen-types.sh` can run at freeze time. **No `App.tsx` route or `nav.ts` entry is authored here** — mounting lands at the integration cycle that merges the page directory (§6.3) | **CF-1 integration cycle (1 of 9)** — reconcile, regenerate `openapi.json`/`schema.d.ts`, required CI, non-author approval, merge, post-merge exact-SHA verification |
| **D3** | **R1** contracts review wave | **5A build** (W1–W4) |
| **D4** | **5A build** | **5A build** — 5A window totals **1.5** |
| **D5** | **I-5A integration cycle (2 of 9)** | **R2** wave on the frozen I-5A head |
| **D6** | **5B build** | **5B build** |
| **D7** | **5B build** — 5B window totals **1.5** | **I-5B integration cycle (3 of 9)** |
| **D8** | **R3** security-weighted wave | **Early-stop checkpoint record** (§9) — a recovery boundary only, never Phase 5 closeout; authored after R3 closes so it can cite R3's verdict and the post-merge SHA |
| **D9** | **Operator continue-window** — an exogenous decision, charged its own half-day; CF-2 is not authored until it is recorded | **CF-2 authoring** (5C contracts) |
| **D10** | **CF-2 shared-surface authoring** (§6.3, 0.10 — the `pages/creative/` type aliases, endpoint wrappers and query keys; **its route and nav entry mount at I-5C**) **+ PR #34 adjacency check** (0.25) — **0.35 charged in one 0.50 slot**; the adjacency check still precedes the CF-3 cycle at D16 AM as §6 requires | **CF-2 integration cycle (4 of 9)** |
| **D11** | **CF-2 contracts review** wave | **5C build** |
| **D12** | **5C build** | **5C build** |
| **D13** | **5C build** — 5C window totals **2.0** *(the owner pre-authors the consolidated CF-3 payload on an owner-exclusive branch across the D11 PM–D13 AM window; it holds no merge point here)* | **I-5C integration cycle (5 of 9)** |
| **D14** | **R4** security-weighted wave | **Integration overflow 1** (0.50) — absorbs the accumulated spill of cycles 1–5 (0.757) **net of the 0.38 of grid slack banked ahead of it at D2 AM (0.23) and D10 AM (0.15)**, leaving 0.377 against this 0.50 slot |
| **D15** | **CONTINGENCY — the round's placed transferable reserve (0.5 day)** | **CF-3 shared-surface authoring** (§6.3, 0.38) — the largest share of the surface, including `queryKeys.ts` gaining its per-user scope dimension for 5E notifications. **The routes and nav entries for `pages/campaigns/` and `pages/analytics/`, and the `notifications.tsx` shell replacement, do NOT land here** — they mount at I-5D and I-5E respectively (§6.3) |
| **D16** | **CF-3 integration cycle (6 of 9)** — the consolidated freeze: 5D campaign hardening (`CampaignStatus`, `campaign_locations` per O-8) + jurisdiction axis columns, the `exports` table with `jurisdiction_code` native from birth, `content_export_enabled` + `Capability.CONTENT_EXPORT` (+ `FeatureFlagsOut` + exact-equality test per O-4), notifications + preferences tables, retention `deleted_at` columns, new `AuditAction` members, the root `conftest.py` import line for `tests/fixtures/analytics.py`, capability pin-tests in the same commit, and the secret-inventory recount (§14) | **CF-3 contracts review** wave |
| **D17** | **5D build** | **5D build** |
| **D18** | **5D build** — 5D window totals **1.5** | **I-5D integration cycle (7 of 9)** |
| **D19** | **R-5D** security-weighted wave (fail-closed jurisdiction, isolation, campaign hardening) | **Integration overflow 2** (0.50) |
| **D20** | **5E build** | **5E build** |
| **D21** | **5E build** | **5E build** |
| **D22** | **5E build** | **5E build** — 5E window totals **3.0** |
| **D23** | **I-5E integration cycle (8 of 9)** | **R-5E** (export/notifications/analytics) ∥ **R-final** (whole-round, reviewers with no prior round role) — concurrently on one frozen snapshot, two rosters, permitted because no workspace edits occur during either |
| **D24** | **Integration overflow 3** (0.50) | **Phase 5 closeout record** — the only record in the round that may assert `PROJECT_PHASE_5_BUILD_COMPLETE`, and only with every 5A–5E exit criterion TRUE with a cited artifact and the R-final verdict recorded verbatim |
| **D25** | **Closeout integration cycle (9 of 9)** | Tail spare |
| **D26** | Tail spare | Tail spare — 1.5 day total across D25 PM–D26 PM; **reachable** (see the ledger below) |

**Per-window ledger — DERIVED FROM THE DAY TABLE ABOVE, WHICH IS AUTHORITATIVE.**
An earlier draft asserted window sizes here independently of the day table; the two
disagreed and the total balanced only because the errors cancelled, hiding a negative
float. The day table is the single source and every figure below is counted from it.
Any future edit must change the table first and re-derive this ledger, never the reverse.

| Window | Calendar days (counted from the table) | Longest lane | Local float | Float usable here? |
|---|---|---|---|---|
| 5A (D3 PM–D4 PM) | 1.5 | 1.15 | +0.35 | yes, within 5A only |
| 5B (D6 AM–D7 AM) | 1.5 | 1.38 | **+0.12** | yes, within 5B only — **the round's tightest window, and the plan's accepted minimum local float** |
| 5C (D11 PM–D13 AM) | 2.0 | 1.57 | +0.43 | yes, within 5C only |
| 5D (D17 AM–D18 AM) | 1.5 | 1.30 | +0.20 | yes, within 5D only |
| 5E (D20 AM–D22 PM) | **3.0** | **2.18** | **+0.82** | yes, within 5E only — of which **0.50 is the operator-designated reachable 5E remediation reserve** |
| **Builder total** | **9.50** | 7.58 | | |

**5E is 3.0 days — an operator decision recorded 2026-09-02, not an estimating
result.** The window carries the entire 5E scope plus a **0.50-day reachable
remediation reserve**, which may not be relabelled as tail-only float or used to
conceal work assigned elsewhere.

The measured analytics test increment of **0.52 lane-days**
(`docs/project-phase-5e-notifications-analytics-export.md` §5, which holds the
per-artifact table and is the authority for this figure) is charged **once**.
**All four analytics artifacts test 5E-W2's production code, so under §6.1 they may be
owned by W1 or W4 only** — never W2, which authored the code, and **never W3, which
under §6.1 authors no tests at all**. An earlier assignment gave the six-family
fixture and the empty-state file to 5E-W3, which violated that rule and was the sole
reason the longest lane reached 2.02. Under the compliant assignment the lane loads
are **W1 2.18, W2 1.80, W3 1.67, W4 1.90** (total 7.55) — W3 having also shed
`apps/web/src/components/layout/notifications.tsx`, which is freeze-owned (§6.3).
**The longest lane is W1 at 2.18 against a 3.0-day window: local float +0.82.**

**The float threshold that once excluded a cheaper window was factually wrong, and is
withdrawn.** An earlier version of the 5E sub-plan justified its threshold as
*"Local float ≥ +0.20 (as 5B/5C/5D carry)"*. **5B carries +0.12** — see the ledger
above, and `docs/project-phase-5b-approval-audit-spine.md` §5, which calls it *"the
round's tightest window"*. 5C (+0.43) and 5D (+0.20) do exceed 0.12; **5B does not**,
so the parenthetical was false as written and the "+0.20 precedent" it asserted does
not exist. The plan's **accepted minimum local float is +0.12**, and the only binding
condition is the one stated below: no window carries a *negative* local float. This
correction is recorded because a fabricated precedent was used to support a schedule
conclusion, which is a more serious failure than an arithmetic error. **It is entirely
distinct from the O-11 ceiling attribution in §12, which was independently verified
and is genuine** — the two must never be conflated.

**A superseded derivation is recorded here because it inflated this figure.** An
earlier draft read the estimating analyst's sensitivity table — which showed what
each lane *would* reach if that single lane absorbed the whole 0.52 (W1 2.23,
W2 2.32, W3 2.24, W4 2.17) — as a simultaneous assignment, and charged 0.52 to all
four lanes. That booked **2.08** lane-days for a measured 0.52 increment and grew
5E-W2 despite it owning none of the new artifacts. The four figures were a
*whichever-lane-takes-it* display whose point was that no lane held 0.52 of float
inside 2.0 (the largest was W4's 0.35). The correct charge is the one above.

**The +0.82 float is not idle slack.** 0.50 of it is the operator-designated reachable
5E remediation reserve. The remainder is identified downside cover: the same analysis
warns these figures are most likely too **low** if 5E-W4's 1.65 base predates analytics
dashboards as a surface, which would add ~0.28 for the web `__tests__/`. Both parts are
spendable only inside 5E and cannot cross the contract freeze.

**No window carries a negative local float.** That is a success condition of this plan,
and it is checked by counting the table rather than by assertion.

**On the nature of window-internal slack — this distinction is binding.** The positive
local floats above are **surplus builder capacity inside a window**, spendable only on
that window's own work, before its contract is frozen. They are estimate variance, not
schedule reserve, and they **cannot be transferred across a contract freeze** to cover
a later deficit. The round's **transferable** reserve is the placed 0.5 day at
**D15 AM**; the 1.5-day tail spare at D25 PM–D26 PM is additionally reachable, for the
reason given next.

**The tail spare IS reachable float — a correction to this plan, in its own favour.**
An earlier version of this section asserted that tail spare *"cannot cover an earlier
dependency-constrained deficit"* and excluded it from the margin. That is wrong for
this round's actual topology, but **not for the reason first given here**. An earlier
version of this paragraph argued that the critical path equals the total charged work,
so the round must be a pure serial chain with no parallel branch. **That argument is
withdrawn: it is true by construction and proves nothing.** The charge table prices
builder windows at *longest-lane duration per milestone*, so the three non-longest
lanes in every window are never charged — the charge total **is** the critical-path sum
by definition, and cannot be evidence about topology. The round in fact has at least
three parallel branches, two of which this plan schedules deliberately: four concurrent
builder lanes per window (27.84 lane-days of measured need against 7.58 on the path),
the owner's CF-3 pre-authoring across D11 PM–D13 AM, and R-5E ∥ R-final sharing D23 PM.

**The correct ground is different, and it holds.** The round has a **single finish
milestone and no intermediate hard deadline** — §8 exempts the only candidate, the
D9 AM operator continue-window, by shifting the ceiling day-for-day rather than drawing
on reserve. Therefore a delay on *any* branch, once its local float is exhausted,
propagates to the finish, where tail capacity absorbs it. Parallel branches do not
strand deficits here; they absorb small ones locally, which is strictly favourable.
The tail spare is therefore counted toward the margin.

**Buffer beneath the ceiling: 26.00 − 23.28 = 2.72 day**, decomposed as **0.50** placed
transferable contingency (D15 AM) + **0.72** disclosed grid slack + **1.50** tail spare.
The required minimum of +0.50 genuine reachable margin is met by the placed contingency
alone. Under the pessimistic case (25.37) the residue is **0.63**, of which the 0.50
contingency survives intact.

**Disclosed schedule mechanics (each is load-bearing and reviewable):**

1. **One substantive review finding costs 1.0 day, and the round now carries exactly
   one.** The cost sequence is forced by the measured ruleset: a 0.5-day fix slot
   (fixes are edits, so under §7.2 they displace builder work rather than overlapping
   it) → the fix push **dismisses the existing approval**
   (`dismiss_stale_reviews_on_push`, verified true) → a full required-check re-run
   bounded by the ~1-hour reader (p90 **66.38** min) → a **fresh non-author approval**
   (`require_last_push_approval`, verified true) → a 0.5-day re-review slot.
   **Budgeting zero findings across nine review waves and nine merge cycles on 27.84
   lane-days of measured new-code need is not a credible base case, and this plan does
   not claim it is.** (27.84 is the sum of the five sub-plans' lane tables — 5A 4.25,
   5B 5.33, 5C 5.92, 5D 4.79, 5E 7.55 — counted from those tables, not asserted.) The **pessimistic** total therefore charges one finding at its
   full 1.0-day cost; a **second** is a §8 breach trigger. **No substantive-finding
   rate exists for this repository and none is derivable** — one is the minimum
   non-zero rate, charged conservatively.
1a. **A finding at the D23 PM wave is the worst placement, and is named here because
   this plan previously left it unplaced.** R-5E and R-final run concurrently on one
   frozen snapshot *"permitted because no workspace edits occur during either"*, so a
   substantive R-5E finding **cannot be fixed until R-final also closes**. The forced
   1.0-day sequence then lands across D24–D25 and consumes tail spare.
2. **Reserve placement, counted from the table.** The transferable 0.5-day contingency
   sits at **D15 AM**, before the CF-3 freeze, and absorbs delays at or before it.
   After it, **D15 PM–D26 carry four integration cycles** (6 of 9 at D16 AM, 7 at
   D18 PM, 8 at D23 AM, 9 at D25 AM) **and three review slots** (D16 PM, D19 AM,
   D23 PM). Those are covered by the **1.5-day tail spare at D25 PM–D26 PM**, which
   **is** reachable float for the pure-serial-chain reason given in the ledger above,
   and by the three placed **integration-overflow** slots at D14 PM, D19 PM and
   D24 AM, which absorb per-cycle spill rather than schedule slip.
3. **Type generation is ordered, and the former circularity is resolved at the freeze,
   not in the window.** `scripts/gen-types.sh` runs `from app.main import app` then
   `app.openapi()`, so route code must exist **in the tree where the command runs**.
   Each CF-* payload therefore carries **API-surface stubs** — frozen response models,
   registered empty-bodied routers, and the matching `types.ts`/`endpoints.ts`
   entries — authored by the integration owner at freeze time. Frontend lanes then
   **consume a frozen contract** instead of racing to author one, and no lane waits on
   a mid-window regeneration. Each merge regenerates what exists at that point;
   **CF-1 regenerating no 5A routes is correct and requires no fix.** *(Scope note: this
   mechanic and that adequacy claim address the **backend** OpenAPI/type regeneration
   only. The frontend has its own ordering constraint — `App.tsx` imports page modules,
   so routes and nav entries mount at the integration cycle that merges the page
   directory, never at the freeze. See §6.3.)*
4. **A second approver may be required at every merge point.** Ruleset 18820692 sets
   `require_extra_approval_for_unattributed_changes = true`. A squashed four-lane
   integration containing any unattributed commit needs **two** approvals, and this
   plan staffs one non-author approver. Either every lane commit is attributed to a
   GitHub-linked identity, or a second approver identity must be staffed. **This is an
   unresolved precondition, not a solved problem.**
5. **`allowed_merge_methods` is `['merge','squash','rebase']`** — "protected squash
   merge" is this round's **convention**, not a ruleset-enforced constraint.
6. **The reserve is placed, not notional.** D15 AM is deliberately empty. If it is
   consumed, the round proceeds on tail spare alone and any further slip beyond that
   1.5 day is a §8 breach.
7. **No approval wait is parallelized in this baseline.** All nine are charged as
   strictly serial dependency time (see the charge-table note above). The single
   unconditional recovery is **packing** — PR #34 (0.25) beside the CF-2 shared surface
   (0.10) in the one D10 AM slot — which requires no overlap and no assumption about an
   unrecoverable active/idle split.

### REJECTED ALTERNATIVE (not operative; retained for the record only): the O-11 split schedule

Nothing below is operative, schedules anything, or may be cited as a delivery
commitment. The operator rejected this shape on 2026-09-01; it is kept only so the
record shows what was rejected. Under the adopted continuous round the operative day
grid, review budget, contingency, and the **sole** Phase 5 closeout point
(**D24 PM**) are defined by the operative table above; no D15, F1–F6, R5/R6 or R-F*
statement in this subsection may be cited for any purpose other than recording what
was rejected.

#### Rejected split, part 1 — the ≤15-working-day committed round (REJECTED — not operative)

**Reconstruction note (builder error, disclosed).** During this round's editing pass a
too-greedy replacement deleted rows **D2–D12** of this non-operative table (the D1 row
survives verbatim below; an earlier version of this note said "D1–D12", which
contradicted both the surviving row and the placeholder). The rows below
are the ones recoverable verbatim; the deleted intermediate rows are **not**
reconstructed, because fabricating them would be worse than recording the loss. No
operative statement anywhere in this plan depends on them — this subsection exists
solely to record a rejected shape, and its part-2 table and the historical mechanics
paragraph below survive intact.

| Day | Work | Gate at end of day |
|---|---|---|
| **D1** | Round setup; **CF-1**: 5A+5B contracts freeze (enums, models, migrations, `recommendation_briefs_enabled` flag + capability member, `require_exact_roles`, `approvals` + `AuditAction` registry, OpenAPI/types regen). A deliberately full day: the contracts are pre-specified in the sub-plans, so CF-1 is transcription + migration authoring, but its 5B payload (19 audit call sites enumerated into the registry) is real work | CF-1 committed |
| *D2–D12* | *(rows lost to the editing error described above; not reconstructed)* | — |
| **D13** | AM: **R4** wave (security-weighted) on frozen I-5C. PM: fixes; **CF-3E** (export-only contracts: `exports` table, `Capability.CONTENT_EXPORT`, migration) — a small, disclosed third activity on a gate day; if R4 fixes consume the afternoon, CF-3E slips to D14 AM and consumes contingency | R4 clean |
| **D14** | AM: CF-3E review (if not done D13). AM–PM: **5E-core build** (export + closeout tooling; small surface, up to four lanes incl. tests) | **I-5E** integrated |
| **D15** | AM: **R5** (export scope) and **R6** (whole-round, reviewers with no prior round role) run **concurrently on the same frozen snapshot** — permitted because no workspace edits occur during either. PM: committed-round closeout record | Closeout |

#### Rejected split, part 2 — the 6-working-day fast-follow window (REJECTED — not operative)

| Day | Work |
|---|---|
| **F1** | **CF-3D**: 5D contracts (campaign hardening, `campaign_locations`, jurisdiction columns — including the `exports.jurisdiction_code` addition — + engine contracts) + same-day review |
| **F2–F3** | **5D build**, four lanes (campaign / jurisdiction / propagation / tests) |
| **F4** | AM: **R-F1** (5D wave). PM: R-F1 fixes; **CF-3N**: notifications + analytics contracts (small) + review |
| **F5** | Notifications + analytics build (two lanes) |
| **F6** | AM: **R-F2** wave. PM: fast-follow closeout record |

*Historical split mechanics (record only — none of this is operative):* the
split's committed round budgeted eight review events (R1, R2, R3, CF-2 review,
R4, CF-3E review, R5, R6) with four more in its fast-follow (CF-3D and CF-3N
reviews, R-F1, R-F2); it carried ≈1 uncommitted day of committed-round
contingency and gave the fast-follow none (any overrun a report-and-stop
breach); it placed the committed-round closeout at the end of its D15 and
fast-follow completion at its F6 — day labels that exist only inside this
rejected alternative. Under the adopted continuous round, the operative day
grid, review budget, contingency, and the **sole** Phase 5 closeout point
(**D24 PM**) are defined by the operative table above; no D15, F1–F6, R5/R6,
or R-F* statement in this subsection may be cited for any purpose other than
recording what was rejected.

## 6. Multi-agent delivery structure

**Four builder lanes** (per milestone; exact path lists in each sub-plan) with
**exclusive path ownership**: no two lanes may own or edit the same file, ever.

### 6.1 Rebalanced lane decomposition (binding; replaces the former L1–L4 split)

The former decomposition — backend-domain / API-service / frontend / **all tests**
— was measured to be structurally unbalanced: the tests lane was the **longest lane
in all five milestones** (spreads of 1.9×–3.6×) while the two backend lanes ran at
50–60% utilisation. Because a milestone's duration is set by its **longest** lane,
one overloaded lane paced the entire round. Tests dominate delivered volume in this
repository: the delivered opportunity-feedback feature measures **959 non-test lines**
against **2,680 lines** in its named test files — a **2.79:1** ratio, i.e. **73.6%**
of that feature's delivered volume. **A previously stated "2,141 test lines … 2.23:1"
and a "50–57% of delivered volume" figure do not reproduce from repository bytes and
are withdrawn**; the whole-tree ratio is lower (`apps/api/app` + `apps/web/src` =
21,450 test against 59,100 total, **36.3%**) because it averages over long-delivered
code, and a PR-diff basis would differ again. The argument needs only that tests exceed
**25%** of a feature's delivered volume, which every basis measured here clears by a
wide margin — so no allocation that keeps tests in one lane, or even two, can balance
four lanes each entitled to 25%.

The operative decomposition is therefore:

| Lane | Carries |
|---|---|
| **W1 Primary domain** | the milestone's largest new backend package, **plus a cross-authored behaviour-test slice for a peer lane's code** |
| **W2 Seam** | the LLM / auth / audit / jurisdiction / emission seams, **plus a cross-authored behaviour-test slice for a peer lane's code** |
| **W3 Primary UI** | the principal new UI surface **only — it authors no tests for its own code** |
| **W4 Adversarial + UI verification** | **the whole security / isolation / negative battery**, plus the co-located tests for W3's UI surfaces |

Two rules make this safe, and neither may be relaxed:

1. **Cross-authored test pairing — no exceptions, including UI.** No lane writes any
   test for code it owns in the same window. W1 authors the behaviour tests for W2's
   seam code and W2 authors them for W1's domain code; **W3 authors no tests at all**,
   and the co-located tests for W3's surfaces are authored by W4. An earlier draft of
   this section granted W3 "its co-located tests", which made every UI lane grade its
   own work in all five milestones — that exception is withdrawn.
   This preserves the independent-test-author property that a naive "each code lane
   writes its own tests" merge would destroy. The dependency class is unchanged from
   the former structure, in which the tests lane already tested peer code in-window
   against CF-frozen contracts.
2. **The adversarial battery stays whole in W4.** Security, isolation and negative
   tests are split by **subject dimension, never 50/50**, so a single consistent
   adversarial author covers the round. Splitting adversarial perspective across
   lanes is a reduction in obligation and is prohibited.

**Test ownership must be exact filename lists, not prefix globs.** Prefix globs
overlap (`test_briefs_*.py` subsumes `test_briefs_isolation.py`), so a prefix
partition is not disjoint and would silently orphan coverage. Each CF-* fixes an
**exact filename list per lane**, and every integration verifies mechanically that
**no test file matches zero lists**. This check is the named control preventing the
four-way split from quietly dropping an obligation.

### 6.2 Lane straddling and freeze-owned files

**Lanes may straddle the stack.** Exclusive ownership is a property of *paths*, not
of languages: a lane may own both Python and TypeScript paths (e.g. 5E-W2 owning
`apps/api/app/analytics/` and `apps/web/src/pages/analytics/` — one product vertical
end-to-end). **This is load-bearing.** Measured **before** the 5E analytics increment:
with pure-stack lanes the longest lanes are 5B 1.63 and 5E 2.39, giving a builder
duration of 9.5 lane-windows; with straddling permitted they are 5B 1.38 and 5E 1.80,
giving 8.5. Adding the measured 0.52 analytics increment
(`docs/project-phase-5e-notifications-analytics-export.md` §5) raises the straddling
figure to 9.00, and the operator's 3.0-day 5E window takes it to the operative
**9.50**. The pure-stack figure rises by at least as much —
the new artifacts fall inside the same lanes — so the ~1.0-lane-window gap is not
closed by the increment and straddling remains load-bearing. Adopting this plan
therefore commits to staffing lanes that can work across both stacks.

**Freeze-owned shared files.** These are owned by **no builder lane**; only the
integration owner edits them, and only during contract freezes (CF-*) and
integration windows (I-*): `apps/api/app/core/enums.py`,
`apps/api/app/core/config.py`, `apps/api/app/core/metrics.py` (closed metric
vocabularies), `apps/api/app/capabilities/registry.py`,
`apps/api/app/api/router.py`, `apps/api/app/jobs/status.py` (`JobType`),
`apps/api/app/jobs/handlers.py` (handler-registration imports),
`apps/api/app/system/routes.py` (`FeatureFlagsOut`),
`apps/web/src/App.tsx` (the single route table),
**`apps/web/src/components/layout/nav.ts`** (the sidebar `navItems` array — a
navigation contract of the same kind as the route table: `breadcrumbs.tsx` derives
its label map from it at module scope and `sidebar.tsx` renders it, so a new route
without a `nav.ts` entry is unreachable and its breadcrumb silently degrades),
**`apps/web/src/components/layout/notifications.tsx`** (the app-shell notification
surface — freeze-owned for exactly the `nav.ts` reason: it is imported at **module
scope** by `apps/web/src/components/layout/app-shell.tsx:9` and rendered at `:78`, and
`AppShell` is the layout route element at `apps/web/src/App.tsx:25`, so it renders on
**every authenticated page**. It is app-shell chrome, not an isolated feature
component, and no builder lane may own it — see §6.3),
`apps/api/alembic/versions/*`, `apps/api/openapi.json`,
`apps/web/src/api/schema.d.ts`,
`docs/operations/aws-staging-secret-inventory.md` (field-count reconciliation),
and every governance-pinned fixture at the **repository root**, `/tests/fixtures/*`.
**That glob is anchored at the repository root and does NOT match
`apps/api/app/tests/fixtures/`**, which is the lane-owned package 5A-W4 creates —
an unanchored `tests/fixtures/*` matched both and put a freeze-owned glob and a
lane-owned claim over the same paths.

**Nine further artifacts join the freeze-owned set (S19–S22).** Each was measured
unowned or mis-assigned, and each has a named gate:

| Artifact | Why freeze-owned | Gate if omitted |
|---|---|---|
| `apps/api/app/db/models.py` | the **sole reachability root** for `Base.metadata` (`alembic/env.py:21`); a hand-written import barrel with no autoloader. Every new model needs one line here | `alembic check` → **`Migrations and API contract`** (required) |
| `apps/api/app/db/seed.py` | executed **three times** inside the required migration job (`ci.yml:200,202,208`) and again by `ci-smoke.sh`; `seed.py:386` constructs `Campaign(status="active")` | `Migrations and API contract` **and** `Integration smoke` (both required) |
| `apps/api/app/core/tracing.py` | exact structural twin of freeze-owned `core/metrics.py`; `validate_span` **raises** on an unknown span or attribute key | `Backend quality` (required), where a span is exercised |
| `apps/web/src/lib/labels.ts` | manual duplication of every backend vocabulary; `label()` falls back to `titleCase` | **NONE — omission is silent.** Must additionally be covered by the contract-equality checker |
| `apps/web/src/components/common/badges.tsx` | the sole consumer of `labels.ts`'s maps; freeze-owning the map but not its consumer splits one surface across two regimes | **NONE — silent** |
| `apps/web/src/test/handlers.ts` | the single central MSW registry; all five milestones need it | `Frontend quality` (required), via `onUnhandledRequest:'error'` |
| `apps/web/src/test/fixtures.ts` | **new**; the CF-1 prerequisite extraction that makes fragment ownership acyclic (§6.4) | as above |
| `.github/workflows/ci.yml` | the enforcement plane for every other control | itself, plus the pin cascade below |
| `tests/test_i28s_command_roots.py` | holds `COMMENT_LINE = 407` and `assert len(roots) == 43`, both of which any `ci.yml` edit moves. It sits in `/tests/`, **not** `/tests/fixtures/`, so the glob above does not reach it | `Revision reader (unit, IaC contract, in-image)` — **advisory today**, merge-blocking after O-9 |

**Any change to `.github/workflows/ci.yml` requires an atomic same-commit payload**, not
a single re-pin. The dependent set is enumerated in §9 *CI pin cascade*; the previously
recorded single dependent
`/tests/fixtures/executed-state-contract.json:654` is **one of several**.

### 6.2.0 Model/migration congruence — one atomic payload per contract freeze

**BINDING, and it supersedes every migration-first/model-later reading in these six
documents.** For every CF cycle that creates or alters a table, a **single atomic
integration payload** must contain, entering together:

1. the **complete, exactly congruent** ORM model — not a stub, not a minimum placeholder;
2. every constraint, server default, index and relationship the migration declares;
3. the `apps/api/app/db/models.py` registration line;
4. the migration itself;
5. the generated or handwritten schemas and contracts that freeze requires;
6. the required negative and migration checks.

**A stub is strictly worse than no model.** `apps/api/alembic/env.py` sets
`compare_type=True` and `compare_server_default=True` and sets **no** `include_object`,
`include_name` or `include_schemas`, so the full database is compared against the full
`Base.metadata`. A table with no model emits `DropTableOp`; an incongruent stub emits
`remove_column`, `modify_type`, `modify_default`, `remove_constraint`, `remove_index` and
`remove_fk`. The **only** element a model may safely omit is a `CheckConstraint` — and
that omission is precisely the silent gate §9.2 exists to close.

**This applies to CF-1, CF-2 and CF-3 alike.** It is a single defect class with nine to
eleven instances, not one instance at CF-3, and a CF-1-shaped fix leaves CF-2 and CF-3
red. `ci.yml` `migration-and-contract` `steps[4]` runs `alembic upgrade head` and then
`alembic check` with nothing between them, inside the **required** context
`Migrations and API contract`.

**Two consequences that resolve contradictions elsewhere in this document set.**

* **Models are CF payload, authored by the integration owner.** Where a milestone lane
  table lists a `models.py` for a table its own freeze creates, the model moves to that
  freeze's payload. The lane retains the service, routes and schema work.
* **`apps/api/app/locations/models.py` moves to the CF-3 payload.** The 5D-W2 lane row
  claims it for the D17 AM–D18 AM window while the CF-3 migration adding
  `jurisdiction_code` runs at **D16 AM** — the database would gain the columns a day
  before the model does. `business_locations` is a **pre-Phase-5** table, so the
  carve-out for *"earlier milestones' tables"* does not reach it.
* **The `exports` model and its migration enter together at CF-3**, with
  `jurisdiction_code` `NOT NULL` and no sentinel (5D §2). No 5E lane may claim the model
  as unwritten work after CF-3.

**Every registration surface is assigned exactly once.** A surface with no named owner,
slot, charge and gate is a defect, not an integration detail.

### 6.2.1 Reconciled architecture decisions (S19–S22, operator-confirmed)

**Closed vocabularies — there are NINE, not five and not eight.** Independently
enumerated by a second source that had never seen the earlier counts:
`ApprovalDecision`, approval `reason_code` (+ its polarity frozensets), `subject_type`,
`AuditAction`, creative `channel`, `CampaignStatus`, `jurisdiction_code` + `UNRESOLVED`,
notification `event_code`, export `format`.

**Placement is by owning metadata/domain boundary, not one central enum module.** Six
bare vocabularies join freeze-owned `apps/api/app/core/enums.py`; the three
metadata-bearing ones go to their domain packages — `AuditAction` to
`apps/api/app/audit/actions.py`, the approval `reason_code` polarity sets to
`apps/api/app/approvals/`, jurisdiction to `apps/api/app/jurisdiction/`. Centralising
all nine would drag product-governance policy into `core/`, which every domain imports
and which is currently a leaf (`capabilities/models.py:50,72-75` calls
`persisted_values()` at class-definition time), and would put all three freezes on one
file.

**Precondition, and it is load-bearing: all nine must be enum-typed on their Pydantic
response schemas.** Otherwise they widen to bare `string` in `schema.d.ts` and no checker
has anything to compare. This is not hypothetical — `campaign_context/schemas.py:86`
types `mode: str` while `CampaignMode` exists at `core/enums.py:33` and appears **zero**
times in `schema.d.ts`.

**`llm_usage_records` goes in a new sibling package `apps/api/app/llm_usage/`.**
`apps/api/app/llm/` has **zero** `sqlalchemy`, `app.db` or `Session` imports across all
seven modules and exactly one external importer (`jobs/pipeline.py:39-40`); every other
domain package owns its own `models.py`. The new package imports `app.llm.base.LLMUsage`
one-way, so persistence depends on provider contracts and never the reverse. **No
database session is added to `app/llm/`.**

**5C exit criterion 5 enters through CF-2.** `ClaimsLibraryEntry`
(`campaign_context/models.py:92-100`) has no `retired_at`/`superseded_by`/`is_active`
column, and no cycle creates one. CF-2 is the only freeze that precedes the 5C build
window, so the soft-retire column joins the **CF-2** migration payload, with
`campaign_context/models.py` carved into the CF-2 freeze-owned payload for that one
column and the sequential touch disclosed to 5D-W1.

### 6.3 Shared frontend contract surface — ONE binding ownership model

**Operator decision (binding): the shared frontend contract surface is FREEZE-OWNED by
the single integration owner.** An earlier draft of this section asserted the opposite
— that `types.ts`/`endpoints.ts` were "lane-owned per window, **not** freeze-owned",
justified by the claim that freeze ownership "moved roughly two days onto the owner's
serial path for no safety benefit". **That claim was measured and disproven** (the true
charge is **0.75 lane-days** across three freezes — 0.70 for the surface plus 0.05 for
the `notifications.tsx` shell — against a measured range of 0.50–0.70 for the surface
alone), and the lane-owned model is withdrawn. Only
the model below is operative anywhere in these six documents.

| Artifact | Status | Owner |
|---|---|---|
| `apps/api/openapi.json` | **generated** by `scripts/gen-types.sh` | integration owner, at its generation freeze |
| `apps/web/src/api/schema.d.ts` | **generated** by `openapi-typescript` (carries a "Do not make direct changes" header) | integration owner, at its generation freeze |
| `apps/web/src/api/types.ts` | **hand-written** — 116 lines, 75 maintained aliases over `./schema` | **integration owner, freeze-owned** |
| `apps/web/src/api/endpoints.ts` | **hand-written** — 365 lines, 51 wrapper functions over `apiRequest` | **integration owner, freeze-owned** |
| `apps/web/src/api/queryKeys.ts` | **hand-written** — 70 lines, 25 key entries, imported by 21 modules | **integration owner, freeze-owned** |
| `apps/web/src/components/layout/nav.ts` | **hand-written** — 77 lines, 8 entries; `navItems` is imported at module scope by both `breadcrumbs.tsx:3` and `sidebar.tsx:3`, and **consumed at module scope** by `breadcrumbs.tsx:6-8` (`sidebar.tsx` consumes it inside the component at `:34`) | **integration owner, freeze-owned** |
| `apps/web/src/App.tsx` | route table | **integration owner, freeze-owned** |
| `apps/web/src/components/layout/notifications.tsx` | **hand-written** — 28 lines; app-shell chrome, imported at module scope by `app-shell.tsx:9`, rendered at `:78`, and `AppShell` is the layout route element at `App.tsx:25`, so it renders on every authenticated page | **integration owner, freeze-owned** |
| Feature components (e.g. `pages/approvals/`, `pages/creative/`, `pages/analytics/`, `pages/notifications/`) | isolated feature files | **feature lane** |

**`notifications.tsx` is freeze-owned, and the test that settles it is mechanical.** A
feature-specific notifications file may be lane-owned only if repository inspection
proves it is an isolated feature page or component, owned by exactly one lane, modified
by no other same-window lane, with its navigation registration, API types, endpoint
functions and query keys all freeze-owned, and its tests cross-authored. **The first
condition fails on measured evidence**: the file sits in
`apps/web/src/components/layout/` beside `nav.ts`, `breadcrumbs.tsx` and `sidebar.tsx`,
and is consumed at module scope by the app shell on every authenticated route. That is
precisely why `nav.ts` is freeze-owned. **The substantive notification feed therefore
moves to a new lane-owned isolated component under `apps/web/src/pages/notifications/`,
owned by 5E-W3 with its co-located `__tests__/` cross-authored to 5E-W4; the
freeze-owned shell renders it, and the shell replacement is charged to the integration
owner at 0.05 day inside the CF-3 payload.** That charge is stated explicitly rather
than folded into an existing line.

**Feature lanes supply exact contract inputs and own isolated feature files. They do not
concurrently edit any shared contract surface.** No generated-file label may be applied
to a hand-written file: only `openapi.json` and `schema.d.ts` are generated.

**Charged, not hidden — across THREE freezes, not two.** The shared-surface work is
charged explicitly in §5 and allocated to **three** grid slots:

| Freeze | Slot | Charge | Payload |
|---|---|---|---|
| CF-1 (5A + 5B) | **D2 AM** | **0.27** | brief, approval, role-assignment and typed-audit-read aliases, wrappers and keys; the API-surface stubs; **the contract-equality checker** |
| CF-2 (5C) | **D10 AM** | **0.10** | `pages/creative/` type aliases, endpoint wrappers, `queryKeys` entries |
| CF-3 (5D + 5E) | **D15 PM** | **0.38** | campaign, analytics, notification and export aliases, wrappers and keys; `queryKeys.ts` gaining its **per-user** scope dimension |
| | | **0.75** | of which the **surface proper is 0.70** and 0.05 is the `notifications.tsx` shell, placed per the mounting rule below |

**MOUNTING IS NOT FREEZE WORK — `App.tsx` routes and `nav.ts` entries land at the
integration cycle that merges the page directory, never at the freeze.** This is a
correction, and the reason is mechanical rather than stylistic.
`apps/web/src/App.tsx` imports every routed page as a **hard module import**
(`import { OverviewPage } from '@/pages/Overview';`), and **`Frontend quality` is one
of the four required status checks**, running `tsc -b --noEmit` and `tsc -b &&
vite build`. A route entry authored at a CF slot therefore imports a module that its
owning builder lane does not create for another **1–6 working days**, so the freeze's
own merge would fail a required check — `plan` states elsewhere that *"every CF is a
merge point requiring green CI on the exact head"*, and it could not be.

This is the **frontend twin of a circularity this plan already cures on the backend**.
The API-surface-stub mechanic (§5 mechanic 3) exists precisely because
`scripts/gen-types.sh` needs route code in-tree at freeze time; no frontend equivalent
was ever written, and a frontend placeholder module could not be authored by the
integration owner in any case, because every `pages/*/` directory is claimed **whole**
by a builder lane and §6 forbids two identities editing one file.

**The resolution needs no new ownership rule**: §6.2 already permits the integration
owner to edit freeze-owned files during **integration windows (I-\*)** as well as
freezes. So the split is:

| Artifact | Lands at | Why |
|---|---|---|
| `types.ts` aliases, `endpoints.ts` wrappers, `queryKeys.ts` entries, API-surface stubs | **the CF slot** | builder lanes *consume* these; they must exist before the window opens, and none imports a page module |
| **`App.tsx` route + `nav.ts` entry + the `notifications.tsx` shell replacement** | **the I-\* cycle that merges the owning lane's page directory** | these *mount* the lane's output; the module must exist first, and at that cycle it does |

Concretely: `pages/approvals/` and `pages/settings/` mount at **I-5B**;
`pages/creative/` at **I-5C**; `pages/campaigns/` at **I-5D**; `pages/analytics/` and
the `notifications.tsx` shell at **I-5E**. `pages/opportunities/BriefPanel.tsx` needs
no route — it mounts into the existing `OpportunityDetail.tsx:303`, owned by 5A-W3.

**The 0.75 total does not change**; it is redistributed from the three CF slots to the
CF slots plus four integration cycles, so this is a **placement** correction, not a
quantity one. The nav entry travels with the route deliberately: a `nav.ts` entry
alone would compile but render a link to a route that does not yet exist.

**How the moved work is charged, stated so it is not double-counted.** The mounting
portion stays inside the **0.75 shared-surface line** in the §5 charge table — it does
**not** raise the per-cycle integration charges of 0.595 / 0.689 / 0.551, which price
reconciliation, regeneration, drift verification, affected tests, CI monitoring, merge
and post-merge SHA verification, and are already taken at the most conservative of
three independent derivations. What changes is *which slot the surface charge sits in*,
not how much it is. The three CF slots' disclosed grid slack (0.23 + 0.15 + 0.12 =
0.50) rises correspondingly as mounting leaves them, and the four integration cycles
absorb it within the overflow placement already provided. **Total charged remains
23.28 and the ceiling identity remains 23.28 + 0.50 + 0.72 + 1.50 = 26.00.**

**Why three and not two, and why this was previously hidden.** An earlier version
allocated the whole charge to two slots — D2 AM and the CF-3 slot — while also stating
that *"CF-3 carries the larger share (~47% of the surface)"*. **47% cannot be the
larger of two shares**, and the contradiction was the symptom: a third share, CF-2's,
existed and was unbudgeted and unplaced. The evidence is direct — **5C creates the new
top-level page directory `apps/web/src/pages/creative/`, and 5D creates
`apps/web/src/pages/campaigns/`, yet earlier versions of both sub-plans mentioned
`types.ts`, `endpoints.ts`, `queryKeys`, `nav.ts` and `App.tsx` a combined ZERO
times.** Both sub-plans now carry an explicit shared-surface section naming all five,
and both record the former omission in the past tense. Without their own surface
entries those pages could be authored but never mounted — the identical
defect already cured for 5A's `OpportunityDetail.tsx` below. Two independent
weightings of the corrected split, one by new endpoint wrappers and one by new routes,
put CF-3 at **47.8%** and **50.0%** respectively, corroborating the plan's own "~47%"
while confirming that the two-slot allocation was a two-freeze figure presented as a
whole-round figure.

**The pessimistic figure is 1.75 and it is NOT sourced — this is disclosed, not
resolved.** Earlier versions of this plan asserted a pessimistic value of **1.40** as an
identical parenthetical, with **no derivation anywhere**. It is **2.333× the 0.60
midpoint** of the stated 0.50–0.70 range (and exactly 2× that range's top). That
2.333× ratio is carried onto the corrected 0.75 to give **1.75** (0.75 × 7/3), and charged in full in the pessimistic total. It is an **unsourced
allowance charged conservatively**, and it may be reduced only if primary evidence is
produced.

**Mechanical drift guard — required, because the hand-authored half is the unprotected
half.** `.github/workflows/ci.yml:214` runs `git diff --exit-code` over
`apps/api/openapi.json` and `apps/web/src/api/schema.d.ts` only; `types.ts`,
`endpoints.ts` and `queryKeys.ts` have **no** mechanical control. Making one identity
their sole author concentrates risk without adding one. CF-1 therefore adds a
**contract-equality checker** wired into CI, asserting that every path in
`openapi.json` has a corresponding wrapper in `endpoints.ts`, that every response
model has an alias in `types.ts`, **and that every `endpoints.ts` wrapper reachable
from a React Query call site has a corresponding entry in `queryKeys.ts`.** Its
authoring is inside the §5 shared-surface charge.

**`queryKeys.ts` is inside the checker deliberately, and this is the security-relevant
part.** An earlier draft guarded only `types.ts` and `endpoints.ts` — omitting the one
hand-written file that gains a *per-user* isolation dimension at CF-3 (below). A
checker that covers the two files whose drift is merely a compile-time inconvenience,
while omitting the file whose drift is a cache-isolation failure, guards the wrong
half of the surface. A missing or wrongly-scoped key does not fail a type check: it
silently serves one user's cached data to another.

**`queryKeys.ts` gains a per-user scope dimension at CF-3.** Every existing key is at
most workspace-scoped. 5E notifications are per-**user**, and 5E's mark-read route is
the round's only new cross-user IDOR class. The key factory is the cache-isolation
boundary, so adding a user dimension is a **design change to an isolation control**, not
a transcription. It is authored at the D15 PM CF-3 slot, and R-5E must review it as an
isolation-control change rather than as a routine key addition.

**`apps/api/app/tests/conftest.py` and the fixtures package.** 5A creates the first
`conftest.py` under `apps/api` (verified: none exists today), which changes fixture
resolution for every existing API test module. It **cannot** remain lane-owned:
under a four-way test split, three or four lanes would need to edit it in the same
window — the one collision class this section calls absolute. Binding structure:
`apps/api/app/tests/fixtures/` is a package with **one module per subject area, each
exclusively owned by one lane**, and the root `conftest.py` becomes **freeze-owned
after I-5A**, containing only imports.

**Consequence, previously unowned and now assigned.** Because the root `conftest.py`
is freeze-owned after I-5A, wiring any later fixture module into it is **CF-* payload
authored by the integration owner** — not lane work. Specifically, the import line
registering `apps/api/app/tests/fixtures/analytics.py` is **CF-3 payload at the D16 AM
cycle** and is named in the §5 day table. Without it, 5E exit criterion 4 (analytics
aggregates on seeded fixtures) is unreachable. This closes a **further** instance of a
recurring class in which an artifact the plan required was owned by nobody. **No count
and no finality is asserted for that class**: an earlier version of this sentence
called it "the fourth and last instance", which the table of previously-unowned paths
below — five rows on its own — already falsified, and which a subsequent review
falsified twice more. Any count must be re-derived from that table, never asserted.

**Post-integration contract files.** Once a milestone integrates, its `models.py`
files (and the `AuditAction` registry file after 5B) join the freeze-owned set:
they are contract-bearing, and later milestones' additive columns (jurisdiction
snapshots, `deleted_at`, author/identity columns) land in CF-* migration payloads
authored by the integration owner — never by a later lane editing an earlier
milestone's models directly.

**Capability pin-tests are CF payload.** The capability governance pin tests —
`test_capability_registry.py`'s exact value-set, declaration-order, and
`future_activation_phase` assertions, and `test_api_isolation.py`'s
flag-reflection assertion when O-4 adds flags — break the moment a CF-* payload
adds a capability member, and every CF is a merge point requiring green CI on
the exact head — which is exactly why no `App.tsx` route may be authored at a freeze
whose page module does not yet exist (§6.3): `Frontend quality` is a required context,
and it would be red. Green CI at every CF is achievable only under that mounting rule.
Continuing: green CI on
the exact head. Their updates are therefore authored **inside the CF-* payload,
in the same commit as the capability member, by the integration owner** — at
CF-1, CF-2, and CF-3 alike — never deferred to a builder lane's later window.
Per resolved O-4 (`ALT_expose_all`), every CF that adds a flag also adds it to
`FeatureFlagsOut` and updates the exact-equality reflection test in the same
commit.

**Cross-milestone touch rule.** When a milestone's work requires edits inside a
module built by an earlier milestone (e.g. 5C's approval-gate hook in
`apps/api/app/approvals/`, notification-emission hooks in earlier services), the
CF-* gate for that milestone must assign the **exact file list** to exactly one
named lane; unlisted files are untouchable. Where two lanes in **different**
windows touch the same file (sequential, never concurrent), both lane tables
must disclose the sequential touch explicitly.

**The 5D-W3 / 5E-W3 direct-edit collision — why those two windows may never
overlap.** 5D-W3 owns `apps/api/app/briefs/service.py` and
`apps/api/app/creative/service.py` (jurisdiction snapshot capture); 5E-W3 owns **those
two files plus** `apps/api/app/approvals/service.py` (notification emission hooks).
**The overlap is two shared files, not three** — `approvals/service.py` is 5E-W3's
alone. Because the two lanes edit the same two files, their windows are separated in
the §5 table by the whole of I-5D, R-5D and an integration-overflow slot: **5D builds
D17 AM–D18 AM and 5E builds D20 AM–D22 PM.** This separation is a *correctness*
requirement independent of any review gate — even if 5E's entry criteria were relaxed,
overlapping the windows would put two lanes in the same file at the same time, which
§6 forbids absolutely. Ownership entries must be exact paths
or directory globs — prose qualifiers ("additions only", "new pages") are not
ownership; where two lanes would share a parent directory in the same window,
each lane's subdirectory is named explicitly (e.g.
`apps/web/src/pages/campaigns/` vs `apps/web/src/pages/analytics/`).

**Co-located frontend tests (`__tests__/`) do NOT follow their page directory.** They
are governed by the cross-authored pairing rule (§6.1), which overrides the directory
rule: no lane authors the tests for code it wrote in the same window. A lane's claim on
a page directory therefore **explicitly excludes that directory's `__tests__/`**, which
is owned by the verification lane (W4) — e.g. in 5E, W2 owns
`apps/web/src/pages/analytics/` *excluding* its `__tests__/`, and W4 owns
`apps/web/src/pages/analytics/__tests__/`. An earlier draft stated the opposite
(tests follow the page-directory owner); that statement contradicted every lane table
in this plan and is withdrawn. The exclusion must be written into both lane entries so
the pair is visible on both sides.

A separate case is
**`apps/web/src/pages/__tests__/`, a flat shared directory of eight
files testing top-level pages owned by different lanes** (`campaign-context.test.tsx`
↔ 5D, `opportunity-detail.test.tsx` ↔ 5A, `settings-runtime.test.tsx` ↔ `Settings.tsx`).
One directory mapping to many owners cannot be resolved by the directory rule, so
files in it are assigned **individually by exact filename** in the CF-* that needs them.

**Previously unowned paths, now assigned (each was a real gap that blocked a stated
exit criterion).** These are named here because the earlier plan left them owned by
nobody while requiring work in them:

| Path | Owner | Why it must be owned |
|---|---|---|
| `apps/api/app/jobs/pipeline.py` | **5A-W2** (D3 PM–D4 PM) and **5C-W2** (D11 PM–D13 AM) — named exactly in both lane tables, **with the sequential touch disclosed on BOTH sides** as §6.2's cross-milestone rule requires, sequential across two windows, never concurrent. A generic "the seam lane of the milestone touching it" is **not** an assignment: this plan's own rule requires an exact file list, and an earlier draft's generic form left the file absent from 5C-W2's path list while 5C's exit criteria depended on it | Holds **both** `llm_service.run` call sites (`:211`, `:587`) and the singleton import (`:40`), plus `check_claim_safety` (`:360`). 5A's tenant-argument exit criterion and 5C's claims-engine change are undeliverable without it. Must sit with W2 because the tenant-arg change and the `llm/service.py` signature change are **atomic** — splitting them puts a signature dependency across two lanes in one window. |
| `apps/web/src/api/queryKeys.ts` | **integration owner, freeze-owned** (§6.3) | 70 lines; the central tenant-scoped query-key factory imported by 21 modules. Every Phase 5 frontend surface needs keys, but it is a **shared** contract surface: it is authored at the CF-* slots, not by any builder lane. An earlier draft assigned it to "the frontend lane of each milestone, sequentially" — that is the withdrawn lane-owned model and it is **not** operative. It is additionally an isolation control (it gains a per-user dimension at CF-3), which is why it is covered by the contract-equality checker above. |
| `apps/web/src/pages/OpportunityDetail.tsx` | **5A-W3** | The panel mount point (`:303`). 5A-W3's former "new files only inside `pages/opportunities/`" ownership meant `BriefPanel.tsx` could be authored but **never mounted**, leaving 5A exit criterion 7 unreachable. Ownership must cover creation *and* reachable integration. |
| `apps/web/src/pages/Settings.tsx` | **5B-W3** (single owner; an earlier draft assigned it to 5B-W4 while the 5B lane table assigned 5B-W3 — resolved here to W3, the Primary-UI lane that owns `pages/settings/`) | 259 lines; the host surface that `apps/web/src/pages/settings/MembersList.tsx` and `apps/web/src/pages/settings/MemberRoleDialog.tsx` must link from (named exactly — no prefix glob). |
| `apps/api/app/jobs/handlers.py` | **integration owner, inside CF-2** | Remains freeze-owned. See the 5C handler note below. |

**The 5C durable-job handler path (corrected).** `apps/api/app/jobs/handlers.py` is a
**flat 99-line module, not a package**, imported at `apps/api/app/jobs/service.py:32`
and at `apps/api/app/tests/test_scout_schedule_worker_integration.py:360`. The path
`apps/api/app/jobs/handlers/creative_generate.py` therefore **cannot exist** — creating
it requires converting the module to a package, which is a freeze-owned restructuring
no builder lane may perform, and which the plan never budgeted. The repository-valid
design, which requires **no** package conversion: the generation handler is a
**new lane-owned sibling module** carrying its own `@register_handler(...)` decorator
(the mechanism defined at `apps/api/app/jobs/registry.py:66`), and the integration
owner adds **one import line** to the freeze-owned `handlers.py` in the CF-2 payload,
exactly as `service.py:32` already does for the existing handlers.

**Single controlled integration owner.** One named agent identity per round:
authors contract freezes, merges lane outputs, regenerates the two **generated**
contract artifacts (`openapi.json`, `schema.d.ts`) and **also authors the hand-written
shared contract surface — `types.ts`, `endpoints.ts`, `queryKeys.ts`, `nav.ts`,
`App.tsx` — which is freeze-owned per §6.3, not lane-owned**, resolves
mechanical conflicts, applies post-review fixes. The integration owner **never
reviews and never approves**; builder lanes never touch freeze-owned files; no one
self-approves anything. The owner may **pre-author a CF-* payload on an
owner-exclusive side branch during a preceding build window** (freeze-owned
files only, no wave open, merge deferred until the preceding wave closes
clean) — the §5 mechanics use this exactly once, for CF-3, and only after the
D9 AM operator continue decision.

**Fresh non-builder review waves after every integration.** Reviewers have no
authoring role in the work they review. Review happens against a **frozen
snapshot** (an exact commit): while a wave is open, no builder lane and no
integration owner edits the workspace — the lesson of prior gates ("never edit the
mutable workspace while a frozen-snapshot panel is executing") is binding here.

**Protected delivery (unchanged from current machinery).** All work lands via PRs
to `main` under ruleset 18820692: four required status checks, one non-author
last-push approval, thread resolution, squash merge, zero bypass actors. Merge only
on green CI for the exact reviewed head, verified via the API — never on an
"approval submitted" claim. **RESOLVED (O-9, 2026-09-01,
`REC_promote_container_build_AND_revision_reader_before_first_PP5_PR`) —
pending operator GitHub-side action; no ruleset was changed in the docs-only
rounds:**
promote the exact status-check contexts **`Container build and security`** and
**`Revision reader (unit, IaC contract, in-image)`** (required checks match by
exact context string) before the first Project Phase 5 PR merges — today they
carry the container-security and Gate-4N apparatus while not being required.

**Measured 2026-09-02 (live ruleset read, PP5-S22).** An earlier draft here asserted
that *a historical PR merged with the reader job red*. **That assertion was not
measured and is withdrawn.** What was measured is the converse: in the observed
20-run window **three** pull requests carried all four required contexts green and the
advisory `Revision reader (unit, IaC contract, in-image)` red, and **all three were
held out by human convention** rather than merged. The existing control worked in
every observed case (n=3).

**O-9 therefore makes an existing convention mechanical; it does not repair a failing
control and it does not add work to the charged merge cycle.** Its measured schedule
effect is **0.00 working days**: `§5`'s merge-cycle decomposition charges a CI
component of **1.026 h**, and the reader's measured p50 is **61.57 min = 1.02617 h** —
the CI component *is* the reader, and that sample was taken while the reader was
already unrequired, so its duration is already inside the charged per-merge cost.

### 9.1 `apps/web/src/test/handlers.ts` — freeze-owned, with a CF-1 prerequisite

The central MSW registry is **freeze-owned by the integration owner**. Builder lanes own
**distinct milestone handler fragments**; the integration owner imports and registers the
completed fragments **exactly once per affected freeze**. Every central-registry touch
carries a named slot and charge.

**The model has a prerequisite the earlier drafts did not measure.** `P` (`:10`), `org`
(`:19`), `workspace` (`:20`) and `brand` (`:28`) are **module-private** today. If the
registry imports the fragments while the fragments import those constants from the
registry, that is an **initialization-order** cycle — the array is built at module scope,
so a fragment observes `undefined`, intermittently and order-dependently. **CF-1 must
therefore extract `apps/web/src/test/fixtures.ts` as a leaf module before 5A's window
opens on D3 PM**, since 5A-W4 authors `brief-panel.test.tsx` in that window and needs a
fragment to write into.

**Exactly one export must be preserved for external consumers: `resetCapabilityOverrides`**
(`operations.test.tsx:6`). `CITIES`, `demoUser` and `noIntelOpportunityId` have **zero**
external importers — `locations.test.tsx:15` is a **comment**, not an import — so they are
**dead exports and no claim that they must be preserved is carried forward.**

**Completeness enforcement must be POSITION-AWARE, not membership-only.** The greedy
catch-all at `handlers.ts:643-649` lists `'campaigns'` in its `kinds` array, so a fragment
spread **after** it is *handled* and returns `HttpResponse.json([])` — **a silently passing
test against empty data**, not an unhandled-request error. The check must assert that the
catch-all remains last, that every required fragment is registered, and that none is
registered twice. Per-fragment mutable-state resets must be aggregated into one reset or
`afterEach` cleanup goes incomplete.

**Gate:** `setup.ts:41` `server.listen({ onUnhandledRequest: 'error' })`, inside the
**required** context `Frontend quality`.

### 9.2 CHECK-widening control — `PP5-CHK-001`

**Alembic never compares `CheckConstraint` at all.** In 1.18.5 the comparator registry
`ddl/_autogen.py` `_clsreg` has exactly three keys — `unique_constraint` (`:165`), `index`
(`:205`), `foreign_key_constraint` (`:254`). `CheckConstraint.__visit_name__` is
`"check_constraint"`: unregistered. Check constraints are **reflected**
(`compare/util.py:30,38`) and never compared.

**The live defect this creates.** `capabilities/models.py:51-60` derives its constraint
from `persisted_values()` and updates automatically; migration
`20260720_1259-98289430a3ec…:66` **hardcodes** three values. Add a capability without the
widening migration and the model says four, the database says three, and `alembic check`
reports *"No new upgrade operations detected"* — green, failing later as a runtime
`IntegrityError`. The existing test
(`test_workspace_capability_override_migration.py:214`) builds its schema with
`Base.metadata.create_all` — **from the model, never the migrations** — and asserts only a
negative, so it is exactly the model-side test that cannot catch this.

The control compares the **Python authority** against the **live migrated database**:

* **authority** — the callable, imported and called, never transcribed;
* **comparand** — `sqlalchemy.inspect(engine).get_check_constraints(table)` against the
  database `alembic upgrade head` builds in the same job;
* **normalization must preserve the `IN`-list PARTITION.** A flat set of literals
  **cannot** distinguish a correct polarity constraint from a swapped or dropped one —
  proven by execution, and `5b:73` specifies `reason_code` as exactly *"the frozenset
  polarity pattern"*, with `ck_opportunity_feedback_reason_polarity` as the live
  precedent;
* **comparison is exact and reported in both directions** — `missing_from_db` **and**
  `unauthorized_in_db`; a superset in the database is as much a defect as a subset;
* **a discovery direction is mandatory** — enumerate every check constraint in the
  migrated database and require each to have an authority row. Without it the control is
  opt-in per vocabulary and a new CHECK simply omitted from the fixture passes green;
* **negative control A** removes one allowed value; **negative control B** adds
  `'__pp5_negative_control__'`; both must fail;
* **positive control** requires exit 0 **and a non-zero count of vocabularies checked**,
  so an empty registry cannot pass as green.

**Failure code `PP5-CHK-001`. Owner: integration owner. Freeze: CF-1. Required context:
`Migrations and API contract`. Charge 0.40 adopted / 0.80 pessimistic.**

### 9.3 CI pin cascade — both checkers are Python tools

`plan:806-810` mandates `scripts/check_contract_equality.py`, and §9.2 adds
`scripts/check_closed_vocabulary_constraints.py`. **Neither exists today**, and **both are
Python tools that self-admit into the governed site universe**: `release_roots()`
(`site_taxonomy.py:927-930`) admits *"every `scripts/*.py` command the workflow actually
EXECUTES … membership is a property of execution, never of spelling"*.

**The vocabulary checker's smaller cascade does not transfer to the contract-equality
checker.** Each must be accounted for on its own.

Each Python checker requires, in one atomic same-commit payload:

* insertion at `migration-and-contract` **step index ≥ 5**. Indices are **0-BASED** and
  **per-job** — proved because `executable-trust-policy.json` pins `$PY` at run-block
  lines 5/7/9 of `ci.yml#migration-and-contract#4`, and `steps[4]`
  (*"Migration round-trip + seed idempotency"*) has `$PY` at exactly 5, 7 and 9, while
  `steps[3]` is *"Install dependencies"*. Index 4 is pinned **seven** times; the job's
  other two pins are name-based and insertion-immune;
* `tests/test_i28s_command_roots.py:483` `assert len(roots) == 43` → **44** per checker,
  or the exact count mechanically re-derived at authoring time;
* `tests/test_i28s_command_roots.py:36` `COMMENT_LINE = 407` re-pinned. **Step indices are
  per-job but absolute line numbers are GLOBAL**, and `migration-and-contract` occupies
  lines 160–226, entirely above 407, so **any** step added there moves it;
* a **real detector** in `tests/fixtures/site-coverage-function-assurance.json` (59
  modules) with `status: "ASSURED"` and a `detector_kind` in the closed set — a registry
  line alone fails closed;
* `tests/fixtures/executed-state-contract.json:654` digest re-pin and the
  `scripts/evidence_binding.py:72-73` re-bind;
* `requirement_key` closure for any **new** root `tests/fixtures/*.json`: the registries
  are **exactly saturated at 168 = 84 matrix + 84 exclusions with zero slack**, and their
  key sets are **fully disjoint**, so the apparatus is substantive rather than
  self-excluding. Each new top-level key needs an executed-matrix catch **or** a governed
  exclusion;
* `tests/fixtures/review-record-ledger.json` `governed_files` digest re-pin;
* **negative control for a stale pin** and **negative control for a partially updated
  cascade**.

**Measured, not predicted:** PR #66 — a **three-line** `docker/build-push-action@v6 → @v7`
bump in `ci.yml` — left all four required contexts **green** while
`Revision reader (unit, IaC contract, in-image)` went **red**. A three-line change fires
the whole cascade, and **the job that fails is not the job that changed**.

**All of the above is ADVISORY today** — `site_coverage.py` (`ci.yml:769-772`),
`mutation_discovery` (`:775`), `evidence_binding` (`:782`) and `pytest tests/` (`:934`) all
sit inside `revision-reader`, 750 of 1167 lines and 64% of the workflow. No required job
runs any governance Python module or the root `tests/` suite. **After O-9 it is
merge-blocking on all nine cycles** — which is the whole substance of the O-9 decision.

### 9.4 `Integration smoke` — a recorded pre-implementation verification item

`ci.yml:381` places **`Integration smoke` — a required context — behind
`needs: [frontend-quality, backend-quality, migration-and-contract]`**. It is the only
required job not independently triggerable. If an upstream job fails it never runs and
never emits a check run.

**Its behaviour in that state is `NOT_MEASURED`.** Every check run in the S22 snapshot is
`success`, and all three failing PRs returned `mergeable_state: "unknown"`, so neither
outcome was observed. **It must not be described as proven fail-closed or proven
fail-open.**

This is a **pre-implementation governance verification item**. It exists today,
independent of Phase 5 and independent of O-9, and **it does not block the S-B O-9
decision**. The decisive test is a ruleset mutation and belongs on a scratch repository.

**PR #34 adjacency check.** 5D touches `apps/api/app/connectors/policy.py`,
which shares a package with the off-limits PR #34 (live RSS,
`apps/api/app/connectors/rss.py`). The check is performed at **D10 AM**, co-located
with the CF-2 shared-surface authoring in a single 0.50-day slot (0.25 + 0.10 = 0.35),
and it therefore precedes the **CF-3 integration cycle at D16 AM** as this rule
requires. The integration owner checks PR #34's recorded diff (local records only) for
overlap with the planned connector-policy change; on any overlap, escalate to
the operator — PR #34 is never modified. **This co-location is packing, not
overlap:** both are integration-owner work in a slot where no review wave is open, and
it depends on no assumption about approval-wait idle.

## 7. Frozen contracts and review conduct (binding rules)

1. A milestone's contracts (enums, models, migrations, API shapes, flag members)
   are frozen at its CF-* gate and reviewed **at the next scheduled review
   slot, always before any builder lane consumes them**. (This is what the §5
   schedule actually implements, counted from the §5 table: CF-1 is authored D1 with
   its shared surface at D2 AM, merges at D2 PM, and is reviewed by **R1 at D3 AM**,
   before the 5A build starts D3 PM; CF-2 is authored **D9 PM**, with its shared surface
   at **D10 AM**, merges **D10 PM**, and
   is reviewed **D11 AM**, before the 5C build starts D11 PM; CF-3's shared surface is
   authored **D15 PM**, merges **D16 AM**, and is reviewed **D16 PM**, before the 5D
   build starts D17 AM. In every case the review precedes first consumption. The binding
   requirement is review-before-consumption, not same-calendar-day.) After the freeze, builder
   lanes may **consume** the contracts but not change them; contract changes found
   necessary mid-build go through the integration owner as an explicit freeze
   amendment with its own mini-review.
2. During any open review wave: no workspace edits by anyone; reviewers inspect the
   exact frozen commit; fixes begin only after the wave closes and are applied by
   the integration owner as new commits, re-reviewed.
3. No self-approval; no builder reviews their own lane; the integration owner
   reviews nothing.
4. Test execution during implementation follows the repository's standing rules
   (pinned Gate-4N invocation, hermetic HOME, quiesced git, staged working tree,
   `SIGNALNEST_CANDIDATE_MANIFEST` set locally); pytest alone is never treated as
   CI-equivalent — ~45 CI guard steps are shell, not pytest.

## 8. Exact-stop behavior

- Any **SUBSTANTIVE** review finding (would change behavior, security posture, a
  contract, or a verdict) halts the affected milestone immediately. Fixes are
  authored only by the integration owner on new commits and re-reviewed by the same
  wave's reviewers.
- Two consecutive substantive-failed re-reviews on the same milestone → **full
  round stop** and an operator report naming the finding class.
- Any evidence of a critical-path blocker that makes the committed
  **26-working-day continuous schedule** infeasible → stop and report the
  exact blocker and measured schedule impact; do not silently stretch, and do
  not compress any review gate to compensate.
- Any unexpected mutation outside the authorized surface (the prior readiness
  audit's hard-stop class) → immediate full stop, no repair, operator adjudication.
- **A SECOND substantive review finding.** The pessimistic total charges exactly one
  at its full 1.0-day cost (§5). The reachable reserve is **2.00 day** — the placed
  0.5 at **D15 AM** plus the 1.5-day tail spare at D25 PM–D26 PM — and the pessimistic
  case consumes 2.09 of the **2.72** total buffer beneath the ceiling (0.50 contingency
  + 0.72 grid slack + 1.50 tail spare), leaving **0.63**. A second
  finding therefore has nothing left to draw on → stop and report.
- Consuming the placed **D15 AM** reserve *and* the tail spare, or any slip beyond
  them → stop and report. **No window is zero-slack** — the per-window local floats
  are 5A +0.35, **5B +0.12** (the round's tightest, and the plan's accepted minimum),
  5C +0.43, 5D +0.20, 5E +0.82 (§5 ledger) — but that slack is *window-internal
  builder capacity*, spendable only inside its own window and never transferable
  across a contract freeze, so **none of it is available to this budget**. An earlier
  draft called 5B/5C/5D/5E "zero-slack windows", which is false as written; the correct
  statement is that their float is real but unreachable from here.
- **Any attempt to treat a GitHub approval wait as free.** All nine are charged as
  strictly serial dependency time. Reclassifying one as parallelizable requires
  amending the affected sub-plan's §3 entry criteria under operator authority, and is
  a §8 stop until that amendment exists.
- The operator's continue decision not being **recorded by the end of its D9 AM
  window** → the remainder of the round shifts day-for-day and the 26-day ceiling no
  longer holds. Report the new ceiling; do **not** absorb operator latency into
  the contingency budget, and do not compress any gate to recover it.

## 9. Early-stop deliverable (Phase 5A + Phase 5B) — a recovery boundary, never Phase 5 closeout

**Binding classification (operator decision O-11, resolved 2026-09-01):** the
D8 checkpoint is a **recovery boundary only**. It is not Phase 5 closeout, may
not be cited as Phase 5 closeout, and may not assert
`PROJECT_PHASE_5_BUILD_COMPLETE` — that assertion exists only in the end-of-round
closeout record (§5, **D24 PM**) with every 5A–5E exit criterion TRUE and the R-final
verdict recorded.

At the D8 checkpoint the round produces a self-contained checkpoint record for
5A+5B: brief and approval **customer surfaces** are dark behind
`recommendation_briefs_enabled` (default `False`, fail-closed — the approval
endpoints are gated by the same capability); all tests green; no generation
surface exists yet. The checkpoint record must **name the deliberately live,
un-flagged deltas** this state carries rather than claim blanket inertness:

1. the audit read route's gate narrowing to an exact role set (the O-1 cure —
   a disclosed security improvement to a live route);
2. the new organization-scoped role-assignment endpoints (live, exact-set
   owner/admin-gated administration surface);
3. the typed audit read API replacing the untyped response, plus login
   success/failure audit rows;
4. the LLM-seam hardening landed in 5A on always-live pipeline paths: tenant
   argument plumbed through both existing call sites, `get_llm_service()`
   accessor, injectable retry clock, input sanitization at the service
   boundary, and fail-closed prompt rendering.

Each of these is a reviewed, test-covered behavior change disclosed in its
sub-plan; none depends on a Phase 5 flag. With that disclosure, the stop is
**safe and complete** — nothing half-built remains. Continuing past 5A+5B
requires the operator's recorded continue decision (the **D9 AM** continue-window)
before CF-2 is authored.

## 10. Activation gates (not part of this round)

Activation of anything built in Project Phase 5 is a separate, future,
per-capability authorization, and is additionally gated on:

- Phase 4B-B, INFRA-9 live deployment, and Phase 4B-C completed or formally
  dispositioned — the authority-boundary clause of the retained plan-authoring
  authorization (`AUTHORIZATION.PP5-PLAN.verbatim.md`, sha256 `5257cc80…`),
  restating definition-audit operator decision D-06 (§14 records that series'
  provenance).
- **Before `recommendation_briefs_enabled` activation (5A) — including any
  activation at or after the D8 early-stop checkpoint.** The D8 checkpoint is a
  sanctioned, potentially long-lived recovery boundary (§9), and the 5A flag is
  `workspace_enableable` with global default `False` (O-3). An operator could
  therefore legitimately enable briefs per-workspace **before 5D exists**, and
  real briefs would then accumulate with **no `jurisdiction_code`**, because
  5D adds that column additively to `briefs` and `creative_drafts` and 5E's
  export service assumes the column is populated "natively from birth". That
  assumption holds only for rows created after CF-3. Required before any 5A
  activation, therefore: **either** a jurisdiction backfill plan for pre-5D
  briefs and drafts (the drop-and-report shape 5D already specifies for
  `campaign_locations`), **or** a recorded operator decision to block export
  and generation for any row whose `jurisdiction_code` is NULL. This is a
  residual risk of the continuous shape and is disclosed rather than assumed
  away: being "born jurisdiction-aware" is a property of the *table*, not of
  every *row* that reaches it.
- **Before `creative_generation_enabled` activation (5C):** tenant identity on the
  LLM seam; prompt/model/version/input-hash provenance persisted; cost ceilings and
  quotas enforced; input sanitization at the LLM service boundary; output-side
  claim review blocking the approval transition; **fail-closed jurisdiction
  resolution merged and verified (built in-round at **D17 AM–D18 AM** — generation
  must not activate while jurisdiction gating fails open)**; a real provider
  explicitly selected and credentialed by the operator; the signal-intelligence
  threat model reopened and re-reviewed for live generation; retention/deletion
  policy decided (O-5).
- **Before Phase 5C and Phase 5D activation:** fail-closed jurisdiction
  resolution (unknown market/jurisdiction denies) replacing the current
  fail-open substring semantics.
- **5E ships two surfaces LIVE and UNGATED — named here because §2's
  "all new capabilities ship dark" is true of *capabilities* and could otherwise be
  read as covering these.** O-4 fixes the Phase 5 flag set at exactly three, so
  notifications and status analytics have no flag and no capability: the notification
  **list** returns empty until upstream emitters fire, the notification **mark-read**
  route is a **live, un-flagged write carrying the round's only new cross-user IDOR
  class**, and **analytics aggregates over existing `opportunities` rows and therefore
  returns real, non-empty tenant data at merge**. Both are gated by
  `require_exact_roles` and tenant scope (5E §2) but by no flag. Whether they should
  carry one is **open operator decision O-12** (§12).
- **Before `content_export_enabled` activation (5E):** export authorization model
  reviewed; retention/deletion policy decided (O-5); and the export service's
  **explicit `jurisdiction_code IS NOT NULL` refusal** (5E §2) verified as
  defence-in-depth, so a NULL-jurisdiction row cannot be exported even if one
  reached the table by a path this plan did not foresee.

**Operator riders (recorded 2026-09-01, `DECISIONS.PP5-O1-O11.md`):** O-5 and
O-7 are **activation blockers only** — the build proceeds mock-first with the
retention mechanism shipped disabled. Per the O-5 rider, no 5C or 5E activation
is permitted until an explicit retention/deletion policy is separately adopted.
Per the O-7 rider, no real provider, credential, network call, **or
provider-specific activation** is authorized,
and no 5C activation is permitted until O-7 and the required threat-model gates
are separately resolved.

## 11. Carried planning decisions

Carried from the operator's plan-authoring authorization; repository evidence was
checked and none proved unsafe:

1. **Dedicated exact-set-membership authorization dependency** (working name
   `require_exact_roles`) for the approver gate and the audit read route; the
   existing rank-floor `require_role` is left semantically untouched everywhere
   else in this round.
2. **New immutable recommendation-brief entity** (pattern:
   `SignalIntelligenceRecord`), not mutable columns on `opportunities`.
3. **Every new flag globally `False` by default**, fail-closed, with controlled
   per-workspace enablement capability via the existing override plane.
4. **Public flag reflection follows resolved operator decision O-4
   (`ALT_expose_all`, 2026-09-01):** all three Phase 5 flags are added to
   `FeatureFlagsOut`, each at the CF-* freeze that creates it, with the
   exact-equality reflection test
   (`apps/api/app/tests/test_api_isolation.py:140`) updated in the same CF
   payload. The originally carried recommendation — expose only UI-required
   read-only flag state — was **not** selected and is superseded; it survives
   only in the historical register at the end of §12.
5. **Retention/deletion policy is a mandatory pre-activation decision** — never
   guessed, never defaulted in code. Per the O-6 rider (2026-09-01), the
   survival of approval/export compliance records is itself subject to the
   future O-5 policy and must not mean indefinite retention by default.
6. **5C activation preconditions** as listed in §10.
7. **Fail-closed jurisdiction resolution before 5C and 5D activation.**
8. **Capsule `phase_5=false` interpreted as capsule-plane authority only** (§2).

## 12. Operator decision register — O-1…O-11 RESOLVED 2026-09-01; **O-12 OPEN**

**Every decision O-1…O-11 is now RESOLVED; O-12 is OPEN and is recorded above.** The
eleven resolved decisions' binding responses are recorded in
the immutable operator decision record `DECISIONS.PP5-O1-O11.md` (SHA-256
`170547271258fa00199f20139c81bef1e5b8a9e4c5b2b039cf7a8ba00de7f103`, operator
round directory) and restated in the Resolutions block below, which governs.
The table is retained as the decision material as it was put to the operator;
where a resolution differs from the table's recommendation (O-4, O-11), **the
resolution governs**. Deadlines in the table are historical, and all eleven
decisions O-1…O-11 fall into exactly three categories (O-12 is open and outside this
classification): (a) **resolved ahead of their
deadlines** — O-1, O-2, O-3, O-4, O-6, O-8, O-10, O-11; (b) **deliberately
reserved, blocking activation only** — O-5 and O-7; (c) **resolved, deadline
not yet reached, operator action outstanding** — O-9, whose deadline (before
the first Project Phase 5 PR merges) has not arrived and whose GitHub-side
promotion remains an operator action not performed in any docs-only round.

### Resolutions (recorded 2026-09-01; verbatim response tokens)

| # | Resolution |
|---|---|
| O-1 | `REC_require_exact_roles_incl_audit_route_cure` — adopt the dedicated `require_exact_roles` dependency and apply it to the audit read route |
| O-2 | `REC_Role_REVIEWER_plus_org_scoped_assignment` |
| O-3 | `REC_all_three_workspace_enableable_default_false` |
| O-4 | `ALT_expose_all` — **all three Phase 5 flags are added to `FeatureFlagsOut`**, each at the CF-* freeze that creates it, with the exact-equality reflection test (`apps/api/app/tests/test_api_isolation.py:140`) updated in the same CF payload |
| O-5 | `UNDECIDED_RESERVED_TO_OPERATOR_LEGAL` — build proceeds with the disabled enforcement mechanism; **no 5C or 5E activation until an explicit retention/deletion policy is separately adopted** |
| O-6 | `REC_compliance_records_survive_FKless_scope_columns` — rider: survival is subject to the future O-5 policy and must not mean indefinite retention by default |
| O-7 | `UNDECIDED_RESERVED_TO_OPERATOR` — deterministic mock-first build; no real provider, credential, network call, **or provider-specific activation** authorized; **no 5C activation until O-7 and the required threat-model gates are separately resolved** |
| O-8 | `REC_campaign_locations_join_table_FKs_backfill` — the consolidated CF-3 freeze carries the join-table contract |
| O-9 | `REC_promote_container_build_AND_revision_reader_before_first_PP5_PR` — decision recorded only; the GitHub-side promotion is a future operator action before the first Project Phase 5 PR merges; no ruleset was changed in the docs-only rounds |
| O-10 | `REC_app_layer_append_only_document_db_level_as_separate_infra` |
| **O-12** | **OPEN — NOT RESOLVED. Should the 5E notifications and status-analytics surfaces carry a feature flag?** They are the round's only new surfaces that ship **live and ungated**: notifications' mark-read is a live, un-flagged write carrying the round's only new cross-user IDOR class, and analytics aggregates over existing `opportunities` rows and therefore returns real, non-empty tenant data at merge. O-4 fixes the Phase 5 flag set at exactly three, so **a fourth flag would amend O-4**. This question is raised in `docs/project-phase-5e-notifications-analytics-export.md` §4 exit criterion 2 and is registered here because a sub-plan must never be the sole carrier of a live operator decision — a reader of §9, §10 and §12 alone would otherwise conclude the round ships nothing live and has no unresolved questions. **No implementation or activation depends on resolving it**; it changes only whether these two surfaces gain a flag before any future activation authorization. |
| O-11 | `REJECT_SPLIT__ADOPT_SINGLE_CONTINUOUS_17_TO_18_WORKING_DAY_ROUND` (**operator token, verbatim and unaltered; the "17–18" in the token name is historical. The ceiling has been raised three times by the operator since the token was issued, and no raise alters the token: first to 21 working days, after the integration-owner merge cycles were charged to the calendar — recorded verbatim at `AUTHORIZATION.PP5-R21.verbatim.md:3` ("I select 21.0 working days as the ceiling for the single continuous Project Phase 5A–5E implementation round"), reaffirmed at `AUTHORIZATION.PP5-RB.verbatim.md:3`, `:27`, `:139`; then to 23 working days, in the contract-surface ownership authorization that made the shared frontend contract surface freeze-owned and required its work to be calendar-charged; and finally, on 2026-09-02, to the operative **26 working days**, after three independent derivations returned pessimistic totals of 25.29, 25.85 and 25.10 days and the operator set the ceiling above all three. All raises are cited here because a reviewer previously judged the 21-day attribution fabricated after checking only the O-11 decision record — which predates every one of these authorizations and records a D1–D18 ceiling. **That reviewer's finding was itself the error: the attribution is genuine, was verified line by line against the retained authorizations, and is settled. It must never be conflated with the separate — and genuine — fabricated "+0.20 float precedent" corrected in §5, which is a different claim with the opposite outcome.** The operative ceiling is 26 — see §5**) — one continuous round delivers all of Phase 5A–5E **under the identical lane, freeze, review, and exact-stop machinery**, accepting that this misses the 10–15-working-day target; the §5 continuous round is operative and the split is a labeled rejected alternative; Phase 5 closeout is exactly the end-of-round closeout (every 5A–5E exit criterion TRUE with cited artifact + R-final verdict recorded); no earlier record, including the D8 checkpoint, counts as Phase 5 closeout; **and nothing built activates under this selection** |

### Register as put to the operator (historical decision material)

| # | Decision | Recommendation | Alternatives | Security impact | Schedule impact | Deadline |
|---|---|---|---|---|---|---|
| O-1 | Approver-gate mechanism | Dedicated `require_exact_roles` dependency; also apply it to the audit read route to cure the live rank-floor over-permission | (a) redesign `require_role` to strict-set semantics globally — touches ~10 call sites and changes existing authorization behavior; (b) leave audit route as-is (not recommended) | High — separation of duties for approvals; cures marketers/reviewers reading audit logs | None if decided by CF-1 | **D1 (CF-1)** |
| O-2 | Reviewer role wiring | Wire `Role.REVIEWER` as the approver role and add a minimal **organization-scoped** role-assignment surface (owner/admin assigns roles) in 5B | (a) use `COMPLIANCE_REVIEWER`; (b) new enum member (an edit to freeze-owned `core/enums.py`, so CF-1 payload); (c) defer role assignment (leaves approver identity unprovable — not recommended) | High — approver identity provability | Small (in 5B scope) | **D1 (CF-1)** — shapes contracts frozen at CF-1 |
| O-3 | Flag enableability shape | All three flags `workspace_enableable=True` with global default `False` (activation still operator-gated) | Global-only (RSS precedent) for `content_export_enabled` if export is judged legally gated | Medium — blast radius of a single workspace enable | None | **D1 (CF-1)** |
| O-4 | Public flag reflection | Add the three flags to `FeatureFlagsOut` only if the UI must render dark-state affordances; otherwise omit | Expose all; expose none | Low — information disclosure of unreleased features | None (if flags are added, the exact-equality test `test_api_isolation.py:140` must be updated; if omitted, no test change is needed) | **D1 (CF-1)** |
| O-5 | **Retention/deletion durations** (legal) | *Reserved to operator/legal.* Plan requires only that the mechanism (soft-delete + purge job) be designed in 5E so any chosen duration is enforceable | Candidate shapes only (nothing decided): fixed classes per data class (e.g. 30/90/365-day tiers for drafts vs compliance records); indefinite-with-deletion-on-request; jurisdiction-dependent schedules | High — compliance | None for build; **blocks 5C/5E activation** | Before any 5C/5E activation authorization (event-relative) |
| O-6 | Survival of approval/export records on workspace deletion | Compliance records survive (FK-less scope columns, documented — the existing `audit_logs` shape), with the rationale recorded this time | Cascade-delete with tombstone export | Medium — evidentiary integrity vs data-minimization | None if decided by CF-1 (affects FK shape) | **D1 (CF-1)** |
| O-7 | **LLM provider selection + credentials** | *Reserved to operator.* Build is mock-first and deterministic; no provider needed for the round | Candidate shapes only (nothing decided): Anthropic adapter; OpenAI adapter; both behind the existing provider seam with per-task selection deferred | High at activation, none during build | None for build; **blocks 5C activation** | Before 5C activation authorization (event-relative) |
| O-8 | Campaign `location_ids` shape | Replace the unvalidated JSON array with a `campaign_locations` join table (real FKs) via additive migration + backfill | Server-side membership validation only (less migration risk, weaker integrity) | Medium — tenant/location isolation | Contained in 5D | **CF-3D (fast-follow F1)** — a day label of the *rejected* split; under the adopted continuous round this deadline is superseded by the consolidated **CF-3 freeze (shared-surface authoring D15 PM, integration cycle D16 AM, reviewed D16 PM)**, and O-8 was in any case resolved ahead of it |
| O-9 | Required-context promotion of the exact emitted contexts `Container build and security` and `Revision reader (unit, IaC contract, in-image)` — the jobs' `name:` values, **never** the YAML keys `container-build`/`revision-reader`, which are not emitted and would block every merge (§9) | Promote both before the first Project Phase 5 PR merges (operator GitHub-side action) | Keep 4 required contexts + human vigilance (3 of 3 observed red-reader PRs were held out by convention) | High — the two unrequired jobs carry the security apparatus | **None — measured 0.00 working days** | Before first Phase 5 PR |
| O-10 | Audit immutability depth | 5B ships app-layer append-only (single-seam enforcement at `record_audit` + closed vocabulary + no update/delete paths) and **documents** that DB-level immutability (restricted DB role or trigger; the app role currently owns the database) is a separate infra change | Do the DB-role change inside 5B (crosses into infra authority — not recommended in this phase) | High — tamper evidence | None for 5B scope as recommended | **D1 (CF-1)** — determines the audit contracts frozen at CF-1 |
| O-11 | Committed-round split (schedule) | Commit ≤15 working days to the complete loop (5A+5B+5C+5E-core export/closeout); deliver 5D + notifications/analytics in a pre-planned 6-working-day fast-follow under identical machinery (§5; split total 21 working days) | Single continuous round for all of 5A–5E, measured at 17–18 working days — ≈3 fewer total days, but misses the 10–15-day target and requires operator acceptance of that | None (both shapes build everything; activation gates unchanged — 5D remains an activation precondition either way) | Defines the committed window | Before implementation authorization is issued |

## 13. Parallel Phase 4 track (separately authorized)

Phase 4B-B canary, INFRA-9 live deployment (staging foundation is live and dark;
blocker ledger: B-1 closed; B-2 partial; B-3 open AWS-side; B-4/B-5/B-6 without
located closure records), and Phase 4B-C activation proceed **in parallel under
their own authorizations**. Nothing in this plan schedules, sequences, or depends
on them for build; they gate activation (§10) **and, until completed or formally
dispositioned under separate authority, they remain IMPLEMENTATION BLOCKERS for all
of Project Phase 5** (§1, §15). No Project Phase 5 work may
touch AWS, deployment, or the capsule.

## 14. Traceability

**Definition-audit provenance.** The Project Phase 5 definition audit was
delivered in chat on 2026-09-01 under a strictly read-only operator
authorization; it is not itself a repository artifact. Its confirmed operator
decisions are the **D-01…D-10** series (distinct from this plan's O-* register);
where this plan set cites a D-* decision, that is the series meant. The audit's
two report-scope corrections are digest-identified on disk:
`CORRECTIONS-REVIEW.PP5-PLAN.md`, sha256
`2f67ba9c32c6276a63c558936bbc32b1bbe4e5f9e21912e20dc9f4b14db77966`, in the
operator's plan-authoring round directory, alongside the retained authorization
(`5257cc80b6cb60d878382d22f49cdaca650ff384b57ec3342bf567156bed87b0`/5894).

**Superseded prior verdict token (supersession chain).** The plan-authoring
round closed with `ROUND-CLOSEOUT.PP5-PLAN.md` (sha256
`819d620be5c4bed226500b9361000b9a6008a473e22bae841cdd8aa75c707e20`/3860) under
the token
`PROJECT_PHASE_5_PLAN_READY_FOR_OPERATOR_REVIEW__10_TO_15_WORKING_DAY_TARGET`.
That record is **historical and immutable and has not been altered**. Its token
referred **only** to the then-proposed committed-round *subset* of decision
O-11, and never asserted complete Phase 5A–5E closeout in 10–15 working days.
For implementation planning it is **superseded** by the operator-selected
continuous schedule, whose operative ceiling is now **26 working days** (§5); the
supersession statement itself lives
in `DECISIONS.PP5-O1-O11.md`, not in the historical file.

Every named correction in this plan traces to the definition audit's findings:
approver gate (audit item 05.1–05.2), audit spine (05.3 — including the corrected
fact that all 19 `record_audit` call sites funnel through the single constructor at
`apps/api/app/audit/service.py:24`), jurisdiction (05.4), LLM seam/provenance
(05.5), cost/quota (05.6), prompt-injection surface (05.7), campaign scoping
(05.8), export/retention (05.9), and the flag-plane frictions (audit item 10: the
`future_activation_phase` blanket assertion, the exact value-set/order pins, the
`FeatureFlagsOut` exact-equality test, and the secret-inventory field-count
reconciliation). **Field-count arithmetic (binding; corrected — the former baseline was stale).**
`docs/operations/aws-staging-secret-inventory.md` asserts **87** `Settings` fields
(lines 55, 204, 206) and was last updated 2026-07-21 (PR #93). `migration_mode` was
added to `Settings` on 2026-07-28 (PR #136), **after** that update, and
`apps/api/app/core/config.py` now declares **88**. The drift is therefore
**pre-existing in the baseline, not introduced by Phase 5**, and CF-1 would fail its
own fail-closed G5 reconciliation on D1 against the stale figure.

**Precondition (blocking CF-1):** the pre-existing 87→88 inventory drift must be
dispositioned before CF-1 is authored. The inventory document is **freeze-owned and
outside the Phase 5 documentation surface**, so this plan cannot correct it; it is
recorded here as a named precondition to be cleared under separate authority.

With the corrected baseline of 88: CF-1 adds 1 (→89), CF-2 adds 5 — the 5C flag plus four
quota/ceiling fields (→94), and the consolidated CF-3 adds the export flag
(→95) plus any notification/retention `Settings` fields, counted from the
actual field list at that freeze (95→95+N). The reconciliation delta is
recomputed at **every** freeze from the actual field list, in the same change, or
G5 fails closed. The Phase 4 boundary facts trace to the R10-P6 closeout
(CLOSEOUT.R10-P6.json, sha256
`bd5201bc4545997c0ba324fbb905fbba44b526ec55127dcea4572ea778cd9110`, length
13744) and the exact-S9 state.

## 15. Non-authorization statement

This plan authorizes no implementation, no migration, no dependency change, no
test execution, no flag change, no deployment, no AWS access, no capsule
interaction, and no change to PR #34. Implementation requires a fresh operator
authorization that names this plan, its sub-plans, and the operator decision
record `DECISIONS.PP5-O1-O11.md` (all §12 decisions are resolved; O-5 and O-7
remain reserved and gate activation only). Implementation additionally remains
blocked until Phase 4B-B, INFRA-9 live deployment, and Phase 4B-C are completed
or formally dispositioned under separate authority. The continuous-plan
reconciliation itself (this document state) adopts nothing: reviewer PASSes and
the reconciliation report are not plan adoption.
