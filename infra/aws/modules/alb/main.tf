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
# Logging (§24.7, resolved pre-live tranche): this module owns the dedicated
# private ALB log-delivery bucket and enables BOTH access logging and connection
# logging into it (distinct prefixes). The bucket mirrors the observability
# audit-bucket pattern (an infra-telemetry bucket, NOT an application bucket —
# `storage` still owns exactly one application bucket), superseding §24.7's
# earlier storage-owned phrasing. `access_logs`/`connection_logs` consume the
# bucket NAME, never an ARN (§24.7).
#
# API Route 53 alias (P6-INF-3; the §24.7 alias deferral was lifted by operator
# authorization on 2026-10-01 for a WINDOWED record — see the record below). WAF
# stays deferred. No ACM certificate is created; none is queried.
#
# CONSUME, not create: the ACM certificate (var.api_certificate_arn, us-east-1) and
# the Route 53 hosted zone (var.hosted_zone_id) are supplied by value; no ACM or
# route53-ZONE resource or data source is declared — the only Route 53 resource is
# the API alias RECORD inside the consumed zone. No provider block/alias is
# declared; the root AWS provider and its committed lock are inherited.
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
  description = "ALB SG for ${var.name_prefix}: public HTTPS 443 ingress only; ALB to API :8000 egress owned by the ecs module."
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
  count = var.enabled ? 1 : 0

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

  # Access + connection logging into the module-owned private log bucket
  # (§24.7 pre-live gate: logging resolved before any live staging plan/apply).
  # Both consume the bucket NAME (never an ARN). Cross-zone load balancing uses
  # the ALB's always-on default; no target-group override is set.
  access_logs {
    bucket  = aws_s3_bucket.alb_logs.id
    prefix  = "alb-access"
    enabled = true
  }

  connection_logs {
    bucket  = aws_s3_bucket.alb_logs.id
    prefix  = "alb-connection"
    enabled = true
  }

  # AWS verifies delivery permission when logging is enabled, so the bucket
  # policy must exist first; the bucket references alone do not order this.
  depends_on = [aws_s3_bucket_policy.alb_logs]

  tags = {
    Name = "${var.name_prefix}-alb"
  }
}

# --- Dedicated private ALB log-delivery bucket (§24.7 pre-live gate) ---------------
# Infra-telemetry bucket owned by this module (`storage` owns APPLICATION buckets;
# `observability` owns the AUDIT bucket — same convention). bucket_prefix per the
# audit-bucket precedent — no global bucket name committed. SSE-S3/AES256 is the
# ONLY server-side encryption ELB log delivery supports (SSE-KMS with a CMK is
# rejected by the delivery service), so AES256 here is a hard requirement, not a
# convenience.
resource "aws_s3_bucket" "alb_logs" {
  bucket_prefix = "${var.name_prefix}-alb-logs-"
  force_destroy = false

  tags = {
    Name = "${var.name_prefix}-alb-logs"
  }
}

resource "aws_s3_bucket_ownership_controls" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  versioning_configuration {
    status = "Enabled"
  }
}

# ELB-log-delivery-only writes + TLS-only access. The configured region
# (us-east-1) predates August 2022, so ALB logs are delivered by the REGIONAL
# ELB SERVICE ACCOUNT root principal — not the `logdelivery.elasticloadbalancing`
# service principal used by newer regions. The account is resolved at plan time
# via `aws_elb_service_account` (no AWS-owned account id literal is committed),
# mirroring the observability module's data-source pattern; our own account id
# comes from `aws_caller_identity` the same way.
data "aws_elb_service_account" "current" {}
data "aws_caller_identity" "current" {}

resource "aws_s3_bucket_policy" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ALBLogDeliveryWrite"
        Effect    = "Allow"
        Principal = { AWS = data.aws_elb_service_account.current.arn }
        Action    = "s3:PutObject"
        Resource = [
          "${aws_s3_bucket.alb_logs.arn}/alb-access/AWSLogs/${data.aws_caller_identity.current.account_id}/*",
          "${aws_s3_bucket.alb_logs.arn}/alb-connection/AWSLogs/${data.aws_caller_identity.current.account_id}/*",
        ]
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.alb_logs.arn,
          "${aws_s3_bucket.alb_logs.arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.alb_logs]
}

# --- API target group (IP targets; private API tasks on port 8000) ----------------
resource "aws_lb_target_group" "api" {
  count = var.enabled ? 1 : 0

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
  count = var.enabled ? 1 : 0

  load_balancer_arn = aws_lb.this[0].arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.api_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api[0].arn
  }

  tags = {
    Name = "${var.name_prefix}-https-listener"
  }
}

# --- API Route 53 alias record (P6-INF-3; windowed) --------------------------------
# One alias record, `api_fqdn` -> this ALB, inside the CONSUMED hosted zone (the same
# zone the edge module writes the web aliases into; the root rejects api_fqdn ==
# web_fqdn so the two modules can never contend for one record name). It shares the
# ALB's `count`, so:
#   * window OPEN  : the record is created AFTER the load balancer (it references the
#                    LB's dns_name/zone_id, which also makes a RE-CREATED ALB re-point
#                    the record automatically — no hand edit per window);
#   * window CLOSED: the record is destroyed BEFORE the load balancer; the hostname
#                    then resolves to nothing (no record for the name) and resolvers may
#                    keep the previous answer for up to the alias TTL (60 s, an AWS-
#                    documented value for ALB aliases, not derivable here).
#   * ALB REPLACEMENT inside a window (not a window transition): aws_lb.this has no
#                    create_before_destroy, so OpenTofu destroys the old ALB, creates the
#                    new one, THEN upserts this record — for that interval the name still
#                    answers with the destroyed ALB's DNS name. Only the open/close
#                    ordering above is guaranteed by the count; a replacement is not.
# Record type is `A` ONLY: the ALB is IPv4-only (`ip_address_type = "ipv4"`, §24.3),
# so an AAAA alias would advertise an address family the target cannot serve. The
# alias target uses the LB's exported canonical hosted-zone id, never a hard-coded
# regional ELB zone id. evaluate_target_health is false: a single record with no
# failover/weighted sibling gains nothing from health evaluation, and this keeps the
# API alias byte-for-byte in the same shape as the web aliases.
resource "aws_route53_record" "api" {
  count = var.enabled ? 1 : 0

  zone_id = var.hosted_zone_id
  # lower(): Route 53 stores record names case-folded, so a mixed-case input would
  # otherwise differ from the stored name after apply (and from the root's lower()ed
  # collision rule and api_url); DNS names are case-insensitive.
  name = lower(var.api_fqdn)
  type = "A"

  alias {
    name                   = aws_lb.this[0].dns_name
    zone_id                = aws_lb.this[0].zone_id
    evaluate_target_health = false
  }
}

# Staging window: these three resources gained `count` (docs/operations/staging-window.md).
# The moved blocks make the first plan from the pre-window state a deterministic rename
# (`has moved to [0]`) instead of an implied move; module-level only (the root forbids moved).
moved {
  from = aws_lb.this
  to   = aws_lb.this[0]
}

moved {
  from = aws_lb_target_group.api
  to   = aws_lb_target_group.api[0]
}

moved {
  from = aws_lb_listener.https
  to   = aws_lb_listener.https[0]
}
