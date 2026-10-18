# Module: `secrets`

## 1. Purpose
Owns the staging secret **containers** and their encryption key for SIGNALNEST_STAGING:
one customer-managed KMS key (+ deterministic alias) and **four EMPTY** AWS Secrets
Manager secret containers — **by name only, never by value**. This module defines
resource bodies; ~~**applies nothing** — no infrastructure exists in AWS~~ *(struck 2026-10-13: accurate when written on 2026-07-22; stale since 2026-07-27 — see the notice)*.

> **Live-state notice (2026-10-13 read-only observation; `P6-GOV-1`).** The secrets CMK (Enabled, yearly rotation) and the four containers were observed; container metadata showed one current version each, last changed 2026-07-28 — values were not read and are never read from this repository. The CMK's key policy and grants, and the `DATABASE_URL` container's resource policy, were unreadable by the reading principal. The module is root-composed (`../../main.tf`, since 2026-07-24). Each fact here is a snapshot of 2026-10-13; current state is not recorded here. Root composition (`main.tf`) is configuration only and establishes neither provisioning nor deployment; the dated observations above are what establish existence, and nothing here establishes successful operation.

## 2. Owned resources (implemented here)
- `aws_kms_key` — one symmetric customer-managed key (ENCRYPT_DECRYPT, rotation on,
  single-region, recoverable delete) encrypting the four secret containers.
- `aws_kms_alias` — one deterministic alias `alias/<name_prefix>/secrets`.
- `aws_secretsmanager_secret` × **4** — empty containers named
  `<name_prefix>/SECRET_KEY`, `/DATABASE_URL`, `/REDIS_URL`, `/LLM_API_KEY`, each
  encrypted by the module KMS key.

There is **no** `aws_secretsmanager_secret_version`, `secret_string`, `secret_binary`,
generated/placeholder value, random resource, SSM parameter, replica, IAM/ECS
resource, or provisioner. An empty container is intentional.

## 3. Out of scope
IAM roles/policies (`iam`), ECS task definitions and secret injection (`ecs`), the
plaintext non-secret task env (`ecs`), and **any secret value** (populated out-of-band,
INFRA-6). The S3/CloudFront web origin and its keys are unrelated (`edge`/`storage`).

## 4. Upstream dependencies
**None.** This module has **zero** upstream module dependencies (§26.1/§26.12): it
consumes no `network`/`edge`/`alb`/`registry`/`storage`/`data_sql`/`data_cache`/`iam`/
`ecs`/`observability`/`cost` output, no role/execution ARN, no database/cache endpoint,
no secret value, and no AWS access key. Downstream, one-way only:
- **`secrets -> iam`** — `iam` consumes `secret_arns` + `kms_key_arn` to build
  least-privilege identity policies.
- **`secrets -> ecs`** — `ecs` consumes `secret_arns` for task-definition `secrets`
  `valueFrom` injection.
- **`secrets -> storage`** — *(added 2026-10-18, P6-CI-6, operator decision of that date)* `storage` consumes `kms_key_arn` as the application bucket's SSE-KMS key: the live role permissions boundary lets the bounded task roles use this key and no other. The key itself is unchanged here.

No `secrets -> iam -> secrets`, `secrets -> ecs -> secrets`, or `data -> secrets -> data`
cycle is possible because this module consumes nothing downstream.

## 5. Inputs
| Name | Type | Default | Notes |
| --- | --- | --- | --- |
| `name_prefix` | string | — (required) | Lowercase alnum/hyphen, 2-63 chars; builds the four secret names and the KMS alias. No account/credential/secret/endpoint/ARN data. |
| `secret_recovery_window_in_days` | number | `30` | Secrets Manager recovery window; validated integer 7-30 (no 0-day force delete). |
| `kms_deletion_window_in_days` | number | `30` | KMS key deletion window; validated integer 7-30. |

No `tags` input: the authoritative eight-tag common set is applied by the root
provider's `default_tags` (`providers.tf`); this module adds only the conventional
per-resource `Name` tag.

