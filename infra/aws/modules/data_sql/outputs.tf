# outputs.tf — non-sensitive SQL data-plane references for downstream modules
#
# All outputs are configuration REFERENCES (identifiers, ARNs, hostnames), not secret
# values, so none is marked sensitive. NO password, complete DATABASE_URL, credentialed
# URL, or secret value is output. The RDS-managed master secret is exposed only by ARN
# (metadata), never by value. `db_endpoint`/`db_address` are hostnames the caller
# composes into DATABASE_URL out-of-band (never read into state); `rds_security_group_id`
# is consumed by the future `ecs` module to author the task↔DB 5432 rules.

output "db_instance_identifier" {
  description = "Identifier of the RDS PostgreSQL instance."
  value       = aws_db_instance.this.identifier
}

output "db_instance_arn" {
  description = "ARN of the RDS PostgreSQL instance."
  value       = aws_db_instance.this.arn
}

output "db_address" {
  description = "Hostname (address only, no port) of the RDS instance endpoint. Composed into DATABASE_URL out-of-band; never read into OpenTofu state as a full credentialed URL."
  value       = aws_db_instance.this.address
}

output "db_endpoint" {
  description = "Connection endpoint of the RDS instance in host:port form (preserved from the module stub). Composed into DATABASE_URL out-of-band; contains no credential."
  value       = aws_db_instance.this.endpoint
}

output "db_port" {
  description = "TCP port the RDS instance listens on (5432)."
  value       = aws_db_instance.this.port
}

output "database_name" {
  description = "Name of the initial PostgreSQL database created on the instance."
  value       = aws_db_instance.this.db_name
}

output "master_username" {
  description = "PostgreSQL master (administrative/bootstrap) username. Not a secret; the master password is RDS-managed and never exposed here."
  value       = aws_db_instance.this.username
}

output "db_subnet_group_name" {
  description = "Name of the DB subnet group placing the instance in the supplied private subnets."
  value       = aws_db_subnet_group.this.name
}

output "rds_security_group_id" {
  description = "ID of the RDS security group owned by this module (created with no rules). Consumed by the future ecs module, which owns the standalone TCP 5432 ingress rules from the API/worker/migration task SGs."
  value       = aws_security_group.rds.id
}

output "master_user_secret_arn" {
  description = "ARN of the RDS-managed master-user Secrets Manager secret (metadata reference only; contains no secret value). This is the administrative/bootstrap credential, NOT the finished application DATABASE_URL, and grants no ECS/IAM access."
  value       = one(aws_db_instance.this.master_user_secret[*].secret_arn)
}
