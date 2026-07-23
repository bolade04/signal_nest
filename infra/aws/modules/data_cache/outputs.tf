# outputs.tf — non-sensitive Redis cache references for downstream modules
#
# All outputs are configuration REFERENCES (identifiers, hostnames), not secret values,
# so none is marked sensitive. NO auth token, password, complete REDIS_URL, or secret
# value is output (Option A). `redis_primary_endpoint`/`redis_port` are the host/port the
# caller composes into REDIS_URL out-of-band (using rediss://, never read into state);
# `redis_security_group_id` is consumed by the future `ecs` module to author the
# task↔Redis 6379 rules (API and worker only; migration excluded).

output "redis_primary_endpoint" {
  description = "Primary endpoint hostname of the Redis replication group. Composed into REDIS_URL out-of-band with the rediss:// scheme; contains no credential."
  value       = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "redis_port" {
  description = "TCP port the Redis replication group listens on (6379)."
  value       = aws_elasticache_replication_group.this.port
}

output "cache_subnet_group_name" {
  description = "Name of the ElastiCache subnet group placing the replication group in the supplied private subnets."
  value       = aws_elasticache_subnet_group.this.name
}

output "redis_security_group_id" {
  description = "ID of the Redis security group owned by this module (created with no rules). Consumed by the future ecs module, which owns the standalone TCP 6379 ingress rules from the API and worker task SGs only (migration receives no Redis access)."
  value       = aws_security_group.redis.id
}
