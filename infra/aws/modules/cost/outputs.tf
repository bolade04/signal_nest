# outputs.tf — non-sensitive budget references
#
# Exactly the planned interface pinned by this module's README: `budget_name`
# and `budget_id`. References only — no notification address, account identity,
# or spend data is exposed. Nothing downstream consumes these in the locked
# graph (§26.12: `cost` is independent); they exist for operator use and future
# root composition.

output "budget_name" {
  description = "Name of the monthly staging cost budget (<name_prefix>-monthly)."
  value       = aws_budgets_budget.monthly.name
}

output "budget_id" {
  description = "ID of the monthly staging cost budget (account:budget-name form as returned by the provider)."
  value       = aws_budgets_budget.monthly.id
}
