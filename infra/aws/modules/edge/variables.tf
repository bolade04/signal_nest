# variables.tf — typed inputs for the staging web/SPA edge module
#
# All addressing/identity is variable-driven. NO real domain, hosted-zone id,
# certificate ARN, account id, or secret has a committed default. `web_fqdn`,
# `hosted_zone_id`, and `acm_certificate_arn` are REQUIRED (no default). All
# validations are STATIC (regex/string) — none queries AWS.
#
# Ownership decision (aws-staging-iac-plan.md §23): the ACM certificate and the
# Route 53 hosted zone are CONSUMED by value, never created by this module.

variable "name_prefix" {
  description = "Deterministic, lowercase resource name prefix from the root (e.g. \"signalnest-staging\")."
  type        = string

  validation {
    condition     = length(trimspace(var.name_prefix)) > 0
    error_message = "name_prefix must be a non-empty string."
  }
}

variable "web_fqdn" {
  description = "One complete web FQDN for the SPA (e.g. \"app.staging.example.com\"). Supplied at apply time; no real domain is committed. Must be a bare hostname with no scheme, path, query, fragment, or empty labels."
  type        = string

  # Reject scheme (contains \"://\" or \":\"), path (\"/\"), query (\"?\"), and
  # fragment (\"#\") explicitly for a clear error, then require a well-formed,
  # multi-label FQDN with no empty labels and a valid TLD.
  validation {
    condition     = !can(regex("[/?#:]", var.web_fqdn))
    error_message = "web_fqdn must be a bare hostname: no scheme (https://), port, path, query string, or fragment."
  }

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$", lower(var.web_fqdn)))
    error_message = "web_fqdn must be a valid multi-label FQDN (lowercase labels 1-63 chars, no empty labels, no trailing dot), e.g. app.staging.example.com."
  }

  validation {
    condition     = length(var.web_fqdn) <= 253
    error_message = "web_fqdn must be <= 253 characters."
  }
}

variable "hosted_zone_id" {
  description = "Existing Route 53 hosted-zone id that owns web_fqdn (CONSUMED, never created). Supplied at apply time; no real id is committed. Statically validated only — never queried against AWS."
  type        = string

  validation {
    condition     = can(regex("^Z[A-Z0-9]{1,32}$", var.hosted_zone_id))
    error_message = "hosted_zone_id must be a plausible Route 53 hosted-zone id (starts with 'Z', uppercase alphanumeric)."
  }
}

variable "acm_certificate_arn" {
  description = "Existing CloudFront ACM certificate ARN (CONSUMED, never created/validated). MUST be in us-east-1 (CloudFront requirement). Supplied at apply time; no real ARN is committed. Statically validated only — never queried against AWS."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-zA-Z-]*:acm:us-east-1:[0-9]{12}:certificate/.+$", var.acm_certificate_arn))
    error_message = "acm_certificate_arn must be an ACM certificate ARN in us-east-1 (arn:aws:acm:us-east-1:<account>:certificate/<id>)."
  }
}

variable "price_class" {
  description = "CloudFront distribution price class. PriceClass_100 (cheapest, NA+EU) is the cost-minimized staging default."
  type        = string
  default     = "PriceClass_100"

  validation {
    condition     = contains(["PriceClass_100", "PriceClass_200", "PriceClass_All"], var.price_class)
    error_message = "price_class must be one of PriceClass_100, PriceClass_200, PriceClass_All."
  }
}
