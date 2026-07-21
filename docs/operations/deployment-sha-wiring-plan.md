# Deployment SHA wiring plan (Phase 4B-C · INFRA-2)

- **Status:** Planning. **Documentation only.** This tranche authorizes **no** code,
  container, workflow, or infrastructure change, **no** AWS authentication, **no**
  provisioning, and **no** deployment. It defines *how* a later, separately authorized
  implementation tranche will wire the immutable Git commit SHA through the build and
  deployment path.
- **Phase:** 4B-C.INFRA-2 (container and runtime hardening — SHA-provenance planning).
- **Baseline SHA:** `f71d58ce895329d848631650004ebfe1f6227b9b`.
- **Authoritative parents:** [adr-0001-aws-ecs-fargate-staging.md](../architecture/adr-0001-aws-ecs-fargate-staging.md),
  [aws-staging-runtime-contract.md](./aws-staging-runtime-contract.md),
  [phase-4b-c-infra-plan.md](../phase-4b-c-infra-plan.md), [deployment.md](./deployment.md).
- **Satisfies:** the INFRA-2 planning requirement and defines readiness gate **G4**
  (deployment-SHA preflight) referenced by the canary readiness sequence.

> **No runtime flip.** Nothing in this plan enables a capability, creates an override, flips
> a global flag, or deploys an artifact. The three flags
> (`opportunity_feedback_enabled`, `scout_scheduling_enabled`, `connector_rss_enabled`)
> remain `False`. **Implementation, provisioning, deployment, and the live canary each
> require separate, explicit, later authorization.**

---

## 0. Current repository behavior (evidence, not assumption)

Derived from the baseline source at `f71d58c`:

- **Settings fields already exist** (`apps/api/app/core/config.py`):
  - `application_version: str = "0.0.0"` — non-secret service version, default `"0.0.0"`.
  - `build_revision: str | None = None` — non-secret build identifier, default `None`.
  - `Settings` uses `pydantic-settings` with `case_sensitive=False`, `extra="ignore"`, no
    `env_prefix`. The environment variables are therefore **`APPLICATION_VERSION`** and
    **`BUILD_REVISION`** (field names, case-insensitive).
- **Only consumer today is the worker.** `apps/api/app/jobs/worker.py` passes both settings
  into the worker registry (`apps/api/app/jobs/worker_registry.py`), which persists them on
  the `worker_registrations` row (`apps/api/app/jobs/worker_models.py`:
  `application_version String(32)`, `build_revision String(64)`).
- **They are deliberately NOT exposed** in operator fleet diagnostics
  (`apps/api/app/jobs/worker_schemas.py` omits `build_revision`/`application_version`), and an
  isolation test forbids them from leaking into that surface
  (`apps/api/app/tests/test_api_isolation.py`).
- **They are NOT in health/readiness output.** `GET /health` (liveness) and `GET /readiness`
  (`apps/api/app/system/routes.py`, `internal_routes.py`, `app/system/probes.py`) do not
  currently emit either value.
- **The image bakes in no revision.** `apps/api/Dockerfile` has **no** `ARG`, **no**
  `org.opencontainers.image.*` `LABEL`, and **no** version `ENV`. It pins `python:3.12-slim`
  (never `latest`), runs non-root (UID/GID `10001`), and is read-only-root compatible.
- **CI builds mutable-tagged images and never publishes.** The `Container build and
  security` job (`.github/workflows/ci.yml`) builds `signalnest-api:ci` and
  `signalnest-worker:ci` with `docker/build-push-action@v6`, `load: true`, **no** `build-args`,
  **no** OCI labels, **no** registry push, **no** digest capture.
- **One image, three roles.** API (`uvicorn app.main:app`), worker
  (`python -m app.jobs.worker`), and the one-shot migration actor
  (`python -m app.db.migrate`, `apps/api/app/db/migrate.py`) all run from the **same**
  multi-stage image (`runtime` stage). Alembic single head: `98289430a3ec`.

