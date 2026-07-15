# RSS connector operations (Phase 3B Batch 2)

**Status:** live egress is **disabled** and no source is approved. This runbook
describes how the live path *would* be operated once owner + legal sign-off lands.
Nothing here enables traffic on its own.

## 1. Enabling a source (order matters — fail closed at every gap)

1. Confirm the source is in `app/connectors/sources.py` with
   `legal_review = approved` and `owner_approval = approved` (with references).
2. Set conservative `burst_limit`, `daily_limit`, `fetch_interval_seconds`,
   `max_response_bytes`.
3. Scope `environments` to `staging` and a single canary `tenants`/`workspaces`.
4. Set the source `enabled = True`.
5. Set `connector_rss_live_enabled = true` **in the canary environment only**.
6. Confirm `connector_rss_kill_switch = false`.
7. Verify the four-market smoke and the connector-disabled path remain green.

At any missing/misconfigured step the connector resolves to the fixture path.

## 2. Canary rollout

- Start in `staging` with one source, one tenant/workspace, low `daily_limit`.
- Use `connector_rss_live_canary_ids` to limit to explicit IDs.
- Observe metrics (§4) for at least one healthy interval before widening.
- Widen tenant/workspace allowlist incrementally; never enable all tenants at once.

## 3. Health checks

- Source health = recent successful fetch within `fetch_interval_seconds × N`.
- Breaker state = `closed` (healthy) / `open` (backing off after repeated failures).
- Not-modified (304) responses are healthy and expected via ETag/Last-Modified.

## 4. Metrics

Provider-neutral, bounded-cardinality (routed through `app/core/metrics.py`):

- `connector_fetch_total` (labels: `operation`, `outcome`)
- `connector_fetch_rejected_total` (label: `error_class` — coarse category)
- `connector_signals_total` (label: `outcome`)
- `connector_source_health` (gauge)

Labels are **stable source IDs and coarse error categories only** — never raw URLs,
hostnames, tenant/workspace IDs, error messages or content text. Query strings and
sensitive headers are redacted; correlation/tracing is preserved.

## 5. Alerts

| Alert | Condition | Action |
|-------|-----------|--------|
| Validation-rejection spike | rejection rate > threshold | Investigate source/redirect; consider kill switch |
| DNS/redirect rejection spike | > threshold | Possible rebinding/poisoning; trip kill switch |
| Timeout/error rate high | > threshold over window | Back off; check source health |
| Source unhealthy | no success within N intervals | Disable source; investigate |
| Cost-ceiling breach | daily fetches near `daily_limit` | Throttle; review limits |

## 6. Retry behavior

Bounded retries for retryable classes only (timeout, network, rate-limited,
upstream 5xx). `Retry-After` honored within bounded limits, with jitter. No retry on
policy/URL/DNS/redirect rejection, invalid media type, permanent 4xx, or malformed
feed. Retry storms prevented by attempt caps + jitter + breaker.

## 7. Failure categories

`policy_blocked`, `dns_unsafe`, `redirect_unsafe`, `timeout`, `connection`,
`http_4xx`, `http_429`, `http_5xx`, `response_too_large`, `media_type_invalid`,
`parse_error`, `empty_feed`, `duplicate_only`, `source_disabled`,
`jurisdiction_blocked`.

## 8. Kill switch

`connector_rss_kill_switch = true` overrides **all** activation immediately,
regardless of any other flag or source state → instant revert to the fixture path.

## 9. Source revocation

Set the source `enabled = False` or remove its record. No migration/redeploy of
logic required.

## 10. Incident response

1. Trip `connector_rss_kill_switch` (fastest global stop).
2. Disable the implicated source.
3. Capture coarse metrics + correlation IDs (no raw content).
4. Assess contamination/isolation via the isolation tests and audit trail.
5. File an incident per `docs/operations/incident_response.md`.

## 11. Rollback

- Instant: `connector_rss_kill_switch = true` **or** `connector_rss_live_enabled =
  false`.
- Full: revert the Batch 2 branch. No schema/data changes to undo (no migration).
