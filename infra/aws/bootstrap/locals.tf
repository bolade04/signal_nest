# locals.tf — deterministic naming + the authoritative eight-tag set
#
# Mirrors the main root's locals.tf EXACTLY (runtime contract §A). Every value
# is non-sensitive and variable-driven; no timestamp, commit SHA, account id, or
# other mutable runtime value is embedded.

locals {
  # Deterministic, lowercase staging name prefix (e.g. "signalnest-staging").
  name_prefix = "${lower(var.project_name)}-${var.environment}"

  # Authoritative eight-tag set — keys and semantics per runtime contract §A.
  # Do not add, rename, or drop keys without updating the runtime contract.
  common_tags = {
    Project     = var.project_name # Project=SignalNest
    Environment = var.environment  # Environment=staging
    Alias       = var.alias        # Alias=SIGNALNEST_STAGING
    Owner       = var.owner        # Owner=<internal-team-logical>
    CostCenter  = var.cost_center  # CostCenter=<logical>
    Phase       = var.phase        # Phase=4B-C
    DataClass   = var.data_class   # DataClass=internal-no-customer
    ManagedBy   = "iac"            # ManagedBy=iac
  }
}
