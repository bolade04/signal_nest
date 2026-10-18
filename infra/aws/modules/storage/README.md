# Module: `storage` (implemented — offline-validated, ~~not root-composed~~ root-composed since 2026-07-24 *(corrected 2026-10-13)*)

## 1. Purpose and ownership
Owns exactly **one private, durable S3 bucket** for SIGNALNEST_STAGING application
object storage — uploads and artifacts accessed at runtime via presigned URLs. This
module is **implemented and offline-validated**: it has been authored and
validated with OpenTofu offline ~~, but it is **not** wired into the root composition
(`infra/aws/main.tf`), nothing is provisioned or deployed, and no AWS account has been
contacted~~ *(struck 2026-10-13: accurate when written on 2026-07-22; the composition clause is stale since 2026-07-24 (`../../main.tf`), the rest since 2026-07-27 — see the notice)*.

> **Live-state notice (2026-10-13 read-only observation; `P6-GOV-1`).** The application bucket was observed (created 2026-07-27) with versioning enabled, SSE-S3, all four public-access-block controls and no lifecycle rule — as authored. A listing of its current objects returned **none**; non-current versions and delete markers could not be listed (denied), so "no object stored" is established for current objects only. Whether the application uses the bucket is not established (no ECS service was observed). Each fact here is a snapshot of 2026-10-13; current state is not recorded here. *(Added 2026-10-18, P6-CI-6: the configuration now authors SSE-KMS with the secrets CMK — §2/§5; "as authored" above refers to the configuration authored when the observation was made (SSE-S3). The change is not applied.)* Root composition (`main.tf`) is configuration only and establishes neither provisioning nor deployment; the dated observations above are what establish existence, and nothing here establishes successful operation.

This is **not** the SPA web-origin bucket (owned by `edge`), the remote-state bucket
(a later bootstrap concern), or secret storage (`secrets`).

## 2. Resources declared
| Resource | Purpose |
| --- | --- |
| `aws_s3_bucket` (×1) | The single private application bucket |
| `aws_s3_bucket_ownership_controls` | `BucketOwnerEnforced` — ACLs disabled, owner owns every object |
| `aws_s3_bucket_public_access_block` | All four public-access controls enabled |
| `aws_s3_bucket_server_side_encryption_configuration` | ~~SSE-S3 (`AES256`) at rest~~ *(2026-10-18, P6-CI-6: SSE-KMS at rest with the secrets CMK, S3 Bucket Key on; IaC finding AWS-0132)* |
| `aws_s3_bucket_versioning` | Versioning enabled |
| `aws_s3_bucket_policy` | Denies any request not sent over TLS |

