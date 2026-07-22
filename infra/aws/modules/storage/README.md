# Module: `storage` (implemented — offline-validated, not root-composed)

## 1. Purpose and ownership
Owns exactly **one private, durable S3 bucket** for SIGNALNEST_STAGING application
object storage — uploads and artifacts accessed at runtime via presigned URLs. This
module is **implemented and offline-validated only**: it has been authored and
validated with OpenTofu offline, but it is **not** wired into the root composition
(`infra/aws/main.tf`), nothing is provisioned or deployed, and no AWS account has been
contacted.

This is **not** the SPA web-origin bucket (owned by `edge`), the remote-state bucket
(a later bootstrap concern), or secret storage (`secrets`).

## 2. Resources declared
| Resource | Purpose |
| --- | --- |
| `aws_s3_bucket` (×1) | The single private application bucket |
| `aws_s3_bucket_ownership_controls` | `BucketOwnerEnforced` — ACLs disabled, owner owns every object |
| `aws_s3_bucket_public_access_block` | All four public-access controls enabled |
| `aws_s3_bucket_server_side_encryption_configuration` | SSE-S3 (`AES256`) at rest, bucket keys enabled |
| `aws_s3_bucket_versioning` | Versioning enabled |
| `aws_s3_bucket_policy` | Denies any request not sent over TLS |

No ACL resource, no uploaded object or folder marker, no lifecycle rule, no KMS
resource, no IAM principal, and no secret value are declared.

## 3. Inputs
| Input | Type | Default | Notes |
| --- | --- | --- | --- |
| `bucket_name` | `string` | — (required) | Explicit, globally-unique lowercase bucket name; statically validated against S3 naming rules (3–63 chars, lowercase alphanumerics/hyphens/dots, start/end alphanumeric, no adjacent dots, no dot-hyphen adjacency, not IPv4-formatted). |
| `force_destroy` | `bool` | `false` | A non-empty staging bucket is never silently deleted unless a later operational tranche deliberately overrides this. |
| `tags` | `map(string)` | `{}` | Caller tags merged onto the bucket, supplementing (not replacing) the root provider `default_tags`. |

No account id, ARN, region, credential, or KMS input is accepted.

## 4. Outputs
| Output | Notes |
| --- | --- |
| `bucket_name` | Passed to the application (`S3_BUCKET`) by the future ECS/root-composition tranche. |
| `bucket_arn` | Consumed by the future `iam` module to scope the task-role S3 identity policy. |

## 5. Security controls
- **Encryption:** server-side encryption with S3-managed keys (`AES256`/SSE-S3), no
  KMS dependency; bucket keys enabled.
- **Versioning:** enabled — overwritten/deleted objects retain recoverable history.
- **Public access:** all four `public_access_block` controls enabled; **no** public
  read or write, **no** ACL resource.
- **Ownership:** `BucketOwnerEnforced` (ACLs disabled); application access is via the
  ECS task-role credential chain, not access keys.
- **Transport:** a bucket policy **denies any request where `aws:SecureTransport` is
  false** (TLS-only). This policy references **no** IAM role/principal.
- **`force_destroy` defaults to `false`.**

## 6. Dependency and integration boundaries
- **Upstream:** none. This module consumes no other module's output.
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
that instantiates this module with syntactically valid example inputs — the module is
**not** composed from `infra/aws/main.tf`. `TF_DATA_DIR` was external to the
repository, no backend was configured, no AWS credentials were used, and no `aws`
command or OpenTofu `plan`/`apply`/`state`/`refresh`/`import` ran. The root
`.terraform.lock.hcl` was **not** modified and **no** child lockfile, `.terraform`
directory, state, or plan artifact entered the repository.

## 8. Scope boundaries (this tranche)
- **No AWS account access**; no inspection of credentials, account identity, or region.
- **No provisioning or deployment**; nothing exists in AWS.
- **No root composition** — the bucket is not composed from `infra/aws/main.tf`.
- **No application activation** — `storage_backend` is unchanged; S3-backed storage is
  not turned on.
- **No operational storage claim** — successful offline validation does not mean any
  bucket exists or that the application uses it.
- **No IAM/ECS integration** — access permissions and application wiring are owned by
  later, separately authorized tranches.
- **INFRA-4 remains incomplete; INFRA-5 remains unstarted.**
