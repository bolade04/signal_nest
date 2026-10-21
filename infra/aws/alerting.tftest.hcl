# P6-INF-4 / P6-INF-5 / P6-INF-6 / P6-INF-13 alerting and retention contract — offline, fully
# mocked provider, no backend, no AWS call. Module-targeted runs (a `module` block) read the
# observability and alb modules' resources directly; root runs prove the wiring only the root
# can (the ALB alarms follow the window, the topic and every other alarm do not, the new root
# inputs are validated).
#
# WHAT THIS PROVES — and what it cannot:
#   * every alarm the observability module owns notifies its own SNS topic on ALARM and OK
#     (never []), the topic is encrypted with the module's alerts key, its policy admits
#     publishing only by this account's <name_prefix>-* alarms, and the human destination is
#     one e-mail subscription to the operator-supplied address;
#   * the three ALB alarms exist exactly while the windowed ALB does and use its suffixes;
#   * the API 5xx-ratio and p95-latency alarms read the EMF namespace and the exact dimension
#     sets apps/api/app/core/metrics_emf.py writes;
#   * the audit and ALB-log buckets expire objects (and noncurrent versions) after the
#     operator-supplied number of days, and no retention value is assumed.
#   It cannot prove delivery: AWS holds an e-mail subscription as PendingConfirmation until the
#   recipient confirms it, and only a real alarm transition in a live window proves a human
#   received it (E4).

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "111122223333"
    }
  }
  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }
  mock_data "aws_region" {
    defaults = {
      region = "us-east-1"
      name   = "us-east-1"
    }
  }
  mock_data "aws_elb_service_account" {
    defaults = {
      arn = "arn:aws:iam::111122223333:root"
    }
  }
  mock_resource "aws_kms_key" {
    defaults = {
      arn    = "arn:aws:kms:us-east-1:111122223333:key/11111111-1111-1111-1111-111111111111"
      key_id = "11111111-1111-1111-1111-111111111111"
    }
  }
  mock_resource "aws_sns_topic" {
    defaults = {
      arn = "arn:aws:sns:us-east-1:111122223333:signalnest-staging-alarms"
    }
  }
  mock_resource "aws_s3_bucket" {
    defaults = {
      id                          = "signalnest-staging-mock-bucket"
      arn                         = "arn:aws:s3:::signalnest-staging-mock-bucket"
      bucket_regional_domain_name = "signalnest-staging-mock-bucket.s3.us-east-1.amazonaws.com"
    }
  }
  # Root runs: sibling modules validate the identifiers they consume (see input_boundary).
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
      arn        = "arn:aws:elasticloadbalancing:us-east-1:111122223333:loadbalancer/app/signalnest-staging-alb/0123456789abcdef"
      arn_suffix = "app/signalnest-staging-alb/0123456789abcdef"
      dns_name   = "mock-alb-0123456789.us-east-1.elb.amazonaws.com"
      zone_id    = "ZSYNTHALBCANONICAL00"
    }
  }
  mock_resource "aws_lb_target_group" {
    defaults = {
      arn        = "arn:aws:elasticloadbalancing:us-east-1:111122223333:targetgroup/signalnest-staging-api-tg/fedcba9876543210"
      arn_suffix = "targetgroup/signalnest-staging-api-tg/fedcba9876543210"
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

# Root inputs (the synthetic values of tests/fixtures/root-wiring-synthetic.tfvars.example).
# Module runs override what they need; extra root values are ignored by a module run.
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
    api_5xx_percent               = 5
    api_p95_latency_ms            = 1000
    alb_5xx_count_per_period      = 10
  }
  alarm_notification_email     = "alerts@example.com"
  audit_log_retention_days     = 365
  alb_log_retention_days       = 90
  staging_window_active        = true
  mail_sending_identity_domain = "mail.synthetic.example.com"
  mail_from_address            = "no-reply@mail.synthetic.example.com"

  # observability module runs
  name_prefix = "signalnest-staging"
  log_group_names = {
    api       = "/ecs/signalnest-staging-api"
    worker    = "/ecs/signalnest-staging-worker"
    migration = "/ecs/signalnest-staging-migration"
  }
  log_group_arns = {
    api       = "arn:aws:logs:us-east-1:111122223333:log-group:/ecs/signalnest-staging-api"
    worker    = "arn:aws:logs:us-east-1:111122223333:log-group:/ecs/signalnest-staging-worker"
    migration = "arn:aws:logs:us-east-1:111122223333:log-group:/ecs/signalnest-staging-migration"
  }
  api_service_name    = "signalnest-staging-api"
  worker_service_name = "signalnest-staging-worker"
}

