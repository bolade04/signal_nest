# Module: `data_cache` (implemented — provider-schema-validated, not root-composed)

## 1. Purpose and ownership
Owns a **private Amazon ElastiCache for Redis** replication group for SIGNALNEST_STAGING
— the cache, queue-coordination, and notify-channel paths — its **cache subnet group**,
its **Redis security group** (created rule-free), and a **Redis parameter group**. This
module is **implemented and provider-schema-validated only**: authored and validated with
OpenTofu against the locked AWS provider, but **not** wired into the root composition
(`infra/aws/main.tf`), **nothing is provisioned or deployed**, no Redis exists, and **no
AWS account has been contacted** (see §8).

## 2. Resources declared (no data source)
| Resource | Purpose |
| --- | --- |
| `aws_elasticache_subnet_group` | Places the replication group in the supplied private subnets |
| `aws_security_group` | Redis SG, **owned here, created with zero rules** |
| `aws_elasticache_parameter_group` | Family `redis6.x` (6 line) or `redis<major>` (7+); no custom parameters this tranche |
| `aws_elasticache_replication_group` | Private, encrypted Redis replication group |

No IAM, ECS, CloudWatch/log-delivery, security-group-rule, or auth/secret resource is
declared, and there is no AWS-querying data source.

## 3. Inputs
`name_prefix` (required), `vpc_id` (required), `private_subnet_ids` (required
`list(string)`, ≥2 distinct), `engine_version` (**required**, no default — a Redis major
like `7` or major.minor like `7.1`; the parameter family is derived from the major),
`node_type` (default `cache.t4g.micro`), `num_cache_clusters` (default `1`),
`automatic_failover_enabled` (default `false`), `multi_az_enabled` (default `false`),
`snapshot_retention_limit` (default `1`, range `0..35`), `kms_key_id` (nullable, default
`null`).

There is **no** `auth_token`/password, `port`, `tags`, ingress, CIDR, source-security-group,
or Redis-URL input.

## 4. Outputs
`redis_primary_endpoint` (host only), `redis_port` (6379), `cache_subnet_group_name`,
`redis_security_group_id` (consumed by future `ecs`). **No** auth token, password,
complete `REDIS_URL`, or secret value is output; none is marked sensitive because none is
a secret value.

## 5. Encryption, authentication, and TLS (Option A)
- **At-rest encryption** is enabled (`at_rest_encryption_enabled = true`). **In-transit
  encryption** is enabled with **`transit_encryption_mode = "required"`** — clients must
  use TLS.
- **No `auth_token`.** No Redis password is generated, read, stored, output, or committed,
  and **none enters OpenTofu state.** Access control is the private Redis security group
  plus subnet isolation.
- **`REDIS_URL` is populated out-of-band** (via Secrets Manager, a separate step) and uses
  the **`rediss://`** scheme (TLS). The application's Redis client (`redis.from_url` /
  `ConnectionPool.from_url`) enables TLS automatically for `rediss://`. This module
  constructs and outputs **no** `REDIS_URL`.

## 6. Networking and security-group ownership
Consumes `vpc_id` and `private_subnet_ids` (from `network`). Owns the cache subnet group
and the Redis SG and **outputs the SG id**. The SG is created with **zero rules** — no
inline ingress/egress, no `aws_security_group_rule`, no `aws_vpc_security_group_*_rule`,
no CIDR/source-SG input, no `0.0.0.0/0`/`::/0`, no public access. The future **`ecs`**
module owns the two standalone TCP 6379 ingress rules — from the **API and worker** task
SGs only; the **migration task receives no Redis access** (it is pinned to
`queue_backend=inprocess`/`cache_backend=memory`, §26.3). One-way `data_cache -> ecs`,
acyclic; **no ECS dependency is introduced here.** Isolated HCL validation cannot prove the
subnets are private, span ≥2 AZs, or exist in the selected account/Region.

## 7. Topology, backups, KMS, maintenance, logging
- **Topology:** single-shard replication group; `num_cache_clusters` default `1`
  (single node), `automatic_failover_enabled` and `multi_az_enabled` default `false`.
  Resource preconditions enforce that failover requires ≥2 nodes and Multi-AZ requires
  failover + ≥2 nodes, so an invalid combination cannot be applied.
- **Backups:** `snapshot_retention_limit` default `1` (Redis is a cache / queue-coordination
  store, not the system of record — PostgreSQL is — so minimal retention is acceptable; `0`
  disables automatic snapshots).
- **KMS:** `kms_key_id` nullable, default `null` → the AWS-managed ElastiCache key. A
  caller-supplied key needs a compatible Region/key-state/policy/grants this isolated module
  cannot validate, and the at-rest KMS choice is **fixed at creation** (not an in-place
  update) — finalize before the first apply. The secrets module CMK is **not** auto-reused.
- **Maintenance:** AWS defaults (no `apply_immediately`, no invented maintenance window).
- **Logging:** no log-delivery / CloudWatch resources in this tranche (deferred to a
  separate observability tranche).

## 8. Validation performed
**AWS-free provider-schema validation.** `tofu fmt`/`fmt -check`, and `tofu init
-backend=false -lockfile=readonly` + `tofu validate` run through a temporary,
**repository-external** harness that instantiates a copy of this module with structurally
valid dummy inputs (`engine_version = "7.1"` used only as a fixture). `TF_DATA_DIR` was
external to the repository, EC2 metadata was disabled, no backend was configured, and **no
`aws` command, AWS API, or metadata service was contacted**. Provider-registry access to
fetch the exact lockfile-selected provider is not AWS account access. No
`plan`/`apply`/`refresh`/`destroy`/`import`/`state` ran. The root `.terraform.lock.hcl` was
**not** modified and no `.terraform`, state, or plan artifact entered the repository.
**GitHub CI does not independently validate HCL** (its five jobs are application/integration
checks); HCL correctness rests on this harness, static assertions, and independent review.

## 9. Scope boundaries (this tranche)
Implemented, but **uncomposed** (not in root `main.tf`), **unprovisioned**, and
**inactive**. No AWS access, no live Redis, no root-composition change, no application
activation (`cache_backend`/`queue_backend` unchanged), no IAM/ECS integration, no ingress
rule. The root `infra/aws/README.md` inventory refresh (7→8 implemented, 4→5 uncomposed,
5→4 stubs) was **completed by the follow-up truthfulness pass** (PR #112, squash commit
`5f32d4b6a2ae` on `main` — its subject records the PR number); the root inventory now
records 8/3/5/4. INFRA-4 remains incomplete; INFRA-5 remains unstarted.
