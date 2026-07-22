# main.tf — web/SPA edge plane (INFRA-4 edge module)
#
# Owns the public web edge for SIGNALNEST_STAGING: a PRIVATE S3 origin holding the
# compiled SPA, a CloudFront distribution serving it over HTTPS via Origin Access
# Control (SigV4), and web A/AAAA DNS aliases pointing at CloudFront. The SPA origin
# receives no public access; only this CloudFront distribution may read it.
#
# CONSUME, not create (aws-staging-iac-plan.md §23): the ACM certificate
# (var.acm_certificate_arn, us-east-1) and the Route 53 hosted zone
# (var.hosted_zone_id) are supplied by value — this module never creates, requests,
# validates, or imports them, and declares no ACM/route53-zone resource or data
# source. No provider block/alias is declared; the root AWS provider and its
# committed version lock are inherited.
#
# Deferred (see README non-goals): the ALB, its listeners/SGs/certificate, the API
# hostname and its Route 53 record, WAF, Lambda@Edge/CloudFront Functions, access
# logs, and any SPA object upload or CloudFront invalidation.
#
# Tagging: the authoritative eight-tag common set is applied automatically to every
# taggable resource by the root provider's `default_tags` (providers.tf). This
# module only adds the conventional per-resource `Name` tag.

# --- Private S3 SPA origin bucket -------------------------------------------------

# bucket_prefix (not a fixed name) avoids committing a real, globally-unique bucket
# name while keeping a deterministic prefix.
resource "aws_s3_bucket" "spa" {
  bucket_prefix = "${var.name_prefix}-spa-"

  tags = {
    Name = "${var.name_prefix}-spa-origin"
  }
}

# ACLs disabled; the bucket owner owns every object (OAC pattern needs no ACLs).
resource "aws_s3_bucket_ownership_controls" "spa" {
  bucket = aws_s3_bucket.spa.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Block all public access — the SPA is reachable only through CloudFront.
resource "aws_s3_bucket_public_access_block" "spa" {
  bucket = aws_s3_bucket.spa.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Server-side encryption at rest (SSE-S3/AES256; no committed KMS key reference).
resource "aws_s3_bucket_server_side_encryption_configuration" "spa" {
  bucket = aws_s3_bucket.spa.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "spa" {
  bucket = aws_s3_bucket.spa.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Lifecycle: reap noncurrent SPA versions and abort stale multipart uploads.
resource "aws_s3_bucket_lifecycle_configuration" "spa" {
  bucket = aws_s3_bucket.spa.id

  rule {
    id     = "expire-noncurrent-spa-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.spa]
}

# --- CloudFront Origin Access Control (SigV4, always sign) ------------------------
resource "aws_cloudfront_origin_access_control" "spa" {
  name                              = "${var.name_prefix}-spa-oac"
  description                       = "OAC for the ${var.name_prefix} private SPA origin"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# --- CloudFront distribution (SPA) ------------------------------------------------
resource "aws_cloudfront_distribution" "spa" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "${var.name_prefix} SPA distribution"
  default_root_object = "index.html"
  price_class         = var.price_class
  aliases             = [var.web_fqdn]

  origin {
    domain_name              = aws_s3_bucket.spa.bucket_regional_domain_name
    origin_id                = local.origin_id
    origin_access_control_id = aws_cloudfront_origin_access_control.spa.id
  }

  default_cache_behavior {
    target_origin_id       = local.origin_id
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    min_ttl                = 0
    default_ttl            = 3600
    max_ttl                = 86400

    # No forwarding of cookies, query strings, or arbitrary headers. The legacy
    # forwarded_values block expresses this without introducing a custom cache
    # policy (explicitly out of scope, §23).
    forwarded_values {
      query_string = false

      cookies {
        forward = "none"
      }
    }
  }

  # SPA client-side routing: serve index.html (HTTP 200) for origin 403/404 so deep
  # links resolve in the browser. No response is cached (error_caching_min_ttl 0).
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # TLS-only viewer access using the CONSUMED us-east-1 ACM certificate.
  viewer_certificate {
    acm_certificate_arn      = var.acm_certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  tags = {
    Name = "${var.name_prefix}-spa-cf"
  }
}

# --- Bucket policy: allow ONLY this CloudFront distribution to read the origin ----
resource "aws_s3_bucket_policy" "spa" {
  bucket = aws_s3_bucket.spa.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudFrontServicePrincipalReadOnly"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.spa.arn}/*"
        Condition = {
          StringEquals = {
            "AWS:SourceArn" = aws_cloudfront_distribution.spa.arn
          }
        }
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.spa]
}

# --- Web DNS aliases → CloudFront (records only; the zone is consumed) ------------
# Alias target uses CloudFront's exported hosted_zone_id, never a hard-coded global
# CloudFront zone id.
resource "aws_route53_record" "web_a" {
  zone_id = var.hosted_zone_id
  name    = var.web_fqdn
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.spa.domain_name
    zone_id                = aws_cloudfront_distribution.spa.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "web_aaaa" {
  zone_id = var.hosted_zone_id
  name    = var.web_fqdn
  type    = "AAAA"

  alias {
    name                   = aws_cloudfront_distribution.spa.domain_name
    zone_id                = aws_cloudfront_distribution.spa.hosted_zone_id
    evaluate_target_health = false
  }
}