# --- observability: window open (ALB alarms present) --------------------------------------
run "every_alarm_notifies_the_encrypted_topic_and_one_email_subscription" {
  command = apply

  module {
    source = "./modules/observability"
  }

  variables {
    alb_alarms_enabled          = true
    alb_arn_suffix              = "app/signalnest-staging-alb/0123456789abcdef"
    api_target_group_arn_suffix = "targetgroup/signalnest-staging-api-tg/fedcba9876543210"
    app_metrics = {
      namespace   = "signalnest-staging/app"
      service     = "signalnest-api"
      environment = "staging"
    }
  }

  assert {
    condition = alltrue([
      for a in concat(
        values(aws_cloudwatch_metric_alarm.log_errors),
        values(aws_cloudwatch_metric_alarm.service_cpu_high),
        values(aws_cloudwatch_metric_alarm.service_memory_high),
        values(aws_cloudwatch_metric_alarm.capability),
        values(aws_cloudwatch_metric_alarm.alb),
        [aws_cloudwatch_metric_alarm.rds_cpu_high, aws_cloudwatch_metric_alarm.rds_storage_low,
          aws_cloudwatch_metric_alarm.rds_memory_low, aws_cloudwatch_metric_alarm.redis_cpu_high,
        aws_cloudwatch_metric_alarm.redis_memory_high],
        aws_cloudwatch_metric_alarm.api_5xx_ratio,
        aws_cloudwatch_metric_alarm.api_p95_latency,
      ) : a.alarm_actions == toset([aws_sns_topic.alarms.arn]) && a.ok_actions == toset([aws_sns_topic.alarms.arn])
    ])
    error_message = "every alarm must notify exactly the module's alarm topic on ALARM and on OK (P6-INF-4: never [])"
  }

  assert {
    condition     = length(output.alarm_arns) == 20
    error_message = "15 existing + 3 ALB + 2 application alarms are expected while the window is open"
  }

  assert {
    condition     = aws_sns_topic.alarms.name == "signalnest-staging-alarms" && aws_sns_topic.alarms.kms_master_key_id == aws_kms_key.alerts.arn
    error_message = "the alarm topic must be <name_prefix>-alarms, encrypted with the module's alerts key"
  }

  assert {
    condition = jsonencode(jsondecode(aws_sns_topic.alarms.policy)) == jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid       = "TopicOwnerAccountManagesThroughIam"
          Effect    = "Allow"
          Principal = { AWS = "arn:aws:iam::111122223333:root" }
          Action = [
            "SNS:AddPermission",
            "SNS:DeleteTopic",
            "SNS:GetTopicAttributes",
            "SNS:ListSubscriptionsByTopic",
            "SNS:Publish",
            "SNS:RemovePermission",
            "SNS:SetTopicAttributes",
            "SNS:Subscribe",
          ]
          Resource = "arn:aws:sns:us-east-1:111122223333:signalnest-staging-alarms"
        },
        {
          Sid       = "CloudWatchAlarmsOfThisDeploymentPublish"
          Effect    = "Allow"
          Principal = { Service = "cloudwatch.amazonaws.com" }
          Action    = "SNS:Publish"
          Resource  = "arn:aws:sns:us-east-1:111122223333:signalnest-staging-alarms"
          Condition = {
            ArnLike      = { "aws:SourceArn" = "arn:aws:cloudwatch:us-east-1:111122223333:alarm:signalnest-staging-*" }
            StringEquals = { "aws:SourceAccount" = "111122223333" }
          }
        },
      ]
    })
    error_message = "the topic policy must be exactly the account-root statement plus CloudWatch publishing for this account's <name_prefix>-* alarms"
  }

  assert {
    condition = jsonencode(jsondecode(aws_kms_key.alerts.policy)) == jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid       = "EnableIamPoliciesInThisAccount"
          Effect    = "Allow"
          Principal = { AWS = "arn:aws:iam::111122223333:root" }
          Action    = "kms:*"
          Resource  = "*"
        },
        {
          Sid       = "AllowCloudWatchAlarmsToPublishToTheEncryptedTopic"
          Effect    = "Allow"
          Principal = { Service = "cloudwatch.amazonaws.com" }
          Action    = ["kms:Decrypt", "kms:GenerateDataKey*"]
          Resource  = "*"
        },
      ]
    })
    error_message = "the alerts key policy must be exactly the account-root statement plus the CloudWatch statement AWS documents for encrypted topics"
  }

  assert {
    condition     = aws_kms_key.alerts.enable_key_rotation && !aws_kms_key.alerts.multi_region && aws_kms_key.alerts.deletion_window_in_days == 30 && !aws_kms_key.alerts.bypass_policy_lockout_safety_check && aws_kms_alias.alerts.name == "alias/signalnest-staging/alerts"
    error_message = "the alerts key must have the P6-CI-6 key shape and the alias alias/<name_prefix>/alerts"
  }

  assert {
    condition     = length(aws_sns_topic_subscription.alarm_email) == 1 && aws_sns_topic_subscription.alarm_email[0].protocol == "email" && aws_sns_topic_subscription.alarm_email[0].endpoint == "alerts@example.com" && aws_sns_topic_subscription.alarm_email[0].topic_arn == aws_sns_topic.alarms.arn
    error_message = "the human destination must be one e-mail subscription of the supplied address to the alarm topic"
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.alb["alb-elb-5xx"].metric_name == "HTTPCode_ELB_5XX_Count" &&
      aws_cloudwatch_metric_alarm.alb["alb-elb-5xx"].dimensions == tomap({ LoadBalancer = "app/signalnest-staging-alb/0123456789abcdef" }) &&
      aws_cloudwatch_metric_alarm.alb["alb-elb-5xx"].threshold == 10 &&
      aws_cloudwatch_metric_alarm.alb["alb-target-5xx"].metric_name == "HTTPCode_Target_5XX_Count" &&
      aws_cloudwatch_metric_alarm.alb["alb-target-5xx"].dimensions == tomap({ LoadBalancer = "app/signalnest-staging-alb/0123456789abcdef", TargetGroup = "targetgroup/signalnest-staging-api-tg/fedcba9876543210" }) &&
      aws_cloudwatch_metric_alarm.alb["alb-unhealthy-hosts"].metric_name == "UnHealthyHostCount" &&
      aws_cloudwatch_metric_alarm.alb["alb-unhealthy-hosts"].threshold == 1 &&
      aws_cloudwatch_metric_alarm.alb["alb-unhealthy-hosts"].statistic == "Maximum" &&
      alltrue([for a in values(aws_cloudwatch_metric_alarm.alb) : a.namespace == "AWS/ApplicationELB" && a.treat_missing_data == "notBreaching"])
    )
    error_message = "the three ALB alarms must watch ELB 5xx, target 5xx and unhealthy hosts on the supplied LoadBalancer/TargetGroup suffixes"
  }

  assert {
    condition = (
      [for q in aws_cloudwatch_metric_alarm.api_5xx_ratio[0].metric_query : q.expression if q.id == "ratio"] == ["100 * FILL(errors, 0) / requests"] &&
      [for q in aws_cloudwatch_metric_alarm.api_5xx_ratio[0].metric_query : q.metric[0].dimensions if q.id == "errors"] == [tomap({ service = "signalnest-api", environment = "staging", outcome = "server_error", status_class = "5xx" })] &&
      [for q in aws_cloudwatch_metric_alarm.api_5xx_ratio[0].metric_query : q.metric[0].dimensions if q.id == "requests"] == [tomap({ service = "signalnest-api", environment = "staging" })] &&
      alltrue([for q in aws_cloudwatch_metric_alarm.api_5xx_ratio[0].metric_query : q.metric[0].namespace == "signalnest-staging/app" && q.metric[0].metric_name == "http_requests_total" && q.metric[0].stat == "Sum" if q.id != "ratio"]) &&
      aws_cloudwatch_metric_alarm.api_5xx_ratio[0].threshold == 5
    )
    error_message = "the 5xx-ratio alarm must divide the EMF 5xx series by the EMF rollup series (alerts.md §1)"
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.api_p95_latency[0].namespace == "signalnest-staging/app" && aws_cloudwatch_metric_alarm.api_p95_latency[0].metric_name == "http_request_duration_ms" && aws_cloudwatch_metric_alarm.api_p95_latency[0].extended_statistic == "p95" && aws_cloudwatch_metric_alarm.api_p95_latency[0].dimensions == tomap({ service = "signalnest-api", environment = "staging" }) && aws_cloudwatch_metric_alarm.api_p95_latency[0].evaluation_periods == 2 && aws_cloudwatch_metric_alarm.api_p95_latency[0].threshold == 1000
    error_message = "the latency alarm must read p95 of the EMF rollup latency series over two 5-minute periods (alerts.md §2)"
  }

  assert {
    condition = (
      length(aws_s3_bucket_lifecycle_configuration.audit) == 1 &&
      [for r in aws_s3_bucket_lifecycle_configuration.audit[0].rule : [r.expiration[0].days, r.noncurrent_version_expiration[0].noncurrent_days] if r.id == "expire-audit-logs"] == [[365, 365]] &&
      [for r in aws_s3_bucket_lifecycle_configuration.audit[0].rule : r.expiration[0].expired_object_delete_marker if r.id == "remove-expired-delete-markers"] == [true] &&
      aws_s3_bucket_lifecycle_configuration.audit[0].bucket == aws_s3_bucket.audit.id
    )
    error_message = "the audit bucket must expire current and noncurrent versions after audit_log_retention_days and clean expired delete markers (P6-INF-13)"
  }
}

