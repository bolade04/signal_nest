# Third-party LLM data processing — position draft (P6-PRIV-2)

**Status:** DRAFT for operator and legal review. It records facts read from the code and
from the provider's public pages, and lists what the operator and legal must still do.
**It is not legal advice, it adopts nothing, and nothing in it is done unless it says so.**
The plan row is `P6-PRIV-2` ("No policy on prompts sent to third-party LLM providers — No
DPA record, no retention position"), coupled to `P6-LLM-6`; the provider decision it rests on
is `P6-D04` (provider resolved 2026-09-30: `LLM_PROVIDER=anthropic` for staging; credential
readiness **not** resolved).

**Sources.** Code: `origin/main` `3b0d9e36` (file references below). Provider pages: fetched
2026-10-20 (dates shown are the pages' own). Technical data-flow facts are owned by the LLM
lane (`P6-LLM-*`); if that lane's open work changes what reaches a prompt, section 1 must be
re-read against its merged code before this draft is relied on.

## 1. What SignalNest sends to the provider (from the code)

There are exactly **two call sites** of the LLM service, both in the scouting pipeline
(`apps/api/app/jobs/pipeline.py`), which runs inside the durable worker:

| Task (prompt) | Call | Inputs passed by the caller | Fields the template renders | What actually reaches the provider on `main` |
|---|---|---|---|---|
| `classify_signal` v1.0.0 | `pipeline.py:211` | `content` = the collected signal's text; `audiences` = the tenant's audience labels | `{content}`, `{keywords}` | the **signal text** (public third-party content from the connector: fixtures today, RSS when the live connector is enabled); `{keywords}` renders empty because the caller passes `audiences`, not `keywords` |
| `explain_opportunity` v1.0.0 | `pipeline.py:587` | `resolved_market`, `audience_fit`, `pain_point_dna`, `safe_positioning` | `{context}` | the **static template only** — none of the four inputs is named `context`, so the context renders empty |

How a request is built (`apps/api/app/llm/providers_real.py`, `AnthropicProvider`): one
`POST https://api.anthropic.com/v1/messages` with `model` (`LLM_MODEL`, default
`claude-sonnet-4-6`), `max_tokens`, `temperature` and a single user message (the rendered
template plus "Respond with strict minified JSON only."). **No** system prompt, **no**
`metadata.user_id`, **no** account, organization or workspace identifier, **no** email or
name of an account holder is sent. Authentication is the `LLM_API_KEY` secret.

Classification is skipped for signals the deterministic noise gate rejects and for
near-duplicates (`pipeline.py`, before the call), so not every collected signal is sent.

What is logged about a call (`apps/api/app/llm/service.py`): task, provider, model, prompt
name and version, latency, token counts, estimated cost, status, an **input hash** and the
attempt number — never the prompt or the response text.

**Personal data in prompts.** The signal text is third-party content and can contain personal
data (names, quotes, handles). Nothing redacts it before it is sent. If the template mismatch
in the explanation task is corrected (an LLM-lane matter), that prompt would also carry the
tenant's market name, an audience label and positioning text — tenant business context,
normally not personal data.

**Defaults that keep data local.** The deterministic mock provider is the default
(`LLM_PROVIDER=mock`) and is refused in staging and production by configuration validation;
no real provider has been called by this repository's tests. The OpenAI adapter also exists;
it is not the selected provider and is outside this draft.

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
| L-8 | Re-read section 1 against the LLM lane's merged code (prompt contents may change when the template mismatches are fixed) | LLM lane |

Credential readiness (`P6-D04`: whether `LLM_API_KEY` holds a valid key) is a separate open item
and is not assessed here.
