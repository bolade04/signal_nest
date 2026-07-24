# providers.tf — default AWS provider for the one-time state-bootstrap root
#
# Region is variable-driven; no credentials, profile, role ARN, assume-role
# block, account id, alias, or endpoint override is present. Authentication is
# supplied only at the later-authorized live bootstrap execution — never
# committed here. This provider block has NOT been initialized against AWS.
#
# NOTE: this root deliberately declares NO `backend` block — it uses implicit
# LOCAL state for its own one-time run, because the S3 state bucket it creates
# cannot host its creator's state before it exists (see README.md). The main
# root's "state is always remote" rule applies to the environment composition,
# not to this bootstrap step.

provider "aws" {
  region = var.aws_region

  # The authoritative eight-tag set (runtime contract §A), identical to the main
  # root's provider default_tags.
  default_tags {
    tags = local.common_tags
  }
}
