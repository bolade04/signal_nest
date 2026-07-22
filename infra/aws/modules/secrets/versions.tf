# versions.tf — secrets module tool + provider requirements
#
# The child module declares ONLY the AWS provider SOURCE so it composes under the
# root's single provider configuration and the committed root dependency lock. It
# deliberately declares NO provider version constraint and NO `provider "aws"`
# block: the root module (infra/aws/versions.tf + providers.tf) owns the sole
# authoritative version constraint and the `.terraform.lock.hcl`, which this
# module must not override. The provider is inherited from the root. Mirrors the
# established network/edge/alb module convention.

terraform {
  required_version = ">= 1.12.3, < 1.13.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}
