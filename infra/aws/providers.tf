# providers.tf — default AWS provider (INFRA-4 skeleton, placeholder)
#
# Region is variable-driven; no credentials, profile, role ARN, assume-role
# block, account id, alias, or endpoint override is present. Authentication is
# supplied only at a later authorized tranche via the CI/OIDC deployment role —
# never committed here. This provider block has NOT been initialized.

provider "aws" {
  region = var.aws_region

  # Standard resource tags applied to every taggable resource once modules
  # exist. See locals.tf for the authoritative eight-tag set (runtime contract
  # §A). No module or resource is composed in this tranche, so no tag is applied
  # yet.
  default_tags {
    tags = local.common_tags
  }
}
