# Module: `data_cache` (documentation-only stub)

## 1. Purpose
Private managed Redis for the staging cache, queue, and notify-channel
coordination paths.

## 2. Planned AWS scope
ElastiCache Redis node/replication group, cache subnet group, parameter group,
encryption settings.

## 3. Out of scope
PostgreSQL (`data_sql`), secret storage (`secrets`), compute (`ecs`).

## 4. Planned upstream dependencies
`network` (private subnets only). This module **creates and owns the Redis security
group** and **outputs its id**; it does **not** consume a data-SG input and consumes **no**
`ecs` output (one-way `data_cache -> ecs`). The task↔Redis **6379** cross-SG rules are owned
by `ecs` and cover the **API and worker** task SGs only — the **migration** task is pinned
with non-Redis backends and has **no** Redis access (§26.3). `REDIS_URL` is composed
out-of-band and injected via Secrets Manager at runtime, never committed and never read into
OpenTofu state.

## 5. Planned inputs (names only, no values)
`cache_subnet_group_name`, `private_subnet_ids`, `node_type`, `engine_version`,
`name_prefix`. No `data_security_group_id` input (this module creates that SG) and no
`tags` input (provider `default_tags`).

## 6. Planned non-sensitive outputs (names only)
`redis_primary_endpoint` (reference composed into `REDIS_URL` out-of-band, never read into
state), `cache_subnet_group_id`, `redis_security_group_id` (consumed by `ecs` for the
task↔Redis rules).

## 7. Security boundaries
Private subnets only; no public IP. Connection string supplied via Secrets
Manager (`REDIS_URL`), never committed. `cache.t4g.micro` staging sizing.

## 8. Staging-only assumptions
Single node, staging sizing; enabled only when the staging profile selects a
Redis-backed cache/queue path.

## 9. Status
No executable HCL yet. No resources created. Implementation and validation are
deferred.

## 10. Owning tranche
Real resource bodies belong to the later, separately authorized INFRA-4 module
implementation tranche.
