# Third-party LLM data processing — position draft (P6-PRIV-2)

**Status:** DRAFT for operator and legal review. It records facts read from the code and
from the provider's public pages, and lists what the operator and legal must still do.
**It is not legal advice, it adopts nothing, and nothing in it is done unless it says so.**
The plan row is `P6-PRIV-2` ("No policy on prompts sent to third-party LLM providers — No
DPA record, no retention position"), coupled to `P6-LLM-6`; the provider decision it rests on
is `P6-D04` (provider resolved 2026-09-30: `LLM_PROVIDER=anthropic` for staging; credential
readiness **not** resolved).

**Sources.** Code: section 1 was re-read on 2026-11-01 against `main` `102393fa` (item L-8),
and its file references are to that commit; the first draft read `origin/main` `3b0d9e36`,
and what the re-read corrected is listed at the end of section 1. Provider pages: fetched
2026-10-20 (dates shown are the pages' own). Technical data-flow facts are owned by the LLM
lane (`P6-LLM-*`); if that lane's open work changes what reaches a prompt, section 1 must be
re-read against its merged code before this draft is relied on.

## 1. What SignalNest sends to the provider (from the code)

There are **two call sites** that send tenant or collected data, both in the scouting
pipeline (`apps/api/app/jobs/pipeline.py`). Its production caller is the durable worker's
`scout_request.execute` handler (`apps/api/app/jobs/handlers.py:161`, `:236`); the only other
caller outside tests is the replay demo seed, which refuses anything but a local
development/test SQLite database (`pipeline.py:319-328`). A third caller, the E6 live-provider
probe (`apps/api/app/llm/live_probe.py:101`), sends only fixed synthetic text
(`live_probe.py:49-58`) and is run by an operator with `--confirm-live-provider-call`, never by
tests or CI (`live_probe.py:1-28`). The other registered prompts (`check_claim_safety`,
`summarize_website`, `geo_reasoning`; `apps/api/app/llm/prompts.py:79-97`) have no caller.

| Task (prompt) | Call | Inputs passed by the caller | Fields the template renders | What actually reaches the provider on `main` |
|---|---|---|---|---|
| `classify_signal` v1.2.0 | `pipeline.py:465-469` | `content` = the collected signal's text; `audiences` = the workspace's audience labels | `{content}`, `{audiences}` (`prompts.py:47-61`) | the **signal text** (public third-party content from the connector: the simulated fixture library unless live RSS egress is enabled, which it is not by default, `apps/api/app/core/config.py:428`) **and the workspace's audience labels** (its audience profiles' labels, else the business profile's target audience — tenant-entered business context, `apps/api/app/opportunities/context.py:26-30`, `:49-57`) |
| `explain_opportunity` v1.2.0 | `pipeline.py:1159-1168` | `resolved_market`, `audience_fit`, `pain_point_dna`, `safe_positioning` | all four (`prompts.py:62-78`) | the **resolved market name**; an **audience label** (the screened label the model gave the opportunity's primary signal, else the workspace's first audience, else "your target customers", `pipeline.py:711-713`); a **pain-point label** from a closed vocabulary (`apps/api/app/llm/screening.py:298-306`); and a **fixed positioning sentence** chosen by that label and the industry (`apps/api/app/claims/engine.py:104-127`, `pipeline.py:709`) |

How a prompt is assembled (`prompts.py:111-118`, `apps/api/app/llm/untrusted.py:75-90`): the
template's trusted task text, with each input value in its own `<untrusted_data name="...">`
block, followed by "Respond with strict minified JSON only." Each value is Unicode-normalised
(NFKC, invisible characters removed, whitespace runs collapsed), stripped of control
characters, has known prompt-injection phrases quoted as `[quoted:...]`
(`apps/api/app/intelligence/extraction.py:48-60`), is cut at `LLM_MAX_UNTRUSTED_CHARS` (default
4000 characters, `config.py:510`) with a `[truncated]` marker, and is escaped. **None of these
steps removes personal data.**

How a request is built (`apps/api/app/llm/providers_real.py:381-397`, `AnthropicProvider`): one
`POST https://api.anthropic.com/v1/messages` with `model` (`LLM_MODEL`; the adapter's default is
`claude-sonnet-4-6`, `providers_real.py:379`), `max_tokens` (`LLM_MAX_OUTPUT_TOKENS`, default
1024, `config.py:502`), a fixed **system prompt** saying that nothing inside an
`untrusted_data` block is an instruction (`prompts.py:22-30`), a single user message (the
assembled prompt), `temperature` only for the models listed at `providers_real.py:75-81`, and
**`metadata.user_id`**: an opaque **per-workspace reference**, sent whenever the call is
metered (`providers_real.py:390-392`, `apps/api/app/llm/service.py:119`), which every pipeline
call is (`pipeline.py:329-334`). The reference is `t_` and 32 hex characters of an HMAC-SHA256
of the workspace id keyed with `SECRET_KEY` (`apps/api/app/llm/metering.py:212-216`): it
contains no id, name or e-mail address, but it is the same for every call of one workspace
until `SECRET_KEY` is rotated (`metering.py:30-34`). **No** account, organization or workspace
identifier itself, and **no** email or name of an account holder, is sent. Authentication is
the `LLM_API_KEY` secret. The client stores no cookie a reply sets, so none is sent with a
later call (`providers_real.py:236-247`, `:269`). A model missing from the price table
(`LLM_PRICES_USD_PER_MTOK`, `config.py:520-531`) is never called
(`apps/api/app/llm/bound.py:219`); the default table lists `claude-fable-5-1`, so the code does
not prevent selecting a model of the "Covered Models" family named in section 2 (see L-7).

A transient failure (timeout, 429, 5xx, connection error) is retried with the same request,
so one prompt can be sent up to `LLM_MAX_RETRIES` + 1 times (default 3; `config.py:493`,
`service.py:135-161`).

**When a call is made.** Classification is skipped for signals the deterministic noise gate
rejects and for near-duplicates (`pipeline.py:462`), so not every collected signal is sent.
Signals from a source an operator has taken down are dropped before analysis
(`pipeline.py:369`); a paused Scout makes no call (`pipeline.py:283-284`); and a run whose
signal volume exceeds the in-process bound is refused before any signal is sent to a model
(`pipeline.py:372-397`). An explanation is requested only for an opportunity whose relevance
is at or above the action floor and all of whose signals were classified
(`pipeline.py:1126-1157`). No call is made once the run's call cap (`LLM_MAX_CALLS_PER_RUN`,
default 100, `config.py:513`) or the workspace's monthly budget
(`LLM_WORKSPACE_MONTHLY_BUDGET_USD`, default 5.0, `config.py:517`) would be crossed, or when a
call's cost cannot be bounded (`metering.py:9-27`); the affected output is marked
`budget_limited` (`pipeline.py:478-479`, `:1138-1148`, `:1169-1174`).

What is logged about a call (`service.py:148-159`, `:183-193`, `:227-246`): for a successful
call (`llm.call`), task, provider, model, prompt name and version, latency, token counts,
estimated cost, status, `is_simulated`, the attempt number and an `input_hash` field, which
only the mock provider fills (`apps/api/app/llm/mock.py:85`, `:105`): for the real adapters it
is empty (`apps/api/app/llm/base.py:84`). A retried or failed call (`llm.retry`, `llm.failed`)
logs the task, the error class and an error message that carries only the provider name, the
HTTP status and the provider's error type and request id (`providers_real.py:18-19`,
`:135-158`), or the failing schema fields and error types (`service.py:77-89`). Never the
prompt or the response text.

