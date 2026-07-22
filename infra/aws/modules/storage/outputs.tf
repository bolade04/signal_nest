# outputs.tf — non-sensitive storage references for downstream modules
#
# The bucket name and ARN are configuration REFERENCES, not secret values, so they
# are not marked sensitive. No credential, access key, policy JSON, object key, or
# full resource object is exposed. The ARN is consumed by the future `iam` module to
# scope the task-role S3 identity policy (storage -> iam); the name is passed to the
# application by the future ECS/root-composition tranche.

output "bucket_name" {
  description = "Name of the private application S3 bucket. Passed to the application (as S3_BUCKET) by the future ECS/root-composition tranche."
  value       = aws_s3_bucket.this.bucket
}

output "bucket_arn" {
  description = "ARN of the private application S3 bucket. Consumed by the future iam module to scope the least-privilege task-role S3 identity policy."
  value       = aws_s3_bucket.this.arn
}
