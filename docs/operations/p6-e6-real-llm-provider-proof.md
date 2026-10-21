# P6 E6 — recorded real-LLM-provider proof (prepared; nothing executed)

**Status: PREPARED ONLY.** This document authorizes nothing. Every step that touches AWS, a
deployment, a live environment or the provider account needs its own written authorization
naming this document. `E6_COMPLETE = false` until the recorded run below has happened in an
authorized environment and its evidence set is sealed.

**Governing criterion** (`docs/project-phase-6-plan.md` §19, item 6): *"A real LLM provider
has served the pipeline under test, including its timeout, 429, 5xx and non-JSON failure
branches."* Recorded-response replay (`apps/api/app/tests/test_llm_provider_replay.py`,
`test_llm_degradation_marker.py`) proves the adapter's handling of every branch offline; it
is the `P6-LLM-1` acceptance path, **not** E6, because no real provider is involved.

**Adopted decision in force:** `P6-D04` — `LLM_PROVIDER=anthropic` for staging (2026-09-30).
Credential readiness is **not** resolved by it: whether the `LLM_API_KEY` secret container
holds a valid key is unverified, and no one reads the value to find out (the probe below
tests it by use, inside the task that already receives it). Placeholders only: `<cluster>`,
`<api-task-family>`, `<subnets>`, `<security-group>`; no account, ARN or key appears here.

## 0. Preconditions — none is satisfied today

| # | Precondition |
|---|---|
| P1 | A `main` image that contains this change (the `app.llm.live_probe` module, the bounded-retry service and the `explanation_source` marker), deployed to the staging API/worker task definitions. Record commit SHA, image digest, task-definition revision. |
| P2 | Staging can run tasks: a window is OPEN under `docs/operations/staging-window.md`, and no recorded block on opening windows is outstanding (check the operator's current window record before scheduling). |
| P3 | The `LLM_API_KEY` secret container holds a provider key, put there by the operator (an operator act through the secrets container; never through a tracked file, a tfvars value or a chat). Provider = `anthropic` in the task environment (`LLM_PROVIDER`). |
| P4 | Model decided: `LLM_MODEL` is set in the task environment, or the code default `claude-sonnet-4-6` is accepted in writing. Infra wires only `LLM_PROVIDER` today, so without a decision the code default is what runs. |
| P5 | A spend ceiling recorded for the run (see §3) and a provider-side spend limit or alert on the account, where the provider console offers one. |
| P6 | Written authorization naming this document, the window, the image digest and the ceiling. |

## 1. Adapter-level probe (one one-off task; at most four billable requests)

1. Run one task from `<api-task-family>` with the command override
   `python -m app.llm.live_probe --confirm-live-provider-call` (same network placement as the
   API service). The flag is required and must be spelled in full (abbreviations are refused).
2. Capture the task's stdout from its log stream. Every line is
   `{"event": "e6_probe", "step": ..., "outcome": ..., "as_expected": ...}` and carries no key,
   no prompt and no model text; inputs are fixed synthetic text, never tenant data.
3. Expected outcomes, one line each:

| Step | What happens | Expected `outcome` | Billable |
|---|---|---|---|
| `success_classify` | real `classify_signal` through `LLMService` (retries off) | `ok`, `is_simulated: false`, token counts > 0 | yes |
| `success_explain` | real `explain_opportunity` | `ok`, all six schema keys validated | yes |
| `truncated` | `max_tokens=4` → `stop_reason: max_tokens` | `LLMMalformedError` (the non-JSON / truncated branch) | yes (≈4 output tokens) |
| `timeout` | 1 ms client timeout | `LLMTimeoutError` | at most one abandoned request |
| `rejected_model` | unknown model id → HTTP 404 | `LLMProviderError` | no |
| `rate_limit_429`, `server_error_5xx` | **not induced** | `not_induced` | — |
| `summary` | | `unexpected: 0`; exit status 0 | — |

**429 and 5xx cannot be produced honestly against a real provider**: a 429 needs flooding the
account and a 5xx needs an outage. Both stay proven by replay. Whether E6 accepts "real
provider for success, timeout, truncation/non-JSON and rejection + replay for 429/5xx" is an
**operator/assessor ruling**, recorded in the LLM lane's decision memo; this document does not
make it.

## 2. Pipeline-level run (the "served the pipeline" limb)

With the same image in both API and worker services and `LLM_PROVIDER=anthropic`:

1. As a test user in a test workspace (not a customer), create one location and one scout
   request and run it once through the API.
2. Record: the scout run's final status and stats; for each resulting opportunity, from the
   detail endpoint, `explanation_source` (`model` expected where relevance is above the action
   floor, `rule_based` below it); and the worker's `llm.call` log lines
   (`provider=anthropic`, `is_simulated=false`, token counts, `estimated_cost_usd`,
   `attempt`), the `scout.completed` line's `llm_attempts` / `llm_cost_usd` /
   `llm_refusals`, and the run's `llm_usage_events` rows (counts and cost only). No
   `llm.failed` line is expected; any `degraded`, `withheld` or `budget_limited`
   opportunity is recorded as observed, not hidden. The workspace's monthly budget
   (`llm_workspace_monthly_budget_usd`) and the per-run cap must be set high enough for the
   run, or the refusal state is what gets recorded.
3. Note: until `P6-DATA-1` lands, connector input is fixture data (`is_simulated=true` on the
   opportunity). E6 concerns the provider, not the source; record both facts separately.

## 3. Budget

Adapter probe: three billable requests of a few hundred input tokens and at most one
`llm_max_output_tokens` (default 1024) reply each — well under USD 0.10 at the bundled list
prices for `claude-sonnet-4-6` (USD 3 / 15 per million input / output tokens; source: the
Anthropic `claude-api` reference bundled with Claude Code, cached 2026-06-24 — **re-verify
against the provider's pricing page on the day**). Pipeline run: one model call per
non-noise, non-duplicate signal plus one per opportunity above the action floor; with the
fixture connector that is on the order of tens of calls. Record the ceiling before the run
and stop if the provider console shows spend above it.

## 4. Evidence hygiene

Capture through the coordination `cap` tool; redact `accessKeyId` from any CloudTrail
capture; never include the key, a full prompt or model text in the sealed set (the probe's
output already omits them). Record the image digest, task ARN suffix only, start/end UTC and
the exit status.

## 5. Not closed by this document

`P6-LLM-6` (production provider and credential provisioning), the 429/5xx ruling above, the
model decision (P4), `P6-PRIV-2` (third-party processing policy, owned by the privacy lane),
and every live-environment gate in §0.
