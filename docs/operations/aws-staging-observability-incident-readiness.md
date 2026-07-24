# AWS staging observability, audit, evidence, and incident readiness (Phase 4B-C · INFRA-7)

- **Status:** DEFINITIONS/PROCEDURES + IaC definitions only. Per the INFRA-7
  entry in [phase-4b-c-infra-plan.md](../phase-4b-c-infra-plan.md): expected
  external resources are **"none created here (defined for INFRA-9 apply)"**
  and the stop boundary is **"This must complete before the canary. No
  override created."** Authoring and merging this tranche creates nothing in
  AWS, runs nothing live, creates no identity, and enables no capability. All
  five flags remain `False`.
- **Authoritative parents:**
  [aws-staging-iac-plan.md](./aws-staging-iac-plan.md) (§26.9 observability
  ownership), [aws-staging-operational-procedures.md](./aws-staging-operational-procedures.md)
  (INFRA-6 — rotation/restore **mechanics** this document decides *when* to
  invoke), [aws-staging-runtime-contract.md](./aws-staging-runtime-contract.md)
  (observer access shape; audit retention "set at INFRA-7"),
  [deployment-sha-wiring-plan.md](./deployment-sha-wiring-plan.md) §G (evidence
  rules), the merged `observability` module contract, and the
  [4B-B evidence template](../verification/4b-b-feedback-canary.md).
- **Relationship to the Phase-3A runbooks:** `observability.md`, `alerts.md`,
  `dashboards.md`, and `incident_response.md` describe the general/local-stack
  posture and remain valid background. This document adds ONLY the
  AWS-staging/canary-specific layer and does not restate them.
- **Sanitization rule (public repository):** no tenant/workspace UUID, account
  id, ARN-with-account, endpoint, personal name, or personal email may appear
  in this document or in any committed evidence. Incident contacts are ROLES.

## 1. Canary observability map (what is watched, and where)

The canary's authoritative signals, all defined in the merged `observability`
module and created only at the INFRA-9 apply:

| Signal | Source | CloudWatch definition | Meaning |
| --- | --- | --- | --- |
| `opportunity_feedback_gate_failed` | API structured log (`apps/api/app/feedback/routes.py`) | filter + fixed-threshold(1) alarm `<prefix>-gate-failed` | The gate errored (fail-closed path). Any occurrence is an incident-triage signal. |
| Override-driven ALLOW (`gate_decided`, `decided_by="workspace_override"`, `outcome="allowed"`) | API structured log | filter + fixed-threshold(1) alarm `<prefix>-override-enable` | While dark: **always unexpected — treat as a possible unauthorized enable.** During the authorized canary window: the primary expected watch signal, correlated against the approved window. |
| Override mutation (`workspace_capability_override_set` / `_clear`) | API structured log (`apps/api/app/capabilities/service.py`) | filter + fixed-threshold(1) alarm `<prefix>-override-mutation` | Every operator override set/clear pages — including a policy-**rejected** enable attempt (the service emits the `_set` event with `outcome="rejected"`), which is deliberate: a rejected attempt is itself signal. Coarse by design — see §2 for the full audit trail. |
| ERROR-severity log lines (×3 workloads) | all workloads | existing filters + caller-thresholded alarms | General error monitoring. |
| ECS/RDS/Redis saturation (×9) | AWS service metrics | existing caller-thresholded alarms | Service health (fail-closed missing-data). |
| Management-plane activity | CloudTrail (single-region, log-file validation) | existing trail + private audit bucket | Control-plane/IAM/secret-access audit. |
| Canary dashboard | `<prefix>-canary-observability` | `aws_cloudwatch_dashboard.canary` (4 widgets) | One operator/observer read surface over all of the above. |

The three capability alarms use an intrinsic fixed threshold of 1 (any
occurrence of a discrete audit event is the signal); they are deliberately not
caller-tunable. ALB-dimension alarms remain deferred behind the separately
authorized `alb` `arn_suffix` output addition.

**Paging precondition (known, tracked gap — not an oversight):** every alarm's
`alarm_actions`/`ok_actions` resolve to an **empty list** until the root
composition is supplied a real `sns_topic_arn` (consumed, never created — the
locked module contract). Until then, alarm state changes are visible only in
the CloudWatch console/dashboard/API: **nothing pages a human.** Supplying a
real topic ARN (and its subscription) is an INFRA-9-adjacent operator
prerequisite, and **the canary must not be authorized while alarm actions are
empty** — "fully observable" includes notification, not just visibility.

## 2. Audit views (the override audit-trail contract)

Two complementary planes — both must be checked during canary review:

1. **CloudWatch (paging plane):** the coarse stdout events above. They carry
   `event`, `outcome`, `capability`, `workspace_id` — enough to page and to
   correlate, not enough to attribute.