# --- observability: window closed (no ALB alarms; everything else unchanged) ---------------
run "closed_window_has_no_alb_alarm_and_every_other_alarm_still_notifies" {
  command = apply

  module {
    source = "./modules/observability"
  }

  variables {
    alb_alarms_enabled          = false
    alb_arn_suffix              = null
    api_target_group_arn_suffix = null
    app_metrics = {
      namespace   = "signalnest-staging/app"
      service     = "signalnest-api"
      environment = "staging"
    }
  }

  assert {
    condition     = length(aws_cloudwatch_metric_alarm.alb) == 0 && length(output.alarm_arns) == 17 && !anytrue([for k in keys(output.alarm_arns) : startswith(k, "alb-")])
    error_message = "with the window closed no ALB alarm may exist; the other 17 alarms persist"
  }

  assert {
    condition     = alltrue([for a in values(aws_cloudwatch_metric_alarm.log_errors) : a.alarm_actions == toset([aws_sns_topic.alarms.arn])]) && aws_cloudwatch_metric_alarm.rds_storage_low.alarm_actions == toset([aws_sns_topic.alarms.arn])
    error_message = "closing the window must not change any other alarm's actions"
  }
}

# --- observability: the module defaults create no subscription, retention rule or app alarm --
run "module_defaults_assume_no_address_retention_or_namespace_but_never_notify_nobody" {
  command = apply

  module {
    source = "./modules/observability"
  }

  variables {
    alarm_notification_email = null
    audit_log_retention_days = null
  }

  assert {
    condition     = length(aws_sns_topic_subscription.alarm_email) == 0 && length(aws_s3_bucket_lifecycle_configuration.audit) == 0 && length(aws_cloudwatch_metric_alarm.api_5xx_ratio) == 0 && length(aws_cloudwatch_metric_alarm.api_p95_latency) == 0
    error_message = "with no address, retention or namespace supplied the module must not invent one"
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.rds_storage_low.alarm_actions == toset([aws_sns_topic.alarms.arn])
    error_message = "even without a subscription every alarm notifies the topic (the root makes the subscription mandatory)"
  }
}