### Gaps this plan closes (for the later implementation tranche)

| # | Gap | Consequence today |
| - | --- | ----------------- |
| 1 | No build-time SHA injection into the image | `build_revision` is `None` at runtime |
| 2 | No OCI `revision`/`source`/`created`/`version` labels | Image carries no provenance |
| 3 | No immutable SHA-based image tag or digest recording | No traceable deployed-artifact identity |
| 4 | `application_version` default `"0.0.0"` is not deterministic per build | Cannot correlate runtime to source |
| 5 | Revision not surfaced to operators (logs / readiness metadata) | No non-secret runtime provenance signal |
| 6 | No preflight that fails closed on missing/mismatched revision | A canary could run on an unverified artifact |

None of these are changed in INFRA-2. Each is mapped to a future tranche in §I.

---

## A. Canonical revision identity

- The **canonical revision** is the complete **40-character lowercase hexadecimal Git commit
  SHA** checked out by CI for the build — the exact source the image was built from.
- It is **never** derived from a branch name, PR number, build timestamp, `latest` tag, or a
  local checkout inspected after the fact.
- **`build_revision` will equal that full 40-character SHA** on every component of a
  deployment.
- **`application_version`** is a **deterministic value derived from the same build**. In
  staging it is set to the **same full Git SHA** (simplest one-to-one correlation). If a
  future release scheme needs a semantic version, it must remain deterministically derivable
  from the same source commit and recorded in the deployment evidence; it must not be a
  free-form or build-time-random value. The `application_version` column is `String(32)`,
  which accommodates a 40-char SHA only if widened — the later tranche must either widen the
  column or store a 12-char short-SHA for `application_version` while `build_revision`
  (`String(64)`) carries the full 40-char SHA. **The full 40-char SHA is authoritative;**
  any short form is a display convenience derived from it. This column-width decision is
  recorded here so the implementation tranche does not re-decide it.
- The INFRA-1 runtime contract already requires the first deploy to be **exact SHA
  `3aadb8a1da0f26ffd183a4b05161747038d5957c`**; this plan preserves that requirement
  unchanged and generalizes the mechanism to any authorized future SHA.

## B. Build-time provenance

A later build tranche will:

- Capture the immutable Git SHA from the **trusted CI checkout** (the SHA CI itself resolved),
  not from any user-supplied field.
- Pass it into the image build as a **build argument** whose value CI controls (never
  arbitrary user-controlled input). Illustrative only — **do not implement in INFRA-2**:

  ```dockerfile
  # FUTURE (INFRA-2 implementation tranche) — DO NOT ADD NOW
  ARG GIT_REVISION
  ARG IMAGE_CREATED
  ENV BUILD_REVISION=${GIT_REVISION} \
      APPLICATION_VERSION=${GIT_REVISION}
  LABEL org.opencontainers.image.revision="${GIT_REVISION}" \
        org.opencontainers.image.source="https://github.com/<ORG>/<REPO>" \
        org.opencontainers.image.created="${IMAGE_CREATED}" \
        org.opencontainers.image.version="${GIT_REVISION}"
  ```

- Store the SHA in **`org.opencontainers.image.revision`** (mandatory) and additionally set:
  - `org.opencontainers.image.source` — the repository URL (public, non-secret).
  - `org.opencontainers.image.created` — the RFC 3339 build timestamp.
  - `org.opencontainers.image.version` — the deterministic version (§A).
- Apply an **immutable SHA-based image tag** (e.g. `:<FULL_GIT_SHA>`) for human traceability,
  **while treating the image digest as the strongest deployed-artifact identifier**. A tag is
  a convenience pointer; a mutable tag is **never** authoritative evidence.
- Preserve the **image digest** (`sha256:<IMAGE_DIGEST>`) as the artifact of record.

> Build args / labels above are **illustrative**. INFRA-2 adds none of them; they belong to
> the later implementation tranche (§I).

## C. Runtime injection

