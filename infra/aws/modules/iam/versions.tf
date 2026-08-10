# versions.tf — iam module tool + provider requirements
#
# The child module declares no `provider "aws"` block, so it composes under the root's
# single provider configuration.
#
# WHY THIS MODULE DECLARES A VERSION CONSTRAINT (Gate 4N-I8). It is the SECOND module,
# after revision_reader, that CI initialises STANDALONE to run offline contract tests
# (`tofu init -backend=false && tofu test`) — here, boundary_durability.tftest.hcl. Child
# module lock files are gitignored, so a standalone init with no constraint has no lock to
# obey and resolves whatever the registry considers latest.
#
# This is not hypothetical: adding the test in Gate 4N-I8 immediately reproduced the exact
# Gate 4N-I5 defect. The standalone init selected hashicorp/aws 6.57.1 while the toolchain
# contract requires exactly 6.55.0, and scripts/check_toolchain_integrity.py failed the
# cache — which is precisely the check working. The rule is now explicit: a module that CI
# initialises standalone MUST carry the constraint.
#
# The constraint below is BYTE-IDENTICAL to infra/aws/versions.tf and to
# modules/revision_reader/versions.tf. The intersection is unchanged for the composed path;
# the standalone path now resolves the same pinned provider without depending on any
# gitignored lock file or pre-existing local cache. If the root constraint changes, change
# it here in the same commit.

terraform {
  required_version = ">= 1.12.3, < 1.13.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.55.0, < 6.56.0"
    }
  }
}