run "alb_alarms_without_suffixes_are_refused" {
  command = plan

  module {
    source = "./modules/observability"
  }

  variables {
    alb_alarms_enabled          = true
    alb_arn_suffix              = null
    api_target_group_arn_suffix = null
  }

  expect_failures = [aws_cloudwatch_metric_alarm.alb]
}

run "an_aws_namespace_for_application_metrics_is_refused" {
  command = plan

  module {
    source = "./modules/observability"
  }

  variables {
    app_metrics = {
      namespace   = "AWS/ApplicationELB"
      service     = "signalnest-api"
      environment = "staging"
    }
  }

  expect_failures = [var.app_metrics]
}

# --- alb: log-bucket retention persists across windows; suffix outputs follow the window ----
run "alb_log_bucket_expires_after_the_supplied_days_even_while_closed" {
  command = apply

  module {
    source = "./modules/alb"
  }

  variables {
    vpc_id             = "vpc-0123456789abcdef0"
    public_subnet_ids  = ["subnet-0123456789abcdef1", "subnet-0123456789abcdef2"]
    enabled            = false
    log_retention_days = 90
  }

  assert {
    condition = (
      length(aws_s3_bucket_lifecycle_configuration.alb_logs) == 1 &&
      [for r in aws_s3_bucket_lifecycle_configuration.alb_logs[0].rule : [r.expiration[0].days, r.noncurrent_version_expiration[0].noncurrent_days] if r.id == "expire-alb-logs"] == [[90, 90]] &&
      [for r in aws_s3_bucket_lifecycle_configuration.alb_logs[0].rule : r.expiration[0].expired_object_delete_marker if r.id == "remove-expired-delete-markers"] == [true]
    )
    error_message = "the ALB log bucket must expire current and noncurrent versions after log_retention_days, window open or closed (P6-INF-13)"
  }

  assert {
    condition     = output.alb_enabled == false && output.alb_arn_suffix == null && output.api_target_group_arn_suffix == null
    error_message = "while closed the alarm inputs must say so: no ALB, null suffixes"
  }
}

