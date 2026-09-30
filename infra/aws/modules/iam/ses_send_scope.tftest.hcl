# P6-AUTH-2 / 6B-4C (FD-1) — the API task role's SES send grant must be EXACTLY scoped.
#
# WHAT THIS PROVES, offline, with a fully mocked provider (no backend, no state, no AWS
# call): the rendered policy grants ses:SendEmail on ONE identity ARN, conditioned on ONE
# bare From address and the SES v2 API — and nothing wider. Positive runs assert the
# rendered JSON; negative runs prove the inputs that would silently widen or break the
# grant are REFUSED before any plan is produced. The data sources are overridden with
# the AWS documentation placeholder account, so the ARN is deterministic and the
# expected rendering can be pinned as decoded-JSON equality in tests/fixtures.
#
# The display-name header (ses:FromDisplayName) is deliberately NOT conditioned here;
# that is a separate decision (docs/operations/aws-staging-runtime-contract.md §F), and
# a run below asserts it stays absent so it cannot be added without a reviewed change.

mock_provider "aws" {}

override_data {
  target = data.aws_caller_identity.current
  values = {
    account_id = "111122223333"
  }
}

override_data {
  target = data.aws_partition.current
  values = {
    partition = "aws"
  }
}

override_data {
  target = data.aws_region.current
  values = {
    region = "us-east-1"
    name   = "us-east-1"
  }
}

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
  github_oidc_provider_arn      = "arn:aws:iam::111122223333:oidc-provider/token.actions.githubusercontent.com"

  # Synthetic sending identity and From address (documentation domain, never a real one).
  mail_sending_identity_domain = "mail.staging.example.com"
  mail_from_address            = "no-reply@mail.staging.example.com"
}

# --- Positive: the rendered grant is exactly one action, one identity, two conditions ---
run "ses_grant_is_one_action_one_identity_one_from_address_v2_only" {
  command = plan

  assert {
    condition     = length(jsondecode(aws_iam_role_policy.api_ses_send.policy).Statement) == 1
    error_message = "the SES send policy must carry exactly one statement"
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.api_ses_send.policy).Statement[0].Effect == "Allow"
    error_message = "the SES send statement must be an Allow"
  }

  assert {
    condition     = tolist(jsondecode(aws_iam_role_policy.api_ses_send.policy).Statement[0].Action) == tolist(["ses:SendEmail"])
    error_message = "the SES send statement must grant ses:SendEmail and nothing else (no SendRawEmail, no ses:*)"
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.api_ses_send.policy).Statement[0].Resource == "arn:aws:ses:us-east-1:111122223333:identity/mail.staging.example.com"
    error_message = "the Resource must be exactly the sending-domain identity ARN (partition, region and account from data sources; the domain from the input)"
  }

  assert {
    condition     = !strcontains(jsondecode(aws_iam_role_policy.api_ses_send.policy).Statement[0].Resource, "*")
    error_message = "the Resource must not contain a wildcard"
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.api_ses_send.policy).Statement[0].Condition.StringEquals["ses:FromAddress"] == "no-reply@mail.staging.example.com"
    error_message = "ses:FromAddress must equal the exact bare From address"
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.api_ses_send.policy).Statement[0].Condition.StringEquals["ses:ApiVersion"] == "2"
    error_message = "ses:ApiVersion must be pinned to \"2\" (the SES v2 API the client calls)"
  }

  assert {
    condition     = length(keys(jsondecode(aws_iam_role_policy.api_ses_send.policy).Statement[0].Condition)) == 1 && length(keys(jsondecode(aws_iam_role_policy.api_ses_send.policy).Statement[0].Condition.StringEquals)) == 2
    error_message = "the Condition must be exactly one StringEquals block with exactly the two keys ses:FromAddress and ses:ApiVersion"
  }

  assert {
    condition     = !contains(keys(jsondecode(aws_iam_role_policy.api_ses_send.policy).Statement[0].Condition.StringEquals), "ses:FromDisplayName")
    error_message = "ses:FromDisplayName is a separate decision and must not be conditioned here"
  }

  assert {
    condition     = aws_iam_role_policy.api_ses_send.name == "signalnest-staging-api-ses-send"
    error_message = "the inline policy name is pinned (renaming needs iam:DeleteRolePolicy, which no repository principal holds)"
  }
}

# --- Positive: the rendered policy equals the tracked expected rendering (decoded JSON) ---
# The fixture is the SAME document tests/test_ses_send_policy.py evaluates with the
# repository's independent IAM evaluator, so the Python semantics run against what
# OpenTofu really renders, not a hand-copied restatement.
run "rendered_policy_matches_the_tracked_fixture" {
  command = plan

  assert {
    condition     = jsondecode(aws_iam_role_policy.api_ses_send.policy) == jsondecode(file("${path.module}/../../../../tests/fixtures/api-ses-send-policy.expected.json"))
    error_message = "the rendered SES send policy drifted from tests/fixtures/api-ses-send-policy.expected.json — update the fixture in the same reviewed change"
  }
}

# --- Positive: the grant binds to the API task role only; the worker gets no SES action ---
run "only_the_api_task_role_carries_the_ses_grant" {
  command = apply

  assert {
    condition     = aws_iam_role_policy.api_ses_send.role == aws_iam_role.api_task.id
    error_message = "the SES send policy must be attached to the API task role"
  }

  # NOTE: a mocked provider assigns every role the same synthetic id, so an id INEQUALITY
  # against the worker/migration roles would be vacuous here. That binding is proven
  # structurally by tests/test_ses_send_policy.py through the repository's .tf parser
  # (scripts/terraform_role_inventory.py): api_ses_send binds api-task and nothing else.

  assert {
    condition     = !strcontains(aws_iam_role_policy.app_s3["worker"].policy, "ses:") && !strcontains(aws_iam_role_policy.app_s3["api"].policy, "ses:") && !strcontains(aws_iam_role_policy.execution.policy, "ses:") && !strcontains(aws_iam_role_policy.ci_publisher[0].policy, "ses:")
    error_message = "no other inline policy may carry an SES action"
  }
}

# --- Negative: a From address outside the identity domain is refused before planning ---
run "from_address_outside_the_identity_domain_is_refused" {
  command = plan

  variables {
    mail_from_address = "no-reply@other.example.com"
  }

  expect_failures = [
    aws_iam_role_policy.api_ses_send,
  ]
}

# --- Negative: a display-name form is not a bare address and is refused ---
run "display_name_form_is_refused_as_from_address" {
  command = plan

  variables {
    mail_from_address = "SignalNest <no-reply@mail.staging.example.com>"
  }

  expect_failures = [
    var.mail_from_address,
  ]
}

# --- Negative: an identity domain carrying a scheme, path or '@' is refused ---
run "identity_domain_with_a_scheme_is_refused" {
  command = plan

  variables {
    mail_sending_identity_domain = "https://mail.staging.example.com"
  }

  expect_failures = [
    var.mail_sending_identity_domain,
  ]
}

# --- Negative: a single-label or apex-less value is refused (must be a multi-label FQDN) ---
run "single_label_identity_domain_is_refused" {
  command = plan

  variables {
    mail_sending_identity_domain = "mail"
    mail_from_address            = "no-reply@mail"
  }

  expect_failures = [
    var.mail_sending_identity_domain,
    var.mail_from_address,
  ]
}

# --- Negative: a local part with a doubled or trailing separator is refused ---
run "malformed_local_part_is_refused" {
  command = plan

  variables {
    mail_from_address = "no-reply..x@mail.staging.example.com"
  }

  expect_failures = [
    var.mail_from_address,
  ]
}
