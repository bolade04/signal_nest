# Live data sources — operations (P6-DATA-1, P6-DATA-2, P6-DATA-4, P6-CI-11, P6-PRIV-4)

**State: implemented and dark.** No live source is approved. `APPROVED_SOURCES` in
`apps/api/app/connectors/sources.py` is empty and `CONNECTOR_RSS_LIVE_ENABLED` defaults to
`false`, so no deployment built from this code fetches anything from a third party. Turning
it on needs the operator's first-source decision and its terms-of-service / legal sign-off
(`docs/project-phase-6-plan.md` `P6-D03`, external prerequisite X1), recorded in a reviewed
change that adds the source record. Nothing in this document is that decision.

## 1. How a scout run picks its data

`apps/api/app/scouting_requests/connectors.py::fetch_signals`, in order:

1. **Live feeds** — every record in `APPROVED_SOURCES` that is enabled, `legal_status =
   approved`, carries an approval reference and date, serves the run's market **exactly**
   (case/spacing-insensitive equality, never a substring) and has not been taken down — but
   only while `CONNECTOR_RSS_LIVE_ENABLED=true` and `CONNECTOR_RSS_KILL_SWITCH=false`, **and**
   only for a workspace whose RSS capability the capability resolver admits (P6-CAP-1:
   `CONNECTOR_RSS_ENABLED=true` globally and no workspace RSS disable override; a workspace
   override can only turn live feeds off, never on). Any `no` is fail-closed: the run falls
   through to the simulated paths or reports `not_covered`.
2. **Sandbox RSS** (simulated) — when `CONNECTOR_RSS_ENABLED=true` and its policy admit it.
3. **Demo fixture library** (simulated) — the four demo markets, unless
   `CONNECTOR_FIXTURE_FALLBACK_ENABLED=false`.
4. Otherwise **no source**: the run completes with zero signals and says so.

Every run records the outcome in `stats.coverage` (and the scout-request API's typed
`coverage` field, and the run-history `stats.coverage`):

| `status` | `reason` | Meaning for the customer |
|---|---|---|
| `covered` | `live_sources` | Approved feeds were read; zero opportunities means nothing relevant was found |
| `covered` | `sandbox_feed` / `fixture_library` | Simulated data (`is_simulated: true`) |
| `not_covered` | `no_source_for_market` | No source serves this market — running again will not help |
| `unavailable` | `sources_unavailable` | Live sources serve the market but all failed this run — try later |

`sources` / `sources_failed` count the live sources consulted. A run that predates this
field has `coverage: null`.

Next to `coverage`, the run's `stats` carry each consulted source's fetch facts (#268,
P6-PLAT-1): `fetch_limits` - per source `max_items` (100), `fetched`, `eligible_seen`,
`clipped` (the source held more than 100 entries; one entry of look-ahead) and, for a
source that failed, `failed: true` with a closed `failure_reason` (a failed source is
never shown as an empty one) - and `partial_fetch` / `partial_fetch_reasons`
(`fetch_limit_reached`, `source_failed`). After the takedown re-check, a run whose
sources together would index more than N_max = 100 signals is refused
(`signal_volume_above_bound`) before any model call, with every source's facts and no
`coverage` object. The full statement is in `aws-staging-runtime-contract.md` §G.

## 2. Egress guard (`apps/api/app/connectors/egress.py`)

* `https` only, port 443, no credentials, no raw IP, no internal-looking names, host must
  be the source's own host or one of its `allowed_redirect_hosts`.
* Resolve, check **every** address, connect to the checked address (pinned; `Host` and
  TLS SNI carry the name, so the certificate is verified against it). Refused: loopback,
  RFC 1918, link-local (incl. `169.254.169.254`), CGNAT, documentation, benchmarking,
  reserved, multicast, unspecified, IPv6 ULA/site-local, and IPv4-mapped / 6to4 / NAT64
  forms of those; Teredo always.
