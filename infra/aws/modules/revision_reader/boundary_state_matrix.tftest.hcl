# Gate 4N-I16 Phase D — ORTHOGONAL boundary-state matrix.
#
# WHY THIS FILE EXISTS. Gate 4N-I15 shipped a Stage-A guard keyed to
# `role_permissions_boundary_arn != null` while the roles consumed the MODE-derived value.
# Bootstrap-enabled + mode "disabled" + a syntactically valid ARN therefore passed every
# guard in the composition and produced an UNBOUNDED publisher role. The defect survived
# review not because no test covered "disabled" mode, but because EVERY "disabled" run in
# reader_contract.tftest.hcl also set the ARN to null. The two axes moved together, so the
# mode axis was never isolated and the ARN axis was doing all the work.
#
# THE RULE THIS FILE FOLLOWS: every run states bootstrap, mode and ARN INDEPENDENTLY and
# LITERALLY. No run derives one input from another, and no run inherits one of the three
# from the file-level defaults. A matrix whose inputs are correlated cannot distinguish
# which input a guard is actually reading — which is exactly how the defect was missed.
#
# Two failure targets appear below, and the difference is meaningful:
#   var.publication_bootstrap_enabled       -> a variable validation, fires before any
#                                              resource is evaluated
#   terraform_data.boundary_mode_precondition -> a resource precondition over the
#                                              authoritative state local

mock_provider "aws" {}

variables {
  # Everything the module needs that is NOT one of the three axes under test. The three
  # axes are deliberately ABSENT here so no run can silently inherit one.
  runtime_enabled              = false
  name_prefix                  = "signalnest-staging"
  aws_region                   = "us-east-1"
  vpc_id                       = "vpc-test"
  rds_security_group_id        = "sg-rds"
  database_url_secret_arn      = "arn:aws:secretsmanager:us-east-1:111122223333:secret:signalnest-staging/DATABASE_URL-bb"
  secrets_kms_key_arn          = "arn:aws:kms:us-east-1:111122223333:key/11111111-2222-3333-4444-555555555555"
  ecs_cluster_arn              = "arn:aws:ecs:us-east-1:111122223333:cluster/signalnest-staging-cluster"
  github_oidc_provider_arn     = "arn:aws:iam::111122223333:oidc-provider/token.actions.githubusercontent.com"
  github_repository            = "example-owner/example-repo"
  revision_reader_image_digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}

# =====================================================================================
# CELL 1 — bootstrap FALSE / mode "disabled" / ARN null
# The legitimate UNBOUNDED_DARK_STATE. Nothing is created, so no ceiling is required.
# =====================================================================================
run "m1_dark_no_bootstrap_disabled_mode_null_arn_is_the_valid_dark_state" {
  command = plan

  variables {
    publication_bootstrap_enabled = false
    role_boundary_mode            = "disabled"
    role_permissions_boundary_arn = null
  }

  assert {
    condition     = length(aws_iam_role.reader_publisher) == 0 && length(aws_ecr_repository.reader) == 0
    error_message = "the valid dark state must create no publisher role and no ECR repository."
  }
}

# =====================================================================================
# CELL 2 — bootstrap FALSE / mode "disabled" / ARN NON-NULL
# INVALID_PARTIAL_BOOTSTRAP. Reads as protected, deploys unbounded. Gate 4N-I15 had no
# check in this direction at all: only required+null was rejected.
# =====================================================================================
run "m2_disabled_mode_with_a_non_null_arn_is_an_incoherent_state" {
  command = plan

  variables {
    publication_bootstrap_enabled = false
    role_boundary_mode            = "disabled"
    role_permissions_boundary_arn = "arn:aws:iam::111122223333:policy/signalnest-staging-role-boundary"
  }

  expect_failures = [terraform_data.boundary_state_coherence]
}

# =====================================================================================
# CELL 3 — bootstrap FALSE / mode "required" / EXACT ARN
# BOUNDARY_ENFORCED without a bootstrap. Coherent: the mode governs roles that already
# exist, and creating nothing new is always permitted under an enforced ceiling.
# =====================================================================================
run "m3_enforced_mode_without_bootstrap_is_coherent" {
  command = plan

  variables {
    publication_bootstrap_enabled = false
    role_boundary_mode            = "required"
    role_permissions_boundary_arn = "arn:aws:iam::111122223333:policy/signalnest-staging-role-boundary"
  }

  assert {
    condition     = length(aws_iam_role.reader_publisher) == 0
    error_message = "an enforced ceiling with no bootstrap must still create nothing."
  }
}

# =====================================================================================
# CELL 4 — bootstrap TRUE / mode "disabled" / ARN null
# Bootstrapping from a dark state. The create is denied by IAM only AFTER the ECR
# resources exist, so it must be rejected at plan time.
# =====================================================================================
run "m4_bootstrap_from_dark_state_is_rejected" {
  command = plan

  variables {
    publication_bootstrap_enabled = true
    role_boundary_mode            = "disabled"
    role_permissions_boundary_arn = null
  }

  expect_failures = [var.publication_bootstrap_enabled]
}

