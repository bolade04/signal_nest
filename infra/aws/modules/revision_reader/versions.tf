# versions.tf — revision_reader module tool + provider requirements
#
# The child module declares no `provider "aws"` block, so it composes under the root's
# single provider configuration. It DOES declare a version constraint, and this module
# is the only one under modules/ that does.
#
# WHY THIS MODULE DIFFERS (Gate 4N-I5). The sibling modules are only ever initialized
# through the root, which owns the authoritative constraint and the committed
# `.terraform.lock.hcl`. This module is additionally initialized STANDALONE by CI to run
# its offline contract tests (`tofu init -backend=false && tofu test`), and child-module
# lock files are gitignored — so on a clean checkout that init had no constraint and no
# lock to obey, and resolved whatever the registry considered latest. Reproduced from a
# clean checkout during Gate 4N-I5: it selected 6.57.1, while the toolchain contract
# requires exactly 6.55.0. That made CI deterministically red and, because a policy test
# also classifies the resulting cache, it invalidated the policy suite along with it.
#
# The constraint below is BYTE-IDENTICAL to infra/aws/versions.tf. Keeping the two in
# lockstep is the point: the intersection is unchanged for the composed path, and the
# standalone path now resolves the same pinned provider without depending on any
# gitignored lock file or pre-existing local cache. If the root constraint is ever
# changed, change it here in the same commit.

terraform {
  required_version = ">= 1.12.3, < 1.13.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.55.0, < 6.56.0"
    }
  }
}
