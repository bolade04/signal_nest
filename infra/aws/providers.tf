# providers.tf — default AWS provider (INFRA-4 skeleton, placeholder)
#
# Region is variable-driven; no credentials, profile, role ARN, assume-role
# block, account id, alias, or endpoint override is present. Authentication is
# supplied only at a later authorized tranche via the CI/OIDC deployment role —
# never committed here. This provider block has NOT been initialized.

provider "aws" {
  region = var.aws_region

  # Standard resource tags applied to every taggable resource composed under THIS
  # default provider. See locals.tf for the authoritative eight-tag set (runtime
  # contract §A). module.revision_reader composes under the aliased provider below
  # instead and receives the same set explicitly via its `tags` input.
  default_tags {
    tags = local.common_tags
  }
}

# Aliased provider WITHOUT default_tags, consumed only by module.revision_reader.
# The reader IAM roles are created out-of-band by the role-bootstrap executor whose
# reviewed grant permits exactly the tag key set {"Name"} (gen_role_bootstrap_policy
# ALLOWED_TAG_KEYS; trust_policies tags_expectation). Injecting the eight-tag common
# set through default_tags made adopting those roles plan an unauthorized TagRole
# drift (B-2 barrier REFUSE, 2026-08-15). The reader module therefore manages its
# tag surface explicitly: the three ROLES carry exactly {"Name": <role name>}, and
# every other reader resource receives the common set through the module's `tags`
# input (passed at the root), never implicitly.
provider "aws" {
  alias  = "revision_reader"
  region = var.aws_region
}