# =====================================================================================
# CELL 5 — bootstrap TRUE / mode "disabled" / EXACT ARN
#
# *** THIS IS THE GATE 4N-I15 DEFECT-1 CELL. ***
#
# Under the superseded guard this combination passed EVERY check in the composition and
# produced `reader_publisher` with permissions_boundary = null. It is the one cell the old
# matrix could not express, because every "disabled" run it contained also nulled the ARN.
# =====================================================================================
run "m5_bootstrap_with_disabled_mode_and_a_valid_arn_is_rejected" {
  command = plan

  variables {
    publication_bootstrap_enabled = true
    role_boundary_mode            = "disabled"
    role_permissions_boundary_arn = "arn:aws:iam::111122223333:policy/signalnest-staging-role-boundary"
  }

  # BOTH axes reject this cell, independently: the Stage-A variable validation (bootstrap
  # requires "required" mode) and the state classifier (disabled + non-null ARN is
  # incoherent). Listing both is the accurate statement — the cell that shipped a live
  # partial-apply path is now closed twice over, and asserting only one would leave the
  # other free to rot unnoticed.
  expect_failures = [
    var.publication_bootstrap_enabled,
    terraform_data.boundary_state_coherence,
  ]
}

# =====================================================================================
# CELL 6 — bootstrap TRUE / mode "required" / ARN null
# Enforced mode cannot enforce anything without a ceiling to point at.
# =====================================================================================
run "m6_bootstrap_in_required_mode_with_a_null_arn_is_rejected" {
  command = plan

  variables {
    publication_bootstrap_enabled = true
    role_boundary_mode            = "required"
    role_permissions_boundary_arn = null
  }

  # Like cell 5, rejected independently twice: the Stage-A variable validation requires a
  # non-null ARN, and the state classifier rejects required+null outright.
  expect_failures = [
    var.publication_bootstrap_enabled,
    terraform_data.boundary_state_coherence,
  ]
}

# =====================================================================================
# CELL 7 — bootstrap TRUE / mode "required" / WRONG ARN
# A syntactically valid ARN naming some other policy is not distinguishable from the
# right one by shape, so the identity of the ceiling is checked by name.
# =====================================================================================
run "m7_bootstrap_with_a_wrong_boundary_policy_is_rejected" {
  command = plan

  variables {
    publication_bootstrap_enabled = true
    role_boundary_mode            = "required"
    role_permissions_boundary_arn = "arn:aws:iam::111122223333:policy/some-other-policy"
  }

  expect_failures = [terraform_data.boundary_mode_precondition]
}

# =====================================================================================
# CELL 9 — bootstrap FALSE / mode "required" / ARN null
#
# ISOLATES THE STATE CLASSIFIER. Cell 6 covers the same mode/ARN pair with bootstrap TRUE,
# but there the `publication_bootstrap_enabled` variable validation fires first — so cell 6
# passes even if the state classifier is broken, and the classifier is never the thing under
# test. This cell removes the bootstrap axis so the ONLY guard left is the state model.
#
# Found by the Phase E mutation harness: corrupting the classifier so required+null reads as
# BOUNDARY_ENFORCED left the whole suite green until this cell existed.
# =====================================================================================
run "m9_required_mode_with_a_null_arn_is_rejected_by_the_state_model_alone" {
  command = plan

  variables {
    publication_bootstrap_enabled = false
    role_boundary_mode            = "required"
    role_permissions_boundary_arn = null
  }

  expect_failures = [terraform_data.boundary_state_coherence]
}

# =====================================================================================
# CELL 8 — bootstrap TRUE / mode "required" / EXACT ARN
# The only cell in which a protected-role bootstrap may proceed. The publisher role must
# carry the ceiling, read from the DERIVED local rather than the raw variable.
# =====================================================================================
run "m8_bootstrap_in_enforced_mode_with_the_exact_arn_proceeds_bounded" {
  command = plan

  variables {
    publication_bootstrap_enabled = true
    role_boundary_mode            = "required"
    role_permissions_boundary_arn = "arn:aws:iam::111122223333:policy/signalnest-staging-role-boundary"
  }

  assert {
    condition     = length(aws_iam_role.reader_publisher) == 1
    error_message = "the only permitted bootstrap cell must actually create the publisher role."
  }

  # The assertion that would have caught Defect 1 had it existed: the role's boundary is
  # non-null. Cell 5 proves the same expression is unreachable under a disabled mode.
  assert {
    condition     = aws_iam_role.reader_publisher[0].permissions_boundary == var.role_permissions_boundary_arn
    error_message = "a bootstrapped publisher role must carry the exact reviewed boundary."
  }
}
