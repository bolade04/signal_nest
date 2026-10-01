# versions.tf — network module tool + provider requirements
#
# The child module declares no `provider "aws"` block (and no alias), so it composes
# under the root's single provider configuration. The provider is inherited from the root.
#
# WHY THIS MODULE DECLARES A VERSION CONSTRAINT (staging window, docs/operations/
# staging-window.md). It is one of the modules CI initialises STANDALONE to run an offline,
# fully mocked contract test (`tofu init -backend=false && tofu test` — here,
# window.tftest.hcl). Child-module lock files are gitignored by design (only the root
# `infra/aws/.terraform.lock.hcl` is authoritative), so a standalone init with no
# constraint has no lock to obey and resolves whatever the registry considers latest:
# wiring this suite into CI without the constraint reproduced the exact Gate 4N-I5 /
# 4N-I8 defect (the standalone init selected hashicorp/aws 6.66.0 while the toolchain
# contract requires exactly 6.55.0, and scripts/check_toolchain_integrity.py refused the
# cache — the check working). The rule from modules/iam/versions.tf applies: a module
# that CI initialises standalone MUST carry the constraint.
#
# The constraint below is BYTE-IDENTICAL to infra/aws/versions.tf and to
# modules/iam/versions.tf and modules/revision_reader/versions.tf. The intersection is
# unchanged for the composed path; the standalone path now resolves the same pinned
# provider without depending on any gitignored lock file or pre-existing local cache.
# If the root constraint changes, change it here in the same commit.
#
# Assurance note: on GitHub Linux runners the module cache is classified as a
# lockfile-verified platform (infra/aws/provider-binary-pin.json) — the checksum
# verification is the one `tofu init` performs against the registry during this
# standalone init, because child-module lock files are gitignored; the exact version
# is still enforced. Same reduced assurance as modules/iam and modules/revision_reader.

terraform {
  required_version = ">= 1.12.3, < 1.13.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.55.0, < 6.56.0"
    }
  }
}
