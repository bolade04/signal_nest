# Staging publish workflow (Phase 4B-C · INFRA-5)

- **Status:** AUTHORED, never executed. INFRA-5's stop boundary
  ([phase-4b-c-infra-plan.md](../phase-4b-c-infra-plan.md)) is verbatim
  "**authoring only; execution needs INFRA-9 authorization**", and its expected
  external resources are "none created by authoring the workflow". Merging this
  tranche creates **no** AWS resource, publishes **no** image, and produces
  **no** digest.
- **Workflow:** [`.github/workflows/staging-publish.yml`](../../.github/workflows/staging-publish.yml)
- **Authoritative parents:**
  [aws-staging-iac-plan.md](./aws-staging-iac-plan.md) (§26.5 two-image
  contract, §26.8 role architecture, §22 gates),
  [deployment-sha-wiring-plan.md](./deployment-sha-wiring-plan.md) (§B build
  provenance, §C runtime injection, §F G4 preflight, §G evidence),
  [deployment.md](./deployment.md), [phase-4b-c-infra-plan.md](../phase-4b-c-infra-plan.md)
  (INFRA-5 entry).

## 1. Two images, two repositories, three actors (§26.5 — locked)

| Logical image | Dockerfile target | Build context | Runtime | ECR repository | Consumed by |
| --- | --- | --- | --- | --- | --- |
| `api` | `apps/api/Dockerfile` `--target api` | `apps/api` | `uvicorn app.main:app`, port 8000, `/health` liveness, UID/GID 10001 | `<name_prefix>/api` | ECS **API** task definition (`repository@sha256:<api_image_digest>`) |
| `worker` | `apps/api/Dockerfile` `--target worker` | `apps/api` | `python -m app.jobs.worker`, no port, UID/GID 10001 | `<name_prefix>/worker` | ECS **worker** task definition **and** the one-shot **migration** task definition (same digest; migration runs the bare `python -m app.db.migrate` upgrade-and-verify entrypoint) |

There is **no third image and no frontend image**: the SPA is compiled
statically and served from the private S3 origin behind CloudFront (`edge`
module). The migration actor never gets its own image. Both images build for
**linux/amd64** (the ECS task definitions pin `LINUX`/`X86_64`).

## 2. Provenance stamping

`apps/api/Dockerfile` accepts `GIT_REVISION` / `IMAGE_CREATED` build args
(defaults `""`) and stamps `org.opencontainers.image.source`, `.revision`,
`.created`, and `.version` on the shared `runtime` stage — inherited by both
targets. The workflow passes the **trusted CI checkout SHA** (`github.sha`),
never user input, and verifies the label round-trip before any push.

Runtime `BUILD_REVISION` / `APPLICATION_VERSION` are deliberately **not** baked
into the image: an empty baked `ENV` would override the application's safe
local defaults (`build_revision=None`). Per wiring-plan §C, runtime injection
is owned by the ECS task-definition environment and is wired at the same later
tranche that supplies the digests.

## 3. Trigger and trust model

- **`workflow_dispatch` only.** No `push` trigger (a push trigger would execute
  the publish on every merge, violating the INFRA-5 stop boundary) and no
  `pull_request`/`pull_request_target` trigger of any kind — untrusted PR/fork
  code can never reach the OIDC-credentialed job.
- **Protected `staging` GitHub environment** on the single job: required human
  reviewer approval before any step runs, deployment branches restricted to
  `main`, plus an in-job hard guard `github.ref == 'refs/heads/main'`.
- **Permissions:** workflow-level `contents: read`; the single job adds
  `id-token: write` for itself only. No other permission is granted anywhere.
- **Concurrency:** a dedicated `staging-publish` group with
  `cancel-in-progress: false` — publications serialize and are never killed
  mid-push.
- **Action pinning:** major-version tags, matching the repository's existing
  `ci.yml` convention.

## 4. AWS authentication — GitHub OIDC, no long-lived keys

- **No AWS access key exists** in any GitHub secret, variable, or repository
  file, and none may ever be added for this path.
- The job assumes `vars.AWS_STAGING_PUBLISH_ROLE_ARN` via
  `aws-actions/configure-aws-credentials` with a 900-second session named
  `signalnest-staging-publish-<run_id>`, region `us-east-1` (ADR-0001).
- The job **fails closed** while the environment variables
  (`AWS_STAGING_PUBLISH_ROLE_ARN`, `AWS_STAGING_ACCOUNT_ID`, optional
  `AWS_STAGING_NAME_PREFIX`) are unset — they can only be populated after the
  INFRA-9-gated live prerequisites exist. After assuming the role it verifies
  `sts get-caller-identity` against the configured staging account **without
  printing the account id** (this is a public repository; wiring-plan §G
  forbids recording account ids or private registry URIs).

### Publisher role specification (IaC authored later, applied at INFRA-9)

The CI-OIDC **publisher role** is deferred by the locked §26.8 contract ("the
CI-OIDC deployment role ... is INFRA-5" — meaning this tranche *specifies* it;
its HCL and live creation belong to the later-authorized IaC/apply, since the
phase plan's expected INFRA-5 repository areas are `.github/workflows/` and
`docs/` and its expected external resources are none). The binding
specification the later tranche must implement:

- **OIDC provider:** `token.actions.githubusercontent.com`, audience
  `sts.amazonaws.com` (account-level; verify-don't-duplicate if one already
  exists).
- **Trust policy:** `sts:AssumeRoleWithWebIdentity` only, conditioned on
  `aud = sts.amazonaws.com` **and**
  `sub = repo:bolade04/signal_nest:environment:staging` (the protected
  environment subject — tighter than a branch subject because it carries the
  human-approval gate; no wildcard repository or ref subjects).
