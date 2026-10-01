# Staging window (docs/operations/staging-window.md): the load balancer, HTTPS listener
# and API target group exist ONLY when `enabled = true`; the ALB security group, its
# ingress rule and the private log bucket persist in both states, and every output that
# names a windowed resource is null while the window is closed. Offline: fully mocked
# provider, no backend, no AWS call.
mock_provider "aws" {
  # The provider validates listener ARN references at plan time; the mock's random
  # computed strings are not ARNs, so the two referenced ARNs are given ARN-shaped defaults.
  mock_resource "aws_lb" {
    defaults = {
      arn = "arn:aws:elasticloadbalancing:us-east-1:111122223333:loadbalancer/app/mock/0123456789abcdef"
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
}

run "window_open_creates_lb_listener_and_target_group" {
  command = plan

  variables {
    enabled = true
  }

  assert {
    condition     = length(aws_lb.this) == 1 && length(aws_lb_listener.https) == 1 && length(aws_lb_target_group.api) == 1
    error_message = "with the window open the ALB, listener and target group must each be planned exactly once"
  }
}

run "window_closed_plans_no_lb_listener_or_target_group" {
  command = plan

  variables {
    enabled = false
  }

  assert {
    condition     = length(aws_lb.this) == 0 && length(aws_lb_listener.https) == 0 && length(aws_lb_target_group.api) == 0
    error_message = "with the window closed no ALB, listener or target group may be planned"
  }

  assert {
    condition     = output.alb_arn == null && output.alb_dns_name == null && output.alb_canonical_hosted_zone_id == null && output.https_listener_arn == null && output.api_target_group_arn == null
    error_message = "every windowed output must be null while the window is closed"
  }

  assert {
    condition     = output.alb_security_group_id != null
    error_message = "the ALB security group persists across windows (free; referenced by the ecs cross-SG rules)"
  }

  assert {
    condition     = aws_s3_bucket.alb_logs.bucket_prefix != null
    error_message = "the ALB log bucket persists across windows (delivered logs are retained)"
  }
}

run "module_default_is_open_so_standalone_behaviour_is_unchanged" {
  command = plan

  assert {
    condition     = length(aws_lb.this) == 1
    error_message = "the module default (enabled = true) must keep the module's standalone behaviour"
  }
}