2. **Application audit table (attribution plane):** every
   `workspace_capability_override.created|updated|rejected|cleared` action is
   written to the Postgres `audit_logs` table with server-attributed actor,
   reason, and bounded previous/new state. It is read through the operator
   plane only:
   - `GET /internal/system/capabilities/effective` — effective state;
   - `GET /internal/system/capabilities/overrides` — stored override rows;
   - direct read-only `audit_logs` queries by the operator (via the
     INFRA-6-defined database procedures) when row-level audit detail is
     needed. There is intentionally no public audit endpoint.

**Documented limitation (reviewed, accepted for the canary):** the full audit
rows do not reach CloudWatch; CloudWatch sees the coarse mutation events only.
Attribution therefore requires the operator read plane. Mirroring audit rows
into stdout would be an application change and is out of INFRA-7's scope
(`docs/`, IaC only); revisit only under a separately authorized app tranche if
the canary review finds the two-plane model insufficient.

## 3. Unexpected-enable detection and response (dark-period watch)

Until the canary is separately authorized, the platform is dark and the
expected count of override-driven ALLOW decisions and override mutations is
**zero**. Response to any firing of `<prefix>-override-enable` or
`<prefix>-override-mutation` during the dark period:

1. Treat as a **possible unauthorized activation** (severity: high).
2. Attribute via the audit table (§2): actor, reason, workspace, timestamps.
3. Contain: clear the override through the operator DELETE plane (§4) —
   containment never requires a deploy or flag change.
4. Verify the clear: `.cleared` audit row present, effective state disabled,
   a `workspace_capability_override_clear` event in CloudWatch.
5. If actor/credential compromise is suspected, invoke the INFRA-6 §5
   rotation mechanics per the decision framework in §5 below.

## 4. Clear-path evidence (the DELETE containment plane)

The kill-switch for the canary is the operator clear plane:
`DELETE /internal/system/capabilities/overrides` — operator-gated,
server-attributed, idempotent (a no-op when no override exists), emitting
exactly one `.cleared` audit row plus the coarse stdout event. It is
implemented and covered by merged tests
(`test_operator_capabilities_override_clear_api.py`,
`test_capability_override_service.py`). The 4B-B evidence template §10
exercises it live before any enable is trusted ("proof the DELETE clear plane
works before enabling"); the INFRA-9-adjacent rehearsal must confirm both the
API response and the CloudWatch `override-mutation` signal.

## 5. Incident decision framework (INFRA-7 side of the INFRA-6 §5.5 boundary)

INFRA-6 owns the **mechanics** (how to rotate each secret, how to restore);
this section owns the **decision**: detection, severity, communication, and
when to invoke which mechanic.

| Trigger (detection) | Severity | Decision | Mechanics invoked |
| --- | --- | --- | --- |
| Suspected/confirmed secret exposure (value seen in any log, transcript, artifact, or unauthorized hands) | Critical | Rotate immediately; treat the value as compromised (inventory §6 rule) | INFRA-6 §5.1–§5.4 per secret; then task restart |
| Dark-period override-enable or override-mutation alarm | High | Contain first (§3/§4 clear), then attribute; rotate only if actor/credential compromise is indicated | §4 clear; conditionally INFRA-6 §5.1 (SECRET_KEY) |
| `gate-failed` alarm | High | The gate failed CLOSED (no exposure). Triage the underlying resolver/DB error; no rotation implied | INFRA-6 §8 restore only if data-tier loss |
| Data-tier loss/corruption | High | Restore per runbook; recompose/rotate DATABASE_URL as part of restore | INFRA-6 §8.1 (includes G5 re-run before reattachment) |
| Canary rollback fails (clear plane errors) | Critical | Escalate to the canary authorizer; the fallback containment is stopping the API service — a deployment-plane action requiring the then-current operational authorization | INFRA-6 §5 + operator escalation |

Communication: every High/Critical event is reported to the **canary
authorizer** and the **independent observer** (roles, §7) during the canary
window; the incident record (sanitized — no tenant UUIDs, no values) goes to
the restricted evidence destination (§6). Until `sns_topic_arn` is wired,
detection relies on active dashboard watch — see the §1 paging precondition;
the canary is not authorized in that state. A general (non-canary) incident
follows the existing `incident_response.md` flow, now cross-linked to the §4
clear plane for capability containment.

**Incident contacts (roles only — never personal identifiers in this repo):**
- *Canary authorizer* — approves activation, owns stop/go during the window.
- *Executing operator* — runs the operator plane; performs containment.
- *Independent observer* — read-only witness; countersigns evidence.
- *Infrastructure operator* — owns INFRA-6 mechanics execution (may be the
  same person as the executing operator; the observer must be distinct).
The concrete person↔role mapping for a given window is recorded in the
restricted evidence copy (§6), never here.

## 6. Secure evidence destination (design; system selection is an operator decision)

The filled 4B-B evidence record (and any incident record) contains restricted
identifiers (tenant/workspace UUIDs, actor identities) and must live only in a
destination with ALL of these properties:

- Private and access-controlled: writable by the executing operator + observer
  only; readable by operator, observer, canary authorizer, and incident
  responders — role-based.
- **Never**: this Git repository, a PR/issue/comment, CI logs or artifacts,
  chat messages carrying real identifiers, or any public/shared surface.
- **Not the CloudTrail audit bucket** — that bucket's reviewed contract is
  CloudTrail-service delivery only; repurposing it for human evidence would be
  an undisclosed scope change to a merged module.
- Retention: at least the audit-retention window in §8, then reviewed
  disposal; access revocation on role change; secure destruction documented.

**Open operator decision (flagged, not improvised):** the concrete system
(e.g., a restricted internal document store) is not selected by any repository
source and must be chosen and recorded by the canary authorizer **before the
canary is scheduled** — the 4B-B template now carries a line referencing it.
Until then, no live evidence exists (nothing is deployed), so nothing is
blocked today.

## 7. Independent observer access (requirements only — creation is INFRA-8)

Per the runtime contract, the observer is **read-only**: logs/audit/
effective-state views, with **no mutation rights and no override rights**. The
observer's session must be independent of the operator's. Concretely the
observer needs read access to: the operator GET endpoints in §2 (never
PUT/DELETE), the CloudWatch log groups/alarms/dashboard, the CloudTrail trail,
and the evidence destination (§6). **Creating** the observer identity, its
role, and its sessions is INFRA-8's scope ("internal test identities; operator
role; observer role; three independent sessions") — INFRA-7 fixes only these
access requirements so INFRA-8 can implement against them.

## 8. Retention and cost (the "set at INFRA-7" decisions)

Per the runtime contract's audit-retention line (set at INFRA-7, sized within
the §M budget):

