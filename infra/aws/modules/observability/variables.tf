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
}

variable "sns_topic_arn" {
  description = "Optional pre-existing SNS topic ARN for alarm and OK actions. This module never creates a notification destination; null means alarms have no actions (state changes remain visible in CloudWatch)."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.sns_topic_arn == null || can(regex("^arn:[^:]+:sns:", var.sns_topic_arn))
    error_message = "sns_topic_arn must be null or an SNS topic ARN (arn:<partition>:sns:...)."
  }
}
