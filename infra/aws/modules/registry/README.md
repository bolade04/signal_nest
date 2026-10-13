# Module: `registry`

## 1. Purpose
Owns the staging container registry for SIGNALNEST_STAGING: **two** private Amazon
ECR repositories — `api` and `worker` — for the immutable, digest-pinned backend
runtime images, each with a safe untagged-image lifecycle policy. This module defines
resource bodies; ~~**applies nothing** — no infrastructure exists in AWS~~ *(struck 2026-10-13: accurate when written on 2026-07-22; stale since 2026-07-27 — see the notice)*.

> **Live-state notice (2026-10-13 read-only observation; `P6-GOV-1`).** Both repositories were present with immutable tags (created 2026-07-27). Their image inventory could **not** be read (describe/list images denied to the reading principal). CloudTrail management-event history, continuous from 2026-07-29 to 2026-10-13, holds the CI image-publisher role's layer uploads and two `PutImage` events on 2026-07-29 at 06:18Z and nothing after; the staging publish workflow's run list (GitHub) shows its last run created 2026-07-29T06:14Z on `ff15dbf9`; those two are the last of the eight pushes on 2026-07-27 to 2026-07-29 recorded by a read of 2026-09-27. Those signals fix the **last observed publish**; they do not show which image, if any, a task currently runs. The module is root-composed (`../../main.tf`, since 2026-07-24). Each fact here is a snapshot of 2026-10-13; current state is not recorded here. Root composition (`main.tf`) is configuration only and establishes neither provisioning nor deployment; the dated observations above are what establish existence, and nothing here establishes successful operation.

## 2. Owned resources (implemented here)
- `aws_ecr_repository` × **2** (`for_each` over `{api, worker}`) — private, immutable
  tags, scan-on-push, AES-256 encryption, force-delete disabled.
- `aws_ecr_lifecycle_policy` × **2** (one per repository) — expire only untagged
  images after N days; preserve all tagged images.

**Two `resource` blocks, four resource instances total** (2 repositories + 2 policies).
There is no repository policy, registry policy, replication, pull-through cache,
account-wide registry configuration, image resource/lookup, KMS resource, IAM
resource, random resource, or provisioner. ~~No image is built, tagged, scanned, or
pushed.~~ *(as a description of the module: it builds/pushes nothing — still true; as a live claim: stale since 2026-07-27, images were pushed on 2026-07-27 to 2026-07-29 — see the §1 notice)*

## 3. Two images, two repositories, three actors
The runtime model (merged PR #103 §26.5; executable `apps/api/Dockerfile` targets
`api`/`worker`; `.github/workflows/ci.yml` builds both) is:
- **API** repository — holds the API image (`uvicorn app.main:app`).
- **worker** repository — holds the worker image (`python -m app.jobs.worker`).
- The **API** task pins the **API** image digest; the **worker** task **and** the
  one-shot **migration** task both pin the **worker** image digest (migration
  runs the bare `python -m app.db.migrate` upgrade-and-verify entrypoint). API and worker are
  **independently built artifacts with distinct immutable digests** — one image does
  **not** serve all three actors. No third (migration/frontend) repository or image.

## 4. Upstream dependencies
**None.** Zero upstream module dependencies (§26.12). Downstream, one-way only:
- **`registry -> iam`** — `iam` uses the repository ARNs to scope least-privilege
  image-pull (execution role) / future publication permissions per repository.
- **`registry -> ecs`** — `ecs` combines each repository URL with a **separately
  verified immutable digest** to pin task-definition images.
- **`registry -> later image-publication operations`** — INFRA-5 CI/OIDC build+push.

No `iam -> registry -> iam` or `ecs -> registry -> ecs` cycle: this module consumes
no downstream output.

## 5. Inputs
| Name | Type | Default | Notes |
| --- | --- | --- | --- |
| `name_prefix` | string | — (required) | Lowercase alnum/hyphen, 2-63 chars, no leading/trailing or double hyphen; builds `<name_prefix>/api` and `<name_prefix>/worker`. No account/credential/region/endpoint/ARN/tag/digest data. |
| `untagged_image_retention_days` | number | `14` | Days after which **untagged** images expire; validated integer 1-365. Tagged images never expire. |