- **CloudWatch workload logs:** 30 days — the ecs module's existing
  `log_retention_days` default is affirmed as the staging retention decision.
- **CloudTrail management events (audit bucket):** retained ≥ 400 days;
  versioned bucket, no lifecycle rule exists yet — object volume at staging
  scale is trivially small, so an expiry lifecycle rule is a deferred,
  separately authorized IaC addition rather than a cost necessity.
- **Application `audit_logs` table:** retained for the life of the staging
  database (bounded by the environment itself; revisit before any production
  design).
- **Alarm/metric cost:** 15 alarms + 6 filters + 1 dashboard ≈ low
  single-digit $/month — within the $200 ceiling alongside the existing
  estimate.

## 9. Redaction matrix (binding on logs, telemetry, dashboards, and evidence)

| Surface | Tenant/workspace ids | Secrets/credentials |
| --- | --- | --- |
| Application structured logs (general) | Never (existing `observability.md` policy + key/URL redaction in code) | Never |
| **Capability gate/override events** | **Present by documented exception** — `organization_id`/`workspace_id` are deliberately carried so an unexpected enable is detectable and attributable (`routes.py` states this design). This is the ONLY sanctioned id-bearing event family. | Never (no reason text, no payloads) |
| CloudWatch metrics/dimensions/alarms/dashboard | Never — deterministic infra names only, by construction | Never |
| Committed evidence and this repository | **Never — strictest rule.** Non-secret provenance (Git SHA, CI run id, digest) is allowed; tenant/workspace UUIDs, actor identities, emails, account ids are not, even though non-secret. | Never |
| Restricted evidence copy (destination §6) | Allowed (that is its purpose) — protected by §6's access rules | Never (values are never evidence; sanitized references only) |

## 10. Evidence-template sufficiency (INFRA-7 acceptance test)

`docs/verification/4b-b-feedback-canary.md` was reviewed against the Consider
list and made sufficient by four additions (this tranche): a
`gate_failed`/alarm-quiet check, an independent-observer read-path proof line,
a restricted-evidence-destination reference line, and an incident-contacts
(roles) line. The template remains placeholder-only; filled copies live only
in the §6 destination.

## 11. What merging this tranche does and does not do

Does: define the canary observability IaC (3 filters, 3 fixed alarms, 1
dashboard — created only at the INFRA-9 apply), lock the audit-view contract,
the incident decision framework (interlocking with INFRA-6 §5.5), the
observer-access requirements, the retention decisions, the redaction matrix,
and the evidence-destination design; make the 4B-B template sufficient.

Does not: create any AWS resource, identity, tenant, role, or session
(INFRA-8/INFRA-9); run any live command; create or clear any override; enable
any flag; populate any secret; deploy anything. The five capability flags
remain `False`; alembic remains `98289430a3ec` (12 migrations); execution of
every live step above occurs only inside its own later authorization.
