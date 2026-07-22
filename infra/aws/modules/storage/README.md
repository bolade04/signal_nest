# Module: `storage` (documentation-only stub)

## 1. Purpose
S3 object storage for the API (uploads/artifacts) with presigned-URL access.

## 2. Planned AWS scope
S3 application bucket(s), lifecycle rules, server-side encryption (SSE), bucket
policy / public-access block.

## 3. Out of scope
The SPA web-origin bucket (owned by `edge`), remote-state bucket (bootstrap,
later), secret storage (`secrets`).

## 4. Planned upstream dependencies
None. `storage` creates and outputs `bucket_arn`; `iam` consumes it to scope the task-role
S3 identity policy (producer → consumer: **`storage -> iam`**). `storage` does **not** consume
any `iam` output — access is granted by the IAM identity policy, not by a bucket policy that
references a role — so there is **no** `storage ↔ iam` cycle
(`docs/operations/aws-staging-iac-plan.md` §26.1/§26.8).

## 5. Planned inputs (names only, no values)
`bucket_name`, `kms_key_id`, `lifecycle_rules`, `name_prefix`. No `tags` input (provider
`default_tags`).

## 6. Planned non-sensitive outputs (names only)
`bucket_id`, `bucket_arn` (reference for IAM policy wiring).

## 7. Security boundaries
Private buckets; public access blocked; SSE enabled. App access via the ECS
task-role credential chain (no access keys). Object access via presigned URLs.
No bucket name or ARN committed.

## 8. Staging-only assumptions
Staging bucket(s) only; no production or customer data.

## 9. Status
No executable HCL yet. No resources created. Implementation and validation are
deferred.

## 10. Owning tranche
Real resource bodies belong to the later, separately authorized INFRA-4 module
implementation tranche.
