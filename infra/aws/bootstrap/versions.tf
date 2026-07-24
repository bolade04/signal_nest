# versions.tf — remote-state bootstrap root tool + provider requirements
#
# This is a STANDALONE one-time root (see README.md: it is NOT a second
# SIGNALNEST_STAGING environment composition), so unlike the child modules it
# owns its own provider version constraint and its own committed
# `.terraform.lock.hcl` (byte-identical to the main root's lock — same provider,
# same constraint range, same checksums).

terraform {
  required_version = ">= 1.12.3, < 1.13.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.55.0, < 6.56.0"
    }
  }
}
