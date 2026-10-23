# RDS security-group description pin — offline, fully mocked provider, no backend, no AWS call.
#
# `aws_security_group.description` is ForceNew in the AWS provider: a changed string plans a
# REPLACE of the group, which cascades into the RDS instance's vpc_security_group_ids and every
# 5432 ingress rule that references the group. #142 changed the string in the configuration
# after the live group had been created with the earlier one, so every full root plan from
# then on proposed that replace. The value below is the one the live group carries (sealed
# live read, 2026-10-13); this suite fails if the configuration drifts from it again.
#
# A module-targeted run (a `module` block) so the assertions can read the module's resource;
# the root suites cannot address module internals.

mock_provider "aws" {}

variables {
  vpc_id                    = "vpc-0123456789abcdef0"
  private_subnet_ids        = ["subnet-0123456789abcdef3", "subnet-0123456789abcdef4"]
  engine_version            = "16.4"
  database_name             = "synthetic"
  master_username           = "synthetic_admin"
  final_snapshot_identifier = "signalnest-staging-postgres-final"
}

run "rds_sg_description_is_the_applied_string_byte_for_byte" {
  command = plan

  module {
    source = "./modules/data_sql"
  }

  variables {
    name_prefix = "signalnest-staging"
  }

  assert {
    condition     = aws_security_group.rds.description == "RDS PostgreSQL SG for signalnest-staging; ingress rules are owned by the ecs module."
    error_message = "the RDS security-group description must stay byte-equal to the applied value (description is ForceNew: any change replaces the DB security group)"
  }

  assert {
    condition     = aws_security_group.rds.name == "signalnest-staging-pg-sg"
    error_message = "the RDS security-group name (also ForceNew) must stay <name_prefix>-pg-sg"
  }
}

run "rds_sg_description_keeps_the_name_prefix_interpolation" {
  command = plan

  module {
    source = "./modules/data_sql"
  }

  variables {
    name_prefix = "probe-prefix"
  }

  assert {
    condition     = aws_security_group.rds.description == "RDS PostgreSQL SG for probe-prefix; ingress rules are owned by the ecs module."
    error_message = "the description template must be exactly \"RDS PostgreSQL SG for $${var.name_prefix}; ingress rules are owned by the ecs module.\""
  }
}
