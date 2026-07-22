# main.tf — staging Application Load Balancer plane (INFRA-4 alb module)
#
# Owns the internet-facing HTTPS entry point for the SIGNALNEST_STAGING API: an
# ALB security group (public 443 ingress only), an internet-facing IPv4 ALB in the
# existing public subnets, an IP-target-type target group for the private API tasks
# on port 8000, and an HTTPS:443 listener that terminates TLS (consumed regional
# ACM certificate) and forwards to the target group. TLS terminates at the ALB;
# ALB->target traffic is plain HTTP inside the restricted VPC security-group path.
#
# Ownership (aws-staging-iac-plan.md §24.2, cycle-free): this module creates+owns
# ONLY the ALB security group and exposes `alb_security_group_id`. The API task
# security group AND both ALB<->API cross-SG rules (ALB SG egress -> API SG :8000,
# API SG ingress <- ALB SG :8000) are owned by the `ecs` module, which consumes
# `alb_security_group_id` and `api_target_group_arn`. This module NEVER consumes an
# ECS/API security-group id, so the dependency is one-way `ecs -> alb`.
#
# Deferred (§24.7): access/connection logging (storage tranche owns the bucket),
# WAF, and the API Route 53 alias. No ACM certificate is created; none is queried.
#
# CONSUME, not create: the ACM certificate (var.api_certificate_arn, us-east-1) is
# supplied by value; no ACM/route53 resource or data source is declared. No provider
# block/alias is declared; the root AWS provider and its committed lock are inherited.
#
# Tagging: the authoritative eight-tag common set is applied to every taggable
# resource by the root provider's `default_tags` (providers.tf). This module only
# adds the conventional per-resource `Name` tag.

# --- ALB security group ------------------------------------------------------------
# Created with NO inline ingress/egress rules. The AWS provider REMOVES the implicit
# allow-all (0.0.0.0/0) default egress rule that AWS attaches on creation, so this SG
# has exactly the rules declared as standalone resources below — no unrestricted
# egress (§24.2). Rules are managed only via standalone `aws_vpc_security_group_*_rule`
# resources (never mixed with inline blocks): this module declares the public 443
# ingress; the `ecs` module later attaches the ALB->API :8000 egress rule to this SG.
resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb-sg"
  description = "ALB SG for ${var.name_prefix}: public HTTPS 443 ingress only; ALB->API :8000 egress owned by the ecs module."
  vpc_id      = var.vpc_id

  tags = {
    Name = "${var.name_prefix}-alb-sg"
  }
}

# Public HTTPS ingress: TCP 443 from anywhere, IPv4 only (§24.3). No port 80, no
# public port 8000, no IPv6 ingress. Standalone rule (not inline) so the ecs module
# can add its egress rule to the same SG without mixing inline and standalone rules.
resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  description       = "Public HTTPS from the internet (IPv4)."
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"

  tags = {
    Name = "${var.name_prefix}-alb-https-ingress"
  }
}

# --- Application Load Balancer -----------------------------------------------------
resource "aws_lb" "this" {
  name               = "${var.name_prefix}-alb"
  internal           = false
  load_balancer_type = "application"
  ip_address_type    = "ipv4"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids

  enable_http2               = true
  idle_timeout               = 60
  drop_invalid_header_fields = true
  desync_mitigation_mode     = "defensive"
  preserve_host_header       = false
  xff_header_processing_mode = "append"
  enable_xff_client_port     = false
  enable_deletion_protection = false

  # Access/connection logging is deferred to the storage/logging tranche (§24.7);
  # no access_logs or connection_logs block is declared here. Cross-zone load
  # balancing uses the ALB's always-on default; no target-group override is set.

  tags = {
    Name = "${var.name_prefix}-alb"
  }
}

# --- API target group (IP targets; private API tasks on port 8000) ----------------
resource "aws_lb_target_group" "api" {
  name        = "${var.name_prefix}-api-tg"
  target_type = "ip"
  protocol    = "HTTP"
  # Provider spelling of the contract's "HTTP/1.1" protocol version (§24.6).
  protocol_version = "HTTP1"
  port             = var.api_target_port
  vpc_id           = var.vpc_id

  deregistration_delay          = 60
  slow_start                    = 0
  load_balancing_algorithm_type = "round_robin"

  # Stickiness disabled (§24.6). The `type` is required by the provider schema even
  # when disabled; no cookie is issued while enabled = false.
  stickiness {
    type    = "lb_cookie"
    enabled = false
  }

  # Health check against the shallow, dependency-free liveness endpoint /health
  # (never the dependency-aware /readiness) so a shared-backend outage cannot cause
  # an ECS task-replacement loop (§24.1 / runtime-contract §D).
  health_check {
    enabled             = true
    protocol            = "HTTP"
    port                = "traffic-port"
    path                = var.health_check_path
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # No load_balancing_cross_zone_enabled override (§24.5): inherit the ALB default.
  # No target registrations / ECS attachments here — those are owned by the ecs module.

  tags = {
    Name = "${var.name_prefix}-api-tg"
  }
}

# --- HTTPS listener (443, TLS terminates here) ------------------------------------
# Consumes the existing regional ACM certificate by ARN (never created/queried).
# No HTTP:80 listener and no HTTP->HTTPS redirect exist (§24.3): HTTPS is mandatory.
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.api_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }

  tags = {
    Name = "${var.name_prefix}-https-listener"
  }
}