## 6. Outputs
| Name | Value | Consumer |
| --- | --- | --- |
| `secret_arns` | map: logical key → Secrets Manager container ARN | `iam`, `ecs` |
| `secret_names` | map: logical key → container name (`<name_prefix>/<KEY>`) | `iam`, `ecs` |
| `kms_key_arn` | ARN of the secrets CMK | `iam` (`kms:Decrypt` scoping; *(2026-10-18, P6-CI-6)* the task roles' S3-scoped KMS grant), `storage` *(2026-10-18, P6-CI-6: the bucket's SSE-KMS key)* |
| `kms_key_id` | key id of the secrets CMK | reference |
| `kms_alias_name` | `alias/<name_prefix>/secrets` | reference |

ARNs/names/ids are configuration references, **not** secret values, and are not marked
sensitive. No secret value, version, ciphertext, credential, account identity, or key
policy is output.

## 7. Deterministic naming
- Secret names: `${var.name_prefix}/<LOGICAL_KEY>` (e.g. `signalnest-staging/SECRET_KEY`).
- KMS alias: `alias/${var.name_prefix}/secrets` (e.g. `alias/signalnest-staging/secrets`).
No real deployment name, account id, region, endpoint, credential, or secret value is
committed.

## 8. Secret container vs. value lifecycle (§26.6)
This module creates **containers only**. Secret **values** never enter Git, HCL,
`.tfvars`, OpenTofu variables/locals/outputs/plan files/state, logs, or CI artifacts.
`DATABASE_URL`/`REDIS_URL` embed endpoints produced by `data_sql`/`data_cache` and can
be composed only **after** those resources exist; all four values are populated
**out-of-band under a separately authorized operational procedure (INFRA-6)**. The
permanent secret-value operator principal remains an unresolved live-operation gate; no
human or automation principal is invented here. **Successful container creation does
NOT make secrets ready for ECS** — the future live order is: (1) deploy prerequisite
infra → (2) obtain DB/cache endpoints via the approved path → (3) populate values
out-of-band → (4) fail-closed G5 secret-readiness check → (5) create/start ECS services
→ (6) execute the migration task under separate authorization. **None of that live
sequence is authorized now.**

## 9. KMS policy / delegation model
No explicit key `policy` is declared. AWS applies its **default key policy**, which
grants the key's **own account** root full access and delegates access control to that
account's IAM. This is deliberate: it enables the account without binding any
IAM-module role ARN, preserving one-way `secrets -> iam` and avoiding an
`iam -> secrets -> iam` cycle. The AWS default policy's `Resource: "*"` is the
conventional scope of a KMS **key resource policy** (it means "this key"), **not**
permission for application/execution roles to use every KMS key. Application and
execution-role KMS permissions (`kms:Decrypt` scoped to `kms_key_arn`) belong to the
later, separately authorized `iam` tranche. `bypass_policy_lockout_safety_check = false`
keeps the account-lockout safety active.

## 10. Deletion/recovery, rotation, tagging
- Secrets Manager recovery window: 30 days (recoverable; no force delete).
- KMS deletion window: 30 days; key rotation **enabled**; single-region; symmetric.
- Tags via the root provider's `default_tags` (+ per-resource `Name`).

## 11. Status
Resource bodies authored and validated **offline only** (`tofu fmt`, `tofu init
-backend=false`, `tofu validate` via an isolated external harness pinning the committed
`hashicorp/aws 6.55.0`). ~~**No `tofu plan`/`apply`, no AWS API call, no state, no secret
version/value, no root composition, and no provisioning have occurred. Nothing exists in
AWS.** This module is not root-composed or deployed.~~ *(struck 2026-10-13: accurate when written on 2026-07-22; "no root composition" stale since 2026-07-24 (`../../main.tf`); the rest stale since 2026-07-27 — see the §1 notice; whether any workload consumes the values is not established (no ECS service observed on 2026-10-13); current state is not recorded here)* Per the network/edge/alb convention,
`versions.tf` declares only the AWS provider **source** (no version constraint, no
`provider` block); the root module owns the sole provider config, version constraint, and
committed lockfile, all inherited by this child module.

## 12. Owning tranche & future gates
INFRA-4 secrets resource-definition tranche. Separately authorized and **not** started
here: `iam` (role/policy) implementation, root composition of this module, secret-value
population (INFRA-6) + G5 readiness, `ecs` implementation, live plan/apply (INFRA-9),
and deployment/migration execution. ~~INFRA-4 remains incomplete; INFRA-5 remains
unstarted.~~ *(stale since 2026-07-24: INFRA-4's repository scope and INFRA-5 workflow authoring completed that day — `../../README.md` §12)*
