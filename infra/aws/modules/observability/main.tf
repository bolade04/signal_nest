# main.tf — staging observability plane (INFRA-4 observability module)
#
# Owns METRIC FILTERS, ALARMS, and the CLOUDTRAIL AUDIT TRAIL locked by
# aws-staging-iac-plan.md §6/§14 and §26.9. OWNERSHIP RULING (evidence-settled):
# the three workload log groups are created and owned by the MERGED `ecs` module
# (aws_cloudwatch_log_group.workload, §26.9) — this module creates NO log group
# and only attaches metric filters to the log-group NAMES supplied as inputs.
# Dependency direction stays one-way and acyclic: `ecs -> observability`
# (log_group_names/arns + api/worker_service_name), and observability is a sink —
# nothing here is consumed by ecs/iam/any module (`iam` scopes logs by
# deterministic prefix, never by these resources, §26.8).
#
# ALARM DIMENSIONS use the repository's deterministic-name pattern (§26.8
# precedent) instead of new producer edges: ClusterName "<name_prefix>-cluster"
# (ecs default), DBInstanceIdentifier "<name_prefix>-postgres" (data_sql local
# db_instance_id), CacheClusterId "<name_prefix>-redis-001" (data_cache
# replication_group_id + single-node "-001" member naming). §26.12's graph gains
# no new edge. ALB-dimension alarms (5xx/unhealthy-host) are DEFERRED: the alb
# module exposes no arn_suffix outputs, and changing alb is outside this tranche.
#
# ERROR-RATE evidence: the application emits one JSON object per record with
# `"severity": record.levelname` (apps/api/app/core/logging.py:65), so the
# error filter pattern is `{ $.severity = "ERROR" }` — evidence-backed, not
# invented. Thresholds are caller-supplied via `alarm_thresholds` (no invented
# values). MISSING-DATA is explicit per alarm: `breaching` (fail-closed) for
# service-health and DB/Redis saturation — a silent metric gap must not look
# healthy; `notBreaching` for the log-error alarms — the filter emits datapoints
# only on matches, so "no datapoint" genuinely means "no errors" (health
# coverage comes from the breaching service alarms).
#
# CLOUDTRAIL (§6 assigns CloudTrail to observability; §14 audit): one
# single-region management-events trail with log-file validation, delivered to a
# DEDICATED private audit bucket owned here (bucket_prefix per the edge-module
# precedent — no global bucket name committed; SSE-S3, versioning, all four
# public-access blocks, BucketOwnerEnforced, TLS-only + CloudTrail-service-only
# bucket policy). This is an AUDIT bucket, not an application bucket (`storage`
# owns application buckets). Alarm/OK actions use the optional caller-supplied
# `sns_topic_arn` only — no SNS topic, dashboard, composite alarm, or anomaly
# detector is created (none is documented). Nothing here is provisioned,
# deployed, or live; offline-validated HCL only.
#
# No `provider "aws"` block; `versions.tf` declares only the provider SOURCE per
# the sibling convention. Tagging is the root provider's `default_tags`; this
# module adds only per-resource `Name` tags.

locals {
  cluster_name     = "${var.name_prefix}-cluster"
  db_instance_id   = "${var.name_prefix}-postgres"
  cache_cluster_id = "${var.name_prefix}-redis-001"

  metric_namespace = "${var.name_prefix}/logs"
  workloads        = toset(["api", "worker", "migration"])

  alarm_actions = var.sns_topic_arn == null ? [] : [var.sns_topic_arn]

  # Shared cadence: 5-minute periods; sustained (3-datapoint) breaches for
  # utilization/saturation, single-period for error counts.
  period_seconds = 300
  sustained_eval = 3
  service_dimensions = {
    api    = { ClusterName = local.cluster_name, ServiceName = var.api_service_name }
    worker = { ClusterName = local.cluster_name, ServiceName = var.worker_service_name }
  }
}

