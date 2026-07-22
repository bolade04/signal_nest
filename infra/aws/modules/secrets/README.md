# Module: `secrets` (documentation-only stub)

## 1. Purpose
Secrets Manager + KMS references for the four staging secret fields — **by name
only**, never by value.

## 2. Planned AWS scope
Secrets Manager secret references (names/ARNs), KMS key/alias references. Values
are never created or committed by IaC.

## 3. Out of scope
IAM role definitions (`iam`), plaintext non-secret config (task env in `ecs`),
any secret value.

## 4. Planned upstream dependencies
None (referenced by `iam` and `ecs`).

## 5. Planned inputs (names only, no values)
`secret_key_ref`, `database_url_ref`, `redis_url_ref`, `llm_api_key_ref`,
`kms_key_alias`, `name_prefix`, `tags`.

## 6. Planned non-sensitive outputs (names only)
`secret_arns` (map of the four references), `kms_key_arn` (reference).

## 7. Security boundaries
Bound to the authoritative 87-field inventory (four Secrets Manager fields:
`secret_key`, `database_url`, `redis_url`, `llm_api_key`) — aligned with the
fail-closed **G5** contract. Injected only via the ECS `secrets` block
(`valueFrom`). Never plaintext env, build arg, image label, or mutable tag. No
secret value, ARN, or KMS id committed. The SPA receives no backend secret.

## 8. Staging-only assumptions
Staging-only secrets; not shared with production; mock provider/dev fallback
forbidden.

## 9. Status
No executable HCL yet. No resources created. Implementation and validation are
deferred.

## 10. Owning tranche
Real resource bodies belong to the later, separately authorized INFRA-4 module
implementation tranche; operationalization is INFRA-6. G5 remains unimplemented.
