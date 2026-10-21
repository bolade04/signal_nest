# outputs.tf — non-sensitive observability references
#
# Exactly the planned interface pinned by this module's README §6: `alarm_arns`
# and `trail_arn`. References only — no secret value, credential, account
# identity, or log content is exposed. Nothing downstream consumes these in the
# locked graph (observability is a sink); they exist for operator use and future
# root composition.

output "alarm_arns" {
  description = "Map of alarm key -> CloudWatch alarm ARN for every alarm owned by this module (log-error x3, service CPU/memory x4, RDS saturation x3, Redis saturation x2, capability gate/override x3 — the INFRA-7 fixed-threshold canary signals; plus the P6-INF-5 API 5xx-ratio and p95-latency alarms when app_metrics is set and the three P6-INF-6 ALB alarms while the window is open)."
  value = merge(
    { for k, a in aws_cloudwatch_metric_alarm.alb : k => a.arn },
    { for a in aws_cloudwatch_metric_alarm.api_5xx_ratio : "api-5xx-ratio" => a.arn },
    { for a in aws_cloudwatch_metric_alarm.api_p95_latency : "api-p95-latency" => a.arn },
    { for k, a in aws_cloudwatch_metric_alarm.log_errors : "${k}-log-errors" => a.arn },
    { for k, a in aws_cloudwatch_metric_alarm.service_cpu_high : "${k}-cpu-high" => a.arn },
    { for k, a in aws_cloudwatch_metric_alarm.service_memory_high : "${k}-memory-high" => a.arn },
    { for k, a in aws_cloudwatch_metric_alarm.capability : "capability-${k}" => a.arn },
    {
      "rds-cpu-high"      = aws_cloudwatch_metric_alarm.rds_cpu_high.arn
      "rds-storage-low"   = aws_cloudwatch_metric_alarm.rds_storage_low.arn
      "rds-memory-low"    = aws_cloudwatch_metric_alarm.rds_memory_low.arn
      "redis-cpu-high"    = aws_cloudwatch_metric_alarm.redis_cpu_high.arn
      "redis-memory-high" = aws_cloudwatch_metric_alarm.redis_memory_high.arn
    },
  )
}

output "canary_dashboard_name" {
  description = "Name of the INFRA-7 canary observability CloudWatch dashboard (operator read surface; nothing downstream consumes it)."
  value       = aws_cloudwatch_dashboard.canary.dashboard_name
}

output "trail_arn" {
  description = "ARN of the single-region management-events CloudTrail audit trail."
  value       = aws_cloudtrail.audit.arn
}

output "alarm_topic_arn" {
  description = "ARN of the module-owned alarm notification topic (P6-INF-4) every alarm notifies. The e-mail subscription on it stays PendingConfirmation until the recipient confirms."
  value       = aws_sns_topic.alarms.arn
}

output "alarm_subscription_count" {
  description = "Number of human (e-mail) subscriptions the module manages on the alarm topic: 1 when alarm_notification_email is set, else 0. Configuration only — whether AWS shows it confirmed is a live read."
  value       = length(aws_sns_topic_subscription.alarm_email)
}
