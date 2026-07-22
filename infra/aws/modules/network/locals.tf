# locals.tf — deterministic AZ ordering and subnet CIDR derivation
#
# Subnet CIDRs are derived solely from the supplied vpc_cidr and the documented
# subnet_newbits scheme; no CIDR is committed. Resource identity is keyed by AZ
# name (never by list index), and the AZ list is sorted so the derived CIDR for a
# given AZ never changes when the input list is reordered.

locals {
  # Order-independent AZ list. Sorting makes both the resource keys and the
  # per-AZ CIDR assignment deterministic regardless of input order.
  azs = sort(var.availability_zones)

  # Reserve the lower half of the derived blocks for public subnets and the upper
  # half for private subnets so the two classes never overlap. With
  # subnet_newbits = 4 this is 16 blocks: public netnums 0..7, private 8..15
  # (up to 8 AZs per class).
  private_subnet_offset = floor(pow(2, var.subnet_newbits - 1))

  # Per-AZ public subnets, keyed by AZ name; netnum uses the sorted-AZ index.
  public_subnets = {
    for idx, az in local.azs : az => {
      availability_zone = az
      cidr_block        = cidrsubnet(var.vpc_cidr, var.subnet_newbits, idx)
    }
  }

  # Per-AZ private subnets, keyed by AZ name; netnum is offset into the upper half.
  private_subnets = {
    for idx, az in local.azs : az => {
      availability_zone = az
      cidr_block        = cidrsubnet(var.vpc_cidr, var.subnet_newbits, idx + local.private_subnet_offset)
    }
  }

  # Deterministic AZ (first sorted) that hosts the single NAT gateway.
  nat_az = local.azs[0]
}
