# Live connector threat model (Phase 3B Batch 2)

**Scope:** the controlled live RSS/news fetch boundary. **Status:** safety
foundation built and tested offline; **live egress disabled** (no real-egress
transport exists). This model guides the code that *will* perform egress once owner
+ legal sign-off lands.

_Independent-review status: NO INDEPENDENT THIRD-PARTY REVIEW COMPLETED._

## 1. Data flow

```
scout request → registry.resolve (policy + approved-source lookup)
             → SafeFeedClient.fetch(approved_source, url)
                 → URL validation (scheme/host/port/credentials/raw-IP)
                 → DNS resolution guard (every address public; else reject)
                 → transport.fetch (INJECTED; default = fail-closed refuse)
                 → redirect guard (per-hop re-validate; re-resolve DNS)
                 → response limits (size / media-type / decompression)
             → safe XML parse (reject DOCTYPE/entity; size cap)
             → content isolation (HTML→text, length cap, injection neutralize)
             → normalize → ConnectorSignal (+ attribution, is_simulated=False)
             → dedup (scoped key) → pipeline → evidence/opportunity
```

## 2. Trust boundaries

- **Untrusted:** everything past `transport.fetch` — DNS answers, HTTP responses,
  redirect targets, feed bytes, feed text, entry URLs.
- **Trusted:** the approved-source registry, the validation/guard code, config.
- The domain layer consumes **normalized signals only**, never raw HTTP responses.

## 3. Threats → controls

| Threat | Control |
|--------|---------|
| **SSRF** to internal services | https-only, host+port allowlist, no raw-IP URLs, reject loopback/private/link-local/multicast/reserved/metadata; approved-source match required |
| **DNS rebinding** | Resolve before connect; reject if *any* resolved address is non-public; re-resolve + re-validate after each redirect; hostname validation is never trusted alone |
| **Open redirect / redirect to private net** | Bounded redirect hops; per-hop URL re-validation + DNS re-check; reject scheme downgrade; reject cross-host to a non-allowlisted host; reject private-IP target; never forward credentials/sensitive headers across hosts |
| **Oversized response** | Hard `max_response_bytes` cap enforced during read (streamed/bounded), before parse |
| **Decompression bomb** | Bounded decompression with a decompressed-size ceiling |
| **XML entity / DOCTYPE (XXE / billion-laughs)** | Reject any DOCTYPE/entity declaration; size cap before parse (inherited from Batch 1 `_assert_safe_xml`) |
| **Slow-loris / connection exhaustion** | Connect + read + total timeouts; bounded global concurrency |
| **Malicious feed content** | Treat as untrusted; HTML→text; no script/markup execution |
| **Prompt injection in feed text** | Provenance labeling, quoted-content separation, instruction neutralization, max text length; no automatic tool invocation from feed text |
| **Unsafe URLs in entries** | URL safety screening before any use; unsafe entry URLs dropped |
| **Duplicate / replayed content** | Scoped dedup key (source+tenant/workspace+location+market+item id+fingerprint) |
| **Cross-market contamination** | FetchScope market binding + jurisdiction allowlist + market-scoped dedup; four-market isolation tests |
| **Source impersonation / poisoned feed** | Allowlist-only host match; attribution recorded; kill switch on anomaly |
| **Log injection** | Coarse, secret-free failure details; no raw response/URL text in logs |
| **Secret leakage** | No credentials on signals or in metrics; no cookies/authorization headers sent |
| **Retry storms** | Bounded retries, retryable-only classes, `Retry-After` honored within bounds, jitter |
| **High-cardinality telemetry** | Bounded metric names + labels; stable source IDs + coarse error categories only (no URLs/hosts/tenant IDs) |
| **Unsafe config defaults** | Fail-closed: default-off flags; malformed config → fixture path; source without legal+owner approval cannot activate |

## 4. Residual risks

- **Publisher-side content correctness** — a legitimate feed could still carry
  low-quality or misleading text; scoring/credibility (Phase 3C) mitigates, not this
  batch.
- **TOCTOU between DNS validation and connect** — mitigated by re-resolution and
  (at enablement) pinning the validated address in the transport; a fully
  pinning-capable real transport is part of the enablement change, not this batch.
- **Approved-source misconfiguration** — mitigated by required legal+owner approval
  gating and conservative defaults, but human error remains possible; canary-first
  rollout limits blast radius.

## 5. Validation tests

Offline, deterministic, fake DNS + fake transports
(`app/tests/test_live_connector_safety.py`): URL/SSRF, DNS (public/private/mixed/
rebinding), redirect (approved/unapproved/private/downgrade/excessive), HTTP limits
(timeout/oversize/media-type/304/ETag/Retry-After/bounded retry/permanent-4xx),
feed handling (RSS/Atom/malformed/DOCTYPE/oversize/attribution/dedup/untrusted
label), isolation (tenant/workspace/location/jurisdiction/market/source-state/dedup),
config (default-off/malformed-fails-closed/unapproved-cannot-activate/kill-switch/
fixture-default). **No real network calls.**

## 6. Rollback triggers

- Validation-rejection or DNS/redirect-rejection rate exceeds threshold.
- Any evidence of contamination across tenant/workspace/location/market.
- Source poisoning or unexpected redirect behavior.
- Timeout/error-rate breach or cost-ceiling breach.

Response: trip `connector_rss_kill_switch` (instant), disable the source, or set
`connector_rss_live_enabled=false` / revert the branch.
