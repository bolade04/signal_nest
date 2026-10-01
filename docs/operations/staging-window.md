# Staging operating window — resource-effect ledger, transition runbook, cost model

**Status: implemented in code and tests only. No window has been opened or closed live; nothing in this document authorizes a plan, an apply, a
shutdown, a start or a deployment.** Governing numbers: the operator's limit is **USD 20 TOTAL per month** (tax-inclusive, since the bill is); the
attested September 2026 bill for the always-on foundation is **USD 91–95/month** (sealed attestation and its supplement, 2026-09-30); the
**provisional planning target is 4 eight-hour windows per month** — a planning assumption, not a guarantee of budget compliance.

## 1. The input

`staging_window_active` (root, `bool`, **required, no default**). `true` = an authorized window is OPEN; `false` = CLOSED. Every execution of the
root must state the target state. `deploy_workload = true` is **refused by validation** unless the window is open (the services need the ALB target
group and the Redis cluster). The operator's `enable_nat_gateway` is ANDed with the window, so the window can turn the NAT gateway off, never on.

## 2. Exact resource-effect ledger

| resource (module) | window OPEN | window CLOSED | data / cost note |
|---|---|---|---|
| NAT gateway + its Elastic IP + private default route (`network`) | present | **destroyed; EIP released** (public IPv4 charge stops) | no data; the EIP address changes every window |
| ALB, HTTPS listener, API target group (`alb`) | present | **destroyed** | no data; the ALB DNS name changes every window (§4 DNS) |
| ALB security group + ingress rule, ALB log bucket + policy (`alb`) | present | present | free; delivered logs retained |
| ElastiCache replication group (`data_cache`) | present | **destroyed** — queue/cache contents NOT preserved | snapshot retention does not apply to a deleted group unless a final snapshot is taken (not configured) |
| ElastiCache subnet group, parameter group, Redis SG (`data_cache`) | present | present | free |
| API/worker task definitions, services, migration task definition (`ecs`) | present only with `deploy_workload = true` | **refused** (validation) | re-registered per window → new task-definition revisions; the AUTH4 rollback floor is a REVISION rule and still applies (§7) |
| ECS cluster, task SGs, cross-SG rules, 3 log groups (`ecs`) | present | present | log groups: 30-day retention, cents |
| RDS instance, subnet/parameter groups, backups (`data_sql`) | running | **NOT touched by OpenTofu** — stopped/started by the runbook (§5) | data preserved; storage/backup charges continue ("RDS remainder", §6) |
| VPC, subnets, route tables, IGW (`network`) | present | present | free |
| Secrets Manager containers + KMS CMK (`secrets`), S3 app/audit/SPA/state buckets, ECR repos, CloudFront + web DNS (`edge`), IAM roles, cost budget, CloudTrail, dashboard (`observability`) | present | present | the idle baseline (§6) |
| CloudWatch alarms (`observability`): 3 RDS + 2 Redis use `treat_missing_data = "breaching"` | OK | **ALARM for the whole closed period** (Redis destroyed, RDS stopped → no metrics); they fire `alarm_actions` at close and `ok_actions` at open | present either way; the 4 ECS service alarms already breach while `deploy_workload = false` (pre-existing). The runbook expects and acknowledges these transitions (§3); a real Redis/RDS failure INSIDE a window is only distinguishable by the alarm timing, so the open receipt must record when the five returned to OK |
| Revision reader (`revision_reader`, run by `reader-run.yml` in private subnets, no public IP, no VPC endpoints) | runnable (needs NAT for ECR/Secrets/Logs and RDS `available`) | **not runnable** | the Gate 4J instrument that gates the workload apply therefore runs only inside a window, after OPEN steps 2 and 4 and before step 6 |

What the ledger does NOT include: the API alias record (P6-INF-3, still deferred under the IaC plan §24.7 lock) — when it exists it must be IaC-managed
so a re-created ALB's DNS name is re-pointed each window (§4); the SPA (persists, ≈ $0); any "second EIP" observed in the 2026-09-30 read-only round whose owner is not
known from the repository — the first live close must identify it (`ec2 describe-addresses`) before assuming it is released.

## 3. Transition runbook (each step is a separately authorized act; every mutation from a saved, inspected plan)

