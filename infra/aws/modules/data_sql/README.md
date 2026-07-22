# Module: `data_sql` (documentation-only stub)

## 1. Purpose
Private managed PostgreSQL (with pgvector) for the staging application and vector
storage.

## 2. Planned AWS scope
RDS PostgreSQL instance (pgvector-capable), DB subnet group, DB parameter group,
automated backups, encryption at rest (KMS) and in transit.

## 3. Out of scope
Application migration execution (owned by the `ecs` migration run-task), Redis
(`data_cache`), secret storage (`secrets`).

## 4. Planned upstream dependencies
`network` (private subnets only). This module **creates and owns the PostgreSQL/RDS
security group** and **outputs its id**; it does **not** consume a data-SG input and
consumes **no** `ecs` output (one-way `data_sql -> ecs`). The task↔PostgreSQL **5432**
cross-SG rules are owned by `ecs` (`docs/operations/aws-staging-iac-plan.md` §26.2-26.3).
`DATABASE_URL` is composed out-of-band and injected via Secrets Manager at runtime, never
committed and never read into OpenTofu state.

## 5. Planned inputs (names only, no values)
`db_subnet_group_name`, `private_subnet_ids`, `instance_class`, `allocated_storage_gb`,
`engine_version`, `kms_key_id`, `name_prefix`. No `data_security_group_id` input (this
module creates that SG) and no `tags` input (provider `default_tags`).

## 6. Planned non-sensitive outputs (names only)
`db_instance_identifier`, `db_endpoint` (reference composed into `DATABASE_URL`
out-of-band, never read into state), `db_subnet_group_id`, `rds_security_group_id`
(consumed by `ecs` for the task↔DB rules).

## 7. Security boundaries
Not publicly accessible; private subnets only. Encrypted at rest (KMS) and in
transit. Credentials are supplied via Secrets Manager (`DATABASE_URL`), never
committed. `db.t4g.micro`, single-AZ, 20 GB gp3 (staging sizing §M).

## 8. Staging-only assumptions
Single-AZ staging instance; production-identical security controls.

## 9. Status
No executable HCL yet. No resources created. Implementation and validation are
deferred.

## 10. Owning tranche
Real resource bodies belong to the later, separately authorized INFRA-4 module
implementation tranche.
