# main.tf — composition root (INFRA-4)
#
# Wires the reusable modules under `infra/aws/modules/`. In THIS tranche exactly
# ONE module is active — `network` — with its foundational VPC resource bodies.
# The remaining eleven modules stay documentation-only stubs (README only, no
# HCL) and are NOT composed here yet. No `resource`, `data`, `import`, or `moved`
# block is declared at the root; all resources live inside the module.
#
# Planned module composition order / dependency flow (aws-staging-iac-plan.md
# §6 and §17); modules beyond `network` are authored under later, separately
# authorized INFRA-4 tranches:
#
#   1. network         — VPC, subnets, route tables, NAT           [ACTIVE]
#   2. edge            — Route53, ACM, CloudFront, S3 web origin    (stub)
#   3. alb             — ALB, HTTPS listeners, target groups        (stub; depends: network, edge)
#   4. iam             — least-privilege roles/policies             (stub; depends: network)
#   5. secrets         — Secrets Manager + KMS references (names)   (stub; depends: iam)
#   6. data_sql        — RDS PostgreSQL + pgvector                  (stub; depends: network)
#   7. data_cache      — ElastiCache Redis                          (stub; depends: network)
#   8. storage         — S3 application buckets                     (stub; depends: iam)
#   9. registry        — ECR repositories (immutable tags)          (stub)
#  10. ecs             — cluster, API/worker services, migration    (stub; depends: network, alb,
#                        iam, secrets, data_sql, data_cache, storage, registry)
#  11. observability   — CloudWatch log groups, alarms, CloudTrail  (stub; depends: ecs)
#  12. cost            — AWS Budgets (50/75/90/100%) + notifications (stub)
#
# `apply` is deferred to INFRA-9 under fresh authorization; nothing is provisioned
# by authoring these resource bodies.

# The eight-tag common set is applied to every taggable resource automatically by
# the provider's default_tags (providers.tf), so it is NOT passed into the module.
module "network" {
  source = "./modules/network"

  name_prefix        = local.name_prefix
  vpc_cidr           = var.vpc_cidr
  availability_zones = var.availability_zones
  subnet_newbits     = var.subnet_newbits
  enable_nat_gateway = var.enable_nat_gateway
}