run "alb_suffix_outputs_exist_while_open" {
  command = apply

  module {
    source = "./modules/alb"
  }

  variables {
    vpc_id             = "vpc-0123456789abcdef0"
    public_subnet_ids  = ["subnet-0123456789abcdef1", "subnet-0123456789abcdef2"]
    enabled            = true
    log_retention_days = 90
  }

  assert {
    condition     = output.alb_enabled && output.alb_arn_suffix == "app/signalnest-staging-alb/0123456789abcdef" && output.api_target_group_arn_suffix == "targetgroup/signalnest-staging-api-tg/fedcba9876543210"
    error_message = "while open the module must export the LoadBalancer and TargetGroup dimension values"
  }
}

run "alb_log_retention_must_be_whole_days_in_range" {
  command = plan

  module {
    source = "./modules/alb"
  }

  variables {
    vpc_id             = "vpc-0123456789abcdef0"
    public_subnet_ids  = ["subnet-0123456789abcdef1", "subnet-0123456789abcdef2"]
    log_retention_days = 0
  }

  expect_failures = [var.log_retention_days]
}

# --- root: the window reaches the ALB alarms only ------------------------------------------
run "root_open_window_adds_the_three_alb_alarms_to_the_always_on_set" {
  command = plan

  # The network module is overridden as a whole (distinct subnet ids; see input_boundary).
  override_module {
    target = module.network
    outputs = {
      vpc_id             = "vpc-0123456789abcdef0"
      public_subnet_ids  = ["subnet-0123456789abcdef1", "subnet-0123456789abcdef2"]
      private_subnet_ids = ["subnet-0123456789abcdef3", "subnet-0123456789abcdef4"]
    }
  }

  variables {
    staging_window_active = true
  }

  assert {
    condition     = length(output.alarm_names) == 20 && alltrue([for k in ["alb-elb-5xx", "alb-target-5xx", "alb-unhealthy-hosts", "api-5xx-ratio", "api-p95-latency", "rds-storage-low"] : contains(output.alarm_names, k)])
    error_message = "with the window open the root must plan the 15 existing, 2 application and 3 ALB alarms"
  }

  assert {
    condition     = output.app_metrics_namespace == "signalnest-staging/app"
    error_message = "the EMF namespace is <name_prefix>/app (the ecs environment's METRICS_NAMESPACE and the alarms' namespace)"
  }
}