The two logical repositories (`api`, `worker`) are **fixed inside the module** — there
is no repository-list/map input, so a caller **cannot** add a third repository. No
`image_tag_mutability`, `lifecycle_policy`, repository-policy, or `tags` input exists
(posture is fixed in HCL; tags via provider `default_tags`).

## 6. Outputs
| Name | Value | Consumer |
| --- | --- | --- |
| `repository_arns` | map: `api`/`worker` → ECR repository ARN | `iam` |
| `repository_names` | map: `api`/`worker` → repository name (`<name_prefix>/<key>`) | `iam`, `ecs` |
| `repository_urls` | map: `api`/`worker` → repository URL | `ecs` |

Keyed by `api`/`worker` so downstream can address each independently — never collapsed
into one reference. No credential, authorization token, image tag, digest, manifest,
scan finding, policy JSON, or full resource object is output. ARNs/names/URLs are
references, not secret values, and are not marked sensitive.

## 7. Deterministic naming & tag posture
- Repository names: `${var.name_prefix}/api`, `${var.name_prefix}/worker`
  (e.g. `signalnest-staging/api`, `signalnest-staging/worker`).
- `image_tag_mutability = "IMMUTABLE"` on both — no overwrite; images are published
  with **unique immutable commit/release tags** and consumed by digest (`@sha256:`),
  never `latest`. `force_delete = false`.

## 8. Scan / encryption / lifecycle posture
- `scan_on_push = true` on both repositories (cannot be disabled via a module input).
- `encryption_configuration.encryption_type = "AES256"` (SSE-S3; the Secrets Manager
  customer-managed KMS key is **not** reused).
- Lifecycle policy per repository: expire **only untagged** images after
  `untagged_image_retention_days` (default 14) days; **all tagged release images are
  preserved**, so active deployment and rollback digests are never silently deleted.
  No tagged age/count expiration, no `latest` rule.

## 9. Artifact lifecycle separation (this module = stage 1 only)
Distinct stages, of which this module performs **only stage 1** declaratively:
(1) create the two registry containers → (2) build the API target → (3) build the
worker target → (4) scan both images → (5) publish each image to its repository with a
unique immutable commit/release tag → (6) require scan completion + fail-closed
acceptance per image → (7) resolve/record the API and worker digests independently →
(8) compose ECS API tasks with the verified API digest → (9) compose ECS worker tasks
with the verified worker digest → (10) run migrations with the verified worker digest +
migration command → (11) deploy under separate authorization. **Successful repository
creation does NOT mean an image exists, passed scanning, was digest-approved, that ECS
is ready, or that a migration may run.**

## 10. Status
Resource bodies authored and validated **offline only** (`tofu fmt`, `tofu init
-backend=false`, `tofu validate` via an isolated external harness pinning the committed
`hashicorp/aws 6.55.0`). ~~**No `tofu plan`/`apply`, no AWS API call, no ECR login/token,
no image build/tag/push/scan/digest, no state, no root composition, and no provisioning
have occurred. Nothing exists in AWS.**~~ *(struck 2026-10-13: accurate when written on 2026-07-22; "no root composition" stale since 2026-07-24; the rest stale since 2026-07-27 — see the §1 notice; current state is not recorded here)* Per the network/edge/alb/secrets convention,
`versions.tf` declares only the AWS provider source (no version constraint, no
`provider` block); the root owns the sole provider config/constraint/lockfile.

## 11. Owning tranche & future gates
INFRA-4 registry resource-definition tranche. Separately authorized and **not** started
here: `iam` (pull/publish policies), root composition of this module, INFRA-5 CI/OIDC
image build+push, per-image scan + fail-closed acceptance, digest resolution, `ecs`
implementation, live plan/apply (INFRA-9), deployment, and migration execution. ~~INFRA-4
remains incomplete; INFRA-5 remains unstarted.~~ *(stale since 2026-07-24 — `../../README.md` §12; the publish workflow has run, last on 2026-07-29)*
