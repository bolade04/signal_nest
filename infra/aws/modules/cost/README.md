# Module: `cost` (implemented — offline-validated, ~~NOT root-composed~~ root-composed since 2026-07-24 *(corrected 2026-10-13)*)

## 1. Purpose
Structural cost guardrails per `docs/operations/aws-staging-iac-plan.md` §15 and
ADR-0001 §M: one monthly AWS Budget with fixed notification thresholds so
overspend alerts exist structurally **before any spend begins**. Implemented and
offline-validated; ~~**no budget exists in AWS**; nothing is provisioned,
deployed, or live, and the module is **not** root-composed~~ *(struck 2026-10-13: accurate when written on 2026-07-24; the composition clause is stale since that same day (`../../main.tf`), the budget clause since 2026-07-27 — see the notice)*.

> **Live-state notice (2026-10-13 read-only observation; `P6-GOV-1`).** The budgets API was **denied** to the reading principal (as on 2026-09-30 and 2026-10-02), so neither this module's budget nor the separate console budget was read. CloudTrail management-event history recorded the two budget creations (2026-07-25 console; 2026-07-27 under the Terraform provider user agent — read 2026-09-30) and **no** budget create, update or delete event from 2026-09-30T21:55Z to 2026-10-13; the last limits recorded — 150 USD for this module's budget by an operator billing-console attestation of 2026-10-01, 20.00 USD for the console budget — are therefore not known to have changed, which is a bound, not a reading. The root bounds the declared limit at or below USD 20 since 2026-10-01 (`../../variables.tf`), stricter than this module's USD 200 ceiling; the live 150 budget exceeds that root bound and is unchanged until an authorized apply. Each fact here is a snapshot of 2026-10-13; current state is not recorded here. Root composition (`main.tf`) is configuration only and establishes neither provisioning nor deployment; the dated observations above are what establish existence, and nothing here establishes successful operation.

## 2. Implemented AWS scope (exactly one resource)
`aws_budgets_budget.monthly` — `<name_prefix>-monthly`, `COST` type, `MONTHLY`,
`USD`, with one **ACTUAL-spend** notification per threshold percentage
(`GREATER_THAN`, `PERCENTAGE`), each delivered to the single caller-supplied
email subscriber. **Notifications are observational** — an AWS Budget alert
never stops, caps, or remediates spending; this module deliberately creates
**no** budget action, automated remediation, SNS topic, IAM resource, or
anomaly detector (none is documented by the contract).

## 3. Ceiling behavior (notification thresholds vs. hard limits)
The **$200/month hard ceiling** (ADR-0001 §M) is enforced **statically at the
input boundary**: `monthly_budget_limit` validation rejects any value above
200, so an over-ceiling budget cannot even validate — raising the ceiling is a
separate authorization, never a silent input change. The budget itself does
**not** prevent spending: the 50/75/90/100% thresholds are notification points,
not enforcement. The design estimates (~$95 baseline / ~$119 typical / ~$165
upper) are documentation context only and are not encoded as defaults.

## 4. Dependencies
None. `cost` is **independent** in the locked graph (§26.12): no
sibling-module input, no data source, no consumer of its outputs. Root
composition later supplies the three inputs; nothing else is required.

## 5. Inputs (implemented)
`name_prefix`; `monthly_budget_limit` (**required**, number, validated
`0 < x <= 200` per the ADR ceiling — no invented default);
`threshold_percentages` (default `[50, 75, 90, 100]` — the §15 fixed staging
set; validated 1–100, unique, non-empty); `notification_target` (**required**
email address, validated, caller-supplied at composition — never committed).
The former stub's `tags` input is deliberately absent per the locked sibling
convention (root provider `default_tags`; this module adds only the `Name`
tag) — §26.15 recorded that cleanup, resolved here.

## 6. Outputs (implemented — exactly two)
`budget_name`, `budget_id`. References only; no notification address, account
identity, or spend data is exposed. No module consumes them (§26.12).

## 7. Security boundaries
No credential, account id, ARN, region, or notification address is committed;
the subscriber email enters only as a validated input at composition time. No
IAM, policy, SNS, or cross-account resource is created. The single documented
safety rule stands: cost reductions never weaken a security control; if a
projected estimate exceeds $200 without weakening a control, the rule is STOP
and reauthorize. A **fresh dated estimate remains mandatory before any
authorized `apply`** (INFRA-9) — this module does not produce it.

## 8. Staging-only assumptions
Single staging budget in a single account; thresholds fixed at 50/75/90/100%;
email-only notification (an SNS-based fan-out would be a later, separately
authorized change).

## 9. Scope boundaries (this tranche)
Implemented~~, but **uncomposed** (not in root `main.tf`), **unprovisioned**, and
**inactive**. No AWS access, no live `tofu` operation~~ *(struck 2026-10-13: accurate when written on 2026-07-24; composed later that day (PR #120); the budget was created by an apply on 2026-07-27 per CloudTrail — see the §1 notice)*, no root-composition
change, no other module touched *(by this tranche)*. Offline validation only: `tofu fmt`,
external-harness `tofu init -backend=false -lockfile=readonly` +
`tofu validate` with the committed root lockfile (locked `hashicorp/aws
6.55.0`), AWS credentials suppressed, artifacts outside the repository. GitHub
CI does not independently validate HCL *(stale since 2026-07-29 — the *Revision reader* CI job validates the root and runs offline `tofu test` suites; `.github/workflows/ci.yml`)*. ~~INFRA-4 remains incomplete (root
composition and the pre-live/remote-state requirements remain); INFRA-5
remains unstarted.~~ *(stale since 2026-07-24 — `../../README.md` §12)*

## 10. Owning tranche
Implemented by the INFRA-4 `cost` module resource-definition tranche. Root
composition, the fresh dated cost estimate, any live `plan`/`apply`
(INFRA-9), and INFRA-5 are later, separately authorized tranches.
