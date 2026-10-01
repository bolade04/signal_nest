# Root input-boundary and alias-wiring contract (offline, fully mocked provider; no backend,
# no AWS call). The module suites prove each module's shape; THIS suite proves what only the
# ROOT can: (1) `monthly_budget_limit` is bounded by the operator's USD 20 TOTAL monthly limit
# (values above 20 — including the historical USD 200 ceiling — fail before any plan, while 20
# is accepted); (2) `api_fqdn` is validated at the root and REJECTED when it collides with
# `web_fqdn` (case-insensitively; both aliases live in the one consumed hosted zone); (3) the
# root wires the single window input to the API alias record: absent while closed, planned and
# named `api_fqdn` while open, with the web edge outputs present either way (the web records themselves
# are pinned textually by tests/test_api_alias_and_budget_bound.py — module resources are not addressable here).
#
# Mocking: provider-computed identifiers are given ARN-/id-shaped defaults because sibling
# modules VALIDATE the values they consume (regexes for vpc-/subnet-/sg- ids, ARNs, repository
# URLs); the network module is overridden as a whole so the two public subnet ids are DISTINCT
# (the alb module rejects duplicates, and per-instance overrides are not addressable). Nothing
# here changes what the root plans — only what the mocked provider pretends AWS returned.
mock_provider "aws" {
  mock_resource "aws_vpc" {
    defaults = {
      id  = "vpc-0123456789abcdef0"
      arn = "arn:aws:ec2:us-east-1:111122223333:vpc/vpc-0123456789abcdef0"
    }
  }
  mock_resource "aws_security_group" {
    defaults = {
      id  = "sg-0123456789abcdef0"
      arn = "arn:aws:ec2:us-east-1:111122223333:security-group/sg-0123456789abcdef0"
    }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = {
      arn = "arn:aws:secretsmanager:us-east-1:111122223333:secret:signalnest-staging/MOCK-abcdef"
    }
  }
  mock_resource "aws_kms_key" {
    defaults = {
      arn = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000"
    }
  }
  mock_resource "aws_s3_bucket" {
    defaults = {
      arn = "arn:aws:s3:::signalnest-staging-mock-bucket"
    }
  }
  mock_resource "aws_ecr_repository" {
    defaults = {
      arn            = "arn:aws:ecr:us-east-1:111122223333:repository/signalnest-staging-mock"
      repository_url = "111122223333.dkr.ecr.us-east-1.amazonaws.com/signalnest-staging-mock"
    }
  }
  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::111122223333:role/signalnest-staging-mock"
    }
  }
  mock_resource "aws_lb" {
    defaults = {
      arn      = "arn:aws:elasticloadbalancing:us-east-1:111122223333:loadbalancer/app/mock/0123456789abcdef"
      dns_name = "mock-alb-0123456789.us-east-1.elb.amazonaws.com"
      zone_id  = "ZSYNTHALBCANONICAL00"
    }
  }
  mock_resource "aws_lb_target_group" {
    defaults = {
      arn = "arn:aws:elasticloadbalancing:us-east-1:111122223333:targetgroup/mock/0123456789abcdef"
    }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      arn = "arn:aws:logs:us-east-1:111122223333:log-group:/mock"
    }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = {
      arn = "arn:aws:ecs:us-east-1:111122223333:cluster/mock"
      id  = "arn:aws:ecs:us-east-1:111122223333:cluster/mock"
    }
  }
  mock_resource "aws_cloudfront_distribution" {
    defaults = {
      domain_name    = "d111111abcdef8.cloudfront.net"
      hosted_zone_id = "ZSYNTHCLOUDFRONT0000"
    }
  }
}

# The revision reader composes under the aliased provider (providers.tf); it must be mocked
# under the same alias or the plan would try to configure a real provider for it.
mock_provider "aws" {
  alias = "revision_reader"
  mock_resource "aws_security_group" {
    defaults = {
      id = "sg-0123456789abcdef1"
    }
  }
  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::111122223333:role/signalnest-staging-reader-mock"
    }
  }
  mock_resource "aws_ecr_repository" {
    defaults = {
      arn            = "arn:aws:ecr:us-east-1:111122223333:repository/signalnest-staging-reader"
      repository_url = "111122223333.dkr.ecr.us-east-1.amazonaws.com/signalnest-staging-reader"
    }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      arn = "arn:aws:logs:us-east-1:111122223333:log-group:/reader"
    }
  }
}

override_module {
  target = module.network
  outputs = {
    vpc_id             = "vpc-0123456789abcdef0"
    public_subnet_ids  = ["subnet-0123456789abcdef1", "subnet-0123456789abcdef2"]
    private_subnet_ids = ["subnet-0123456789abcdef3", "subnet-0123456789abcdef4"]
  }
}