* Redirects followed by hand, at most `CONNECTOR_EGRESS_MAX_REDIRECTS` (default 3), each hop
  re-validated and re-resolved; no `https → http`.
* Connect/read timeouts plus an overall deadline (`CONNECTOR_EGRESS_*_TIMEOUT_SECONDS`), a
  byte cap enforced while streaming (`CONNECTOR_EGRESS_MAX_RESPONSE_BYTES`, default 2 MiB),
  feed media types only, `Accept-Encoding: identity` with any encoded body refused.
* Ambient proxy variables are ignored.
* User-Agent `SignalNestBot/<version> (+<CONNECTOR_EGRESS_CONTACT_URL>)`; the contact URL
  is required (https) whenever live egress is enabled.
* Politeness: at most one fetch per source per `CONNECTOR_RSS_MIN_FETCH_INTERVAL_SECONDS`
  (default 900; a source record may only raise it) per process, **whether the source
  answers or fails**; conditional `If-None-Match` / `If-Modified-Since` with the body reused
  on `304`.
  * Within one fetch, transient failures (a timeout, a connection failure, a `5xx` without
    `Retry-After`) are retried with a short backoff, `CONNECTOR_RSS_MAX_ATTEMPTS` requests
    in all (default 3). A `429`, a permanent `4xx` such as `404`, any answer carrying
    `Retry-After`, a guard refusal, a response that is not a feed and unsafe content are
    never retried.
  * **Every failed fetch then starts a cool-down** (#255 review r2-MINOR-1), whatever the
    failure, including an unreadable body. The source is not contacted for one politeness
    interval after a failure; each further consecutive failure doubles the hold-off, up to
    four intervals (one hour at the default interval; never beyond a day unless the
    source's own interval is longer). A `Retry-After` on a `429` or `503` (honoured up to a
    day) can only lengthen the hold-off, never shorten it below the interval. A good read
    or a `304` ends the streak; a takedown drops it with the rest of the source's cache
    entry.
  * While a source cools down, its last good body, if any, is served without a request.
    Without one, each run reports the source failed with the same closed
    `failure_reason` as the run that met the failure (`rate_limited` during a
    `Retry-After` hold-off).
  * So a failing publisher receives at most `CONNECTOR_RSS_MAX_ATTEMPTS` requests per
    interval from each process, however many scout runs its market has. A source that
    recovers is read again when its current hold-off ends (at most four intervals later).
* Parsing: size cap, DOCTYPE/entity refusal and `defusedxml`; RSS 2.0, RSS 1.0 and Atom.
  Text is reduced to bounded plain text (markup, script/style bodies and control characters
  removed; instruction-override phrases bracketed as quoted data); only plain `http(s)`
  entry links without credentials are kept. A feed the parser cannot decode (for example a
  declared Shift_JIS, EUC-JP, GB18030, Big5 or UTF-32 encoding, an unknown encoding name, or
  a byte-order mark that contradicts the declaration) is a `parse_error` for that source.
* Isolation: each source is read on its own. Any failure while fetching, parsing or
  turning its items into signals is recorded for that source; the other sources of the run
  still deliver. A body is cached only after it parsed: an unreadable response is never
  stored and starts the same cool-down as any other failed fetch (above): no request
  meanwhile, and a previously cached good body keeps being served.

## 3. Onboarding a source (after the operator's decision)

Add a `SourceRecord` to `APPROVED_SOURCES` in a reviewed pull request:

| Field | Content |
|---|---|
| `source_key` | stable lowercase slug, never reused (a takedown retires it for good) |
| `publisher`, `display_name`, `attribution` | who publishes it and how it is credited |
| `feed_url` | the exact https feed URL |
| `markets` | the exact market names it serves, e.g. `"Toronto, ON"` |
| `tos_url` | the publisher's terms of use |
| `robots_url`, `robots_disposition` | where the robots policy lives and what was recorded for the feed path, with a date |
| `approval_reference`, `approved_on`, `legal_status` | the public decision / ticket id covering this feed, its date, `approved` |
| `allowed_redirect_hosts` | only hosts the feed is known to redirect to |
| `min_fetch_interval_seconds` | the publisher's stated polling limit, if stricter |
| `enabled` | `True` only when everything above is recorded |

Never put a person's name or address in a record: this repository is public. Then set
`CONNECTOR_EGRESS_CONTACT_URL`, `CONNECTOR_RSS_LIVE_ENABLED=true` **and**
`CONNECTOR_RSS_ENABLED=true` in the target environment (an operator act; staging egress also
needs the window and network path that `docs/operations/staging-window.md` governs). The
last flag is the capability resolver's global RSS decision: without it, or with a workspace
RSS disable override in place, that workspace's runs get no live feeds and report the
simulated path or `not_covered` (the replay seed sets it for its own process).

## 4. Stop, take down, verify

* **Stop everything now:** `CONNECTOR_RSS_KILL_SWITCH=true` (or
  `CONNECTOR_RSS_LIVE_ENABLED=false`). Runs then use the simulated paths or report
  `not_covered`.
* **Take one source down** (operator token):
  `POST /api/v1/internal/system/data-sources/{source_key}/takedown`
  with `{"reason": "<free text, 1–2000 characters>", "external_reference": "<your own
  ticket id, optional, ≤ 200 characters, one line>"}`. **Both fields are private (D-9e):**
  they are written only to a restricted **operator case record**
  (`source_takedown_cases`, one row per request, a repeat adds a new one) and never to a
  tenant-visible API, audit entry, organization export, log line or metric. The product
  gives each request an opaque `case_id` (returned by the call); that id is the only
  reference an affected workspace's audit entry carries. In one transaction the call records
  the takedown (the key is never fetched again), purges the source's raw and normalized
  signals, their location evidence and intelligence records **in every tenant**, deletes
  every opportunity built only from that source **together with the customers' feedback on
  it** (and its scores and validation evidence), removes the source's entries from mixed
  opportunities' evidence, and writes **one audit entry in each affected workspace**
  (`data_source.taken_down`, actor = the operator, `reason` = one fixed generic sentence,
  `context` = exactly `{source_key, case_id, repeat, opportunities_deleted,
  opportunities_redacted}` — the removed ids — and the counts in `new_state`). The
  answer carries `purged` counts and `remaining` counts **recounted from the database**,
  and `verified: true` only when every remaining count is zero. Repeating it is safe and
  re-runs the purge; the first purge's counts stay on the record (`purge_summary`) and
  each repeat appends `{at, purged}` to `repeat_purges`.
* **What the takedown record holds (D-9d):** `source_takedowns` is a permanent suppression
  record and nothing more — the source key, the time it was taken down, and aggregate
  counts (`purge_summary`, `repeat_purges[*].{at, purged}`). It never holds a user id, free
  text or a reference, and no erasure, anonymisation or expiry of the audit log or of the
  case records can re-enable fetching the source.
* **Where the reason lives (D-9e, ruled):** in the operator case record only. A printable
  string without `@` was never sufficient evidence that a reference is free of sensitive
  content, so the operator-typed ticket id is private too. Each `source_takedown_cases`
  row holds the opaque case id, the source key, the operator's `reason` and
  `external_reference`, the acting operator (`actor_user_id`, set to NULL when that
  account is erased; the case is kept) and the time. The record is not exported to
  customers (privacy inventory: `exported=False`). Read it with the operator-only
  `GET /api/v1/internal/system/data-sources/{source_key}/cases` (a customer, including an
  OWNER, gets the usual non-enumerable 403); the registry shows only `cases_count` and
  the latest `case_id`. **Its access and retention policy is separately approved and still
  pending: no retention duration is set or implied** (retention class unset, "retention
  duration pending operator decision (D-9e access/retention policy)"). Until that policy
  is approved, no job deletes case records and nothing here is a legal position. Do not
  put anything into `reason` that you would not accept being kept for that undecided
  period, and do not upload personal data to an external service on the strength of this
  record.
* **Cached feed bytes:** the API process that performs the takedown drops the source's
  cached bytes at once; each worker process drops them at its next scout run, before it
  fetches anything. Until then they may sit unused in a worker's memory — no run reads a
  taken-down key.
* **Runs in flight:** a scout run that started before the takedown re-checks the takedowns
  under the source's lock before it writes anything, and a takedown takes that lock
  exclusively (PostgreSQL advisory locks; SQLite serialises writers). Either the run drops
  the source's signals, or the takedown waits for the run to commit and then purges them —
  a `verified` takedown is not followed by re-inserted signals.
* **Verify later:** `GET /api/v1/internal/system/data-sources` lists every record's terms
  record, whether it can run, its takedown (with `cases_count` and the latest `case_id`,
  never the reason), and `stored` counts measured at request time.
* A takedown is permanent for its key. Re-onboarding the publisher is a new record with a
  new key and a fresh approval.
* Downgrading below migration `da25533622b2` drops the takedown and case tables. Do not downgrade
  while live egress is enabled: earlier code would fetch a taken-down source again. Signals
  keep their source in their provenance, and upgrading again restores each signal's
  `source_key` from it, so a takedown still reaches them; the takedown and case records
  themselves are lost, so take each such source down again after re-upgrading.

## 5. Replay corpus and the Integration smoke gate (P6-CI-11)

`apps/api/app/connectors/captures/` holds feed bodies (SHA-256-pinned in `manifest.json`)
that `python -m app.connectors.replay seed` serves from a loopback server through the live
connector path into the demo database; `scripts/smoke_http.py` then asserts the result over
HTTP when `SMOKE_REAL_DATA` is `replay` or `required`. The replay origin setting
(`CONNECTOR_RSS_REPLAY_ORIGIN`) is refused outside development and test.

The shipped captures are **synthetic** (`"synthetic": true`, IANA-reserved example
domains): their signals are stored as simulated, and `SMOKE_REAL_DATA=required` fails on
them by design. A recorded capture (`"synthetic": false`) counts as real data only when an
approved, enabled `APPROVED_SOURCES` record with the same source key and feed URL covers its
markets; it then replays under that record's own approval and terms. Without one it replays
as simulated and is reported as an unverified recorded capture. Replacing the synthetic
captures with a recorded capture of an approved source is what makes the gate assert real
data:

1. after a source is approved and enabled, the operator runs
   `python -m app.connectors.replay capture <source_key> <dir>` — this is live egress, under
   that source's approval, never from CI;
2. committing the captured bytes to this **public** repository redistributes the
   publisher's content: do it only when the publisher's terms (or licence, e.g. an open
   government licence) allow redistribution, otherwise keep the gate on `replay`;
3. trial it first without touching the bundled corpus:
   `python -m app.connectors.replay seed --corpus <dir>` (after `npm run demo:setup`), then
   `SMOKE_REAL_DATA=required bash scripts/ci-smoke.sh`;
4. add the printed manifest entry, keep or remove the synthetic entries, and switch the
   smoke step to `SMOKE_REAL_DATA=required`.

## 6. Known limits

* Politeness, conditional-request validators and cool-downs (with each source's run of
  consecutive failures) are per process (in memory); several workers each keep their own,
  and a restarted process starts without them, so its first run may contact a failing
  source again.
* While a held good body is served during a cool-down, the run counts the source as read
  (`sources_failed` does not include it). Only the run that met the failure reports it,
  and the `connector.source_failed` log event records it.
* `robots.txt` is recorded per source at onboarding, not evaluated at fetch time.
* No connector metrics are exported; outcomes are structured log events
  (`connector.source_read`, `connector.source_failed`, `connector.source_taken_down`).
* A purge deletes user feedback attached to opportunities built only from the taken-down
  source; the count is reported as `feedback_removed`.
