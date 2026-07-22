# main.tf — foundational staging VPC network (INFRA-4 network module)
#
# Owns the foundational network plane for SIGNALNEST_STAGING: one VPC, an internet
# gateway, public and private subnets across the supplied AZs, a single
# cost-minimized NAT gateway, and route tables. No task, database, or cache node
# receives a public IP (runtime contract §D); public subnets do not auto-assign
# public IPs, and only the NAT/IGW provide egress/ingress paths.
#
# Deliberately NOT owned here (see README non-goals): security groups, VPC
# endpoints, and flow logs — their least-privilege rules reference peer resources
# owned by the still-unbuilt alb/ecs/data_sql/data_cache modules, so they are
# deferred to avoid cross-module coupling. No provider block is declared here; the
# module inherits the root AWS provider and its committed version lock.
#
# Tagging: the authoritative eight-tag common set is applied automatically to
# every taggable resource by the root provider's `default_tags` (providers.tf).
# This module therefore only adds the conventional per-resource `Name` tag and
# does not re-apply the common set (avoiding redundant/duplicated tags).

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = var.enable_dns_support
  enable_dns_hostnames = var.enable_dns_hostnames

  tags = {
    Name = "${var.name_prefix}-vpc"
  }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${var.name_prefix}-igw"
  }
}

# Public subnets — one per AZ, keyed by AZ name. No auto-assigned public IPs; the
# public tier hosts only the ALB/NAT/edge origin path, never a workload node.
resource "aws_subnet" "public" {
  for_each = local.public_subnets

  vpc_id                  = aws_vpc.this.id
  availability_zone       = each.value.availability_zone
  cidr_block              = each.value.cidr_block
  map_public_ip_on_launch = false

  tags = {
    Name = "${var.name_prefix}-public-${each.key}"
  }
}

# Private subnets — one per AZ, keyed by AZ name. Host ECS tasks, RDS, and
# ElastiCache; no public IP and no direct internet ingress.
resource "aws_subnet" "private" {
  for_each = local.private_subnets

  vpc_id            = aws_vpc.this.id
  availability_zone = each.value.availability_zone
  cidr_block        = each.value.cidr_block

  tags = {
    Name = "${var.name_prefix}-private-${each.key}"
  }
}

# Public routing: default route to the internet gateway.
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${var.name_prefix}-public-rt"
  }
}

resource "aws_route" "public_default" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  for_each = aws_subnet.public

  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

# Single NAT gateway (cost-minimized staging egress; contract §M "largest fixed
# driver"). One EIP + one NAT gateway placed in the first sorted public subnet.
resource "aws_eip" "nat" {
  count = var.enable_nat_gateway ? 1 : 0

  domain = "vpc"

  tags = {
    Name = "${var.name_prefix}-nat-eip"
  }

  depends_on = [aws_internet_gateway.this]
}

resource "aws_nat_gateway" "this" {
  count = var.enable_nat_gateway ? 1 : 0

  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[local.nat_az].id

  tags = {
    Name = "${var.name_prefix}-nat"
  }

  depends_on = [aws_internet_gateway.this]
}

# Private routing: a single shared route table (single-NAT staging topology). When
# egress is enabled, a default route points at the NAT gateway; otherwise there is
# no default route and the private tier is fully isolated.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${var.name_prefix}-private-rt"
  }
}

resource "aws_route" "private_default" {
  count = var.enable_nat_gateway ? 1 : 0

  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[0].id
}

resource "aws_route_table_association" "private" {
  for_each = aws_subnet.private

  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}

# Cross-variable capacity assertion. When vpc_cidr/availability_zones are unknown
# (e.g. at `tofu validate` with no supplied values) the condition is unknown and
# the check is skipped; with real values it fails fast if too many AZs are
# requested for the chosen subnet_newbits.
check "subnet_addressing_capacity" {
  assert {
    condition     = length(var.availability_zones) <= local.private_subnet_offset
    error_message = "availability_zones count exceeds the per-class subnet capacity for the chosen subnet_newbits; increase subnet_newbits or reduce the AZ count."
  }
}