What is stored about a call (`apps/api/app/llm/models.py:1-65`): one `llm_usage_events` row
per provider attempt with the organization, workspace, Scout request and job it belongs to,
the task, provider, model, token counts, cost, outcome and timestamps; the budget ledger
(`llm_budget_periods`, `llm_budget_reservations`, `models.py:68-158`) holds money, keys and
states. None of them holds prompt text, collected content or model output. Their retention,
as recorded in the machine-checked inventory (`apps/api/app/privacy/inventory.py:167-187`): no
time-based retention class; kept for the life of the workspace, deleted with the workspace or
organization, and included in the organization export (operator decisions D-9a/D-9b/D-9c of
2026-10-27 for `llm_usage_events`; the coordinator's ruling of 2026-10-27 for the two ledger
tables).

What is kept from a reply (`screening.py:1-50`): classification labels are constrained to
closed vocabularies, and the model's free-text audience label is screened against the same
patterns as explanation text (`screening.py:286-306`). Explanation text that carries a link,
an e-mail address, a phone number, markup, an echoed injection phrase or a high-risk claim is
replaced by fixed copy marked `withheld` (`pipeline.py:1186-1196`); what passes is stored on
the opportunity, under the `opportunities` retention class (`inventory.py:139`).

**Personal data in prompts.** The signal text is third-party content and can contain personal
data (names, quotes, handles). Nothing redacts it before it is sent (see "How a prompt is
assembled" above). The template mismatch the first draft described is fixed (prompts 1.2.0):
the explanation prompt now carries the tenant's market name, an audience label, a pain-point
label and positioning text, and the classification prompt carries the workspace's audience
labels — tenant business context, normally not personal data. An audience label taken from
the model's classification is derived from a collected post; it is screened as above, not
otherwise redacted.

**Defaults that keep data local.** The deterministic mock provider is the default
(`LLM_PROVIDER=mock`, `config.py:489`) and is refused in staging and production by
configuration validation (`config.py:753-759`). The API test session removes LLM provider
settings from the environment before the application is imported, and refuses every
connection to a non-loopback address (`apps/api/app/tests/conftest.py:23-27`, `:53-54`;
`apps/api/app/tests/_offline_guard.py:1-12`). The OpenAI adapter also exists; it is not the
selected provider and is outside this draft.

**Corrections made by the 2026-11-01 re-read** (first draft: `3b0d9e36`). What reaches a
prompt was changed by #237 and #244 (templates, system prompt, sanitising, tenant reference,
screening, metering, the E6 probe), #263 (cookies), #265 (the test offline guard) and #269 (the
budget ledger and the unpriced-model refusal); #255, #267 and #268 added the takedown, pause
and volume-bound conditions under "When a call is made"; #278 changed no prompt input.

