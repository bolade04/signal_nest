# Staging window (docs/operations/staging-window.md): the Redis replication group exists
# ONLY when `enabled = true`; the subnet group, parameter group and security group persist,
# and the endpoint outputs are null while the window is closed. Offline: mocked provider.
mock_provider "aws" {}

variables {
  name_prefix        = "signalnest-staging"
  vpc_id             = "vpc-0123456789abcdef0"
  private_subnet_ids = ["subnet-0123456789abcdef1", "subnet-0123456789abcdef2"]
  engine_version     = "7.1"
}

run "window_open_creates_the_replication_group" {
  command = plan

  variables {
    enabled = true
  }

  assert {
    condition     = length(aws_elasticache_replication_group.this) == 1
    error_message = "with the window open the replication group must be planned exactly once"
  }
}

run "window_closed_plans_no_replication_group_and_keeps_the_persistent_pieces" {
  command = plan

  variables {
    enabled = false
  }

  assert {
    condition     = length(aws_elasticache_replication_group.this) == 0
    error_message = "with the window closed no replication group may be planned"
  }

  assert {
    condition     = output.redis_primary_endpoint == null && output.redis_port == null
    error_message = "endpoint outputs must be null while the window is closed"
  }

  assert {
    condition     = output.redis_security_group_id != null && output.cache_subnet_group_name != null
    error_message = "the security group and subnet group persist across windows"
  }

  assert {
    condition     = aws_elasticache_parameter_group.this.name == "signalnest-staging-redis-params"
    error_message = "the parameter group persists across windows"
  }
}