run "root_closed_window_keeps_every_alarm_but_the_alb_ones" {
  command = plan

  # The network module is overridden as a whole (distinct subnet ids; see input_boundary).
  override_module {
    target = module.network
    outputs = {
      vpc_id             = "vpc-0123456789abcdef0"
      public_subnet_ids  = ["subnet-0123456789abcdef1", "subnet-0123456789abcdef2"]
      private_subnet_ids = ["subnet-0123456789abcdef3", "subnet-0123456789abcdef4"]
    }
  }

  variables {
    staging_window_active = false
  }

  assert {
    condition     = length(output.alarm_names) == 17 && !anytrue([for k in output.alarm_names : startswith(k, "alb-")]) && contains(output.alarm_names, "api-5xx-ratio")
    error_message = "with the window closed only the three ALB alarms may disappear"
  }
}

run "root_rejects_a_malformed_alarm_address" {
  command = plan

  # The network module is overridden as a whole (distinct subnet ids; see input_boundary).
  override_module {
    target = module.network
    outputs = {
      vpc_id             = "vpc-0123456789abcdef0"
      public_subnet_ids  = ["subnet-0123456789abcdef1", "subnet-0123456789abcdef2"]
      private_subnet_ids = ["subnet-0123456789abcdef3", "subnet-0123456789abcdef4"]
    }
  }

  variables {
    alarm_notification_email = "not-an-address"
  }

  expect_failures = [var.alarm_notification_email]
}

run "root_rejects_a_zero_day_audit_retention" {
  command = plan

  # The network module is overridden as a whole (distinct subnet ids; see input_boundary).
  override_module {
    target = module.network
    outputs = {
      vpc_id             = "vpc-0123456789abcdef0"
      public_subnet_ids  = ["subnet-0123456789abcdef1", "subnet-0123456789abcdef2"]
      private_subnet_ids = ["subnet-0123456789abcdef3", "subnet-0123456789abcdef4"]
    }
  }

  variables {
    audit_log_retention_days = 0
  }

  expect_failures = [var.audit_log_retention_days]
}

run "root_rejects_a_fractional_alb_log_retention" {
  command = plan

  # The network module is overridden as a whole (distinct subnet ids; see input_boundary).
  override_module {
    target = module.network
    outputs = {
      vpc_id             = "vpc-0123456789abcdef0"
      public_subnet_ids  = ["subnet-0123456789abcdef1", "subnet-0123456789abcdef2"]
      private_subnet_ids = ["subnet-0123456789abcdef3", "subnet-0123456789abcdef4"]
    }
  }

  variables {
    alb_log_retention_days = 1.5
  }

  expect_failures = [var.alb_log_retention_days]
}
