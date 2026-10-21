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
   only while `CONNECTOR_RSS_LIVE_ENABLED=true` and `CONNECTOR_RSS_KILL_SWITCH=false`.
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
* Politeness: one fetch per source per `CONNECTOR_RSS_MIN_FETCH_INTERVAL_SECONDS` (default
  900; a source record may only raise it) per process; conditional `If-None-Match` /
  `If-Modified-Since` with the body reused on `304`; a `429`/`503` `Retry-After` starts a
  cool-down (capped at a day). Transient failures retry with backoff; `429`, permanent
  `4xx`, guard refusals and unsafe content never retry.
* Parsing: size cap, DOCTYPE/entity refusal and `defusedxml`; RSS 2.0, RSS 1.0 and Atom.
  Text is reduced to bounded plain text (markup, script/style bodies and control characters
  removed; instruction-override phrases bracketed as quoted data); only plain `http(s)`
  entry links without credentials are kept.

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
`CONNECTOR_EGRESS_CONTACT_URL` and `CONNECTOR_RSS_LIVE_ENABLED=true` in the target
environment (an operator act; staging egress also needs the window and network path that
`docs/operations/staging-window.md` governs).

## 4. Stop, take down, verify

* **Stop everything now:** `CONNECTOR_RSS_KILL_SWITCH=true` (or
  `CONNECTOR_RSS_LIVE_ENABLED=false`). Runs then use the simulated paths or report
  `not_covered`.
* **Take one source down** (operator token):
  `POST /api/v1/internal/system/data-sources/{source_key}/takedown`
  with `{"reason": "...", "reference": "<public ticket id>"}`. In one transaction it records
  the takedown (the key is never fetched again), purges the source's raw and normalized
  signals, their location evidence and intelligence records **in every tenant**, deletes
  opportunities built only from that source (with their scores, validation evidence and
  feedback), removes its entries from mixed opportunities' evidence, and drops its cached
  feed bytes. The answer carries `purged` counts and `remaining` counts **recounted from the
  database**, and `verified: true` only when every remaining count is zero. Repeating it is
  safe and re-runs the purge.
* **Verify later:** `GET /api/v1/internal/system/data-sources` lists every record's terms
  record, whether it can run, its takedown, and `stored` counts measured at request time.
* A takedown is permanent for its key. Re-onboarding the publisher is a new record with a
  new key and a fresh approval.
* Downgrading below migration `da25533622b2` drops the takedown table. Do not downgrade
  while live egress is enabled: earlier code would fetch a taken-down source again.

## 5. Replay corpus and the Integration smoke gate (P6-CI-11)

`apps/api/app/connectors/captures/` holds feed bodies (SHA-256-pinned in `manifest.json`)
that `python -m app.connectors.replay seed` serves from a loopback server through the live
connector path into the demo database; `scripts/smoke_http.py` then asserts the result over
HTTP when `SMOKE_REAL_DATA` is `replay` or `required`. The replay origin setting
(`CONNECTOR_RSS_REPLAY_ORIGIN`) is refused outside development and test.

The shipped captures are **synthetic** (`"synthetic": true`, IANA-reserved example
domains): their signals are stored as simulated, and `SMOKE_REAL_DATA=required` fails on
them by design. Replacing them with a recorded capture is what makes the gate assert real
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

* Politeness, conditional-request validators and cool-downs are per process (in memory);
  several workers each keep their own.
* `robots.txt` is recorded per source at onboarding, not evaluated at fetch time.
* No connector metrics are exported; outcomes are structured log events
  (`connector.source_read`, `connector.source_failed`, `connector.source_taken_down`).
* A purge deletes user feedback attached to opportunities built only from the taken-down
  source; the count is reported as `feedback_removed`.
