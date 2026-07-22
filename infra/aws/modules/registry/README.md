# Module: `registry` (documentation-only stub)

## 1. Purpose
Private container registry for the immutable, digest-pinned API and worker images.

## 2. Planned AWS scope
ECR repositories (immutable tags), lifecycle policies, repository access policy.

## 3. Out of scope
Image build/push (INFRA-5 CI/OIDC), ECS services (`ecs`), IAM role definitions
(`iam`).

## 4. Planned upstream dependencies
None (referenced by `ecs` and by the future INFRA-5 build workflow).

## 5. Planned inputs (names only, no values)
`repository_names`, `image_tag_mutability`, `lifecycle_policy`, `name_prefix`,
`tags`.

## 6. Planned non-sensitive outputs (names only)
`repository_urls`, `repository_arns` (references for IAM/ECS wiring).

## 7. Security boundaries
Immutable tags (no overwrite); images pulled by digest (`@sha256:`). No account
id, ARN, or digest committed. Pull access via least-privilege execution role.

## 8. Staging-only assumptions
Staging repositories only; one image serves api/worker/migration actors.

## 9. Status
No executable HCL yet. No resources created. Implementation and validation are
deferred.

## 10. Owning tranche
Real resource bodies belong to the later, separately authorized INFRA-4 module
implementation tranche.
