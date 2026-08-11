# Gate 4N-I8 Defect 8 — the permissions boundary must be DURABLE.
#
# THE DEFECT. `permissions_boundary` is a MANAGED attribute on every role this module
# creates. Through Gate 4N-I7 the root variable defaulted to null, no tfvars set it, and no
# rollout operation persisted it. So after the boundary was attached out-of-band, the next
# OpenTofu execution supplied null, planned REMOVAL, and the following apply would have
# stripped the boundary from all five deployed roles. The Gate 4N-I7 adversarial lane found
# this; no artifact, claim or test covered it.
#
# These tests run at MODULE level, offline, with a fully mocked provider: no backend, no
# state, no AWS call. They prove the attribute actually reaches every role and that a null
# input is visible as an unbounded role rather than silently ignored.
#
# SCOPE NOTE, stated plainly: the ROOT-level cross-variable validation
# (role_boundary_mode = "enforced" requires a non-null ARN) is NOT exercised here. The root
# composition cannot be planned offline without a large synthetic fixture for every module,
# and this gate forbids a backend plan. That validation is covered structurally by
# tests/test_boundary_durability.py, which is weaker and is labelled as such.

mock_provider "aws" {}

variables {
  name_prefix = "signalnest-staging"
  secret_arns = {
    DATABASE_URL = "arn:aws:secretsmanager:us-east-1:111122223333:secret:signalnest-staging/DATABASE_URL-AAAAAA"
    REDIS_URL    = "arn:aws:secretsmanager:us-east-1:111122223333:secret:signalnest-staging/REDIS_URL-BBBBBB"
    SECRET_KEY   = "arn:aws:secretsmanager:us-east-1:111122223333:secret:signalnest-staging/SECRET_KEY-CCCCCC"
    LLM_API_KEY  = "arn:aws:secretsmanager:us-east-1:111122223333:secret:signalnest-staging/LLM_API_KEY-DDDDDD"
  }
  kms_key_arn = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000"
  bucket_arn  = "arn:aws:s3:::signalnest-staging-app-testfixture"
  repository_arns = {
    api    = "arn:aws:ecr:us-east-1:111122223333:repository/signalnest-staging/api"
    worker = "arn:aws:ecr:us-east-1:111122223333:repository/signalnest-staging/worker"
  }
  role_boundary_mode            = "required"
  role_permissions_boundary_arn = "arn:aws:iam::111122223333:policy/signalnest-staging-role-boundary"
  # ci_publisher is count-gated on this being non-null. Supplying it means all FIVE roles
  # are exercised; without it the fifth would silently drop out of the assertions.
  github_oidc_provider_arn = "arn:aws:iam::111122223333:oidc-provider/token.actions.githubusercontent.com"
}

run "every_role_carries_the_boundary_when_it_is_supplied" {
  command = plan

  assert {
    condition     = aws_iam_role.execution.permissions_boundary == var.role_permissions_boundary_arn
    error_message = "the ECS execution role does not carry the supplied boundary"
  }

  assert {
    condition     = aws_iam_role.api_task.permissions_boundary == var.role_permissions_boundary_arn
    error_message = "the api task role does not carry the supplied boundary"
  }

  assert {
    condition     = aws_iam_role.worker_task.permissions_boundary == var.role_permissions_boundary_arn
    error_message = "the worker task role does not carry the supplied boundary"
  }

  assert {
    condition     = aws_iam_role.migration_task.permissions_boundary == var.role_permissions_boundary_arn
    error_message = "the migration task role does not carry the supplied boundary"
  }

  assert {
    condition     = aws_iam_role.ci_publisher[0].permissions_boundary == var.role_permissions_boundary_arn
    error_message = "the CI publisher role does not carry the supplied boundary"
  }
}

run "explicit_disabled_mode_produces_unbounded_roles" {
  command = plan

  variables {
    role_boundary_mode            = "disabled"
    role_permissions_boundary_arn = null
  }

  # GATE 4N-I14. This used to be "a null input produces unbounded roles", which documented
  # the DEFECT: nullness alone decided the outcome, so omission silently stripped the
  # boundary. The mode decides now. Reaching the unbounded state requires SAYING "disabled".
  assert {
    condition     = aws_iam_role.execution.permissions_boundary == null
    error_message = "explicit disabled mode must produce an unbounded role"
  }

  assert {
    condition     = aws_iam_role.ci_publisher[0].permissions_boundary == null
    error_message = "explicit disabled mode must produce an unbounded role"
  }
}

run "required_mode_with_a_null_arn_fails_before_any_resource_is_planned" {
  command = plan

  variables {
    role_boundary_mode            = "required"
    role_permissions_boundary_arn = null
  }

  # THE durability defect, now a hard failure rather than a silent removal diff.
  # Gate 4N-I16: attributed to the COHERENCE axis specifically, so a mutation of the
  # state classifier cannot be masked by the ceiling-identity axis failing instead.
  expect_failures = [terraform_data.boundary_state_coherence]
}

run "an_unknown_mode_is_rejected" {
  command = plan

  variables {
    role_boundary_mode = "enforced"
  }

  expect_failures = [var.role_boundary_mode]
}

run "required_mode_with_a_wrong_boundary_name_fails" {
  command = plan

  variables {
    role_boundary_mode            = "required"
    role_permissions_boundary_arn = "arn:aws:iam::111122223333:policy/some-other-policy"
  }

  expect_failures = [terraform_data.boundary_mode_precondition]
}

run "a_malformed_boundary_arn_is_rejected_before_any_resource_is_planned" {
  command = plan

  variables {
    role_boundary_mode            = "required"
    role_permissions_boundary_arn = "not-an-arn"
  }

  expect_failures = [var.role_permissions_boundary_arn]
}