- **Permission policy (least privilege, exactly):**
  - `ecr:GetAuthorizationToken` on `*` (the sole documented Resource-`*`
    exception, mirroring the iam module's execution-role pattern);
  - `ecr:BatchCheckLayerAvailability`, `ecr:InitiateLayerUpload`,
    `ecr:UploadLayerPart`, `ecr:CompleteLayerUpload`, `ecr:PutImage`,
    `ecr:BatchGetImage`, `ecr:DescribeImages` scoped to **exactly the two**
    registry-module repository ARNs.
  - **No** `ecr:*`, no repository/lifecycle/policy administration, no IAM
    action, no ECS action, no Secrets Manager action, no production access.
    (ECS deploy permissions, if ever added, require a separate re-reviewed
    authorization; deployment execution is INFRA-9's.)
- **Session:** 900 seconds, name `signalnest-staging-publish-<run_id>`.

## 5. Build and security gates (before any credential is issued)

In order: cache-free `linux/amd64` builds of both targets with the SHA build
args → `scripts/docker-security-check.sh` on both images (non-root,
secret-free) → API import smoke → migration-actor + schema-gate smoke in the
worker image → OCI-revision/platform verification → Trivy vulnerability scan
of both images failing on **CRITICAL/HIGH** (the plans require fail-closed
scan acceptance without fixing a threshold; INFRA-5 sets CRITICAL+HIGH,
`ignore-unfixed: true`, subject to review — ECR `scan_on_push` re-scans after
the push). Only after every gate passes does the job authenticate to AWS.

## 6. Publication, digest read-back, and handoff

- Each image is pushed under its **immutable full-commit-SHA tag** (the
  registry enforces tag immutability). `latest` is never pushed or referenced.
- Before any push output can be emitted, the **account-bearing ECR registry
  hostname is registered as a log mask** (`::add-mask::`), so `docker push`'s
  own repository-host output — and any future line referencing the registry —
  is redacted from the public job log (wiring-plan §G; the ECR login action's
  `mask-password` covers only the auth token, not the hostname).
- The **registry-reported digest** is read back via `aws ecr describe-images`
  per repository and cross-checked against the local push digest; a fabricated
  or missing digest fails the run. The digest — not any tag — is the artifact
  of record (wiring-plan §D).
- A **non-secret digest manifest** artifact
  (`staging-digest-manifest-<sha>`) records: source commit, platform, run
  id/attempt, and per-image logical repository + tag + registry digest —
  **never** an account id or private registry URI (§G). Retained 90 days.
- **Operator handoff:** copy `api_image_digest` / `worker_image_digest` from
  the manifest into the git-ignored `*.tfvars` consumed by the composed root
  (`infra/aws/variables.tf` validates `^sha256:[0-9a-f]{64}$`). The migration
  task definition reuses `worker_image_digest` automatically (§26.5). Real
  tfvars are never committed.

## 7. What a run can never do

No ECS API call, task-definition registration, service update, migration
execution against any real database, secret read/write, tfvars mutation, or
deployment of any workload. Rollback of an unwanted publication is simply to
not consume its digest — pushed images are inert until INFRA-9 pins them.

## 8. Execution preconditions (all INFRA-9-gated, none exist today)

*[Sequencing corrected 2026-07-24, INFRA-9 execution-path tranche — this
supersedes the earlier single-apply phrasing of step 2, which named
"composed-root apply creating the two ECR repositories AND the CI-OIDC
publisher role" as one event. That is not executable: the composed root's
`api_image_digest`/`worker_image_digest` are plan-time-validated and the ECS
task defs consume them, so the root cannot plan until real digests exist — yet
the digests require the ECR repos + a publish run. The composed root now has a
`deploy_workload` flag (default `false` = foundation stage) that creates the
ECR repos, cluster, log groups, SGs, roles, data stores, and secret containers
WITHOUT the API/worker task definitions/services (which need digests). The
CI-publisher role is created in the `iam` module when `github_oidc_provider_arn`
is supplied.]*

1. Live remote-state bootstrap (`infra/aws/bootstrap/`) executed under fresh
   authorization.
2. **Foundation-stage** composed-root apply (`deploy_workload = false`) creating
   the two ECR repositories (`registry` module) and — if
   `github_oidc_provider_arn` is supplied — the CI-publisher role (`iam`
   module); the account-wide GitHub OIDC provider itself is a consumed external
   prerequisite, not created by the root.
3. The `staging` environment variables populated with the resulting non-secret
   identifiers (including the publisher role ARN output `ci_publisher_role_arn`).
4. Fresh human authorization to run the publish itself → the manifest → tfvars
   handoff (both digests) → secret population → the **workload-stage** apply
   (`deploy_workload = true` with the real digests) that creates the task
   definitions/services → the one-shot migration run.

Until all four hold, every dispatch of this workflow fails closed at the
prerequisite guard without contacting AWS.

## 9. Known limitations (disclosed, separately authorized follow-ups)

- **No Python dependency lockfile exists** (`apps/api/pyproject.toml` uses
  floor constraints; the image installs `.[full]`). Reproducibility in the
  authoritative plans means deterministic **source SHA → immutable digest**
  provenance, which this workflow implements; byte-identical dependency
  re-resolution is not guaranteed and would require a separately authorized
  hash-pinned lockfile tranche.
- **No SBOM/signed-provenance attestation** — not required by any
  authoritative plan; revisit for production.
- The Trivy CRITICAL/HIGH threshold is an INFRA-5-set policy pending an
  explicit repository-wide severity decision.