No ACL resource, no uploaded object or folder marker, no lifecycle rule, no KMS
resource (the key is the `secrets` module's, consumed by ARN), no IAM principal, and no secret value are declared.

## 3. Inputs
| Input | Type | Default | Notes |
| --- | --- | --- | --- |
| `bucket_name` | `string` | — (required) | Explicit, globally-unique lowercase bucket name; statically validated against S3 naming rules (3–63 chars, lowercase alphanumerics/hyphens/dots, start/end alphanumeric, no adjacent dots, no dot-hyphen adjacency, not IPv4-formatted). |
| `force_destroy` | `bool` | `false` | A non-empty staging bucket is never silently deleted unless a later operational tranche deliberately overrides this. |
| `tags` | `map(string)` | `{}` | Caller tags merged onto the bucket, supplementing (not replacing) the root provider `default_tags`. |
| `kms_key_arn` | `string` | — (required) | *(Added 2026-10-18, P6-CI-6.)* The secrets CMK ARN (`module.secrets.kms_key_arn`), the bucket's SSE-KMS key; a key ARN, never an alias (validated). |

~~No account id, ARN, region, credential, or KMS input is accepted.~~ *(struck 2026-10-18, P6-CI-6: the module now accepts one KMS key ARN, `kms_key_arn`; it still accepts no account id, region or credential.)*

## 4. Outputs
| Output | Notes |
| --- | --- |
| `bucket_name` | Passed to the application (`S3_BUCKET`) by the future ECS/root-composition tranche. |
| `bucket_arn` | Consumed by the future `iam` module to scope the task-role S3 identity policy. |

## 5. Security controls
- **Encryption:** ~~server-side encryption with S3-managed keys (`AES256`/SSE-S3), no
  KMS dependency.~~ *(2026-10-18, P6-CI-6, operator decision of that date:)* SSE-KMS with the **secrets** CMK and the S3 Bucket Key on. A dedicated data key is not used because the live role permissions boundary denies the bounded api/worker task roles every KMS data-key operation outside the secrets CMK (`scripts/gen_boundary_policy.py`, `DenyKmsUseOutsideSecretsCmk`), which would make every application read and write fail. The task roles reach the key only through S3 for this bucket (`iam` module, `AppBucketKmsViaS3Only`: `kms:ViaService` = S3 and the encryption context = this bucket's ARN, the context S3 uses with a Bucket Key). Turning the Bucket Key off fails closed (every object operation denied).
- **Versioning:** enabled — overwritten/deleted objects retain recoverable history.
- **Public access:** all four `public_access_block` controls enabled; **no** public
  read or write, **no** ACL resource.
- **Ownership:** `BucketOwnerEnforced` (ACLs disabled); application access is via the
  ECS task-role credential chain, not access keys.
- **Transport:** a bucket policy **denies any request where `aws:SecureTransport` is
  false** (TLS-only). This policy references **no** IAM role/principal.
- **`force_destroy` defaults to `false`.**

## 6. Dependency and integration boundaries
- **Upstream:** ~~none. This module consumes no other module's output.~~ *(2026-10-18, P6-CI-6:)* one — `secrets -> storage` (`kms_key_arn`). `secrets` consumes nothing, so no cycle is created; the edge is an addition to the §26.12 graph made by that operator decision.
- **Downstream (one-way):** `storage -> iam` — the future `iam` module consumes
  `bucket_arn` to build the least-privilege task-role S3 identity policy. This module
  never consumes an `iam` output, so there is **no** `storage ↔ iam` cycle
  (`docs/operations/aws-staging-iac-plan.md` §26).
- The future **ECS/root-composition** tranche passes `bucket_name` to the application.
- This module creates **no** users, roles, access keys, credentials, ECS definitions,
  environment variables, or secret values, and does **not** grant application access.

## 7. Validation performed
Offline only: `tofu fmt`/`tofu fmt -check`, and `tofu init -backend=false` +
`tofu validate` run through a temporary, **repository-external** validation harness
that instantiates this module with syntactically valid example inputs — ~~the module is
**not** composed from `infra/aws/main.tf`.~~ *(struck 2026-10-14: accurate when written on 2026-07-22 (UTC); stale since 2026-07-24, when the root composition added `module "storage"` in `b2567484` — see [`infra/aws/main.tf`](../../main.tf); this note records root composition only, not provisioning, deployment, applied commit or operation)* `TF_DATA_DIR` was external to the
repository, no backend was configured, no AWS credentials were used, and no `aws`
command or OpenTofu `plan`/`apply`/`state`/`refresh`/`import` ran. The root
`.terraform.lock.hcl` was **not** modified and **no** child lockfile, `.terraform`
directory, state, or plan artifact entered the repository.

## 8. Scope boundaries (this tranche)
- **No AWS account access**; no inspection of credentials, account identity, or region *(a description of the authoring tranche, 2026-07-22; dated read-only observations since 2026-09-27 are recorded in `../../README.md`)*.
- ~~**No provisioning or deployment**; nothing exists in AWS.~~ *(struck 2026-10-13: stale since 2026-07-27 — the bucket was observed on 2026-10-13; deployment of a workload that uses it was not observed)*
- ~~**No root composition** — the bucket is not composed from `infra/aws/main.tf`.~~ *(struck 2026-10-13: stale since 2026-07-24 — composed in `../../main.tf`)*
- **No application activation** — `storage_backend` is unchanged; S3-backed storage is
  not turned on.
- **No operational storage claim** — successful offline validation does not mean any
  bucket exists or that the application uses it.
- **No IAM/ECS integration** — access permissions and application wiring are owned by
  later, separately authorized tranches.
- ~~**INFRA-4 remains incomplete; INFRA-5 remains unstarted.**~~ *(stale since 2026-07-24 — `../../README.md` §12)*