**OPEN (closed → open), target ≈ 1 billable hour of provisioning:**
1. Pre-check (read-only): window state recorded as CLOSED in the previous close receipt; RDS instance status `stopped` or `available`; no ECS deployment in progress; the planned apply identity holds the §8 actions.
2. RDS: `aws rds start-db-instance --db-instance-identifier <prefix>-postgres` (outside OpenTofu); wait for `available` (5–10 min).
3. tfvars: `staging_window_active = true`, `deploy_workload = false` (first apply of a window never creates services — §7).
4. `tofu plan -out window-open.plan` → `tofu show -json` → review: ONLY creates of NAT gateway, EIP, private default route, ALB, listener, target group, replication group; **reject** any destroy or any change outside those addresses. `tofu apply window-open.plan`.
5. Record the new ALB DNS name and the new NAT EIP; re-point the API alias record if/when it is IaC-managed (§4); verify the Redis endpoint output;
   record when the 3 RDS + 2 Redis alarms return to OK (they were ALARM for the whole closed period — expected, §2).
5a. Revision reader (if this window's purpose needs the Gate 4J read): only now — it needs the NAT route and RDS `available`.
6. Workload (only if this window's purpose needs it, and only as the AUTH4 cutover prescribes): tfvars `deploy_workload = true` + digests + desired counts per `deployment.md` ("P6-AUTH-4 one-way cutover" Apply 1/2/3), run the migration task if the head moved, verify.

**CLOSE (open → closed), target ≈ 1 billable hour of cleanup:**
1. Drain: stop accepting work (`api_desired_count = 0`, `worker_desired_count = 0` apply, or the cutover's quiesce step), wait for in-flight jobs; Redis contents will be lost — the durable jobs table is in PostgreSQL, in-flight queue entries are not.
2. tfvars: `deploy_workload = false`, `staging_window_active = false`.
3. `tofu plan -out window-close.plan` → review: ONLY destroys of the services/task definitions (if any), replication group, listener, target group, ALB, private default route, NAT gateway, EIP; **reject** any destroy of RDS, buckets, secrets, KMS, roles, SGs, subnets, CloudFront. `tofu apply window-close.plan`.
4. RDS: `aws rds stop-db-instance --db-instance-identifier <prefix>-postgres` (optionally `--db-snapshot-identifier` for a dated snapshot); wait for `stopped`.
5. Verify (read-only): the 3 RDS + 2 Redis alarms go to ALARM (expected; acknowledge, do not "fix"); `ec2 describe-nat-gateways` none `available`; `ec2 describe-addresses` lists NO allocation owned by this composition; `elbv2 describe-load-balancers` none; `elasticache describe-replication-groups` none; `rds describe-db-instances` status `stopped`; write the close receipt with the UTC times and the billable-hour count.

**Seven-day automatic restart (§5) is part of every CLOSED period, not of the transition.**

**FIRST execution from the current always-on state (special case, once):** the live state holds `module.alb.aws_lb.this`, `aws_lb_target_group.api`,
`aws_lb_listener.https` and `module.data_cache.aws_elasticache_replication_group.this` WITHOUT an index. The modules carry `moved` blocks for the
four, so the first plan must show exactly four `has moved to … [0]` lines and NO replace of those resources; a plan that proposes to destroy and
re-create any of them is REJECTED. With `staging_window_active = true` that first plan is otherwise empty (a no-op confirmation run, recommended
before the first close); with `false` it is the first CLOSE. Before it, the operator confirms the four addresses with a read-only `tofu state list`.
⚠ `auto_minor_version_upgrade = true` on the RDS instance with a major.minor `engine_version`: a minor version applied by AWS (possible after an
automatic restart) surfaces as an in-place RDS change in a later plan, which the abort rule rejects — that case needs a config edit under its own
authorization, not a bypass. The managed master password's rotation cannot run while the instance is stopped (AWS retries; impact unverified).

## 4. DNS handling and dependency-safe recreation

- Web: `app.<…>` A/AAAA aliases point at CloudFront, which persists — unaffected by windows.
- API: the ALB is re-created per window with a NEW DNS name. The P6-INF-3 alias record does not exist yet (§24.7 lock). When it is created it MUST be
  managed by this root (alias → `module.alb.alb_dns_name` / `alb_canonical_hosted_zone_id`, both null while closed, so the record must itself be
  windowed or conditional), never hand-edited, or every window silently breaks the API hostname and the SPA's compiled API origin.
- The ALB target group is created before the ECS service that references it (module graph `alb → ecs`); the replication group before the workload
  (secret `REDIS_URL` is populated out of band — it carries the endpoint hostname, which is deterministic for a re-created group with the same
  `replication_group_id`, so it does not change per window; verify on the first live open).
- Certificate: the ACM certificate is consumed by ARN; it persists and is re-attached to each new listener.

## 5. RDS stop/start and the seven-day automatic restart

OpenTofu has no "stopped" state for `aws_db_instance`; the runbook stops and starts it with `rds:StopDBInstance` / `rds:StartDBInstance`. AWS
**automatically starts a stopped instance after seven days**. Consequences and handling: every CLOSED period longer than seven days needs a re-stop
within 24 h of each automatic restart (an operator check, or a later scheduled automation — not implemented here, it would be new resources);
each unattended restart-day costs ≈ USD 0.38 (instance hours) and is counted in the cost model as exposure (§6). An apply while the instance is
stopped is safe as long as the plan makes NO change to the instance; any planned RDS modification requires the instance to be `available` first.
Backups, snapshots, the master secret and the parameter/subnet groups are retained while stopped; storage is billed throughout.

## 6. Cost model (per calendar month; prices from the sealed `PRICING-SOURCES`; attested lines from the sealed supplement)

Idle baseline (window CLOSED, RDS stopped): RDS non-instance remainder **2.46** (attested August RDS line 14.36 minus 744 h × 0.016 — this remainder is
**unclassified**: storage, backup and any other RDS line item collapsed; it is NOT treated as verified storage cost) + Secrets 2.00 + KMS 2.00 +
Route 53 0.51 + CloudWatch 0.50 + ECR 0.07 + S3 0.05 + CloudTrail/CloudFront 0 + IPv4 0 (released) = **7.59 pre-tax**.
Automatic-restart exposure: up to 4 restarts × ≤ 24 h × 0.016 = **1.54 pre-tax** (0 if re-stopped immediately; 11.90 if never).
Per window (8 operating hours + 2 billable provisioning/cleanup hours = 10 billable hours; NAT and ALB bill whole hours): NAT 0.45 + NAT data 0.05 +
ALB 0.225 + LCU 0.01 + RDS instance 0.16 + ElastiCache 0.16 + IPv4 (2–4 addresses) 0.10–0.20 + Fargate 2 tasks 0.25 + workload usage/logs/SES 0.05 =
**1.46–1.56, use 1.55 pre-tax**. Tax: the attested ratio **6.59%** (observed, not guaranteed). Contingency: **10%**.

| windows / month | pre-tax = 7.59 + 1.54 + 1.55 × N | with tax | with contingency | margin vs 20 |
|---|---|---|---|---|
| 3 | 13.78 | 14.69 | 16.16 | +3.84 |
| **4 (provisional target)** | **15.33** | **16.34** | **17.97** | **+2.03** |
| 5 | 16.88 | 17.99 | 19.79 | +0.21 |
| 6 | 18.43 | 19.64 | 21.61 | −1.61 |

Remaining uncertainty (not in the margin): real provisioning/cleanup durations; whether a 16-hour window (AUTH4 cutover) counts as 2; the second EIP's
owner; the 2.46 remainder's composition; SES volume above E8 scale; a retained NAT EIP (+3.65/month) or a kept Redis (+11.90) breaks the fit; the tax
ratio. Continuous operation of the foundation does not fit under any option (sealed design, unchanged).

## 7. Interaction with the AUTH4 one-way cutover and the rollback floor

The cutover (`deployment.md`) is executed INSIDE a window: Apply 1 (register, desired 0, rollback off), migration run, Apply 2 (start), verify, SPA,
smoke, Apply 3 (floor). Because task definitions are destroyed at close and re-registered at the next open, the recorded `AUTH4_ROLLBACK_FLOOR` is
compared by **image digest and source revision**, not by task-definition revision number: every window's registration must use digests at or above
the floor, and the breaker's automatic rollback target inside a window is always the current window's own revision. Sessions (`auth_sessions`) live in
PostgreSQL and survive closes; Redis does not hold them.

## 8. Required permissions and the W0 question

Window transitions need (apply identity): `ec2:CreateNatGateway/DeleteNatGateway/AllocateAddress/ReleaseAddress/CreateRoute/DeleteRoute`,
`elasticloadbalancing:Create/DeleteLoadBalancer, Create/DeleteTargetGroup, Create/DeleteListener`, `elasticache:Create/DeleteReplicationGroup`;
the live W0 role's inline policy (sealed read, 2026-09-30) carries all of these, BUT that live policy is the unreviewed drifted document (FV-20 open).
**The repository-reviewed W0 carries NONE of them**: every `ec2:`/`elasticloadbalancing:`/`elasticache:`/`rds:` action in
`scripts/gen_operator_policies.py` is `Describe*`/`ListTags*` except `rds:DeleteDBInstance` and `rds:ModifyDBInstance`, which are in its deny list.
So today NO repository-reviewed principal can open or close a window — a KNOWN BLOCKER, not a to-do: either the reviewed W0 is extended under
its own authorization (and the drift adjudicated), or a new window-transition principal is designed.
RDS stop/start needs `rds:StopDBInstance` / `rds:StartDBInstance`: **held by no repository principal** (W0's Rds statement has neither) — a new,
narrowly scoped grant (resource = the one instance ARN) under separate authorization. Service creation/deletion (`ecs:CreateService/UpdateService/
DeleteService/RegisterTaskDefinition`) is **not** in W0 — the cutover's separately authorized principal, as already designed.

## 9. Governance amendments requiring an operator ruling (always-on invariants this design changes)

- IaC plan §26.10 "two long-running services (API, worker)", "API desired count 1, worker desired count 1" — inside windows only.
- Runtime contract §C (service topology: two continuously running services) and §M (cost contract: its "scale-to-zero off-hours" lever and its
  planning estimate): availability becomes windowed; the planning estimate is superseded by the attested figures and this cost model.
- Runtime contract §D (network: outbound egress "required for the LLM provider … and image pulls"): no egress while closed — acceptable only because
  no task runs while closed.
- `deployment.md` rolling deployment: valid inside a window; the cutover becomes a window-internal sequence (§7).
- The IaC plan's "no targeted apply" rule is **preserved** (the window is a declared input, not a `-target`).
- P6-INF-3 (API alias) must be designed as a windowed/conditional record (§4) — its §24.7 deferral still stands.
None of these is amended by this change; each is a ruling for the operator.

## 10. Dependencies

- PR #194 (6B-4C mail wiring) and this change are logically independent, but were NOT conflict-free: `main.tf` and `variables.tf` hunks did not
  overlap; `terraform.tfvars.example` (both inserted after the same base line) and the synthetic root fixture (both appended at end of file) collided.
  RESOLVED in the #194 integration (merge of main into its branch): both files carry BOTH sets of required root inputs — the example keeps
  `staging_window_active = false` and the placeholder mail tokens, the positive-control fixture keeps `staging_window_active = true` and the
  synthetic mail values — so the required-variable parity test (`tests/test_root_wiring_check.py`) passes. Mail is only exercised inside a window.
- The first live open/close needs: the W0 ruling (§8), the RDS stop/start grant, the §24.7 alias design, the cost budget corrected to the limit
  (separate package), and a window authorization naming date, duration, purpose and the apply identity.

## 11. Limitations

Nothing here has been planned or applied against AWS. The module tests prove open/closed shapes with a mocked provider and RUN IN CI as graded steps of the `revision-reader` job
(`window_tests_alb` 3 runs, `window_tests_data_cache` 2, `window_tests_network` 2; offline, fully mocked, no AWS call), followed by
`window_negative_control`, which initialises a doctored copy of the network module offline from the already-verified network cache and
requires its one false assertion to FAIL `tofu test`. Each module carries the root's byte-identical provider constraint (the modules/iam Gate
4N-I8 rule; committed child-module lock files stay prohibited), and the three module caches are members of the review-pinned
`check_toolchain_integrity.py::EXPECTED_CACHE_ROOTS` collection under ledger record `REV-2026-10-01-P6-STAGING-WINDOW-CI-CACHE-ROOTS`:
classified by the post-init step (expected version, binary provenance), not exempted. `tests/test_window_module_ci.py` pins the wiring
hermetically; `scripts/failure_propagation.py` and `scripts/ci_invocation_model.py` grade the step shells. The root is validated and
structurally tested, not planned (the repository's root positive-control plan runs with the window OPEN so its resource set is unchanged). The
Redis endpoint determinism, the second EIP, real transition durations and the exact billed hours are to be measured at the first authorized window.