# The synthetic root inputs (mirrors tests/fixtures/root-wiring-synthetic.tfvars.example —
# tests/test_api_alias_and_budget_bound.py pins the two files' shared values).
variables {
  vpc_cidr                  = "10.255.0.0/16"
  availability_zones        = ["us-east-1a", "us-east-1b"]
  web_fqdn                  = "synthetic.example.com"
  hosted_zone_id            = "ZSYNTH00000000000000"
  acm_certificate_arn       = "arn:aws:acm:us-east-1:000000000000:certificate/00000000-0000-0000-0000-000000000000"
  api_certificate_arn       = "arn:aws:acm:us-east-1:000000000000:certificate/00000000-0000-0000-0000-000000000001"
  api_fqdn                  = "api.synthetic.example.com"
  app_bucket_name           = "synthetic-probe-bucket-000000"
  db_engine_version         = "16.4"
  db_name                   = "synthetic"
  db_master_username        = "synthetic_admin"
  redis_engine_version      = "7.1"
  llm_provider              = "anthropic"
  monthly_budget_limit      = 20
  budget_notification_email = "probe@example.com"
  role_boundary_mode        = "disabled"
  alarm_thresholds = {
    ecs_cpu_high_percent          = 80
    ecs_memory_high_percent       = 80
    log_error_count_per_period    = 1
    rds_cpu_high_percent          = 80
    rds_free_storage_low_bytes    = 1000000000
    rds_freeable_memory_low_bytes = 100000000
    redis_cpu_high_percent        = 80
    redis_memory_high_percent     = 80
  }
  staging_window_active        = true
  mail_sending_identity_domain = "mail.synthetic.example.com"
  mail_from_address            = "no-reply@mail.synthetic.example.com"
}

# --- budget: the operator limit is the root's upper bound --------------------------------
run "budget_at_the_operator_limit_is_accepted" {
  command = plan

  variables {
    monthly_budget_limit = 20
  }

  assert {
    condition     = output.budget_name == "signalnest-staging-monthly"
    error_message = "a 20 USD limit must plan the single monthly budget"
  }
}

run "budget_above_the_operator_limit_is_rejected" {
  command = plan

  variables {
    monthly_budget_limit = 21
  }

  expect_failures = [var.monthly_budget_limit]
}

run "budget_at_the_historical_200_ceiling_is_rejected_at_this_root" {
  command = plan

  variables {
    # 200 is still accepted by the generic cost MODULE's own interface; the staging ROOT
    # encodes the stricter operator limit, so the historical ceiling no longer passes here.
    monthly_budget_limit = 200
  }

  expect_failures = [var.monthly_budget_limit]
}

# --- api_fqdn: validated at the root; collision with web_fqdn rejected -------------------
run "api_fqdn_equal_to_web_fqdn_is_rejected_case_insensitively" {
  command = plan

  variables {
    api_fqdn = "SYNTHETIC.example.com"
  }

  expect_failures = [var.api_fqdn]
}

run "api_fqdn_with_a_path_is_rejected" {
  command = plan

  variables {
    api_fqdn = "api.synthetic.example.com/v1"
  }

  expect_failures = [var.api_fqdn]
}

# --- window wiring: the alias follows staging_window_active; web DNS does not -------------
run "closed_window_plans_no_api_alias_and_keeps_the_web_alias" {
  command = plan

  variables {
    staging_window_active = false
  }

  assert {
    condition     = output.api_alias_record_name == null && output.alb_dns_name == null && output.alb_canonical_hosted_zone_id == null
    error_message = "with the window closed the root must plan no ALB and no API alias record"
  }

  assert {
    # The web RECORDS are module resources and not addressable from a root run; this asserts the
    # web edge outputs the root still exports (a configuration echo + the planned distribution).
    # The records' non-windowed shape is pinned by tests/test_api_alias_and_budget_bound.py.
    condition     = output.web_url == "https://synthetic.example.com" && output.cloudfront_domain_name != null
    error_message = "closing the window must leave the web edge outputs (web_url echo, CloudFront distribution) in place"
  }

  assert {
    condition     = output.api_url == "https://api.synthetic.example.com"
    error_message = "api_url is a configuration echo and must not depend on the window"
  }
}

run "open_window_plans_the_api_alias_named_api_fqdn" {
  command = plan

  variables {
    staging_window_active = true
  }

  assert {
    condition     = output.api_alias_record_name == "api.synthetic.example.com"
    error_message = "with the window open the root must plan the API alias record for api_fqdn"
  }

  assert {
    condition     = output.alb_dns_name != null && output.alb_canonical_hosted_zone_id != null
    error_message = "the alias target (ALB dns_name + canonical zone id) must be planned in the same run"
  }

  assert {
    condition     = output.web_url == "https://synthetic.example.com"
    error_message = "opening the window must not change the web edge"
  }
}
