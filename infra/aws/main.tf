# main.tf — composition root (INFRA-4)
#
# Wires ALL TWELVE reusable modules under `infra/aws/modules/`. Every module body
# is implemented and offline-validated; this file composes them per the locked
# acyclic producer -> consumer graph (aws-staging-iac-plan.md §26.12/§26.15).
# No `resource`, `data`, `import`, or `moved` block is declared at the root; all
# resources live inside the modules. Composition is CONFIGURATION ONLY — nothing
# is provisioned, and `plan`/`apply` remain deferred to INFRA-9 under fresh
# authorization with the pre-live requirements (§25/§26.14) resolved first.
#
# Locked graph (§26.12; arrow points at the consumer):
#   network -> alb (vpc_id, public_subnet_ids)
#   network -> data_sql, data_cache, ecs (private subnets / vpc)
#   edge     : independent (root vars only; no network edge)
#   secrets -> iam (secret_arns, kms_key_arn) ; secrets -> ecs (secret_arns)
#   registry -> iam (repository_arns) ; registry -> ecs (repository_urls)
#   storage -> iam (bucket_arn)   [one-way; storage never consumes iam]
#   data_sql -> ecs (rds_security_group_id) ; data_cache -> ecs (redis_security_group_id)
#   iam -> ecs (execution + api/worker/migration task-role ARNs)
#   alb -> ecs (alb_security_group_id, api_target_group_arn)
#   ecs -> observability (log groups + service names)   [observability is a sink]
#   cost     : independent (budgets)
#
# The eight-tag common set is applied to every taggable resource automatically by
# the provider's default_tags (providers.tf), so it is NOT passed into any module.

# §26.11 ordinary (non-secret) environment, composed at the root: only values
# that must differ from safe application defaults, plus the three capability
# flags EXPLICITLY "false" (dark posture is stated, never implied). Secrets
# never appear here (the ecs module's denylist validation also rejects them).
# VECTOR_BACKEND is deliberately omitted: the safe application default
# (bruteforce) applies until the deferred pgvector bootstrap is separately
# authorized. The migration workload is pinned to non-Redis backends (§26.3).
locals {
  workload_env_common = {
    ENVIRONMENT                  = "staging"
    APP_MODE                     = "full"
    LLM_PROVIDER                 = var.llm_provider
    STORAGE_BACKEND              = "s3"
    S3_BUCKET                    = module.storage.bucket_name
    S3_REGION                    = var.aws_region
    OPPORTUNITY_FEEDBACK_ENABLED = "false"
    SCOUT_SCHEDULING_ENABLED     = "false"
    CONNECTOR_RSS_ENABLED        = "false"
  }
  workload_env_redis = merge(local.workload_env_common, {
    QUEUE_BACKEND = "redis"
    CACHE_BACKEND = "redis"
  })
  # API-only (§25 rate-limiting-behind-proxy resolution): uvicorn trusts proxy
  # headers exclusively from in-VPC peers, so `request.client` becomes the
  # rightmost UNTRUSTED X-Forwarded-For entry — the ALB-appended real client
  # (xff_header_processing_mode = "append"), immune to client-prepended spoofing.
  # SAFETY INVARIANT: this trust is sound only while §26.3 keeps the API task
  # SG's sole ingress the ALB SG on TCP 8000. NEVER widen this to "*": uvicorn
  # degenerates to the leftmost (client-forgeable) entry when every host is
  # trusted. The worker/migration workloads serve no HTTP and never set it.
  workload_env_api = merge(local.workload_env_redis, {
    FORWARDED_ALLOW_IPS = var.vpc_cidr
  })
  workload_env_migration = merge(local.workload_env_common, {
    QUEUE_BACKEND = "inprocess"
    CACHE_BACKEND = "memory"
  })
}

module "network" {
  source = "./modules/network"

  name_prefix        = local.name_prefix
  vpc_cidr           = var.vpc_cidr
  availability_zones = var.availability_zones
  subnet_newbits     = var.subnet_newbits
  enable_nat_gateway = var.enable_nat_gateway
}

# Web/SPA edge (private S3 SPA origin + CloudFront/OAC + web DNS aliases). The ACM
# certificate and Route 53 hosted zone are CONSUMED by value (§23), never created.
module "edge" {
  source = "./modules/edge"

  name_prefix         = local.name_prefix
  web_fqdn            = var.web_fqdn
  hosted_zone_id      = var.hosted_zone_id
  acm_certificate_arn = var.acm_certificate_arn
  price_class         = var.price_class
}

# Internet-facing HTTPS ALB for the private API service. Owns only the ALB SG and
# target group; the API task SG and both ALB<->API cross-SG rules are ecs-owned
# (§26.2/§26.13), so no ECS input is required here (one-way `alb -> ecs`).
module "alb" {
  source = "./modules/alb"

