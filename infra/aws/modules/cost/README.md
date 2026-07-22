# Module: `cost` (documentation-only stub)

## 1. Purpose
Structural cost guardrails so overspend alerts exist before any spend begins.

## 2. Planned AWS scope
AWS Budgets with 50/75/90/100% thresholds and budget notifications (SNS/email
targets referenced by name).

## 3. Out of scope
CloudWatch service alarms (`observability`), any resource sizing decision (owned
by each resource module).

## 4. Planned upstream dependencies
None (references a notification target).

## 5. Planned inputs (names only, no values)
`monthly_budget_limit`, `threshold_percentages`, `notification_target`,
`name_prefix`, `tags`.

## 6. Planned non-sensitive outputs (names only)
`budget_name`, `budget_id` (reference).

## 7. Security boundaries
Hard ceiling **$200/month** (ADR-0001 §M). A fresh dated estimate is mandatory
before any authorized `apply` (INFRA-9). Cost reductions never weaken a security
control; if a projected estimate exceeds $200 without weakening a control, the
rule is STOP and reauthorize. No account id or notification address committed.

## 8. Staging-only assumptions
Single staging budget; thresholds fixed at 50/75/90/100%.

## 9. Status
No executable HCL yet. No resources created. Implementation and validation are
deferred.

## 10. Owning tranche
Real resource bodies belong to the later, separately authorized INFRA-4 module
implementation tranche; the fresh dated estimate is required at INFRA-9.