The future deployment process makes the revision available through the **existing** settings,
using their **existing** environment variable names:

- **`BUILD_REVISION`** → `settings.build_revision` — set to the full 40-char SHA.
- **`APPLICATION_VERSION`** → `settings.application_version` — set to the deterministic value
  from §A (staging: the same SHA / its short form per the column-width decision).

Injection rules:

- Values are supplied by the **ECS task definition** (container environment), populated from
  the deployment pipeline / IaC variable that carries the authorized SHA — never from tenant
  or client input, request headers, or a mutable lookup.
- Every component of one deployment (**API, worker, one-shot migration actor**) receives the
  **same** `BUILD_REVISION` / `APPLICATION_VERSION`, because all three run the **same image
  digest**. A single task-definition family (or shared container definition) supplies the pair
  identically to each role.
- Because API, worker, and migration actor share one image digest, their revision is
  identical by construction; the preflight (§F) verifies this rather than assuming it.
- **Tenant/client input must never control these fields** — they are deployment provenance,
  not request data. `pydantic-settings` reads them only from the process environment, never
  from request scope.

> No task definition is created or modified and no IaC is authored in INFRA-2.

## D. Immutable artifact selection

Intended AWS ECS/Fargate behavior (defined here, applied later):

- The **ECR image is built once** for a given source SHA.
- Deployment selects the exact image **by digest** wherever supported (task-definition
  `image` pinned to `...@sha256:<IMAGE_DIGEST>`), not by a mutable tag.
- **API, worker, and migration actor use the same verified artifact** (same digest).
- **`latest` is never acceptable** deployment evidence.
- **Rebuilding a different image under an existing tag is prohibited.** Promotion preserves
  artifact identity (re-tag / reference the same digest); it never rebuilds source
  independently to a reused tag.
- **Rollback selects a previously verified task-definition revision and image digest** — an
  earlier known-good artifact, never a rebuild.

## E. Startup and observability behavior

The revision must eventually be visible to operators **without exposing secrets**:

- **Structured startup logging.** API and worker log the non-secret `build_revision` /
  `application_version` once at startup (alongside the existing `service_name`), so an
  operator can correlate a running process to a source commit from logs alone.
- **Migration-actor correlation.** The one-shot migration actor logs the same pair at start,
  so the migration run is attributable to the same source revision as the services it
  precedes.
- **Health/readiness metadata.** Revision metadata may later be surfaced in operator readiness
  output **only if** it stays consistent with the existing contract (liveness `/health`
  remains a minimal liveness signal; any revision echo belongs on the operator/readiness
  surface, not on unauthenticated liveness). **INFRA-2 adds and alters no endpoint.**
- **Error-monitoring / release metadata.** If a release-tracking integration is later added,
  the revision is the natural release identifier; still non-secret, still the same SHA.
- **API ↔ worker ↔ migration correlation.** All three report the same revision for one
  deployment (§C), enabling cross-component correlation.
- **The Git SHA is non-secret**, but logs and evidence must remain free of credentials,
  tenant IDs, raw user identity, tokens, and secret values (consistent with the existing
  `hide_input_in_errors=True` and the worker-diagnostics omission of provenance identifiers).

## F. G4 preflight verification (fail-closed)

**G4** is a deterministic, **fail-closed** deployment-SHA preflight. It runs before any
runtime-canary authorization and **cannot be bypassed by any override**. G4 verifies **at
minimum**:

1. An **expected deployment SHA** was **explicitly supplied** to the preflight (no implicit
   default, no inference).
2. It is a **valid full 40-character lowercase hexadecimal** Git SHA (unless the authoritative
   plan later specifies an equivalent normalization).
3. The expected SHA identifies the **exact authorized source revision** (matches the approved
   deployment record).
4. The **container OCI `org.opencontainers.image.revision` label == expected SHA**.
5. The **deployed image digest == the approved digest**.
6. **API `build_revision` == expected SHA.**
7. **Worker `build_revision` == expected SHA.**
8. The **migration actor uses the same image digest and expected SHA.**
9. **`application_version` follows the documented deterministic rule** (§A).
10. **No component** reports `unknown`, `local`, `dirty`, `latest`, unset/`None`, or a
    mismatched revision.
