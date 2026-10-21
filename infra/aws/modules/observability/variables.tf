# variables.tf — typed inputs for the staging observability plane
#
# The input set is EXACTLY the planned interface pinned by this module's README
# and aws-staging-iac-plan.md §26.15: the ecs-owned log-group/service outputs,
# `alarm_thresholds`, `sns_topic_arn`, and `name_prefix`. Alarm thresholds are
# deliberately CALLER-SUPPLIED (no invented defaults — the plan documents alarm
# CATEGORIES, not values). DB/Redis/cluster alarm dimensions are derived from
# `name_prefix` via the repository's deterministic-name pattern (§26.8
# precedent), so no data_sql/data_cache/extra-ecs input or graph edge is
# introduced. No `tags` input (root provider `default_tags`). No secret,
# account id, region, or real ARN is committed.

variable "name_prefix" {
  description = "Deterministic, lowercase resource name prefix from the root (e.g. \"signalnest-staging\"). Builds alarm/filter/trail/bucket names AND the deterministic alarm dimensions: ECS ClusterName \"<name_prefix>-cluster\" (ecs module default), RDS DBInstanceIdentifier \"<name_prefix>-postgres\" (data_sql local), ElastiCache CacheClusterId \"<name_prefix>-redis-001\" (data_cache single-node replication group). Root composition must keep those deterministic names for the dimensions to match."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be lowercase alphanumeric and hyphens, 2-63 chars, no leading/trailing hyphen."
  }
}

variable "log_group_names" {
  description = "Map of workload (api|worker|migration) -> ecs-owned CloudWatch log-group name, from the ecs module output of the same name (§26.9 — ecs owns the groups; this module only attaches metric filters)."
  type        = map(string)

  validation {
    condition = alltrue([
      for k in ["api", "worker", "migration"] : contains(keys(var.log_group_names), k)
    ])
    error_message = "log_group_names must contain the api, worker, and migration keys (the three ecs-owned workload log groups)."
  }
}

variable "log_group_arns" {
  description = "Map of workload (api|worker|migration) -> ecs-owned CloudWatch log-group ARN, from the ecs module output of the same name. Accepted for interface completeness/monitoring reference; no log group is created or modified here."
  type        = map(string)

  validation {
    condition = alltrue([
      for k in ["api", "worker", "migration"] : contains(keys(var.log_group_arns), k)
    ])
    error_message = "log_group_arns must contain the api, worker, and migration keys."
  }
}

variable "api_service_name" {
  description = "Name of the API ECS service, from the ecs module output of the same name. ServiceName dimension of the API service-health alarms."
  type        = string
}

variable "worker_service_name" {
  description = "Name of the worker ECS service, from the ecs module output of the same name. ServiceName dimension of the worker service-health alarms."
  type        = string
}

variable "alarm_thresholds" {
  description = "Caller-supplied alarm thresholds (the plan documents alarm categories — service health, error rate, DB/Redis saturation — and leaves values to composition; nothing is invented here). Percent fields are 1-100; byte/count fields are positive."
  type = object({
    ecs_cpu_high_percent          = number
    ecs_memory_high_percent       = number
    log_error_count_per_period    = number
    rds_cpu_high_percent          = number
    rds_free_storage_low_bytes    = number
    rds_freeable_memory_low_bytes = number
    redis_cpu_high_percent        = number
    redis_memory_high_percent     = number
    # P6-INF-5 / P6-INF-6 (optional here so existing callers keep their shape; the staging
    # root requires all three). Required by a precondition on each alarm that uses them.
    api_5xx_percent          = optional(number)
    api_p95_latency_ms       = optional(number)
    alb_5xx_count_per_period = optional(number)
  })

  validation {
    condition = alltrue([
      for p in [
        var.alarm_thresholds.ecs_cpu_high_percent,
        var.alarm_thresholds.ecs_memory_high_percent,
        var.alarm_thresholds.rds_cpu_high_percent,
        var.alarm_thresholds.redis_cpu_high_percent,
        var.alarm_thresholds.redis_memory_high_percent,
      ] : p > 0 && p <= 100
    ])
    error_message = "Percent thresholds must be greater than 0 and at most 100."
  }

  validation {
    condition = alltrue([
      var.alarm_thresholds.log_error_count_per_period > 0,
      var.alarm_thresholds.rds_free_storage_low_bytes > 0,
      var.alarm_thresholds.rds_freeable_memory_low_bytes > 0,
    ])
    error_message = "log_error_count_per_period, rds_free_storage_low_bytes, and rds_freeable_memory_low_bytes must be positive."
  }

  validation {
    condition = alltrue([
      try(var.alarm_thresholds.api_5xx_percent == null || (var.alarm_thresholds.api_5xx_percent > 0 && var.alarm_thresholds.api_5xx_percent <= 100), false),
      try(var.alarm_thresholds.api_p95_latency_ms == null || var.alarm_thresholds.api_p95_latency_ms > 0, false),
      try(var.alarm_thresholds.alb_5xx_count_per_period == null || var.alarm_thresholds.alb_5xx_count_per_period >= 1, false),
    ])
    error_message = "api_5xx_percent must be greater than 0 and at most 100, api_p95_latency_ms positive, and alb_5xx_count_per_period at least 1 (each may be null only where its alarm is not created)."
  }
}

