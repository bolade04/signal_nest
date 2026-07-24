# outputs.tf — non-sensitive observability references
#
# Exactly the planned interface pinned by this module's README §6: `alarm_arns`
# and `trail_arn`. References only — no secret value, credential, account
# identity, or log content is exposed. Nothing downstream consumes these in the
# locked graph (observability is a sink); they exist for operator use and future
# root composition.

output "alarm_arns" {
  description = "Map of alarm key -> CloudWatch alarm ARN for every alarm owned by this module (log-error x3, service CPU/memory x4, RDS saturation x3, Redis saturation x2)."
  value = merge(
    { for k, a in aws_cloudwatch_metric_alarm.log_errors : "${k}-log-errors" => a.arn },
    { for k, a in aws_cloudwatch_metric_alarm.service_cpu_high : "${k}-cpu-high" => a.arn },
    { for k, a in aws_cloudwatch_metric_alarm.service_memory_high : "${k}-memory-high" => a.arn },
    {
      "rds-cpu-high"      = aws_cloudwatch_metric_alarm.rds_cpu_high.arn
      "rds-storage-low"   = aws_cloudwatch_metric_alarm.rds_storage_low.arn
      "rds-memory-low"    = aws_cloudwatch_metric_alarm.rds_memory_low.arn
      "redis-cpu-high"    = aws_cloudwatch_metric_alarm.redis_cpu_high.arn
      "redis-memory-high" = aws_cloudwatch_metric_alarm.redis_memory_high.arn
    },
  )
}

output "trail_arn" {
  description = "ARN of the single-region management-events CloudTrail audit trail."
  value       = aws_cloudtrail.audit.arn
}