11. A **missing, malformed, or mismatched** value **blocks the canary** (hard failure).
12. **No capability override can bypass G4** — G4 is upstream of, and independent from, the
    override control plane.
13. **Retrying G4 cannot mutate runtime or tenant state** — it is a read-only verification;
    re-running it is idempotent and side-effect-free.

**Evidence classes G4 distinguishes** (each verified independently, then cross-checked):

- **Build-time evidence** — the SHA CI checked out and the labels/args it stamped.
- **Registry/image evidence** — the ECR image digest and its OCI `revision` label.
- **Deployment/task-definition evidence** — the task-def revision and the digest it pins.
- **Runtime evidence** — the `build_revision`/`application_version` the running API, worker,
  and migration actor actually report.
- **Human-approval evidence** — the explicitly authorized source SHA and approver record.

A canary is authorized only when **all five classes agree** on one SHA and one digest.

## G. Deployment evidence record

A later controlled deployment must record this **non-secret** evidence (placeholders only —
never real values here):

- Authorized source SHA: `<FULL_GIT_SHA>`
- CI run ID and URL: `<CI_RUN_ID>` / `<CI_RUN_URL>`
- Image repository name or approved **logical** reference (no private account URI)
- Immutable image digest: `sha256:<IMAGE_DIGEST>`
- SHA-based image tag: `<FULL_GIT_SHA>`
- OCI revision label observed: `<FULL_GIT_SHA>`
- ECS task-definition revision: `<TASK_DEFINITION_REVISION>`
- API observed `build_revision` / `application_version`
- Worker observed `build_revision` / `application_version`
- Migration-actor image digest and revision
- Deployment timestamp (RFC 3339)
- Operator identity via an **approved non-secret identifier** (e.g. GitHub handle) — never an
  email, credential, or token
- Independent observer confirmation
- G4 result (pass/fail + which evidence class, if any, failed)

**Never record** a real AWS account ID, private ECR URI, tenant/workspace UUID, credential,
token, email address, or any secret. The restricted evidence artifact template lives under
`docs/verification/` (e.g. alongside `docs/verification/4b-b-feedback-canary.md`).

## H. Failure and rollback behavior

- **G4 failure prevents runtime-canary authorization** — full stop.
- A **revision mismatch is an incident**, not a warning: it means the deployed artifact is not
  the approved source.
- A **failed or mismatched deployment must not activate opportunity feedback** — the gate
  stays dark and fail-closed.
- **No global flag and no capability override is changed** as part of SHA verification; G4 is
  strictly upstream of activation.
- **Rollback uses an earlier verified image digest / task-definition revision** — never a
  force push, a mutable-tag rewrite, or an unreviewed rebuild.
- **Database rollback is governed separately** (single migration actor, explicit target
  revision — see [migrations.md](./migrations.md)); an application/artifact rollback must
  **not** automatically reverse migrations. Migrations are additive-first so the prior image
  runs against the newer schema (`ahead`).
- **No later stage may silently bypass missing build metadata** — absence is a hard failure,
  not a defaulted value.

## I. Future implementation map (a map only — nothing changed now)

Each item below is a **separate, later, authorized** change. INFRA-2 modifies none of them.

