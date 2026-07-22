# main.tf — staging container registry plane (INFRA-4 registry module)
#
# Owns exactly TWO private Amazon ECR repositories for SIGNALNEST_STAGING — `api`
# and `worker` — and one safe lifecycle policy per repository (two policy instances).
# The runtime model is TWO IMAGES, TWO REPOSITORIES, THREE ACTORS (merged PR #103
# §26.5): the API task pins the API image digest; the worker task AND the one-shot
# migration task both pin the WORKER image digest (migration overrides the command).
# The two logical repositories are FIXED here (local.repositories) — callers cannot
# add a third repository.
#
# This module performs ONLY declarative registry-container creation. It builds no
# image, pushes no image, logs into no registry, requests no authorization token,
# creates no tag, resolves no digest, and queries no scan finding. Successful
# creation does NOT mean an image exists, passed scanning, or that ECS is ready.
#
# Dependency boundary (§26.12): ZERO upstream module dependencies. Downstream only,
# one-way: `registry -> iam` (iam scopes least-privilege pull/publish to the
# repository ARNs) and `registry -> ecs` (ecs pins a separately verified immutable
# digest per repository). This module consumes no iam/ecs output, so no
# iam->registry->iam or ecs->registry->ecs cycle is created.
#
# No `provider "aws"` block is declared; `versions.tf` declares only the provider
# SOURCE (no version constraint) per the network/edge/alb/secrets convention, so the
# root owns the sole provider config, version constraint, and committed lockfile,
# inherited here. Tagging is the root provider's `default_tags`; this module adds
# only the conventional per-resource `Name` tag. No secret value, account id, ARN,
# region, repository URL, image tag, or digest is committed. The Secrets Manager KMS
# key is NOT reused (ECR uses AES-256 SSE here).

locals {
  # The two FIXED logical repositories mapped to deterministic physical ECR paths.
  # Exactly `api` and `worker` — no third repository is possible.
  repositories = {
    api    = "${var.name_prefix}/api"
    worker = "${var.name_prefix}/worker"
  }
}

# --- Two private ECR repositories (api, worker) -----------------------------------
resource "aws_ecr_repository" "app" {
  for_each = local.repositories

  name                 = each.value
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = {
    Name = "${var.name_prefix}-${each.key}-ecr"
  }
}

# --- One safe lifecycle policy per repository (two instances) ----------------------
# Expires ONLY untagged images after N days; tagged (immutable release) images are
# never selected, so active deployment and rollback digests are preserved. No
# tagged-count/age expiration, no `latest` contract.
resource "aws_ecr_lifecycle_policy" "app" {
  for_each = aws_ecr_repository.app

  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after ${var.untagged_image_retention_days} days; tagged release images are preserved."
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = var.untagged_image_retention_days
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}
