# outputs.tf — non-sensitive compute-plane references for downstream modules
#
# Exactly the planned interface pinned by the ecs README §6 and
# aws-staging-iac-plan.md §26.15: `observability` consumes the log-group and
# service-name outputs for metric filters/alarms (one-way `ecs -> observability`);
# the future run-task/deploy tranches consume the cluster and migration family.
# All values are configuration REFERENCES — no secret value, credential, image
# digest, or account identity is exposed.

output "cluster_id" {
  description = "ID/ARN of the staging ECS cluster."
  value       = aws_ecs_cluster.this.id
}

output "api_service_name" {
  description = "Deterministic name of the API ECS service (consumed by observability for service metrics/alarms). Emitted as the stable `<name_prefix>-api` string so it is stage-independent — identical to the service's own name and available in the foundation stage before the service exists (observability alarms may reference it either way)."
  value       = "${var.name_prefix}-api"
}

output "worker_service_name" {
  description = "Deterministic name of the worker ECS service (consumed by observability for service metrics/alarms). Emitted as the stable `<name_prefix>-worker` string so it is stage-independent — identical to the service's own name."
  value       = "${var.name_prefix}-worker"
}

output "migration_task_family" {
  description = "Deterministic family of the one-shot migration task definition (consumed by the later, separately authorized run-task step; never a service). Emitted as the stable `<name_prefix>-migration` string so it is stage-independent — identical to the task definition's own family."
  value       = "${var.name_prefix}-migration"
}

output "api_task_security_group_id" {
  description = "ID of the ecs-owned API task security group (informational; alb never consumes it — §26.13)."
  value       = aws_security_group.task["api"].id
}

output "log_group_names" {
  description = "Map of workload (api|worker|migration) -> deterministic ecs-owned CloudWatch log-group name (/ecs/<name_prefix>-<workload>). Consumed by observability (§26.9)."
  value       = { for w, lg in aws_cloudwatch_log_group.workload : w => lg.name }
}

output "log_group_arns" {
  description = "Map of workload (api|worker|migration) -> ecs-owned CloudWatch log-group ARN. Consumed by observability (§26.9); iam never consumes these (deterministic prefix scoping, §26.8)."
  value       = { for w, lg in aws_cloudwatch_log_group.workload : w => lg.arn }
}
