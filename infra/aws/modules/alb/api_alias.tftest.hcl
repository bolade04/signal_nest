# API Route 53 alias (P6-INF-3, windowed; docs/operations/staging-window.md §4): the alias
# record `api_fqdn -> ALB` exists ONLY when `enabled = true`, is a single `A` alias (the ALB
# is IPv4-only), is bound to the PLANNED load balancer's dns_name / canonical zone id (so a
# re-created ALB re-points it with no hand edit), lives in the consumed hosted zone, and the
# module rejects malformed hostnames and zone ids before planning. Offline: fully mocked
# provider, no backend, no AWS call.
mock_provider "aws" {
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
}

variables {
  name_prefix         = "signalnest-staging"
  vpc_id              = "vpc-0123456789abcdef0"
  public_subnet_ids   = ["subnet-0123456789abcdef1", "subnet-0123456789abcdef2"]
  api_certificate_arn = "arn:aws:acm:us-east-1:111122223333:certificate/00000000-0000-0000-0000-000000000000"
  hosted_zone_id      = "ZSYNTH00000000000000"
  api_fqdn            = "api.synthetic.example.com"
}

run "window_open_plans_one_a_alias_bound_to_the_planned_alb" {
  command = plan

  variables {
    enabled = true
  }

  assert {
    condition     = length(aws_route53_record.api) == 1
    error_message = "with the window open exactly one API alias record must be planned"
  }

  assert {
    condition     = aws_route53_record.api[0].type == "A" && length(aws_route53_record.api[0].alias) == 1
    error_message = "the API record must be a single A alias (the ALB is IPv4-only; no AAAA, no plain A/CNAME value)"
  }

  assert {
    condition     = aws_route53_record.api[0].name == var.api_fqdn && aws_route53_record.api[0].zone_id == var.hosted_zone_id
    error_message = "the record must be api_fqdn inside the consumed hosted zone"
  }

  assert {
    # Replacement targeting: the alias is bound to the load balancer PLANNED in this same
    # run, not to a literal — a destroyed-and-recreated ALB (new dns_name) re-points it.
    condition     = aws_route53_record.api[0].alias[0].name == aws_lb.this[0].dns_name && aws_route53_record.api[0].alias[0].zone_id == aws_lb.this[0].zone_id
    error_message = "the alias target must be the planned ALB's dns_name and canonical hosted-zone id"
  }

  assert {
    condition     = aws_route53_record.api[0].alias[0].evaluate_target_health == false
    error_message = "evaluate_target_health stays false (single record, no failover sibling; same shape as the web aliases)"
  }

  assert {
    condition     = output.api_alias_record_name == var.api_fqdn
    error_message = "the module must export the alias record name while open"
  }
}

run "window_closed_plans_no_alias_record" {
  command = plan

  variables {
    enabled = false
  }

  assert {
    condition     = length(aws_route53_record.api) == 0 && length(aws_lb.this) == 0
    error_message = "with the window closed no API alias record (and no ALB for it to target) may be planned"
  }

  assert {
    condition     = output.api_alias_record_name == null && output.alb_dns_name == null
    error_message = "the alias output must be null while closed, like every other windowed output"
  }

  assert {
    condition     = output.alb_security_group_id != null
    error_message = "closing the window must not touch the persistent ALB security group"
  }
}

run "api_fqdn_with_a_scheme_is_rejected" {
  command = plan

  variables {
    api_fqdn = "https://api.synthetic.example.com"
  }

  expect_failures = [var.api_fqdn]
}

run "api_fqdn_with_a_trailing_dot_is_rejected" {
  command = plan

  variables {
    api_fqdn = "api.synthetic.example.com."
  }

  expect_failures = [var.api_fqdn]
}

run "api_fqdn_with_whitespace_is_rejected" {
  command = plan

  variables {
    api_fqdn = "api.synthetic.example.com x"
  }

  expect_failures = [var.api_fqdn]
}

run "malformed_hosted_zone_id_is_rejected" {
  command = plan

  variables {
    hosted_zone_id = "not-a-zone-id"
  }

  expect_failures = [var.hosted_zone_id]
}
