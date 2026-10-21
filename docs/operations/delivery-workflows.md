# Delivery workflows — SPA publish/rollback and API deploy/rollback (P6-INF-2, P6-INF-7)

**Status: DRAFT, repository authoring only.** Nothing here has run. Every dispatch of these
workflows is a live act that needs its own written authorization, the prerequisites in §4,
and the protected GitHub environment's human approval. No plan row is closed by this file.

| Workflow | Row | Environment | Role (OIDC) | Writes |
|---|---|---|---|---|
| `spa-publish.yml` | P6-INF-2 | `staging-spa-publish` | SPA publisher | objects in the SPA origin bucket; one CloudFront invalidation |
| `spa-rollback.yml` | P6-INF-2, §16 | `staging-spa-publish` | SPA publisher | one object copy (`index.html`); one invalidation |
| `staging-deploy.yml` | P6-INF-7, E2 | `staging-deploy` | deployer | 3 task-definition revisions; 1 migration task; 2 service updates; 1 ledger version |
| `staging-rollback.yml` | P6-INF-7, E2 | `staging-deploy` | deployer | 2 task-definition revisions; 2 service updates; 1 ledger version |

All four: `workflow_dispatch` only, **no inputs**; `main` only; `contents: read` plus
`id-token: write` on the single job; one concurrency group per pair, never cancelled; the
assumed account verified right after federation; every decision taken offline by
`scripts/release_plan.py` from the JSON the workflow read (tests:
`tests/test_p6_release_plan.py`, `tests/test_p6_delivery_workflows.py`).

## 1. SPA (P6-INF-2)

**Publish.** `npm ci` and `npm run build` with `VITE_API_BASE_URL` = the API origin (the
P6-AUTH-5 CSP guard runs inside the build). `release_plan.py spa-plan` classifies the build:
`assets/*` (content-hashed) `public, max-age=31536000, immutable`; root files such as the
favicon `public, max-age=3600`; `index.html` `no-cache`. It refuses source maps, dotfiles,
symlinks, un-hashed assets and unknown types. Assets are uploaded first, `index.html` last,
and nothing is deleted. `index.html` carries object metadata `source-commit`,
`index-sha256`, `previous-index-sha256`, `release-run`. One invalidation (`/index.html`, `/`)
is awaited; then the live `/` and a deep link (`/reset-password`, through the 403/404 →
`/index.html` rewrite) must hash to the built `index.html`. If a CSP response header is
served (P6-INF-19), its `connect-src` must name the build's API origin.

**Rollback.** The target is the retained `index.html` version whose `index-sha256` equals the
live object's `previous-index-sha256` — a recorded chain, not "the previous version". The
copy re-states the target's own lineage, so a second rollback goes further back. Horizon:
noncurrent versions expire after 30 days (`modules/edge`).

## 2. API and worker (P6-INF-7, E2)

**Model.** OpenTofu registers one *template* revision per family (`ManagedBy=iac`). A release
revision is the template with only the container image digest replaced; `release_plan.py
ecs-render` refuses any other difference and any task-definition member it does not know.
The digest comes from the immutable ECR tag `<commit>` that `staging-publish.yml` pushed; a
push-scan CRITICAL finding refuses the release.

**Deploy order:** floor check (the commit must contain `DEPLOY_ROLLBACK_FLOOR_COMMIT`, i.e.
`AUTH4_ROLLBACK_FLOOR`) → resolve digests → read the running release (refuses a deployment in
progress, desired count 0, circuit-breaker rollback off, or tasks on mixed digests) →
render and register → run the migration actor (worker digest, no override) and require exit
0 and one `migrate.upgrade_verify.done` event → update both services → `services-stable` →
every running task must report the target digest on the registered revision → append the
ledger → smoke (`/health`, `/api/v1/system/readiness` ready and migrated, the six P6-AUTH-5
headers, a 404 with headers). A failed smoke fails the run and leaves the release for a
separately approved rollback.

**Ledger.** One SSM String parameter `/<prefix>/release/ledger` whose version history is the
release record: each version names the release (a commit per workload, both digests,
revisions) and `previous` — what was *actually running* before, read from ECS, never from
the ledger. Per workload, because a deployment can end with the circuit breaker rolling one
service back and not the other; that state is recorded as it is and stays restorable. It
survives window closes; task definitions do not.

**Rollback** (`gh workflow run staging-rollback.yml --ref main`, then approve): target =
the latest record's `previous`, only if the services run exactly the latest record (else
refused as ambiguous); every target commit must contain the floor; the prior digests must
still carry their commit tags; re-rendered from the *current* templates; never migrates.

## 3. Records (E2)

Each run uploads a JSON record (artifact, 90 days) and the deploy pair appends the ledger.
No record contains an account id, ARN, hostname or bucket name: task definitions appear as
`family:revision`, images as digests. E2 needs, in one evidence set: a deploy run (SHA +
digests recorded), a rollback run restoring the previous digests, both smokes, and the
ledger history read back.

## 4. Prerequisites — none exists today

1. **GitHub environments** `staging-spa-publish` and `staging-deploy`, created WITH required
   reviewers and a `main`-only branch policy before any dispatch (as for P6-INF-8).
   Environment **secrets** (not variables: GitHub prints a step's `env:` values in the log
   header, and only secrets are masked there): `AWS_ACCOUNT_ID`, the role ARN, and the
   operator identifiers each workflow names. Variables: `NAME_PREFIX`,
   `DEPLOY_ROLLBACK_FLOOR_COMMIT`, `RELEASE_LEDGER_PARAMETER`.
2. **IAM.** Two OIDC roles whose trust pins `repo:<owner>/<repo>:environment:<name>` with
   StringEquals, least-privilege policies, and the role permissions boundary amended to let
   exactly these principals use what it denies today (task-definition registration, service
   update, pass of the execution and task roles, run of the migration family, the SPA key).
3. **IaC.** `ignore_changes = [task_definition]` on both ECS services; the ledger parameter;
   the SPA CMK applied (P6-CI-6).
4. **Operations.** `AUTH4_ROLLBACK_FLOOR` recorded (the P6-AUTH-4 cutover is the first AUTH4
   deployment, never these workflows); an open staging window for the deploy pair; the
   window's tfvars digests set to the ledger's current release before a window reopens.
5. **Governance.** New `run:` command words, npm call sites and paths must be accepted by the
   repository's executable, npm and coherence controls in the same change.
