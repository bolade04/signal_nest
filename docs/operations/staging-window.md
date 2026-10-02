# Staging operating window — resource-effect ledger, transition runbook, cost model

**Status: implemented in code and tests only (window 2026-09-30/10-01; API alias record and the USD 20 root budget bound 2026-10-01; the
window-transition principal's policy documents 2026-10-02 — generated, tested, NOT provisioned, §8/§12). No window
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
1. Pre-check (read-only): window state recorded as CLOSED in the previous close receipt; RDS instance status `stopped` or `available`; no ECS deployment in progress; the window form (§12) is filled and its **expiry rule** holds — the apply identity is the window-transition principal of §8 whose inline and read-closure documents expire at the form's `expiry_utc` (≤ 24 h after `issuance_utc`), and **no plan or apply may START within 3 hours of that expiry** (`latest_start_utc` = expiry − 3 h: an apply that crosses expiry loses state persistence and lock release mid-run — stranded resources plus a lock the principal can no longer release); the Name tags the ec2 conditions rely on were read (`ec2 describe-route-tables/subnets/vpcs` → `<prefix>-private-rt`, `<prefix>-public-<az>`, `<prefix>-vpc`); a read-only `tofu state list` confirmed the four un-indexed addresses for the `moved` blocks and whether a reader task definition is in state; the planned apply identity holds the §8 actions — for the alias record, `route53:ChangeResourceRecordSets` on the intended hosted-zone ARN (`arn:aws:route53:::hostedzone/<hosted_zone_id>`, scoped by the record condition keys to `api_fqdn` / type `A` / CREATE, UPSERT, DELETE as §8 recommends — the drifted live W0 holds the action unconditioned and would NOT satisfy this pre-check as worded, deliberately) AND, separately, `route53:GetChange` on change resources (`arn:aws:route53:::change/*`; `GetChange` is change-scoped, never zone-scoped) and, while the live budget still differs from the root bound, `budgets:ModifyBudget` on the one budget ARN (§8); the `hosted_zone_id` in the tfvars is the identity-tier zone the reviewed `Route53Read` statement names (an exact-ARN statement — a different zone id would fail at refresh); write down the EXPECTED NON-WINDOW DIFFS this plan will carry (repository changes merged since the last apply that are not window resources — see step 4), so the abort rule in step 4 is an enumeration, not a guess.
2. RDS: `aws rds start-db-instance --db-instance-identifier <prefix>-postgres` (outside OpenTofu); wait for `available` (5–10 min).
3. tfvars: `staging_window_active = true`, `deploy_workload = false` (first apply of a window never creates services — §7).
4. `tofu plan -out window-open.plan` → `tofu show -json` → review: the window creates — NAT gateway, EIP, private default route, ALB, listener, target group, API alias record (`module.alb.aws_route53_record.api[0]`), replication group — PLUS the **expected non-window diffs enumerated in the pre-read of step 1** (today: the `module.cost.aws_budgets_budget.monthly` in-place update 150 → ≤ 20, because this root now rejects `monthly_budget_limit > 20` and the live budget is the attested 150; the 6B-4C creates from #194 that were never applied — `module.iam.aws_iam_role_policy.api_ses_send` — and, on the very first execution, the four `moved` lines below); **reject** any destroy, any replace, and any change outside that enumerated list. `tofu apply window-open.plan`. A plan that cannot be reconciled line-by-line with the enumeration is not applied; it is recorded and the enumeration is corrected under its own authorization first.
5. Record the new ALB DNS name and the new NAT EIP; verify `api_alias_record_name` equals `api_fqdn` and `dig +short <api_fqdn>` resolves to the NEW ALB (the record is IaC-managed; a stale answer within the 60 s alias TTL is expected, a stale answer after it is a defect — §4); verify the Redis endpoint output;
   record when the 3 RDS + 2 Redis alarms return to OK (they were ALARM for the whole closed period — expected, §2).
5a. Revision reader (if this window's purpose needs the Gate 4J read): only now — it needs the NAT route and RDS `available`.
6. Workload (only if this window's purpose needs it, and only as the AUTH4 cutover prescribes): tfvars `deploy_workload = true` + digests + desired counts per `deployment.md` ("P6-AUTH-4 one-way cutover" Apply 1/2/3), run the migration task if the head moved, verify.

**CLOSE (open → closed), target ≈ 1 billable hour of cleanup:**
0. Pre-read (read-only), as for OPEN step 1 (window form §12, expiry rule: no START within 3 h of `expiry_utc`, tag and state-list reads): write down the EXPECTED NON-WINDOW DIFFS this plan will carry (repository changes merged since the last apply that are not window resources — today the same (b)/(c) as the first-execution paragraph below: the budget update 150 → ≤ 20 and the #194 IAM policy create, if a previous apply has not already carried them); the apply identity holds the §8 actions.
1. Drain: stop accepting work (`api_desired_count = 0`, `worker_desired_count = 0` apply, or the cutover's quiesce step), wait for in-flight jobs; Redis contents will be lost — the durable jobs table is in PostgreSQL, in-flight queue entries are not.
2. tfvars: `deploy_workload = false`, `staging_window_active = false`.
3. `tofu plan -out window-close.plan` → review: the window destroys — services/task definitions (if any), replication group, API alias record, listener, target group, ALB, private default route, NAT gateway, EIP — PLUS the **expected non-window diffs enumerated in step 0** (and, on the very first execution, the four `moved` lines); **reject** any destroy of RDS, buckets, secrets, KMS, roles, SGs, subnets, CloudFront or the web `A`/`AAAA` aliases, any replace, and any change outside that enumerated list — the same rule as OPEN step 4, so a first live execution that happens to be a CLOSE is judged by the same enumeration. `tofu apply window-close.plan`.
   **Expected, non-fatal denial (pre-listed so the abort rule is not tripped):** after `DeleteLoadBalancer` the provider's ENI clean-up looks for
   lingering `ELB app/…` interfaces (`ec2:DescribeNetworkInterfaces`, granted) and may try `DetachNetworkInterface`/`DeleteNetworkInterface`,
   which the window principal deliberately does NOT hold; the provider logs that `AccessDenied` as a WARN and continues. It is the ONE denial the
   window form lists as expected; any OTHER permission denial is a STOP under the abort rule — never an escalation. **Recovery prerequisite
   (stale EIP association):** the `aws_eip` destroy may call `DisassociateAddress` with the association id still held in state from the
   pre-destroy refresh (the NAT gateway is already gone). If EC2 answers `InvalidAssociationID.NotFound` the provider continues; if it answers
   `UnauthorizedOperation` the CLOSE fails at its LAST step with the EIP still allocated (IPv4 charge continues) — the recovery is a re-plan
   (refresh clears `association_id`) and a re-apply that skips the call; record which happened in the close receipt. Window resources carry no
   data, so a partial OPEN/CLOSE is recoverable by a corrected re-plan — but only from a saved, inspected plan, never by widening a grant.
4. RDS: `aws rds stop-db-instance --db-instance-identifier <prefix>-postgres` — **NO `--db-snapshot-identifier` under the window-transition principal**
   (`rds:CreateDBSnapshot` is in its deny ceiling, §8/§12; a dated snapshot would be a second, unexpected denial and therefore a STOP); wait for `stopped`.
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

**Reviewed design (2026-10-02; operator selections D1 = A, D3, D4, D5, D6 — repository delivery only):** W0 is to be RESTORED to the
repository-reviewed document (`scripts/gen_operator_policies.py permanent_w0_policy()`, unchanged by this tranche) and window transitions run under
a **separate, expiring window-transition principal** emitted by the same generator in three documents, because the complete window closure does not
fit one Identity Center permission-set inline policy (quota 10,240 non-whitespace characters == the IAM role aggregate inline limit; customer managed
policies 6,144 each):

| document | emit | content | expiry |
|---|---|---|---|
| permission-set INLINE policy | `--emit window-transition-inline --expiry <utc> --api-fqdn <api_fqdn> [--issuance <utc>]` | state backend (exact state object, lock table, state CMK via s3/dynamodb only); the window writes on their exact resources — `ec2:AllocateAddress`/`ReleaseAddress`/`DisassociateAddress`, `CreateNatGateway`/`DeleteNatGateway`, `CreateRoute`/`DeleteRoute`, `CreateTags` (Name-tag and `ec2:CreateAction` conditions), `elasticloadbalancing` load balancer/target group/listener (+ `AddTags` tag-on-create; `CreateListener` authorizes against the load balancer), `elasticache` replication group (encryption `Bool` keys on CREATE only), `route53:ChangeResourceRecordSets` on the ONE consumed zone confined to `api_fqdn` / `A` / `CREATE,UPSERT,DELETE` by the three record condition keys, `route53:GetChange` on `change/*` (change-scoped, never zone-scoped), `budgets:ModifyBudget` on the one budget, `iam:PutRolePolicy` on the api-task role ONLY and ONLY with the reviewed permissions boundary (D5, the #194 `api_ses_send` create), `rds:StartDBInstance`/`StopDBInstance` on the one instance (D4; **no dated snapshot** — `rds:CreateDBSnapshot` stays denied, so the optional `--db-snapshot-identifier` is NOT available to this principal); five `NotResource` fences | every Allow `DateLessThan` the window expiry |
| customer managed policy 1 | `--emit window-transition-read-closure --expiry <utc>` | the full refresh read closure (identical action sets to W0) + `ec2:DescribeNetworkInterfaces` (the ALB-destroy ENI clean-up look-up) + `ecs:DescribeTaskDefinition` (kept: the reader task definition is gated by the reader flags, not `deploy_workload`) | every Allow expiring |
| customer managed policy 2 | `--emit window-transition-deny-ceiling` | the flat deny ceiling: `PERMANENT_DENY ∪ FORBIDDEN_CAPABILITIES` minus the seven scoped capabilities (re-denied by the fences); `ecs:RegisterTaskDefinition`, `iam:CreateRole`, `iam:PassRole`, `rds:CreateDBSnapshot`, `cloudtrail:StopLogging` … stay flatly denied | never |

`--emit window-transition-effective` renders the three concatenated — what the reserved role effectively evaluates; it is the document the
allow-model ceiling proof, the deny probes and the action classifier analyse, and it is never provisioned as one document. The generator refuses
to emit a document above its quota, a malformed or placeholder `api_fqdn`, or an expiry that is not authorized against its issuance
(`expiry_authorization`, purpose `window_transition`, ≤ 24 h, ≥ 15 min). The two operator-held inputs (`api_fqdn` from the tfvars; the per-window
issuance/expiry) are supplied at generation and never committed. `infra/aws/operator-closure-contract.json` → `window_transition_closure` is the
independently authored expectation the generator is tested against (`tests/test_window_transition_policy.py`). **Digest supersession:** the generator's
Sids carry a `Win` prefix (e.g. `WinDenyDangerous`), so its canonical digests differ from the hand-built candidate documents of the sealed preparation
set (`candidate-policies/A1s-*`, digests `602f0977…` / `5044dfc0…` / `1b8c6863…`); every readback expectation at execution (the sealed proposal's steps
3.3 / 4.4 / 4.6, §12 `document_digests`) is the GENERATOR's digest of the FILLED documents, computed at fill time — never the candidate digest.

**What this repository change does NOT do:** it restores nothing, creates no permission set, no customer managed policy and no assignment, and
provisions nothing. Those are live acts under their own authorization (sealed `P6-W0-TRANSITION-PERMS-prep`, `EXECUTION-PROPOSAL.txt`; operator
decisions D2 — provisioning path and mechanism — and D7 — fill values — remain open). **A stored permission-set document is NOT a provisioned
principal:** `PutInlinePolicyToPermissionSet` + a readback proves only the store; provisioning is proven by a TERMINAL `ProvisionPermissionSet`
status (`SUCCEEDED`, never `IN_PROGRESS`) AND an effective-role read (`iam get-role-policy` on the reserved role whose canonical digest equals the
stored document, both customer managed policies attached). The 2026-08-17 W0 attempt stored the reviewed document and never reached the role.

The live W0 role's inline policy (sealed reads 2026-09-27 and 2026-09-30) is the unreviewed drifted document (FV-20): it carries the window writes
unconditioned plus role minting, zone-wide DNS write, CloudTrail stop and KMS/RDS destruction; it is adopted nowhere. **The repository-reviewed W0
carries NONE of the window writes** (every `ec2:`/`elasticloadbalancing:`/`elasticache:`/`rds:` action in `permanent_w0_policy()` is
`Describe*`/`ListTags*` except the denied `rds:DeleteDBInstance`/`ModifyDBInstance`), which is why the window principal exists. Service
creation/deletion (`ecs:CreateService/UpdateService/DeleteService/RegisterTaskDefinition`) is in NEITHER — the cutover's separately authorized
principal, as already designed.

**API alias record and budget correction (added 2026-10-01; mapped to the reviewed apply identity, nothing granted):**
- `module.alb.aws_route53_record.api` (create on OPEN, UPSERT on an in-window ALB replacement, delete on CLOSE) needs `route53:ChangeResourceRecordSets`
  on the ONE consumed zone (`arn:aws:route53:::hostedzone/<hosted_zone_id>`) and `route53:GetChange` on `arn:aws:route53:::change/*` (the provider
  polls the change to INSYNC). Natural scope for a future grant: `ForAllValues:StringEquals` on `route53:ChangeResourceRecordSetsNormalizedRecordNames`
  (= `api_fqdn`, lowercase, no trailing dot), `route53:ChangeResourceRecordSetsRecordTypes` (= `A`), `route53:ChangeResourceRecordSetsActions`
  (= `CREATE`/`UPSERT`/`DELETE`). The plan-time and read-after-write reads (`route53:GetHostedZone`, `route53:ListResourceRecordSets`) ARE in the
  reviewed W0 (`Route53Read`, an exact-ARN statement on the identity-tier zone — so the tfvars `hosted_zone_id` must be that zone; apply-time pre-check).
- `module.cost.aws_budgets_budget.monthly` (the 150 → ≤ 20 update the next apply necessarily carries) needs `budgets:ModifyBudget` on
  `arn:aws:budgets::<account>:budget/<prefix>-monthly` (the service authorization reference maps the `UpdateBudget`, `CreateNotification`,
  `DeleteNotification` and `UpdateNotification` API operations to that one IAM action on the `budget` resource; there is no `budgets:UpdateBudget`
  or `budgets:ModifyNotification` IAM action); `budgets:TagResource`/`UntagResource` only if the live budget's tags drifted from `default_tags`.
  The reads (`budgets:ViewBudget`, `ListTagsForResource`) ARE in the reviewed W0 (`BudgetsRead`).
  - Legacy portal vs programmatic authorization (qualified 2026-10-01 from current official AWS documentation): the Budgets operations table also
    lists `aws-portal:ModifyBilling` (writes) / `aws-portal:ViewBilling` (reads). Those are the LEGACY console-era billing permissions: AWS states
    the `aws-portal` namespace "reached the end of standard support on July 2023", that the fine-grained replacement actions (`account`, `billing`,
    `payments`, …) are already in effect for accounts and organizations created on or after 2023-03-06 11:00 PDT, and that "API access to AWS Cost
    Explorer, AWS Cost and Usage Reports, and AWS Budgets remains unaffected" by the migration. The OpenTofu provider's Budgets calls are
    programmatic API requests, so the repository does NOT treat `aws-portal:*` as a universally required grant for the budget update. The separate
    "Activate IAM Access" console setting is documented by AWS as governing the Billing and Cost Management console pages and NOT the Budgets API
    ("doesn't control access to … The Billing and Cost Management SDK APIs (AWS Cost Explorer, AWS Budgets, and AWS Cost and Usage Reports APIs)"),
    so it is not a precondition for the provider's call. What the repository cannot see and records as **UNKNOWN**: whether THIS account /
    organization predates the 2023-03-06 cutover and still evaluates legacy `aws-portal` grants, and whether any live grant or SCP references
    `aws-portal` — to be read (read-only) before the budget grant is designed. Sources: Service Authorization Reference "Actions, resources, and
    condition keys for AWS Budget Service" (and "… for Amazon Route 53" for the alias actions); AWS Billing user guide "Migrating access control for
    AWS Billing" and "Overview of managing access permissions". Nothing here grants, changes or adopts any permission.
- The repository-reviewed W0 (`scripts/gen_operator_policies.py`, Sids `Route53Read`, `BudgetsRead`) holds none of the write-path actions
  (`route53:ChangeResourceRecordSets`, `route53:GetChange` — access level List in the reference, but needed only by the write path —
  `budgets:ModifyBudget`) nor the conditional `budgets:TagResource`/`UntagResource`; they are implicitly denied (absent from every Allow, absent from
  the explicit deny lists). **No reviewed principal can create or delete the alias record or
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
None of the remaining items is amended by this change; each is a ruling for the operator. The W0 drift adjudication was answered in design on
2026-10-02 (D1 = A: restore W0 to the reviewed baseline + the separate window principal of §8); the RESTORE itself and the provisioning are live
acts that remain pending (D2).

## 10. Dependencies

- PR #194 (6B-4C mail wiring) and this change are logically independent, but were NOT conflict-free: `main.tf` and `variables.tf` hunks did not
  overlap; `terraform.tfvars.example` (both inserted after the same base line) and the synthetic root fixture (both appended at end of file) collided.
  RESOLVED in the #194 integration (merge of main into its branch): both files carry BOTH sets of required root inputs — the example keeps
  `staging_window_active = false` and the placeholder mail tokens, the positive-control fixture keeps `staging_window_active = true` and the
  synthetic mail values — so the required-variable parity test (`tests/test_root_wiring_check.py`) passes. Mail is only exercised inside a window.
- The first live open/close needs: the W0 RESTORE and the window principal PROVISIONED (§8: permission set + two customer managed policies +
  one assignment; D2 path/mechanism open) — which together carry the RDS stop/start grant, the Route 53 record-change grant for the alias and the budget
  grant (`budgets:ModifyBudget`) — the budget correction is NO LONGER a separable package: the root REJECTS `monthly_budget_limit > 20`, so the
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

## 12. Window authorization form (one per window; nothing opens without it)

Every field is operator-filled; the form travels with the window's evidence set. Fields marked ★ are new with the window-transition principal.

```
window_id:              W-YYYY-MM-DD-nn
purpose:                (e.g. "E8 real-mail reset proof", "AUTH4 cutover Apply 1-3")
opens_utc / closes_utc: max 8 h operating + 2 h transitions = 10 billable hours (a 16-hour purpose counts as TWO windows against the target of 4)
apply_identity:         the window-transition principal (§8) — permission-set name only, no ids in the repository
rds_identity:           the same principal (D4) — rds:StartDBInstance / StopDBInstance on the one instance, NO dated snapshot
★ issuance_utc:          the window's issuance instant; expiry_utc ≤ issuance_utc + 24 h (expiry_authorization, purpose window_transition)
★ expiry_utc:            the DateLessThan stamped into the inline + read-closure documents (the deny ceiling never expires)
★ latest_start_utc:      expiry_utc − 3 h — NO plan or apply may START after this instant (an apply crossing expiry strands resources and the lock)
★ document_digests:      canonical sha256 of the FILLED inline, read-closure and deny-ceiling documents (measured at fill time; inline ≤ 10,240,
                         managed ≤ 6,144 non-whitespace characters — the generator refuses otherwise) and of the effective composition
★ provisioning_evidence: ProvisionPermissionSet requestId + TERMINAL status (SUCCEEDED) + iam get-role-policy digest of the reserved role ==
                         inline digest + the two customer managed policy ARNs attached (a stored-document readback alone is NOT provisioning)
★ pre_reads:             Name tags of <prefix>-private-rt / <prefix>-public-<az> / <prefix>-vpc confirmed; `tofu state list` (four un-indexed
                         addresses; reader task definition present/absent); no IN_PROGRESS permission-set provisioning
workload_stage:         none | cutover-apply-1..3 | rolling-deploy (each only as deployment.md prescribes; a DIFFERENT principal)
expected_cost:          1.55 pre-tax (10 billable hours) + any workload extras; running month-to-date from the attested bills must be shown
★ expected_denials:      exactly ONE, at CLOSE: the provider's ALB ENI clean-up Detach/DeleteNetworkInterface AccessDenied WARN (non-fatal, §3)
abort_criteria:         any planned destroy outside the ledger's windowed set; any planned change to RDS, secrets, KMS, buckets, roles, CloudFront;
                        RDS not `available` after 15 min; a plan touching more than the expected addresses; ANY permission denial other than the
                        one expected (stop, do not escalate); the current time past latest_start_utc before `tofu apply` begins
★ recovery_prerequisites: a stale-EIP-association failure at the last CLOSE step is recovered by re-plan + re-apply (never by widening); the
                         close receipt records which EC2 answer (NotFound vs UnauthorizedOperation) was observed
```