variable "sns_topic_arn" {
  description = "Optional ADDITIONAL pre-existing SNS topic ARN for alarm and OK actions. Every alarm always notifies the module-owned topic (alerting.tf, P6-INF-4); null adds nothing."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.sns_topic_arn == null || can(regex("^arn:[^:]+:sns:", var.sns_topic_arn))
    error_message = "sns_topic_arn must be null or an SNS topic ARN (arn:<partition>:sns:...)."
  }
}

# --- P6-INF-4 / -5 / -6 / -13 ------------------------------------------------------------

variable "alarm_notification_email" {
  description = "E-mail address subscribed to the alarm topic (the human destination, P6-INF-4). An OPERATOR VALUE supplied through a git-ignored *.tfvars; never committed. AWS holds the subscription as PendingConfirmation until the recipient confirms it (a live human act). Null = no subscription (the staging root requires an address)."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.alarm_notification_email == null || can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.alarm_notification_email))
    error_message = "alarm_notification_email must be null or a plausible email address (local@domain.tld)."
  }
}

variable "alb_alarms_enabled" {
  description = "Create the three ALB alarms (P6-INF-6). The root wires the alb module's `alb_enabled` output (its window input), which is known at plan time; the alarms then exist exactly while the windowed load balancer does."
  type        = bool
  default     = false
}

variable "alb_arn_suffix" {
  description = "LoadBalancer dimension value (the alb module's `alb_arn_suffix` output). Required while alb_alarms_enabled is true; null while the window is closed."
  type        = string
  default     = null
  nullable    = true
}

variable "api_target_group_arn_suffix" {
  description = "TargetGroup dimension value (the alb module's `api_target_group_arn_suffix` output). Required while alb_alarms_enabled is true; null while the window is closed."
  type        = string
  default     = null
  nullable    = true
}

variable "app_metrics" {
  description = "Where the API's CloudWatch EMF metrics land (P6-INF-5): the namespace and the `service`/`environment` dimension values the application stamps (METRICS_NAMESPACE, SERVICE_NAME, ENVIRONMENT of the API task). Null = no application-metric alarms."
  type = object({
    namespace   = string
    service     = string
    environment = string
  })
  default  = null
  nullable = true

  validation {
    condition     = var.app_metrics == null || try(can(regex("^[A-Za-z0-9._/#:-]{1,255}$", var.app_metrics.namespace)) && !startswith(var.app_metrics.namespace, "AWS/"), false)
    error_message = "app_metrics.namespace must be a custom CloudWatch namespace: 1-255 of letters, digits and . - _ / # :, not starting with AWS/ (the application applies the same rule)."
  }
}

variable "audit_log_retention_days" {
  description = "Days the audit bucket keeps a delivered CloudTrail log or digest object before it expires; a deleted or overwritten (noncurrent) version is kept for the same number of days (worst-case lifetime: twice this). The operator's retention decision (coupled to the privacy lane's retention schedule); null = no lifecycle rule. The staging root requires a value."
  type        = number
  default     = null
  nullable    = true

  validation {
    condition     = var.audit_log_retention_days == null || (try(var.audit_log_retention_days == floor(var.audit_log_retention_days), false) && try(var.audit_log_retention_days >= 1 && var.audit_log_retention_days <= 3650, false))
    error_message = "audit_log_retention_days must be null or a whole number of days between 1 and 3650."
  }
}