| Component | Purpose | Inputs → Outputs | Required tests | Security | Rollback / compatibility |
| --------- | ------- | ---------------- | -------------- | -------- | ------------------------- |
| `apps/api/Dockerfile` | Accept SHA build arg; stamp OCI labels + `ENV` | `ARG GIT_REVISION`, `IMAGE_CREATED` → labels + `BUILD_REVISION`/`APPLICATION_VERSION` env | image-inspect asserts labels + env present and equal to arg | build arg CI-controlled; no secret in labels; stays non-root/read-only-root | local build without arg must keep a safe default (`build_revision=None`, `application_version="0.0.0"`) |
| `apps/api/app/core/config.py` | Possibly widen `application_version` column consumer / confirm env names | existing `APPLICATION_VERSION`/`BUILD_REVISION` env | settings test: env → field mapping; default behavior | fields already non-secret; keep `hide_input_in_errors` | no behavior change when unset |
| `apps/api/app/jobs/worker_models.py` (+ migration) | Widen `application_version` `String(32)` if full SHA is stored there | migration adding column width | migration up/down test; single-head invariant | non-secret column | additive migration; single Alembic head preserved |
| API + worker startup | Emit `build_revision`/`application_version` in structured startup log | settings → one log record | log-capture test asserts non-secret fields present, no secrets | no tenant/credential in log | none (additive log) |
| Migration actor (`app/db/migrate.py`) | Log same revision at start | settings → log | test asserts correlation field | non-secret | none |
| `.github/workflows/ci.yml` (or a separate deploy workflow) | Pass `--build-arg GIT_REVISION=${{ github.sha }}`; capture digest | CI SHA → build args + recorded digest | workflow lint / dry-run | OIDC only for later push; no long-lived keys | build-only in CI; publish is a later deploy tranche |
| IaC (INFRA-3) task definitions | Set `BUILD_REVISION`/`APPLICATION_VERSION` env; pin image by digest | authorized SHA/digest → task-def env + `image@sha256:...` | `plan`-only validation | least-privilege; private subnets | rollback = prior task-def revision |
| Preflight script (G4) | Fail-closed SHA/digest verification (§F) | expected SHA + observed runtime/registry/task-def evidence → pass/fail | unit tests for every §F clause incl. malformed/mismatch/`None` | read-only; no override path; no state mutation | idempotent; safe to re-run |

## J. Acceptance criteria for the later implementation

The implementation tranche is complete only when **all** hold:

- **One source SHA maps to one verified image digest.**
- **API, worker, and migration actor report the same source revision.**
- **Runtime values match the OCI metadata and the approved deployment record.**
- **Missing or mismatched metadata fails closed** (G4 blocks the canary).
- **No mutable tag is treated as proof** (digest is authoritative).
- **No secret** appears in labels, environment-output evidence, logs, or documentation.
- **Local-development behavior remains explicitly defined**: with no build arg / env, the app
  keeps its safe defaults (`build_revision=None`, `application_version="0.0.0"`) and never
  fails startup for want of a revision — G4 applies only to the authorized staging canary
  path, not to local runs.
- **Existing CI remains green.**
- **All three feature flags remain `False`.**
- **No runtime canary begins** without a separate preflight (G4) **and** a separate,
  explicit activation authorization.

---

## Runtime-contract consistency (INFRA-1)

This plan stays consistent with [aws-staging-runtime-contract.md](./aws-staging-runtime-contract.md):
AWS ECS/Fargate; `us-east-1`; **$200/month** hard ceiling with a mandatory pre-provisioning
cost gate (no provisioning before it); `ENVIRONMENT=staging`; `APP_MODE=full`; single API +
single worker replica; one-shot migration actor before service rollout; API/worker/migration
share one immutable image; secrets injected from the future secret-management layer;
read-only root filesystem with writable `/tmp`; termination grace ≥
`WORKER_SHUTDOWN_GRACE_SECONDS`; private managed PostgreSQL+pgvector, Redis, and object
storage; and a live canary that still requires separately provisioned internal tenant
identities, credentials, an independent observer, a reachable runtime, and **fresh explicit
authorization**. INFRA-2 implements none of these controls — it only documents the SHA-wiring
design that a later tranche will build.

## Separate-authorization requirement

Implementation of the wiring in §B–§F, any container/workflow/IaC change, provisioning,
deployment, and the live canary are **each** a separate, later, explicitly authorized tranche.
Merging this INFRA-2 document authorizes **none** of them. **INFRA-3 is not started.**
