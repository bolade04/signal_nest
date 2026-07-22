# main.tf — composition root (INFRA-4 skeleton, placeholder)
#
# This is the future composition root that will wire the twelve reusable modules
# under `infra/aws/modules/`. In THIS tranche it contains NO `module`, `resource`,
# `data`, `import`, or `moved` blocks — the modules are documentation-only stubs
# and no AWS resource is declared.
#
# Planned module composition order / dependency flow (aws-staging-iac-plan.md
# §6 and §17), authored under a later, separately authorized INFRA-4 module
# implementation tranche:
#
#   1. network         — VPC, subnets, route tables, NAT, VPC endpoints, SGs
#   2. edge            — Route53, ACM, CloudFront, S3 web origin
#   3. alb             — ALB, HTTPS listeners, target groups        (depends: network, edge)
#   4. iam             — least-privilege roles/policies             (depends: network)
#   5. secrets         — Secrets Manager + KMS references (names)   (depends: iam)
#   6. data_sql        — RDS PostgreSQL + pgvector                  (depends: network)
#   7. data_cache      — ElastiCache Redis                          (depends: network)
#   8. storage         — S3 application buckets                     (depends: iam)
#   9. registry        — ECR repositories (immutable tags)
#  10. ecs             — cluster, API/worker services, migration    (depends: network, alb,
#                        iam, secrets, data_sql, data_cache, storage, registry)
#  11. observability   — CloudWatch log groups, alarms, CloudTrail  (depends: ecs)
#  12. cost            — AWS Budgets (50/75/90/100%) + notifications
#
# No composition is performed here. Module bodies and their wiring are a later
# authorized tranche; `apply` is deferred to INFRA-9 under fresh authorization.
