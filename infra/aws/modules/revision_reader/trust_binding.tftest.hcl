# trust_binding.tftest.hcl — the HCL half of the reader trust-parity control (B-2B).
#
# WHY THIS EXISTS. The reader IAM roles are created OUT-OF-BAND by the role-bootstrap
# executor from scripts/trust_policies.py, then ADOPTED by this module. The B-2 Stage-A
# barrier (2026-08-15) refused an import because this module rendered the same trust
# documents differently (no Sid) — two sources of truth, unreconciled, and the diff a
# plan would produce is an UpdateAssumeRolePolicy of hash-verified reviewed trust.
#
# THE CONTROL. Both renderers are pinned to ONE golden fixture,
# tests/fixtures/reader-trust-golden.json (synthetic tier, placeholder account):
#   - tests/test_reader_trust_golden.py proves trust_policies.py still renders the
#     fixture's bytes;
#   - this file proves the module renders STRUCTURALLY IDENTICAL documents under the
#     same synthetic inputs.
# Either side drifting from the fixture fails its own test; the two cannot diverge
# from each other while both pass.
#
# Offline only: mocked provider, no backend, no AWS calls. All identifiers synthetic.

mock_provider "aws" {}

# Same synthetic ARN pins as reader_contract.tftest.hcl: the provider's generated mock
# values are random strings, which the task definition rejects as malformed ARNs.
override_resource {
  target = aws_iam_role.reader_execution
  values = { arn = "arn:aws:iam::111122223333:role/signalnest-staging-revision-reader-execution" }
}

override_resource {
  target = aws_iam_role.reader_publisher
  values = { arn = "arn:aws:iam::111122223333:role/signalnest-staging-revision-reader-publisher" }
}

override_resource {
  target = aws_iam_role.reader_runner
  values = { arn = "arn:aws:iam::111122223333:role/signalnest-staging-revision-reader-runner" }
}

override_resource {
  target = aws_ecr_repository.reader
  values = {
    arn            = "arn:aws:ecr:us-east-1:111122223333:repository/signalnest-staging/revision-reader"
    repository_url = "111122223333.dkr.ecr.us-east-1.amazonaws.com/signalnest-staging/revision-reader"
  }
}

# The execution trust interpolates data.aws_caller_identity.current.account_id into
# aws:SourceAccount. The provider's generated mock value is a RANDOM STRING, which
# would make a byte comparison against the fixture non-deterministic — pin it to the
# same placeholder account the fixture was rendered with.
override_data {
  target = data.aws_caller_identity.current
  values = { account_id = "111122223333" }
}

# Inputs matching the fixture's synthetic rendering exactly: same placeholder account,
# same OIDC provider ARN, and the REAL repository slug (the fixture's sub claims carry
# it because trust_policies.py pins GITHUB_REPOSITORY authoritatively).
variables {
  publication_bootstrap_enabled = true
  role_boundary_mode            = "required"
  role_permissions_boundary_arn = "arn:aws:iam::111122223333:policy/signalnest-staging-role-boundary"
  runtime_enabled               = true
  name_prefix                   = "signalnest-staging"
  aws_region                    = "us-east-1"
  vpc_id                        = "vpc-test"
  rds_security_group_id         = "sg-rds"
  database_url_secret_arn       = "arn:aws:secretsmanager:us-east-1:111122223333:secret:signalnest-staging/DATABASE_URL-bb"
  secrets_kms_key_arn           = "arn:aws:kms:us-east-1:111122223333:key/11111111-2222-3333-4444-555555555555"
  ecs_cluster_arn               = "arn:aws:ecs:us-east-1:111122223333:cluster/signalnest-staging-cluster"
  github_oidc_provider_arn      = "arn:aws:iam::111122223333:oidc-provider/token.actions.githubusercontent.com"
  github_repository             = "bolade04/signal_nest"
  revision_reader_image_digest  = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}

run "module_trust_equals_authoritative_golden_rendering" {
  command = plan

  assert {
    condition = jsondecode(aws_iam_role.reader_publisher[0].assume_role_policy) == jsondecode(
    file("${path.module}/../../../../tests/fixtures/reader-trust-golden.json")).trust.publisher
    error_message = "publisher assume_role_policy diverges from the authoritative trust_policies.py rendering (tests/fixtures/reader-trust-golden.json) — adoption of the executor-created role would plan a trust rewrite"
  }

  assert {
    condition = jsondecode(aws_iam_role.reader_runner[0].assume_role_policy) == jsondecode(
    file("${path.module}/../../../../tests/fixtures/reader-trust-golden.json")).trust.runner
    error_message = "runner assume_role_policy diverges from the authoritative trust_policies.py rendering (tests/fixtures/reader-trust-golden.json)"
  }

  assert {
    condition = jsondecode(aws_iam_role.reader_execution[0].assume_role_policy) == jsondecode(
    file("${path.module}/../../../../tests/fixtures/reader-trust-golden.json")).trust.execution
    error_message = "execution assume_role_policy diverges from the authoritative trust_policies.py rendering (tests/fixtures/reader-trust-golden.json)"
  }
}

run "passed_tag_set_reaches_every_non_role_resource" {
  command = plan

  # default_tags no longer backstops the reader module (aliased provider), so the
  # module's OWN tags plumbing is load-bearing: deleting a `tags = var.tags` or a
  # merge() from a non-role resource would silently zero its tags with every other
  # test green (round-2 architect finding). Roles stay exactly {Name} regardless.
  variables {
    tags = { CommonProbe = "tag-plumbing-check" }
  }

  assert {
    condition     = alltrue([for r in [aws_ecr_repository.reader[0], aws_cloudwatch_log_group.reader[0], aws_security_group.reader[0], aws_ecs_task_definition.reader[0]] : lookup(r.tags, "CommonProbe", "") == "tag-plumbing-check"])
    error_message = "a non-role reader resource no longer applies the passed tag set — its tags silently zero once default_tags is out of the path"
  }

  assert {
    condition     = alltrue([for r in [aws_vpc_security_group_egress_rule.reader_to_postgres[0], aws_vpc_security_group_egress_rule.reader_https[0], aws_vpc_security_group_ingress_rule.rds_from_reader[0]] : r.tags == tomap({ CommonProbe = "tag-plumbing-check" })])
    error_message = "a reader SG rule no longer carries the passed tag set"
  }

  assert {
    condition     = alltrue([for r in [aws_iam_role.reader_publisher[0], aws_iam_role.reader_execution[0], aws_iam_role.reader_runner[0]] : keys(r.tags) == tolist(["Name"])])
    error_message = "a reader ROLE picked up a passed tag — roles must stay the executor's literal {\"Name\"} even when the module receives a wider set"
  }
}

run "adopted_role_surface_stays_executor_shaped" {
  command = plan

  # The executor writes trust+boundary+tags and NOTHING else. A description reintroduced
  # here plans an UpdateRole on adoption; a tag beyond {"Name"} plans a TagRole outside
  # the bootstrap grant's aws:TagKeys ceiling.
  assert {
    condition     = alltrue([for r in [aws_iam_role.reader_publisher[0], aws_iam_role.reader_execution[0], aws_iam_role.reader_runner[0]] : r.description == null || r.description == ""])
    error_message = "a reader role sets a description the executor contract never writes — adoption would plan UpdateRole"
  }

  assert {
    condition     = alltrue([for r in [aws_iam_role.reader_publisher[0], aws_iam_role.reader_execution[0], aws_iam_role.reader_runner[0]] : keys(r.tags) == tolist(["Name"])])
    error_message = "a reader role carries tag keys beyond the executor contract's exact {\"Name\"} set"
  }
}
