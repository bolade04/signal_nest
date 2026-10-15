# Bootstrap root: OpenTofu remote-state backend (one-time, ~~configuration only~~ *(struck 2026-10-13: accurate when written on 2026-07-24; executed on 2026-07-27 — see §2)*)

## 1. What this is — and is not

This directory is the **one-time state-backend bootstrap root** fixed by
[`docs/operations/aws-staging-iac-plan.md`](../../../docs/operations/aws-staging-iac-plan.md)
§7: it declares the S3 remote-state bucket (SSE-KMS via a dedicated CMK,
versioned, all four public-access-block controls, TLS-only), the state CMK +
alias, and the DynamoDB lock table that the **main root's** currently-empty
`backend "s3"` block (`../backend.tf`) will later point at.

It is **not** a second SIGNALNEST_STAGING environment composition. The main
root README's §3 rule — *no second root module* — forbids parallel
**environment** roots (`environments/`, `staging/`, `production/`-style
splits); this directory composes **zero** environment resources (no VPC, ALB,
ECS, RDS, Redis, …). It exists only because a root cannot remote-back itself
into a bucket that does not exist yet: the backend object must be created by
something outside the environment root. See §3 of `../README.md` for the
explicit carve-out.

## 2. Execution status: ~~NOTHING APPLIED~~ executed on 2026-07-27 *(corrected 2026-10-13)*

~~**Configuration only.**~~ Per §7, "the state backend itself is provisioned once
under a later authorized step"; the repo status ledger (`../README.md` §12)
bundles that live bootstrap with the INFRA-9 fresh-authorization gate. In this
tranche the root was validated **offline only** (`tofu fmt`,
`tofu init -backend=false`, `tofu validate`, credentials suppressed): ~~**no
`plan`, no `apply`, no AWS API call — no bucket, table, or key exists in
AWS.**~~ *(struck 2026-10-13: accurate when written on 2026-07-24; stale since 2026-07-27, when this root was executed — `../README.md` §8 and `../operator-closure-contract.json` (2026-08-17 text) record the executed backend with DynamoDB-only locking, and a read-only observation of 2026-10-13 found the state bucket (created 2026-07-27, versioned, SSE-KMS with the state CMK, public access blocked), the state CMK (Enabled, yearly rotation) and the lock table, whose creation CloudTrail dates to 2026-07-27. The same observation found the lock table reporting **`INACCESSIBLE_ENCRYPTION_CREDENTIALS`** since 2026-10-07T15:00:03Z while the CMK described as Enabled; key policy and grants were unreadable by the reading principal and the **cause is UNKNOWN** — no OpenTofu operation that acquires the lock should be attempted while the table is in that state. The state bucket held one object at the key the main root's `backend.hcl.example` names, last modified 2026-07-28T20:31:16Z (metadata only; contents never read). Current state is not recorded here.)* OpenTofu is never auto-applied (`../README.md` §13).

## 3. Design notes

- **Local state, deliberately.** This root declares **no** backend block: its
  own tiny one-time state (resource identifiers only, no secret) stays with the
  operator and is never committed (`*.tfstate*` is git-ignored). The "state is
  always remote" rule applies to the environment root, whose backend this
  bootstrap creates.
- **Names are never committed** (§7). `state_bucket_name` and
  `lock_table_name` are REQUIRED variables supplied via a git-ignored
  `*.tfvars` at the live run; only `terraform.tfvars.example` (placeholder
  tokens) is tracked.
- **Locking mechanism.** §7 names a **DynamoDB lock table** primarily, allowing
  "the tool-native equivalent" (S3 `use_lockfile` conditional-write locking,
  supported by the pinned OpenTofu ≥ 1.12.3). DynamoDB is authored here as the
  plan's named mechanism; the live-bootstrap authorizer may drop the table in
  favor of `use_lockfile` before executing.
- **Own lockfile.** As a standalone root it owns its provider constraint and
  its committed `.terraform.lock.hcl` — byte-identical to the main root's lock
  (same provider, same range, same checksums).
- After the live bootstrap, the operator fills the main root's git-ignored
  `backend.hcl` from this root's outputs (template: `../backend.hcl.example`)
  and runs a backend-configured `tofu init` — all under the same later
  authorization, never before it. *(2026-10-13: the backend-configured `init` is inferred to have run — `../README.md` §8 — and the example template's state key matched the one live state object's key; whether the operator-held `backend.hcl` uses that key is an operator comparison this repository cannot make.)* *(2026-10-15: by operator statement no operator-held `backend.hcl` exists anywhere — operator-attested, not independently verified across every machine; the comparison was NOT PERFORMED and equality NOT ESTABLISHED, and on 2026-10-15 the operator explicitly waived it for the `P6-GOV-1` documentation criterion — a limited waiver, not a completed comparison; a future backend-configured `init` would need a backend configuration that, by that statement, is not currently in the operator's possession)*

## 4. Files

`versions.tf` (tool/provider pins), `providers.tf` (region + eight-tag
`default_tags`; local state), `variables.tf` (identity tags + required
bucket/table names), `locals.tf` (name prefix + tag set), `main.tf` (CMK +
alias, state bucket + hardening + TLS-deny policy, lock table), `outputs.tf`
(non-sensitive names/ARNs for `backend.hcl`), `terraform.tfvars.example`
(synthetic placeholders), `.terraform.lock.hcl` (committed lock).
