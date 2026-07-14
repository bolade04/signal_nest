# Phase 3A.4a Acceptance Report

Independent acceptance review of **Phase 3A.4a — Production Data-Plane Adapters &
Worker-Fleet Foundation** (PR #30). This report is authoritative for 3A.4a acceptance.
Design intent is documented in
[`phase-3a-production-data-plane.md`](phase-3a-production-data-plane.md); Phase 1–2
acceptance is in [`acceptance-report.md`](acceptance-report.md).

> This is a **review artifact only**. The reviewer did not approve, merge, close,
> rebase, or otherwise mutate PR #30, the branch protection ruleset, or repository
> governance. Approval remains outstanding (see *Approval path*).

## Identity and baseline

| Item | Value |
| --- | --- |
| Repository | `bolade04/signal_nest` |
| Pull request | [#30](https://github.com/bolade04/signal_nest/pull/30) — OPEN, `MERGEABLE`, not draft |
| Author | `bolade04` |
| Branch | `feat/phase-3a-production-data-plane` |
| Base | `main` |
| Review baseline (base ancestor) | `67ed4385c873a2048bc38fa3dd6cc8e6d280dda2` |
| Prior reviewed head | `60390664fcbd465ca8a46d2de54d3d4697130325` (`6039066`) |
| Current reviewed head | the `fix(workers): require fresh heartbeats for fleet readiness` commit (this branch HEAD, supersedes `6039066`; see *Findings* §3) |
| Baseline is ancestor of head | yes |
| Prior validated CI run | `29279995309` (success, head `6039066`) |
| Fix-head CI run | `29305640415` (success, fix head `a259529`) — all four required jobs green |
| Working tree at review | clean (`git status --short` empty) |

## Governance invariants (verified unchanged)

- **Branch-protection ruleset `18820692`** ("main protection") — `active`, `target: branch`; unmodified.
- **Secret scanning** — `enabled`. **Push protection** — `enabled`.
- **Dependabot** — 9 historical alerts, **all in state `fixed`** (0 open: 2 critical, 2 high, 5 medium — all fixed).
- **PR #6** (`deps(deps-dev): bump typescript 5.9.3 → 7.0.2`) — still OPEN, untouched. TypeScript 7 migration remains out of scope.
- **Safety branch** `backup/signalnest-phase-1-2-pre-history-stitch` — present locally and untouched. It is **not** published to `origin` (remote returns 404); this is a local pre-history snapshot and is informational only.

## Scope accepted

All 29 changed files are within 3A.4a scope; no deferred-phase surface was introduced.

| Area | File(s) | Verdict |
| --- | --- | --- |
| Config + bounded validation, soft/hard gating | `app/core/config.py` | Accepted |
| Dialect-isolated engine/session | `app/db/session.py`, `app/db/models.py` | Accepted |
| PostgreSQL `FOR UPDATE SKIP LOCKED` claim | `app/jobs/store.py`, `app/jobs/service.py` | Accepted |
| Redis cache (JSON-only, tenant-scoped) | `app/infra/cache.py` | Accepted |
| Redis coordination (wake-up + advisory lock) | `app/jobs/coordination.py` | Accepted |
| S3 storage (key validation, private default) | `app/infra/storage.py` | Accepted |
| Worker status state machine | `app/jobs/worker_status.py` | Accepted |
| Worker registry + model + migration | `app/jobs/worker_registry.py`, `worker_models.py`, `alembic/versions/…d4f6a8c0b2e1_*.py` | Accepted |
| Worker lifecycle | `app/jobs/worker.py` | Accepted |
| Error taxonomy | `app/core/errors.py` | Accepted |
| Runtime capability | `app/core/runtime.py` | Accepted |
| Readiness probe (policy-gated) | `app/system/probes.py` | Accepted |
| Operator fleet diagnostics API | `app/system/internal_routes.py`, `app/jobs/worker_schemas.py` | Accepted |
| Tests | `test_production_adapters.py`, `test_worker_fleet.py`, `test_worker_migration.py`, `test_api_isolation.py`, `test_readiness_probes.py`, `test_runtime_foundation.py` | Accepted |
| Contract/deps/CI | `openapi.json`, `apps/web/src/api/schema.d.ts`, `pyproject.toml` (dev extras: `fakeredis`, `redis`, `psycopg[binary]`), `.github/workflows/ci.yml` | Accepted |

## Explicit deferrals (confirmed absent — 3A.4b)

Metrics/tracing/OpenTelemetry; structured operational-event framework; readiness-result
caching; Docker/Compose packaging and a production `.env` template; deploy documentation;
the operator-facing frontend fleet panel; dashboards/checklists. No connector work and no
TypeScript 7 migration are present. Default local mode remains zero-dependency.

## Architecture acceptance (code review)

- **One winner per job, both dialects.** `store.claim_one` dispatches on
  `db.get_bind().dialect.name`: PostgreSQL locks a single due row via `SELECT … FOR
  UPDATE SKIP LOCKED` then updates it; SQLite keeps the fenced compare-and-set scan.
  Both share one lifecycle (`_finalize_claim`): identical priority/FIFO ordering, a fresh
  per-claim `lease_token`, a `claimed` audit event, and one commit. Attempt counting lives
  solely in `mark_running`, so the attempt budget cannot diverge. `_fenced_update` gates
  every mutation on `(id, worker_id, lease_token, status ∈ allowed)` and raises on
  `rowcount != 1`. An always-run test proves the compiled SQL contains `FOR UPDATE SKIP
  LOCKED`; a gated live two-worker test proves distinct rows to distinct claimers.
- **Correctness never depends on Redis.** The job is DB-committed before any wake-up;
  publish failure raises `RedisNotifyFailedError` and is swallowed at the enqueue seam; the
  next bounded poll still finds the job. `RedisAdvisoryLock` releases via WATCH/MULTI
  compare-and-delete (deletes only its own token). Locks are advisory and never gate ownership.
- **Keys cannot escape scope.** `validate_object_key` rejects empty/null-byte/absolute/
  backslash/`..`/non-normalized keys before any I/O; `tenant_object_key` derives the
  `{org}/{ws}/` prefix server-side. S3 puts carry no ACL (private default); signed-URL TTL
  is bounded; no implicit bucket creation.
- **Config gating is correct.** New PG-pool/Redis/S3/worker settings are range-validated at
  construction. A selected-but-unconfigured production backend is a **soft** unconfigured
  capability locally and a **hard** failure in full/production. `worker_stale_after_seconds
  > worker_heartbeat_seconds` is enforced.
- **Fleet state is derived and coarse.** Registration is idempotent/self-replacing;
  heartbeats write no audit rows; stale is derived (`not terminal` AND heartbeat age >
  threshold) and never touches job ownership. `WorkerStatus` has an explicit transition map
  with `stopped` terminal. The operator schema (`WorkerSummaryOut`) omits `worker_id`,
  `build_revision`, `host_fingerprint` and `application_version`.
- **Readiness policy.** `_check_worker_registry` is informational by default (schema present
  ⇒ healthy) and blocking only when `require_worker_fleet` is enabled (then ≥1 active
  worker required); `required` is driven by the setting. It never names a worker id. An
  "active" worker requires **both** an active status (`ready`/`busy`) **and** a heartbeat
  within `worker_stale_after_seconds` — see *Findings* §3 (M1) for why status alone was
  insufficient.

## Security acceptance

- **No secret leakage.** DB/Redis URLs, passwords, bucket names, endpoints and raw
  driver/SDK text are never logged or returned; `AdapterError._envelope` exposes only
  `code` + static message + `request_id`; raw exceptions appear only as chained `from exc`.
  A static scan of the backend diff found **no hardcoded secret literals**.
- **Operator gate.** `/internal/system/workers` is `require_operator`-guarded; the isolation
  suite asserts `401` anonymous, `403` non-operator, and a coarse, secret-free operator body
  (no `worker_id`/`build_revision`/`host_fingerprint`/`application_version`/`redis://`/`postgresql://`).
- **Tenant isolation intact.** The four-market HTTP smoke (Dallas/London/Lagos/Nairobi)
  confirms per-location opportunity separation with no cross-market contamination; worker
  context is rebuilt from persisted tenant columns, never widened from payload.

## Migration acceptance

- Additive `worker_registrations` (PK + unique `worker_id` + 3 indexes: status/heartbeat/type).
- Round-trip on a throwaway SQLite DB: `upgrade head` → `alembic check` reports **"No new
  upgrade operations detected"** → `downgrade base` → re-`upgrade head`, all clean.
- Downgrade is surgical (drops only the worker table/indexes; business/job data preserved),
  proven by `test_worker_migration.py`. Chain: `d4f6a8c0b2e1` ← `c3e5a7b9d1f2`.

## Test evidence (reproduced locally)

| Gate | Result |
| --- | --- |
| `npm audit` | 0 vulnerabilities |
| `npm run test:ci-pipefail` (failure-propagation regression) | pass |
| `npm run lint` (`--max-warnings 0`) | clean |
| `npm run type-check` (`tsc -b`) | clean |
| Frontend `vitest` | 20 passed / 20 |
| `npm run build` | success |
| `gen:types` + `git diff --exit-code` (openapi.json, schema.d.ts) | no contract drift |
| `ruff check` | clean |
| Alembic upgrade/check/downgrade/re-upgrade | clean, no drift |
| Backend `pytest` | 205 passed, 1 skipped (gated PG test; no local Postgres) — includes 9 new worker-fleet freshness cases |
| HTTP isolation smoke | 13 checks passed; four-market isolation confirmed |

## CI evidence

CI run `29305640415` on the fix head `a259529` — **all four required jobs succeeded**
(Frontend quality, Backend quality, Migrations and API contract, Integration smoke). The
prior baseline run `29279995309` on head `6039066` was likewise fully green. The
Backend-quality job runs the gated cross-worker claim test against a disposable `postgres:16`
service via `TEST_POSTGRES_URL`, exercising the real `FOR UPDATE SKIP LOCKED` path in CI.

## Findings and corrective changes

Three corrective changes were made on the branch during review; all are narrowly scoped and
covered by CI:

1. **CI PostgreSQL wiring** (`.github/workflows/ci.yml`) — added `TEST_POSTGRES_URL` to the
   Backend-tests step so the gated live claim test runs against the postgres service. The app
   under test stays in local SQLite mode; only the opt-in test connects to Postgres.
2. **Gated live-test FK seeding** (`test_production_adapters.py`, commit `6039066`) — the
   gated `test_postgres_two_workers_claim_different_jobs` enqueued jobs referencing
   `org-1`/`ws-1` without creating them; PostgreSQL (unlike the SQLite skip path) enforces the
   `jobs → organizations/workspaces` FKs, causing a `ForeignKeyViolation`. Fixed by seeding
   the parent `Organization`/`Workspace` rows before enqueue. This corrects the **test
   harness only** — no production behavior changed — and turned the Backend job green.
3. **M1 — worker-fleet readiness ignored heartbeat freshness (production defect, fixed).**
   `WorkerRegistry.active_count` counted any worker in an active status (`ready`/`busy`)
   regardless of heartbeat age, and `_check_worker_registry` gated `require_worker_fleet`
   readiness on that count. Because the only process that runs `sweep_stale` is a worker's own
   heartbeat loop (`worker.py::_start_registry_heartbeat`), a **fleet-wide death** leaves no
   one to flag its rows `stale`: every row stays `ready`/`busy`, `active_count` stays ≥ 1, and
   readiness reports green while nothing is actually processing jobs — precisely the outage
   `require_worker_fleet` exists to catch. **Fix:** a worker is now "active" only when its
   status is active **and** `last_heartbeat_at >= now − worker_stale_after_seconds`. A single
   authoritative predicate (`WorkerRegistry._fresh_active_conditions`) backs both the new
   `active_count(db, *, stale_after_seconds, now=None)` and is the exact complement of
   `find_stale` (`< cutoff`), so a ready/busy worker is counted active *or* overdue — never
   both, never neither — independent of sweep cadence. `probes._check_worker_registry` and the
   `/internal/system/workers` operator endpoint were threaded through the shared threshold.
   **Method:** a failing regression (`test_worker_probe_unhealthy_when_only_worker_is_overdue_unswept`)
   was written and confirmed red against the unfixed code *before* the production change, and
   it was **not** made to pass by manually calling `sweep_stale()` — readiness derives liveness
   from heartbeat age directly. Coverage was extended to 9 new cases (fresh ready/busy →
   healthy; overdue ready/busy unswept, stopped, explicitly-swept-stale → unhealthy; mixed
   fresh+dead → healthy; the exact freshness boundary as the complement of `find_stale`;
   PostgreSQL query compilation; and a duplicate-`worker_id` residual proving re-registration
   cannot forge freshness — see *Residual risks*). **No new migration:** the existing
   `status` and `last_heartbeat_at` indexes already cover the predicate, and the table holds
   one row per worker process. This corrects **production readiness behavior**; job ownership,
   lease fencing, and tenant isolation are untouched.

Excepting M1 above, no correctness, security, migration, tenant-isolation, adapter-failure,
or rollback defect was reproduced in production code.

## Residual risks (accepted)

- **Duplicate active worker-ID overwrite.** Registering a known `worker_id` overwrites that
  registry row and resets it to `starting`; there is no generation/ownership token on the
  registration. **Accepted, not blocking:** (a) the default `worker_id` embeds PID +
  `secrets.token_hex(3)`, so a collision requires explicit operator misconfiguration; (b)
  job ownership is fenced entirely by the per-claim `lease_token`, independent of
  `worker_id`, so two same-id processes each claim distinct rows with distinct tokens — no
  double-run or loss; (c) the registry is operational/observability only. Re-registration also
  resets the row to `starting` with a fresh `last_heartbeat_at`, so it **cannot forge
  freshness** for the M1 readiness count without a legitimate `mark_ready` + heartbeat —
  proven by `test_duplicate_registration_cannot_forge_freshness`. *Recommendation for 3A.4b:*
  add a registration generation/ownership token. Adding it now would be scope creep.
- **Pre-existing Dependabot history** — 9 alerts, all `fixed`; none introduced by this PR.
  New backend deps are dev-extras (`fakeredis`, `redis`, `psycopg[binary]`); `npm audit`
  reports 0 vulnerabilities.

## Rollback acceptance

- **Migration:** `alembic downgrade -1` drops only `worker_registrations` + its indexes
  (no business/job data); workers re-register on next startup after re-upgrade. Verified.
- **Adapters:** all new behavior is additive behind existing seams and inert in default local
  mode; reverting the branch restores prior construction with no schema change beyond the
  migration downgrade.
- **Policy:** `require_worker_fleet` defaults `false`, so API readiness never depends on a
  worker being up unless an operator opts in.

## Final classification

**Accepted.** Phase 3A.4a is complete, scoped, secure, migration-safe, and green on the
required CI (fix-head run `29305640415`, head `a259529`; prior baseline run `29279995309`,
head `6039066`). One production defect was found and corrected during
review — **M1**, worker-fleet readiness ignoring heartbeat freshness (*Findings* §3) — with a
failing-first regression and 9 new covering cases. The single accepted residual (duplicate
worker-ID overwrite) is a documented, non-blocking operational limitation mitigated by lease
fencing and now proven unable to forge readiness freshness, with a concrete 3A.4b
recommendation. Remaining corrective changes were test-harness/CI wiring.

## Approval path

PR #30 is authored by `bolade04`; the acting reviewer is the same identity, so GitHub
self-approval is unavailable and the required approving review remains outstanding. This
report records reviewer acceptance; a second maintainer must submit the formal PR approval
before merge. The reviewer did **not** approve, merge, close, or rebase the PR, and did not
modify the ruleset or begin Phase 3A.4b / 3B.