# --- Error metric filters over the ecs-owned log groups (§26.9: consume, never create)
resource "aws_cloudwatch_log_metric_filter" "errors" {
  for_each = local.workloads

  name           = "${var.name_prefix}-${each.key}-errors"
  log_group_name = var.log_group_names[each.key]
  # Evidence-backed pattern: the JSON formatter writes "severity": "<LEVELNAME>".
  pattern = "{ $.severity = \"ERROR\" }"

  metric_transformation {
    name          = "${each.key}_error_count"
    namespace     = local.metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

# --- Log-error alarms (one per workload) -------------------------------------------
resource "aws_cloudwatch_metric_alarm" "log_errors" {
  for_each = local.workloads

  alarm_name          = "${var.name_prefix}-${each.key}-log-errors"
  alarm_description   = "ERROR-severity log lines in the ${each.key} workload reached the caller-supplied threshold within one 5-minute period. Source: metric filter over the ecs-owned ${var.log_group_names[each.key]} group."
  namespace           = local.metric_namespace
  metric_name         = "${each.key}_error_count"
  statistic           = "Sum"
  period              = local.period_seconds
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = var.alarm_thresholds.log_error_count_per_period
  # Filters emit datapoints only on matches: no datapoint means no errors.
  treat_missing_data = "notBreaching"
  alarm_actions      = local.alarm_actions
  ok_actions         = local.alarm_actions

  tags = {
    Name = "${var.name_prefix}-${each.key}-log-errors"
  }
}

# --- ECS service-health alarms (CPU + memory, api and worker) ----------------------
resource "aws_cloudwatch_metric_alarm" "service_cpu_high" {
  for_each = local.service_dimensions

  alarm_name          = "${var.name_prefix}-${each.key}-cpu-high"
  alarm_description   = "Sustained high CPU on the ${each.key} ECS service (3 consecutive 5-minute averages). Missing data is treated as breaching (fail-closed): a silent metric gap must not look healthy."
  namespace           = "AWS/ECS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = local.period_seconds
  evaluation_periods  = local.sustained_eval
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = var.alarm_thresholds.ecs_cpu_high_percent
  dimensions          = each.value
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  tags = {
    Name = "${var.name_prefix}-${each.key}-cpu-high"
  }
}

resource "aws_cloudwatch_metric_alarm" "service_memory_high" {
  for_each = local.service_dimensions

  alarm_name          = "${var.name_prefix}-${each.key}-memory-high"
  alarm_description   = "Sustained high memory on the ${each.key} ECS service (3 consecutive 5-minute averages). Missing data is treated as breaching (fail-closed)."
  namespace           = "AWS/ECS"
  metric_name         = "MemoryUtilization"
  statistic           = "Average"
  period              = local.period_seconds
  evaluation_periods  = local.sustained_eval
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = var.alarm_thresholds.ecs_memory_high_percent
  dimensions          = each.value
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  tags = {
    Name = "${var.name_prefix}-${each.key}-memory-high"
  }
}

# --- RDS saturation alarms (deterministic DBInstanceIdentifier) --------------------
resource "aws_cloudwatch_metric_alarm" "rds_cpu_high" {
  alarm_name          = "${var.name_prefix}-rds-cpu-high"
  alarm_description   = "Sustained high CPU on the staging PostgreSQL instance ${local.db_instance_id}. Missing data is treated as breaching (fail-closed)."
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = local.period_seconds
  evaluation_periods  = local.sustained_eval
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = var.alarm_thresholds.rds_cpu_high_percent
  dimensions          = { DBInstanceIdentifier = local.db_instance_id }
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  tags = {
    Name = "${var.name_prefix}-rds-cpu-high"
  }
}

resource "aws_cloudwatch_metric_alarm" "rds_storage_low" {
  alarm_name          = "${var.name_prefix}-rds-storage-low"
  alarm_description   = "Free storage on the staging PostgreSQL instance fell to or below the caller-supplied floor (bytes). Missing data is treated as breaching (fail-closed)."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Average"
  period              = local.period_seconds
  evaluation_periods  = local.sustained_eval
  comparison_operator = "LessThanOrEqualToThreshold"
  threshold           = var.alarm_thresholds.rds_free_storage_low_bytes
  dimensions          = { DBInstanceIdentifier = local.db_instance_id }
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  tags = {
    Name = "${var.name_prefix}-rds-storage-low"
  }
}

resource "aws_cloudwatch_metric_alarm" "rds_memory_low" {
  alarm_name          = "${var.name_prefix}-rds-memory-low"
  alarm_description   = "Freeable memory on the staging PostgreSQL instance fell to or below the caller-supplied floor (bytes). Missing data is treated as breaching (fail-closed)."
  namespace           = "AWS/RDS"
  metric_name         = "FreeableMemory"
  statistic           = "Average"
  period              = local.period_seconds
  evaluation_periods  = local.sustained_eval
  comparison_operator = "LessThanOrEqualToThreshold"
  threshold           = var.alarm_thresholds.rds_freeable_memory_low_bytes
  dimensions          = { DBInstanceIdentifier = local.db_instance_id }
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  tags = {
    Name = "${var.name_prefix}-rds-memory-low"
  }
}

# --- Redis saturation alarms (deterministic CacheClusterId) ------------------------
resource "aws_cloudwatch_metric_alarm" "redis_cpu_high" {
  alarm_name          = "${var.name_prefix}-redis-cpu-high"
  alarm_description   = "Sustained high CPU on the staging Redis node ${local.cache_cluster_id}. Missing data is treated as breaching (fail-closed)."
  namespace           = "AWS/ElastiCache"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = local.period_seconds
  evaluation_periods  = local.sustained_eval
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = var.alarm_thresholds.redis_cpu_high_percent
  dimensions          = { CacheClusterId = local.cache_cluster_id }
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  tags = {
    Name = "${var.name_prefix}-redis-cpu-high"
  }
}

resource "aws_cloudwatch_metric_alarm" "redis_memory_high" {
  alarm_name          = "${var.name_prefix}-redis-memory-high"
  alarm_description   = "Redis database memory usage on ${local.cache_cluster_id} reached the caller-supplied percentage. Missing data is treated as breaching (fail-closed)."
  namespace           = "AWS/ElastiCache"
  metric_name         = "DatabaseMemoryUsagePercentage"
  statistic           = "Average"
  period              = local.period_seconds
  evaluation_periods  = local.sustained_eval
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = var.alarm_thresholds.redis_memory_high_percent
  dimensions          = { CacheClusterId = local.cache_cluster_id }
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  tags = {
    Name = "${var.name_prefix}-redis-memory-high"
  }
}

# --- Dedicated private CloudTrail audit bucket (§6/§14) ----------------------------
# AUDIT bucket owned by observability (storage owns APPLICATION buckets).
# bucket_prefix per the edge-module precedent — no global bucket name committed.
resource "aws_s3_bucket" "audit" {
  bucket_prefix = "${var.name_prefix}-audit-"
  force_destroy = false

  tags = {
    Name = "${var.name_prefix}-audit"
  }
}

resource "aws_s3_bucket_ownership_controls" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket = aws_s3_bucket.audit.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id

  versioning_configuration {
    status = "Enabled"
  }
}

# CloudTrail-service-only writes + TLS-only access. The service principal is
# scoped to this trail's ARN via aws:SourceArn (confused-deputy guard); no
# account id, region, or ARN literal is committed (data sources at plan time).
data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  trail_name = "${var.name_prefix}-audit"
  trail_arn  = "arn:${data.aws_partition.current.partition}:cloudtrail:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:trail/${local.trail_name}"
}

