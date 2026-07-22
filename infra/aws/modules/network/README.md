# Module: `network`

## 1. Purpose
Foundational VPC networking for SIGNALNEST_STAGING: one dedicated VPC with public
and private subnets across the supplied availability zones and a single
cost-minimized NAT gateway for controlled outbound egress. This module defines
resource bodies but **applies nothing** — no infrastructure exists in AWS.

## 2. Owned resources (implemented here)
- `aws_vpc` (DNS support + DNS hostnames enabled)
- `aws_internet_gateway`
- `aws_subnet` public — one per AZ, **no** auto-assigned public IPs
- `aws_subnet` private — one per AZ
- `aws_eip` + `aws_nat_gateway` — a **single** NAT gateway (when `enable_nat_gateway`)
- `aws_route_table` / `aws_route` / `aws_route_table_association` — public (→ IGW)
  and a shared private route table (→ NAT when egress is enabled)

## 3. Deferred / explicit non-goals (NOT in this module)
- **Security groups** — their least-privilege rules (ALB→API 8000, API/worker→RDS,
  →Redis) reference peer resources owned by the `alb`/`ecs`/`data_sql`/`data_cache`
  modules, which do not exist yet. Deferred to avoid cross-module coupling and
  ownership overlap.
- **VPC endpoints** (S3 gateway; interface endpoints for ECR/Secrets Manager/
  CloudWatch Logs) — a NAT-cost optimization requiring an endpoint security group
  and interface-service selection. Traffic functions via the NAT path without
  them; deferred to a follow-on tranche.
- **VPC flow logs** — not assigned to this module by the authoritative plan
  (observability is the `observability` module).
- ALB/listeners (`alb`), DNS/TLS (`edge`), IAM (`iam`), and any compute or data
  node (`ecs`/`data_sql`/`data_cache`).

## 4. Upstream dependencies
None (foundational).

## 5. Inputs
| Name | Type | Default | Notes |
| --- | --- | --- | --- |
| `name_prefix` | string | — (required) | Deterministic name prefix from the root. |
| `vpc_cidr` | string | — (required) | VPC IPv4 CIDR; no real CIDR committed; /16–/24. |
| `availability_zones` | list(string) | — (required) | Explicit AZ names; supplied, **not** discovered via a data source. |
| `subnet_newbits` | number | `4` | Bits added to `vpc_cidr` per subnet; supports up to `2^(subnet_newbits-1)` AZs per class. |
| `enable_nat_gateway` | bool | `true` | Single NAT gateway + private default route when true. |
| `enable_dns_support` | bool | `true` | VPC DNS resolution. |
| `enable_dns_hostnames` | bool | `true` | VPC DNS hostnames. |

The module takes **no** `tags` input: the authoritative eight-tag common set is
applied to every taggable resource automatically by the root provider's
`default_tags` (`providers.tf`). This module adds only the conventional
per-resource `Name` tag, avoiding redundant/duplicated tags.

Subnet CIDRs are derived deterministically from `vpc_cidr` via `cidrsubnet()`:
the lower half of the derived blocks are public subnets and the upper half are
private subnets, keyed by (sorted) AZ name so identity never depends on input
order.

## 6. Non-sensitive outputs
`vpc_id`, `vpc_cidr_block`, `availability_zones`, `public_subnet_ids`,
`private_subnet_ids`, `public_route_table_id`, `private_route_table_id`.

## 7. Security boundaries
No task, database, or cache node receives a public IP (`map_public_ip_on_launch`
is `false` on public subnets). Addressing is variable-driven; no CIDR is
committed. Least-privilege security groups and endpoint controls are owned by the
consuming modules / a later tranche (§3).

## 8. Staging-only assumptions
Single VPC in `us-east-1`; a **single** NAT gateway (cost-minimized, contract §M
"largest fixed driver"); single-AZ is acceptable (runtime contract §N) though
`>= 2` AZs are recommended so later ALB/RDS subnet groups can be created.

## 9. Status
Resource bodies authored and validated offline only. **No `tofu plan`/`apply`,
no AWS API call, no state, and no provisioning have occurred. Nothing exists in
AWS.**

## 10. Owning tranche
INFRA-4 network resource-definition tranche. Remote-state bootstrap and any
`apply` remain later, separately authorized tranches (`apply` is INFRA-9).
