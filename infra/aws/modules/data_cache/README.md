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
`network` (private subnets, data security group).

## 5. Planned inputs (names only, no values)
`cache_subnet_group_name`, `private_subnet_ids`, `data_security_group_id`,
`node_type`, `engine_version`, `name_prefix`, `tags`.

## 6. Planned non-sensitive outputs (names only)
`redis_primary_endpoint` (reference, consumed via secret at runtime),
`cache_subnet_group_id`.

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