resource "aws_s3_bucket_policy" "audit" {
  bucket = aws_s3_bucket.audit.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "CloudTrailAclCheck"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.audit.arn
        Condition = {
          StringEquals = { "aws:SourceArn" = local.trail_arn }
        }
      },
      {
        Sid       = "CloudTrailWrite"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.audit.arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
        Condition = {
          StringEquals = {
            "s3:x-amz-acl"  = "bucket-owner-full-control"
            "aws:SourceArn" = local.trail_arn
          }
        }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.audit.arn,
          "${aws_s3_bucket.audit.arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.audit]
}

# --- CloudTrail management-events audit trail (§14) --------------------------------
# Single-region, management events only (control-plane + IAM/secret access),
# log-file validation enabled. Default SSE; a customer-managed KMS design remains
# separately authorized. No data events, no organization trail, no CloudWatch
# Logs delivery (log groups are ecs-owned; trail->logs wiring is not documented).
resource "aws_cloudtrail" "audit" {
  name                          = local.trail_name
  s3_bucket_name                = aws_s3_bucket.audit.id
  include_global_service_events = true
  is_multi_region_trail         = false
  enable_log_file_validation    = true

  tags = {
    Name = local.trail_name
  }

  depends_on = [aws_s3_bucket_policy.audit]
}
