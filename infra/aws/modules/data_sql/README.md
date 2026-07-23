# Module: `data_sql` (implemented — provider-schema-validated, not root-composed)

## 1. Purpose and ownership
Owns a **private Amazon RDS for PostgreSQL** DB instance for SIGNALNEST_STAGING (the
application's SQL + vector store), its **DB subnet group**, its **RDS security group**
(created rule-free), and a **TLS-enforcing DB parameter group**. This module is
**implemented and provider-schema-validated only**: it has been authored and validated
with OpenTofu against the locked AWS provider schema, but it is **not** wired into the
root composition (`infra/aws/main.tf`), **nothing is provisioned or deployed**, no live
database exists, and **no AWS account has been contacted** (see §7).

## 2. Resources declared (exactly four; no data source)
| Resource | Purpose |
| --- | --- |
| `aws_db_subnet_group` | Places the instance in the supplied private subnets |
| `aws_security_group` | RDS SG, **owned here, created with zero rules** |
| `aws_db_parameter_group` | Family `postgres<major>`; sets **only** `rds.force_ssl = "1"` |
| `aws_db_instance` | Private, encrypted RDS PostgreSQL instance |

No password generator, Secrets Manager secret/version, IAM resource, KMS key,
CloudWatch resource, security-group-rule resource, RDS Proxy, replica, cluster,
snapshot resource, or application/migration resource is declared.

## 3. Inputs
`name_prefix` (required), `vpc_id` (required), `private_subnet_ids` (required
`list(string)`, ≥2 distinct), `db_subnet_group_name` (nullable — preserved from the
prior stub; when null, derived as `<name_prefix>-pg`), `engine_version` (**required**,
no default — a PostgreSQL major like `16` or major.minor like `16.3`; the parameter
family is derived from its major), `instance_class` (default `db.t4g.micro`),
`allocated_storage_gb` (default `20`), `max_allocated_storage_gb` (nullable, default
`null`), `database_name` (**required**, no invented default), `master_username`
(**required**, no invented default; not sensitive), `storage_kms_key_id` (nullable,
default `null`), `master_user_secret_kms_key_id` (nullable, default `null`), `multi_az`
(default `false`), `backup_retention_period` (default `7`, range `1..35`),
`deletion_protection` (default `true`), `skip_final_snapshot` (default `false`),
`final_snapshot_identifier` (nullable, default `null`).

There is **no** `password`, `port`, `tags`, ingress, CIDR, log-export, monitoring,
Performance-Insights, apply-immediately, or database-URL input.

## 4. Outputs
`db_instance_identifier`, `db_instance_arn`, `db_address` (host only), `db_endpoint`
(host:port; preserved from the stub), `db_port`, `database_name`, `master_username`,
`db_subnet_group_name`, `rds_security_group_id` (consumed by future `ecs`),
`master_user_secret_arn` (ARN metadata only). **No** password, complete `DATABASE_URL`,
credentialed URL, or secret value is output; none is marked sensitive because none is a
secret value.

## 5. Engine version and parameter family
`engine_version` is required with **no default** (no invented version is committed). The
DB parameter-group family is derived from its major component as `postgres<major>`, and
`allow_major_version_upgrade = false`, so the engine version and parameter family
**cannot drift apart**.

## 6. pgvector boundary (deferred database bootstrap)
pgvector is **not** added to `shared_preload_libraries` (that is not how the `vector`
extension is enabled). **No committed Alembic migration runs `CREATE EXTENSION ... vector`**
(verified read-only across `apps/api/alembic/versions/`), and this module does **not**
activate pgvector. Enabling the extension (`CREATE EXTENSION IF NOT EXISTS vector`)
remains an **unresolved deployment/database-bootstrap dependency** for `full` mode
(`app.core.config.vector_backend = "pgvector"`), handled by a separately authorized step
— not by this HCL tranche.

## 7. Security, credential, and TLS boundaries
- **Not publicly accessible** (`publicly_accessible = false`); private subnets only.
- **Encrypted at rest** (`storage_encrypted = true`, gp3). TLS in transit is **enforced**
  by the parameter group (`rds.force_ssl = "1"`).
- **Master password is RDS-managed** (`manage_master_user_password = true`): RDS
  generates and stores it in an RDS-owned Secrets Manager secret. There is **no**
  password variable/local/`random_password`/secret-version/password output, and the
  value **never enters OpenTofu state** (only the RDS-managed secret ARN/metadata may).
- **Master ≠ application credential.** The RDS-managed master secret is an
  administrative/bootstrap credential. This module does **not** populate the
  application-facing `DATABASE_URL` secret, grant ECS/IAM access, read any secret value,
  or create least-privilege application/migration database roles — all deferred to a
  separately authorized bootstrap/deployment workflow.
- **Future `DATABASE_URL` construction** must use the approved driver and explicit TLS,
  at minimum `postgresql+psycopg://…?sslmode=require`; credential percent-encoding and
  stronger certificate verification are finalized in that separate integration. This
  module constructs and outputs **no** `DATABASE_URL`.

## 8. Networking and security-group ownership
Consumes `vpc_id` and `private_subnet_ids` (from `network`). Owns the DB subnet group
and the RDS SG and **outputs the SG id**. The SG is created with **zero rules** — no
inline ingress/egress, no `aws_security_group_rule`, no
`aws_vpc_security_group_(in|e)gress_rule`, no CIDR input, no `0.0.0.0/0`/`::/0`. The
future **`ecs`** module owns the three standalone TCP 5432 ingress rules (from the API,
worker, and migration task SGs) — one-way `data_sql -> ecs`, acyclic. **No ECS
dependency is introduced here.** Isolated HCL validation cannot prove the subnets are
private, span ≥2 AZs, or exist in the selected account/Region — proven only at later
root composition and AWS-backed deployment.

## 9. KMS boundaries
`storage_kms_key_id` and `master_user_secret_kms_key_id` both default to `null`. When
null, RDS storage uses the AWS-managed RDS key and the RDS-managed master secret uses
the AWS-managed Secrets Manager key. The **secrets module CMK is not automatically
reused.** A caller-supplied key requires a compatible Region, key state, key policy, and
RDS/Secrets Manager grants that this isolated module **cannot** validate.

## 10. Backups, deletion, snapshots, autoscaling
`backup_retention_period` defaults to `7` (range `1..35`; backups cannot be disabled).
`deletion_protection` defaults to `true`. `skip_final_snapshot` defaults to `false`, and
a resource **precondition** requires a non-null `final_snapshot_identifier` whenever it
is false (no timestamp generated in HCL, no fixed reusable name that could collide after
destroy/recreate). A second precondition requires `max_allocated_storage_gb`, when set,
to be ≥110% of `allocated_storage_gb`. Snapshot restoration is out of scope.

## 11. Deferred observability
`enabled_cloudwatch_logs_exports`, Performance Insights, enhanced monitoring, monitoring
IAM roles/intervals, custom CloudWatch log groups, and alarms are **omitted entirely**
(no partial Boolean/interval knobs). RDS observability is a separately authorized
tranche.

## 12. Validation performed
**AWS-free provider-schema validation.** `tofu fmt`/`fmt -check`, and `tofu init
-backend=false -lockfile=readonly` + `tofu validate` run through a temporary,
**repository-external** harness that instantiates a copy of this module with structurally
valid dummy inputs (`engine_version = "16"` used only as a test fixture, not an
engine-version decision). `TF_DATA_DIR` was external to the repository, EC2 metadata was
disabled, no backend was configured, and **no `aws` command, AWS API, or metadata service
was contacted**. Provider-registry access to fetch the exact lockfile-selected provider
is not AWS account access. No `plan`/`apply`/`refresh`/`test`/`destroy`/`import`/`state`
ran. The root `.terraform.lock.hcl` was **not** modified and no `.terraform`, state, or
plan artifact entered the repository. **GitHub CI does not independently validate HCL**
(its five jobs are application/integration checks); HCL correctness rests on this harness,
static assertions, and independent review.

## 13. Scope boundaries (this tranche)
Implemented, but **uncomposed** (not in root `main.tf`), **unprovisioned**, and
**inactive**. No AWS access, no live database, no root-composition change, no application
activation, no IAM/ECS integration, no migration created/executed, no pgvector
activation. The root `infra/aws/README.md` inventory refresh is a **separate follow-up
truthfulness pass** after this module merges (repository precedent). INFRA-4 remains
incomplete; INFRA-5 remains unstarted.
