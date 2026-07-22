# versions.tf — OpenTofu + provider compatibility constraints (INFRA-4 skeleton)
#
# Bounded compatibility ranges only. Exact dependency selection and the
# generation of `.terraform.lock.hcl` are DEFERRED to a separately authorized,
# tool-assisted validation tranche. This file was NOT produced by `tofu init`
# and no provider was downloaded to author it.

terraform {
  # OpenTofu is the authoritative IaC CLI (INFRA-4 decision). The constraint is
  # a bounded compatibility range, not an exact pin; the exact version is pinned
  # in the later tool-assisted tranche.
  required_version = ">= 1.12.3, < 1.13.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.55.0, < 6.56.0"
    }
  }
}
