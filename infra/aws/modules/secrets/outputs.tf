# outputs.tf — non-sensitive secret/KMS references for downstream modules
#
# ARNs, names, and key identifiers are configuration REFERENCES, not secret values,
# so they are not marked sensitive. No secret value, secret version, ciphertext,
# credential, account identity, or key-policy JSON is exposed. `iam` consumes the
# secret ARNs + `kms_key_arn` for identity policies; `ecs` consumes the secret ARNs
# for task-definition `secrets` valueFrom injection (one-way `secrets -> iam`,
# `secrets -> ecs`).

output "secret_arns" {
  description = "Map of the four logical secret keys (SECRET_KEY/DATABASE_URL/REDIS_URL/LLM_API_KEY) -> Secrets Manager container ARN. References only; contains no secret value."
  value       = { for k, s in aws_secretsmanager_secret.app : k => s.arn }
}

output "secret_names" {
  description = "Map of the four logical secret keys -> Secrets Manager container name (<name_prefix>/<KEY>)."
  value       = { for k, s in aws_secretsmanager_secret.app : k => s.name }
}

output "kms_key_arn" {
  description = "ARN of the customer-managed KMS key that encrypts the four secret containers (consumed by iam for kms:Decrypt scoping)."
  value       = aws_kms_key.secrets.arn
}

output "kms_key_id" {
  description = "Key id of the secrets customer-managed KMS key."
  value       = aws_kms_key.secrets.key_id
}

output "kms_alias_name" {
  description = "Deterministic KMS alias name for the secrets CMK (alias/<name_prefix>/secrets)."
  value       = aws_kms_alias.secrets.name
}