  name_prefix         = local.name_prefix
  vpc_id              = module.network.vpc_id
  public_subnet_ids   = module.network.public_subnet_ids
  api_certificate_arn = var.api_certificate_arn
}

# Four EMPTY Secrets Manager containers + one customer-managed KMS key/alias.
# No secret value exists or is populated here (§26.6; population is INFRA-6).
module "secrets" {
  source = "./modules/secrets"

  name_prefix = local.name_prefix
}

# Two private ECR repositories (api, worker) + lifecycle policies (§26.5).
# No image is built or pushed by composition (build/push is INFRA-5).
module "registry" {
  source = "./modules/registry"

  name_prefix = local.name_prefix
}

# One private application S3 bucket. The caller-supplied globally unique name
# arrives via a git-ignored *.tfvars; no real bucket name is committed.
module "storage" {
  source = "./modules/storage"

  bucket_name = var.app_bucket_name
}

# Private RDS PostgreSQL (rule-free SG; ecs owns the 5432 rules, §26.3). The
# master credential is RDS-managed (§26.6) — no password enters HCL or state.
# skip_final_snapshot stays false; the deterministic final-snapshot identifier
# satisfies the module precondition without committing a real identifier.
module "data_sql" {
  source = "./modules/data_sql"

  name_prefix               = local.name_prefix
  vpc_id                    = module.network.vpc_id
  private_subnet_ids        = module.network.private_subnet_ids
  engine_version            = var.db_engine_version
  database_name             = var.db_name
  master_username           = var.db_master_username
  final_snapshot_identifier = "${local.name_prefix}-postgres-final"
}

# Private ElastiCache Redis (rule-free SG; ecs owns the 6379 rules, §26.3;
# TLS required, no auth token — Option A, §26.6).
module "data_cache" {
  source = "./modules/data_cache"

  name_prefix        = local.name_prefix
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids
  engine_version     = var.redis_engine_version
}

# One shared execution role + three application task roles (§26.8). Consumes
# only producer outputs; scopes its Logs policy by deterministic name prefix,
# never by ECS outputs (the §26.8 cycle break).
module "iam" {
  source = "./modules/iam"

  name_prefix     = local.name_prefix
  secret_arns     = module.secrets.secret_arns
  kms_key_arn     = module.secrets.kms_key_arn
  bucket_arn      = module.storage.bucket_arn
  repository_arns = module.registry.repository_arns
}

# ECS/Fargate compute plane (§26.2-§26.15): cluster, 3 log groups, 3 task SGs +
# every task-side cross-SG rule, 3 digest-pinned task definitions, 2 services.
# Image digests arrive via git-ignored *.tfvars (no digest exists yet; build/push
# is INFRA-5). Migration is a task definition only — never a service.
module "ecs" {
  source = "./modules/ecs"

  name_prefix             = local.name_prefix
  vpc_id                  = module.network.vpc_id
  private_subnet_ids      = module.network.private_subnet_ids
  alb_security_group_id   = module.alb.alb_security_group_id
  api_target_group_arn    = module.alb.api_target_group_arn
  rds_security_group_id   = module.data_sql.rds_security_group_id
  redis_security_group_id = module.data_cache.redis_security_group_id
  repository_urls         = module.registry.repository_urls
  api_image_digest        = var.api_image_digest
  worker_image_digest     = var.worker_image_digest
  execution_role_arn      = module.iam.execution_role_arn
  api_task_role_arn       = module.iam.api_task_role_arn
  worker_task_role_arn    = module.iam.worker_task_role_arn
  migration_task_role_arn = module.iam.migration_task_role_arn
  secret_arns             = module.secrets.secret_arns
  api_environment         = local.workload_env_api
  worker_environment      = local.workload_env_redis
  migration_environment   = local.workload_env_migration
}

# Metric filters, caller-thresholded alarms, and the CloudTrail audit trail
# (§26.9). Consumes the ecs outputs; observability is a graph sink. Thresholds
# arrive via a git-ignored *.tfvars (the plan documents categories, not values).
module "observability" {
  source = "./modules/observability"

  name_prefix         = local.name_prefix
  log_group_names     = module.ecs.log_group_names
  log_group_arns      = module.ecs.log_group_arns
  api_service_name    = module.ecs.api_service_name
  worker_service_name = module.ecs.worker_service_name
  alarm_thresholds    = var.alarm_thresholds
  sns_topic_arn       = var.sns_topic_arn
}

# One monthly AWS Budget with fixed 50/75/90/100% ACTUAL notifications (§15).
# Observational only; the $200 ADR-§M ceiling is enforced statically at the
# input boundary. Independent module (§26.12).
module "cost" {
  source = "./modules/cost"

  name_prefix          = local.name_prefix
  monthly_budget_limit = var.monthly_budget_limit
  notification_target  = var.budget_notification_email
}
