# outputs.tf — non-sensitive bootstrap reference outputs
#
# Echo the caller-supplied names and derived ARNs the operator needs to fill
# the main root's git-ignored `backend.hcl` (see ../backend.hcl.example) after
# the later-authorized live bootstrap. No key material, credential, or secret
# is exposed; values exist only in the operator's local state, never in Git.

output "state_bucket_name" {
  description = "Name of the remote-state S3 bucket (backend.hcl `bucket`)."
  value       = aws_s3_bucket.state.bucket
}

output "state_bucket_arn" {
  description = "ARN of the remote-state S3 bucket."
  value       = aws_s3_bucket.state.arn
}

output "lock_table_name" {
  description = "Name of the DynamoDB state-lock table (backend.hcl `dynamodb_table`)."
  value       = aws_dynamodb_table.lock.name
}

output "state_kms_key_arn" {
  description = "ARN of the state-encryption CMK (backend.hcl `kms_key_id`)."
  value       = aws_kms_key.state.arn
}
