# Staging operating window — resource-effect ledger, transition runbook, cost model

**Status: implemented in code and tests only (window 2026-09-30/10-01; API alias record and the USD 20 root budget bound 2026-10-01). No window
has been opened or closed live, no DNS record and no budget change exist in AWS; nothing in this document authorizes a plan, an apply, a shutdown,
a start, a record change or a deployment.** Governing numbers: the operator's limit is **USD 20 TOTAL per month** (tax-inclusive, since the bill is); the
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
| API Route 53 alias record `api_fqdn` → ALB (`alb`, `aws_route53_record.api`, P6-INF-3; **code only since 2026-10-01, never created live**) | present — created AFTER the load balancer, re-pointed automatically to the window's new ALB DNS name | **destroyed BEFORE the load balancer**; the name then has no record | no data; one `A` alias (IPv4-only ALB), zone consumed; residual caching: after CLOSE up to the alias TTL (60 s, AWS-documented, not repository-derivable), after OPEN up to the zone's negative-caching TTL (§4) |
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

What the ledger does NOT include: the SPA (persists, ≈ $0); any "second EIP" observed in the 2026-09-30 read-only round whose owner is not
known from the repository — the first live close must identify it (`ec2 describe-addresses`) before assuming it is released. (The API alias record,
formerly listed here as deferred under §24.7, is now a windowed row above: IaC-managed, re-pointed each window — §4.)

## 3. Transition runbook (each step is a separately authorized act; every mutation from a saved, inspected plan)

