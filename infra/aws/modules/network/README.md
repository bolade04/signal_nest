# Module: `network` (documentation-only stub)

## 1. Purpose
Foundational VPC networking for SIGNALNEST_STAGING: one dedicated VPC with public
and private subnets and controlled egress.

## 2. Planned AWS scope
VPC, public/private subnets, route tables, Internet Gateway, NAT Gateway, VPC
endpoints (e.g. ECR, S3, Secrets Manager, CloudWatch Logs), security groups.

## 3. Out of scope
ALB/listeners (`alb`), DNS/TLS (`edge`), IAM (`iam`), any compute or data node.

## 4. Planned upstream dependencies
None (foundational).

## 5. Planned inputs (names only, no values)
`vpc_cidr`, `az_count`, `public_subnet_cidrs`, `private_subnet_cidrs`,
`enable_nat_gateway`, `name_prefix`, `tags`.

## 6. Planned non-sensitive outputs (names only)
`vpc_id`, `public_subnet_ids`, `private_subnet_ids`, `app_security_group_id`,
`data_security_group_id`.

## 7. Security boundaries
No task, database, or cache node receives a public IP. Least-privilege security
groups; no `0.0.0.0/0` ingress except the public HTTPS entry points owned by
`alb`/`edge`. Addressing is variable-driven; no CIDR is committed.

## 8. Staging-only assumptions
Single VPC in `us-east-1`, single-AZ-tolerant sizing per the runtime contract §N.

## 9. Status
No executable HCL yet. No resources created. Implementation and validation are
deferred.

## 10. Owning tranche
Real resource bodies belong to the later, separately authorized INFRA-4 module
implementation tranche.