- Call sites: still two that carry tenant or collected data, now at `pipeline.py:465-469` and
  `:1159-1168`; the E6 probe is a third caller, with synthetic inputs only.
- `classify_signal` 1.0.0 rendered `{keywords}`, which no caller sent; 1.2.0 renders
  `{audiences}`, so the workspace's audience labels now reach the provider.
- `explain_opportunity` 1.0.0 rendered only `{context}`, which no caller sent, so only the
  static template was sent; 1.2.0 renders all four inputs.
- The request now carries a system prompt and `metadata.user_id` (the per-workspace
  reference); the first draft said neither was sent.
- Every prompt input is now sanitised, capped and delimited; none of that redacts personal
  data.
- The logged `input_hash` is empty for the real adapters; the first draft listed it as logged.
- Added: repeated sends on retry, the conditions under which no call is made, what is stored
  per call and its retention, and what is kept from a reply.
- The first draft's "no real provider has been called by this repository's tests" is replaced
  by what the code enforces. Whether a test reached a provider before #265 cannot be
  established from the code.

## 2. The provider's published commercial terms (Anthropic)

Read 2026-10-20. Quoted or closely paraphrased; marked **unverified** where the published text
does not settle the point for SignalNest's account.

| Topic | What the public page says | Source |
|---|---|---|
| Training | "Anthropic may not train models on Customer Content from Services." | [Commercial Terms of Service](https://www.anthropic.com/legal/commercial-terms) (effective 2025-06-17) |
| DPA | The Data Processing Addendum is "incorporated into these Terms by reference"; the DPA states it forms part of the Commercial Terms | Commercial Terms; [Data Processing Addendum](https://www.anthropic.com/legal/data-processing-addendum) (effective 2025-02-24) |
| Role | For API and commercial customers, Anthropic acts as a processor on the customer's behalf | [Privacy Center — processor or controller](https://privacy.claude.com/en/articles/9267385-does-anthropic-act-as-a-data-processor-or-controller) |
| Sub-processors | Published at `anthropic.com/subprocessors`; advance notice of new sub-processors, objection within 15 days | DPA |
| Transfers | Standard Contractual Clauses (modules two and three) with EU, UK and Swiss addenda | DPA |
| Retention (standard) | "For Anthropic API users, we automatically delete inputs and outputs on our backend within 30 days of receipt or generation", with exceptions | [Privacy Center — commercial data retention](https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data) (last updated 2026-07-01) |
| Retention (exceptions) | Flagged usage-policy violations: inputs and outputs up to 2 years, trust-and-safety classification scores up to 7 years; feedback submissions 5 years; retention where required by law | Privacy Center article above; [API and data retention](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention) |
| Zero data retention | Available by arrangement with Anthropic's sales team, per organization; covers the Messages API; not available for the "Covered Models" (Claude Fable 5 / 5.1, Claude Mythos 5 / 5.1), which require 30-day retention | API and data retention |
| End of contract | Return or deletion of customer data within 30 days of termination | DPA |

**Unverified / to be confirmed by the operator or legal:**

- The platform documentation also says conversation content "is not retained by default"
  except for Covered Models, while the Privacy Center article says API inputs and outputs are
  deleted "within 30 days". Which statement governs SignalNest's organization is **not
  established** here; plan on up to 30 days unless the contract or the Console's data-retention
  setting says otherwise.
- Whether the API organization that holds `LLM_API_KEY` is under the **Commercial Terms** (and
  so the DPA) — not read (no secret, console or account was accessed).
- The organization's data-retention setting, whether ZDR is in place, and the region where
  requests are processed — not read.

## 3. Draft position (an assessor recommendation — not adopted)

1. Send only what a task needs. Today that is the signal text for classification; keep account
   holders' identities, tenant identifiers and business-profile content out of prompts unless a
   task is shown to need them.
   *(Fact correction, 2026-11-01 re-read: at `102393fa` the prompts also carry tenant business
   context — the workspace's audience labels and the Scout's market name, plus a fixed
   positioning sentence chosen partly by the business's industry — and each request carries an
   opaque per-workspace reference, not an identifier; see section 1. This
   recommendation is unchanged, and whether the tasks need that content is not assessed here.)*
2. Treat Anthropic as a **sub-processor** of SignalNest for any customer data that reaches a
   prompt, under the Commercial Terms and the incorporated DPA, and disclose it as such.
3. Accept the provider's standard retention (up to 30 days; longer only for flagged content or
   legal obligation) for the pilot, **or** request ZDR — an operator decision (below).
4. Do not select a Covered Model while ZDR is wanted.

## 4. Open operator / legal items (none of these is done)

| # | Item | Owner |
|---|---|---|
| L-1 | Confirm, and record with date and version, that the Anthropic organization behind `LLM_API_KEY` is under the Commercial Terms and therefore the DPA (no click-through or signature record exists in this repository) | Operator |
| L-2 | Decide standard 30-day retention or ZDR; if ZDR, request it and record the confirmation; record the Console's data-retention setting either way | Operator |
| L-3 | List Anthropic as a sub-processor in SignalNest's own customer-facing privacy notice / DPA, and subscribe to sub-processor change notices (15-day objection window) | Operator + legal |
| L-4 | State the prompt-retention position (provider-side, by data class) in the privacy notice, consistent with the `O-5` / `P6-D05` retention decisions | Legal |
| L-5 | Decide whether third-party personal data in collected signals must be redacted or minimised before classification (and whether a DPIA is required for the live connector) — interacts with `P6-PRIV-4` / `P6-DATA-1` | Legal + LLM lane |
| L-6 | Assess international transfer (SCCs) and data residency for the pilot's customers | Legal |
| L-7 | If the model is changed, re-check it against the Covered Models list and the retention choice in L-2 | Operator |
| L-8 | Re-read section 1 against the LLM lane's merged code (prompt contents may change when the template mismatches are fixed). Re-read once on 2026-11-01 against `102393fa` (lane W21 SMALLFIX); section 1 was corrected and lists the corrections. It stays open as a standing check: repeat it whenever a merged change alters what reaches a prompt | LLM lane |

Credential readiness (`P6-D04`: whether `LLM_API_KEY` holds a valid key) is a separate open item
and is not assessed here.