**OPEN (closed → open), target ≈ 1 billable hour of provisioning:**
1. Pre-check (read-only): window state recorded as CLOSED in the previous close receipt; RDS instance status `stopped` or `available`; no ECS deployment in progress; the planned apply identity holds the §8 actions (including `route53:ChangeResourceRecordSets`/`GetChange` on the one zone and, while the live budget still differs from the root bound, `budgets:ModifyBudget`); the `hosted_zone_id` in the tfvars is the identity-tier zone the reviewed `Route53Read` statement names (an exact-ARN statement — a different zone id would fail at refresh); write down the EXPECTED NON-WINDOW DIFFS this plan will carry (repository changes merged since the last apply that are not window resources — see step 4), so the abort rule in step 4 is an enumeration, not a guess.
2. RDS: `aws rds start-db-instance --db-instance-identifier <prefix>-postgres` (outside OpenTofu); wait for `available` (5–10 min).
3. tfvars: `staging_window_active = true`, `deploy_workload = false` (first apply of a window never creates services — §7).
4. `tofu plan -out window-open.plan` → `tofu show -json` → review: the window creates — NAT gateway, EIP, private default route, ALB, listener, target group, API alias record (`module.alb.aws_route53_record.api[0]`), replication group — PLUS the **expected non-window diffs enumerated in the pre-read of step 1** (today: the `module.cost.aws_budgets_budget.monthly` in-place update 150 → ≤ 20, because this root now rejects `monthly_budget_limit > 20` and the live budget is the attested 150; the 6B-4C creates from #194 that were never applied — `module.iam.aws_iam_role_policy.api_ses_send` — and, on the very first execution, the four `moved` lines below); **reject** any destroy, any replace, and any change outside that enumerated list. `tofu apply window-open.plan`. A plan that cannot be reconciled line-by-line with the enumeration is not applied; it is recorded and the enumeration is corrected under its own authorization first.
5. Record the new ALB DNS name and the new NAT EIP; verify `api_alias_record_name` equals `api_fqdn` and `dig +short <api_fqdn>` resolves to the NEW ALB (the record is IaC-managed; a stale answer within the 60 s alias TTL is expected, a stale answer after it is a defect — §4); verify the Redis endpoint output;
   record when the 3 RDS + 2 Redis alarms return to OK (they were ALARM for the whole closed period — expected, §2).
5a. Revision reader (if this window's purpose needs the Gate 4J read): only now — it needs the NAT route and RDS `available`.
6. Workload (only if this window's purpose needs it, and only as the AUTH4 cutover prescribes): tfvars `deploy_workload = true` + digests + desired counts per `deployment.md` ("P6-AUTH-4 one-way cutover" Apply 1/2/3), run the migration task if the head moved, verify.

**CLOSE (open → closed), target ≈ 1 billable hour of cleanup:**
0. Pre-read (read-only), as for OPEN step 1: write down the EXPECTED NON-WINDOW DIFFS this plan will carry (repository changes merged since the last apply that are not window resources — today the same (b)/(c) as the first-execution paragraph below: the budget update 150 → ≤ 20 and the #194 IAM policy create, if a previous apply has not already carried them); the apply identity holds the §8 actions.
1. Drain: stop accepting work (`api_desired_count = 0`, `worker_desired_count = 0` apply, or the cutover's quiesce step), wait for in-flight jobs; Redis contents will be lost — the durable jobs table is in PostgreSQL, in-flight queue entries are not.
2. tfvars: `deploy_workload = false`, `staging_window_active = false`.
3. `tofu plan -out window-close.plan` → review: the window destroys — services/task definitions (if any), replication group, API alias record, listener, target group, ALB, private default route, NAT gateway, EIP — PLUS the **expected non-window diffs enumerated in step 0** (and, on the very first execution, the four `moved` lines); **reject** any destroy of RDS, buckets, secrets, KMS, roles, SGs, subnets, CloudFront or the web `A`/`AAAA` aliases, any replace, and any change outside that enumerated list — the same rule as OPEN step 4, so a first live execution that happens to be a CLOSE is judged by the same enumeration. `tofu apply window-close.plan`.
4. RDS: `aws rds stop-db-instance --db-instance-identifier <prefix>-postgres` (optionally `--db-snapshot-identifier` for a dated snapshot); wait for `stopped`.
5. Verify (read-only): the 3 RDS + 2 Redis alarms go to ALARM (expected; acknowledge, do not "fix"); `ec2 describe-nat-gateways` none `available`; `ec2 describe-addresses` lists NO allocation owned by this composition; `elbv2 describe-load-balancers` none; `elasticache describe-replication-groups` none; `rds describe-db-instances` status `stopped`; write the close receipt with the UTC times and the billable-hour count.

**Seven-day automatic restart (§5) is part of every CLOSED period, not of the transition.**

**FIRST execution from the current always-on state (special case, once):** the live state holds `module.alb.aws_lb.this`, `aws_lb_target_group.api`,
`aws_lb_listener.https` and `module.data_cache.aws_elasticache_replication_group.this` WITHOUT an index. The modules carry `moved` blocks for the
four, so the first plan must show exactly four `has moved to … [0]` lines and NO replace of those resources; a plan that proposes to destroy and
re-create any of them is REJECTED. With `staging_window_active = true` that first plan is NOT empty; from the repository alone it is expected to
show (a) the one create `module.alb.aws_route53_record.api[0]` (no record exists live — P6-INF-3 was never created), (b) the in-place update of
`module.cost.aws_budgets_budget.monthly` from the attested 150 to the tfvars value ≤ 20 (this root rejects anything above 20, so the operator's
tfvars MUST change before this plan — the budget correction cannot be separated from the next apply of this root), and (c) whatever #194 (merged
a582440, repository delivery only) added that was never applied — at least the create of `module.iam.aws_iam_role_policy.api_ses_send`; the API/
worker task-definition environment changes surface only with `deploy_workload = true`. Anything beyond (a)–(c) and the four `moved` lines is
unexplained and the plan is rejected. With `false` it is the first CLOSE: (b) and (c) still appear (they are not windowed) and (a) does not (no
alias record exists to destroy). The live state was last read on 2026-09-30 (read-only round); this list is derived from the repository, not from a
fresh read, and must be re-derived against `git log <last-applied>..HEAD -- infra/aws` before the plan. Before it, the operator confirms the four addresses with a read-only `tofu state list`.
⚠ `auto_minor_version_upgrade = true` on the RDS instance with a major.minor `engine_version`: a minor version applied by AWS (possible after an
automatic restart) surfaces as an in-place RDS change in a later plan, which the abort rule rejects — that case needs a config edit under its own
authorization, not a bypass. The managed master password's rotation cannot run while the instance is stopped (AWS retries; impact unverified).

## 4. DNS handling and dependency-safe recreation

- Web: `app.<…>` A/AAAA aliases point at CloudFront, which persists — unaffected by windows.
- API: the ALB is re-created per window with a NEW DNS name. The P6-INF-3 alias record is now authored (2026-10-01; §24.7 deferral lifted for a
  windowed record) as `module.alb.aws_route53_record.api` — one `A` alias `api_fqdn` → the ALB, in the consumed `hosted_zone_id`, with the same
  `count` as the load balancer. Because its alias target is the planned `aws_lb` `dns_name`/`zone_id` (not a literal), every window's new ALB is
  re-pointed by the same apply that creates it; nothing is hand-edited. **It does not exist live** until an authorized apply.
  - Creation/deletion ordering is implied by the reference: OPEN creates the load balancer, then the record (the API hostname has no answer until
    the record exists — in the plan's own ordering, seconds after the ALB; the ALB target group has no healthy target until the workload stage anyway);
    CLOSE destroys the record, then the load balancer. For WINDOW TRANSITIONS the hostname therefore never points at a destroyed ALB. This does NOT
    hold for an ALB **replacement inside a window** (e.g. a subnet or name change): `aws_lb.this` has no `create_before_destroy`, so OpenTofu
    destroys the old ALB, creates the new one and only then upserts the record — during that interval the name still answers with the destroyed
    ALB's DNS name. Such a plan shows `-/+` on the ALB and is a rejected shape under §3 anyway.
  - Residual DNS caching: Route 53 answers ALB aliases with a 60 s TTL (AWS-documented behaviour; not derivable from this repository). After OPEN,
    resolvers that cached the previous negative answer (negative TTL = the zone SOA minimum, operator-controlled, outside this repository) may keep it
    for that long; after CLOSE, resolvers may return the old answer for up to 60 s — an address from the ALB pool that AWS may reassign after deletion,
    so the only guarantee the repository can give is that TLS FAILS: nothing that answers presents a certificate for `api_fqdn` (the repository
    attaches `api_certificate_arn` only to this listener; what else the consumed certificate is attached to is not knowable from the repository). The API hostname is meant to be the origin a future staging SPA build receives as `VITE_API_BASE_URL`
    (`deployment.md`; no staging SPA build exists yet — P6-INF-2 open), so that origin stays valid across windows; only resolution is windowed.
  - Type `A` only: the ALB is IPv4-only (`ip_address_type = "ipv4"`); no AAAA. `evaluate_target_health = false` (single record, no failover sibling).
  - Collision guard: the root rejects `api_fqdn == web_fqdn` (case-insensitive) — one name cannot alias both CloudFront (edge) and the ALB (alb) in the
    same zone; the record name is `lower(var.api_fqdn)` because Route 53 stores names case-folded. Web `A`/`AAAA` aliases are untouched by windows:
    edge has no window input and its two records carry no `count` — pinned textually by
    `tests/test_api_alias_and_budget_bound.py::test_the_web_dns_records_are_unchanged_and_not_windowed` (the root suite can only assert the web edge
    OUTPUTS, since module resources are not addressable from a root run).
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

**API alias record and budget correction (added 2026-10-01; mapped to the reviewed apply identity, nothing granted):**
- `module.alb.aws_route53_record.api` (create on OPEN, UPSERT on an in-window ALB replacement, delete on CLOSE) needs `route53:ChangeResourceRecordSets`
  on the ONE consumed zone (`arn:aws:route53:::hostedzone/<hosted_zone_id>`) and `route53:GetChange` on `arn:aws:route53:::change/*` (the provider
  polls the change to INSYNC). Natural scope for a future grant: `ForAllValues:StringEquals` on `route53:ChangeResourceRecordSetsNormalizedRecordNames`
  (= `api_fqdn`, lowercase, no trailing dot), `route53:ChangeResourceRecordSetsRecordTypes` (= `A`), `route53:ChangeResourceRecordSetsActions`
  (= `CREATE`/`UPSERT`/`DELETE`). The plan-time and read-after-write reads (`route53:GetHostedZone`, `route53:ListResourceRecordSets`) ARE in the
  reviewed W0 (`Route53Read`, an exact-ARN statement on the identity-tier zone — so the tfvars `hosted_zone_id` must be that zone; apply-time pre-check).
- `module.cost.aws_budgets_budget.monthly` (the 150 → ≤ 20 update the next apply necessarily carries) needs `budgets:ModifyBudget` on
  `arn:aws:budgets::<account>:budget/<prefix>-monthly` (it also covers the provider's notification re-sync; there is no `budgets:UpdateBudget` or
  `ModifyNotification` IAM action); `budgets:TagResource`/`UntagResource` only if the live budget's tags drifted from `default_tags`. The reads
  (`budgets:ViewBudget`, `ListTagsForResource`) ARE in the reviewed W0 (`BudgetsRead`).
- The repository-reviewed W0 (`scripts/gen_operator_policies.py`, Sids `Route53Read`, `BudgetsRead`) holds NONE of the four write actions; they are
  implicitly denied (absent from every Allow, absent from the explicit deny lists). **No reviewed principal can create or delete the alias record or
  correct the budget — two MISSING GRANTS, recorded here, not added.** The live drifted W0 (sealed 2026-09-30 captures; `P6-W0-remediation-design/
  LIVE-VS-REVIEWED-MATRIX.txt` Route 53 and Budgets rows) DOES carry `route53:ChangeResourceRecordSets` on the zone WITHOUT any record-name/type/action
  condition, `route53:GetChange` on `change/*` and `budgets:ModifyBudget` on `budget/*` — exactly the unreviewed over-grant FV-20 records; this change
  neither adopts nor extends it, and the W0 drift adjudication remains pending.
- `infra/aws/provider-api-operation-map.json` / `operator-closure-contract.json` enumerate the READ (refresh) closure and key by resource TYPE
  (`aws_route53_record` is already mapped from the edge module; `scripts/verify_closure.py` stays clean); the write actions above are outside them by
  design and belong to a window-transition principal that does not exist yet. The map's `unit: "edge"` label for `aws_route53_record` is now stale
  (the type is also declared in `alb`); no consumer reads `unit`, and the governed map is left unchanged here.

## 9. Governance amendments requiring an operator ruling (always-on invariants this design changes)

- IaC plan §26.10 "two long-running services (API, worker)", "API desired count 1, worker desired count 1" — inside windows only.
- Runtime contract §C (service topology: two continuously running services) and §M (cost contract: its "scale-to-zero off-hours" lever and its
  planning estimate): availability becomes windowed; the planning estimate is superseded by the attested figures and this cost model.
- Runtime contract §D (network: outbound egress "required for the LLM provider … and image pulls"): no egress while closed — acceptable only because
  no task runs while closed.
- `deployment.md` rolling deployment: valid inside a window; the cutover becomes a window-internal sequence (§7).
- The IaC plan's "no targeted apply" rule is **preserved** (the window is a declared input, not a `-target`).
- ~~P6-INF-3 (API alias) must be designed as a windowed/conditional record (§4) — its §24.7 deferral still stands.~~ Done in code 2026-10-01 (§4; the
  §24.7 deferral was lifted for exactly this windowed record by operator authorization). Live creation remains a separate act (§8 grant missing).
None of the remaining items is amended by this change; each is a ruling for the operator. The W0 drift adjudication also remains pending.

## 10. Dependencies

- PR #194 (6B-4C mail wiring) and this change are logically independent, but were NOT conflict-free: `main.tf` and `variables.tf` hunks did not
  overlap; `terraform.tfvars.example` (both inserted after the same base line) and the synthetic root fixture (both appended at end of file) collided.
  RESOLVED in the #194 integration (merge of main into its branch): both files carry BOTH sets of required root inputs — the example keeps
  `staging_window_active = false` and the placeholder mail tokens, the positive-control fixture keeps `staging_window_active = true` and the
  synthetic mail values — so the required-variable parity test (`tests/test_root_wiring_check.py`) passes. Mail is only exercised inside a window.
- The first live open/close needs: the W0 ruling (§8), the RDS stop/start grant, the Route 53 record-change grant for the alias (§8), the budget
  grant (`budgets:ModifyBudget`, §8) — the budget correction is NO LONGER a separable package: the root REJECTS `monthly_budget_limit > 20`, so the
  next authorized apply of this root necessarily carries the 150 → ≤ 20 update (§3 step 4), while nothing changes in AWS until then (the live budget
  is still the attested 150) — and a window authorization naming date, duration, purpose and the apply identity.
- Budget semantics (2026-10-01): the root's `monthly_budget_limit <= 20` validation and the AWS Budget's 50/75/90/100 % notifications are
  **observational** — they constrain what the budget declares and e-mail when ACTUAL spend crosses a threshold; nothing stops, caps or
  remediates spending. No budget action was added, no notification was re-sent, no billing inclusion changed. The historical USD 200 ceiling is the
  generic `cost` module's own bound (unchanged); the USD 20 operator limit is the staging root's.

## 11. Limitations

Nothing here has been planned or applied against AWS. The module tests prove open/closed shapes with a mocked provider and RUN IN CI as graded steps of the `revision-reader` job
(`window_tests_alb` 9 runs — `window.tftest.hcl` 3 + `api_alias.tftest.hcl` 6 (the P6-INF-3 alias: open/closed, `A` only, bound to the planned
ALB, malformed `api_fqdn`/`hosted_zone_id` rejected) —, `window_tests_data_cache` 2, `window_tests_network` 2; offline, fully mocked, no AWS call), followed by
`window_negative_control`, which initialises a doctored copy of the network module offline from the already-verified network cache and
requires its one false assertion to FAIL `tofu test`. Each module carries the root's byte-identical provider constraint (the modules/iam Gate
4N-I8 rule; committed child-module lock files stay prohibited), and the three module caches are members of the review-pinned
`check_toolchain_integrity.py::EXPECTED_CACHE_ROOTS` collection under ledger record `REV-2026-10-01-P6-STAGING-WINDOW-CI-CACHE-ROOTS`:
classified by the post-init step (expected version, binary provenance), not exempted. `tests/test_window_module_ci.py` pins the wiring
hermetically; `scripts/failure_propagation.py` and `scripts/ci_invocation_model.py` grade the step shells. The ROOT has its own graded step since
2026-10-01, `root_boundary_tests` (`infra/aws/input_boundary.tftest.hcl`, 7 runs, `tofu init -backend=false` so the committed S3 backend is never
initialised; fully mocked — sibling-module validations need ARN-shaped mock defaults and the network module is overridden as a whole for distinct
subnet ids): `monthly_budget_limit` 20 accepted, 21 and 200 rejected; `api_fqdn == web_fqdn` rejected case-insensitively; a malformed `api_fqdn`
rejected; closed window → no ALB and no alias record while the web edge persists; open window → the alias record named `api_fqdn`. Each new
suite was shown to FAIL on a doctored scratch copy before being trusted (bound widened to 200: the first rejection run, 21, fails and `tofu test`
skips the rest of the file; alias record ungated: the first closed-window run fails — by an evaluation error at the alias's `aws_lb.this[0]`
references, i.e. an ungated record cannot even plan while closed — and the rest are skipped) — recorded as
`raw/negative-control-root-budget-bound-widened.txt` and `raw/negative-control-alb-alias-ungated.txt` in the evidence set
`P6-WINDOW-DNS-BUDGET-impl` (sealed at tranche close; digests then in its `EVIDENCE-SHA256.txt`); CI itself executes the suites (`window_tests_alb`,
`root_boundary_tests`) but its negative control covers the network suite only. The root is otherwise validated and
structurally tested, not planned against AWS (the repository's root positive-control plan runs with the window OPEN so its resource set is unchanged
except for the one added alias record). The
Redis endpoint determinism, the second EIP, real transition durations and the exact billed hours are to be measured at the first authorized window.
