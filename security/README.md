# Merge-path security scanning and SBOMs (P6-CI-6)

This directory wires CVE, dependency, SAST and IaC analysis plus SBOM generation into the
two existing **required** CI contexts that build artifacts — `Container build and security`
and `Revision reader (unit, IaC contract, in-image)` — so that every pull request to `main`
and every push to `main` is analysed before merge. *(Since the coverage-gap tranche of
2026-10-20, the four other required contexts — `Frontend quality`, `Backend quality`,
`Migrations and API contract` and `Integration smoke` — also scan, as installed, the API virtualenv
they install (`apps/api/.venv`, and the lock toolchain `.locktools` in Backend quality), and every
context analyses the Trivy binary on its PATH; their other installs (`node_modules`, the Playwright
browser) are not scanned as installed. See "Coverage-gap tranche".)* The six required context names and the
repository ruleset are unchanged. No optional job, no manual workflow and no external
analysis service is involved; the publish workflows (`staging-publish.yml`,
`reader-publish.yml`) and their Trivy steps are untouched.

## What runs, where, over what

| Category | Tool | Job | Population analysed | Report |
|---|---|---|---|---|
| Vulnerabilities, locked dependencies | Trivy 0.76.0 (`aquasecurity/trivy-action@v0.36.0`, `trivy-config: security/trivy.yaml`) | Container build and security | `package-lock.json` (runtime and development npm packages), `apps/api/requirements-runtime.txt`, `apps/api/requirements-dev.txt`, `apps/api/requirements-bootstrap.txt` and `apps/api/requirements-locktools.txt` (since 2026-10-20), `scripts/requirements-gate.txt`, `scripts/requirements-gate-hashes.txt`, `security/requirements-semgrep.txt`, `apps/revision-reader/requirements-dev.txt`, `apps/revision-reader/requirements-toolcache-pip.txt` (2026-10-21) | `trivy-locks-vuln.json` |
| Vulnerabilities, installed tooling (2026-10-19; extended 2026-10-20) | Trivy 0.76.0 `rootfs`, reconciled against the environment's own `pip inspect` (and, for the hash-locked API environments, against their locks) | Container build and security; Revision reader; since 2026-10-20 Frontend quality, Backend quality, Migrations and API contract, Integration smoke | `.security-venv` (Semgrep and its hash-locked set, ~~plus the interpreter's bundled pip~~ pip included in the lock since CERT-2, 2026-10-21, replacing the bundled one); `.reader-venv` (the hash-locked reader tooling set); the reader job's interpreter prefix (`pythonLocation`: ~~its pip~~ the locked pip of `apps/revision-reader/requirements-toolcache-pip.txt`, installed over the toolcache's bundled one since 2026-10-21, and the Gate 4N PyYAML); `apps/api/.venv` in each of the four API jobs (the bootstrap and dev locks plus the editable project); `.locktools` in Backend quality (its lock) — each as installed, inventoried again before completeness | `trivy-*-tooling-*-vuln.json`, `trivy-api-venv-vuln.json`, `trivy-locktools-vuln.json`, `*-installed*.json` |
| Vulnerabilities, packages an installed pip vendors (2026-10-20) | Trivy 0.76.0 `fs` over the pins each installed pip declares in `pip/_vendor/vendor.txt`, staged by `stage_inputs.py` | every job above, for each environment it scans as installed | the vendored packages of every installed pip of those environments (they carry no dist-info, so the `rootfs` scan sees them only when pip ships an SBOM of them) | `trivy-vendored-pins-vuln.json` |
| Vulnerabilities, scanner and tool binaries (2026-10-20) | Trivy 0.76.0 `rootfs` Go binary analysis of copies of the binaries resolved on PATH, bound to pinned release digests (`scanner-binaries.json`) | every job (Trivy); Revision reader also OpenTofu | the `trivy` and `tofu` executables the jobs run: their Go module graph and Go standard library version | `trivy-scanner-binaries-vuln.json`, `scanner-binaries-manifest.json` |
| Vulnerabilities, built images | Trivy 0.76.0 | Container build and security; Revision reader | OS packages and installed Python packages of `signalnest-api:ci`, `signalnest-worker:ci`, `signalnest-revision-reader:ci` as built in that run | `trivy-image-*-vuln.json` |
| SAST | Semgrep 1.181.0 (hash-locked install, `security/requirements-semgrep.txt`) with registry rulesets `p/python`, `p/typescript`, `p/javascript`, `p/react`, `p/security-audit` | Container build and security | `apps/`, `scripts/`, `security/` minus `.semgrepignore` (tests, e2e, generated and vendored paths) and `security/controls` | `semgrep.json`, `semgrep-rules-provenance.json` |
| IaC / configuration | Trivy 0.76.0 misconfiguration checks | Container build and security | `infra/aws` OpenTofu root and modules, `infra/aws/bootstrap`, both Dockerfiles (13 targets, 246 checks at the baseline) | `trivy-config.json` |
| SBOM | Trivy 0.76.0, CycloneDX 1.7 JSON | both jobs | the three images and the repository locks | `sbom-*.cdx.json` + `sbom-*.identity.json` |

Every report is produced with `exit-code: 0` and then evaluated by `security/gate.py`, which
fails the job itself. Each gate writes `security-reports/<name>.summary.txt`; a final
`complete` step refuses to pass unless every expected summary exists with the expected
verdict, and digests every file into `security-reports/MANIFEST.txt`. The whole directory is
uploaded as a workflow artifact (`security-reports-container-build`,
`security-reports-revision-reader`; *since 2026-10-20 also `security-reports-frontend`, `-backend`, `-migrations` and
`-integration`*) with 30-day retention, on success and on failure.

### What is NOT covered (declared exclusions)

- ~~The revision reader's `pyproject.toml` has no lock; its dependency set is analysed only as
  installed inside the built reader image (OS packages and `psycopg`), not from a manifest.~~
  *(2026-10-19: the reader's CI tooling set — its dependencies, the `[dev]` extra, pip, the
  setuptools build backend and PyYAML — is locked with hashes in
  `apps/revision-reader/requirements-dev.txt`, scanned as a lock and as installed. The shipped
  image is unchanged: its Dockerfile copies `pyproject.toml`, `revision_reader/` and `assets/`
  only, and its runtime set is still analysed as installed inside the built image.)*
- GitHub Actions workflow files, `infra/docker-compose.yml` and `.github/dependabot.yml`
  are not configuration classes Trivy's checks evaluate; they are not IaC-scanned.
- Semgrep runs the community registry rulesets. Their TypeScript/React content is largely
  framework-specific (Express, Next, Node crypto, JWT libraries); the rules that apply to a
  Vite/React single-page application are few. The scan still parses every `.ts`/`.tsx`
  file (the gate requires `apps/web/src` and `.tsx` in the scanned population) but SAST
  depth for the frontend is rule-limited, and that limitation is not closed here.
- Test directories, e2e fixtures and the negative-control fixtures are excluded from SAST
  (`.semgrepignore`, `--exclude security/controls`); the control fixtures are excluded from
  the real Trivy scans with `skip-dirs`.
- `security/requirements-semgrep.txt` is a real manifest and GitHub's dependency graph will
  index it (development scope, CI tooling); `.github/dependabot.yml` does not schedule
  version updates for it, so it is regenerated by hand. The negative-control pin list is
  deliberately not named like a manifest so no alert is ever raised for a fixture.
- No secret scanning is added here (`scripts/leak_scan.py` and the image security check are
  the existing controls).
- Nothing is signed or attested; the SBOMs are files bound to the commit and image id by
  the identity records, not provenance attestations.

## Policy — ADOPTED for the merge path (operator order 2026-10-19)

~~Policy — PROPOSED, enforced, not adopted~~ *(the heading until 2026-10-19)*

`security/policy.json` holds the thresholds the gates enforce. Since the operator order of
2026-10-19 its `_status` is ADOPTED for merge-path enforcement: it governs the required gates of
`ci.yml` only, and changes no live environment, deployment or publish workflow
(`staging-publish.yml` and `reader-publish.yml` keep their own Trivy steps and thresholds).

- **Vulnerabilities (adopted 2026-10-19)**: rules are per severity, and the severity is
  evaluated first. **CRITICAL fails in every scope** — built images, runtime dependencies and
  development, test and security-tool dependencies, including installed tooling environments —
  whether or not a fix is published; neither fix availability, development classification nor
  a missing `FixedVersion` can filter a CRITICAL out. **HIGH** keeps the earlier treatment,
  unchanged: images and runtime dependencies fail when a fix exists (a non-empty
  `FixedVersion`, exactly as before), development-scope HIGH findings are `REPORTED`, and
  unfixed HIGH findings are listed as `EXCLUDED`. MEDIUM and LOW are reported. ~~**Not ruled by
  the order, and disclosed as the implementation's reading (unchanged from before the
  adoption) pending an operator ruling:** the severity evaluated is Trivy's selected `Severity`
  field, so a finding another source rates CRITICAL but Trivy selects lower is evaluated at
  Trivy's severity.~~ *(Superseded 2026-10-19 by the operator decisions below.)* Instances at
  adoption, kept as disclosed: `CVE-2019-1010022` in `libc6`/`libc-bin` (API and
  reader images; NVD 9.8, Debian and Trivy LOW) and `CVE-2026-19445` in the reader image's
  `python3.13` packages (two vendors CRITICAL, Trivy HIGH, no fix: `EXCLUDED`). ~~UNKNOWN
  (unscored) findings are reported like the lower severities~~ *(superseded the same day, below)*,
  for example `CVE-2026-82560` in the API image's `perl-base` (no fix).
- **Severity source and UNKNOWN (operator decisions of 2026-10-19, merge-path enforcement
  only):**
  - **Severity source.** The gate result is decided by the `Severity` that the pinned Trivy
    (0.76.0) selects by its documented default selection: the selected data source's vendor
    severity, else a severity derived from the CVSS score, else NVD, else other vendors' ratings
    in trivy-db's documented order of preference, and only then `UNKNOWN`. No source override (such
    as `--vuln-severity-source`) is used; the pinned configurations, the reviewed trivy-action
    inputs and the gate's `TRIVY_*` refusal keep it that way. The reports keep `SeveritySource`,
    the alternate vendor ratings (`VendorSeverity`) and the CVSS data.
  - **The guarantee, stated precisely:** Trivy-selected CRITICAL findings fail across the
    covered populations, with or without a fix. A finding that NVD or another vendor rates
    higher than Trivy's selection is evaluated at Trivy's selection. That does not establish the
    finding's harmlessness or remediation; the cases above stay disclosed. Measured in the CI
    reports of `52d0aa6e`, three CVEs are rated CRITICAL by some vendor above Trivy's selection:
    the two above and `CVE-2025-69720` in `libncursesw6`/`libtinfo6` of all three images (two
    vendors CRITICAL, Trivy HIGH, no fix: `EXCLUDED`). In all, 166 report rows carry some vendor
    rating above Trivy's selection; the complete list is kept in the review-request evidence
    record. *(Re-measured 2026-10-23 in push run 39859641559 on `main` `831cc2ea`, counted over every
    `trivy-*vuln.json` report in that run's `security-reports-*` artifacts: six distinct CVEs that some vendor rates
    CRITICAL are selected lower — the three above (`CVE-2019-1010022` is in the worker image too, in that run and in
    run 39310040179 on `52d0aa6e`) and, in the OpenTofu binary, `CVE-2026-39821` (`golang.org/x/net`
    and `stdlib`, selected HIGH), `CVE-2026-56857` (`stdlib`, LOW) and `CVE-2026-78663` (`golang.org/x/net` and
    `stdlib`, MEDIUM); 187 report rows carry some vendor rating above Trivy's selection. The two row counts cover
    different populations: over the seven reports of the earlier record (run 39310040179 on `52d0aa6e`, 166 rows)
    the comparable count is 164, and the other 23 rows are the OpenTofu binary's, in Revision reader's
    `trivy-scanner-binaries-vuln.json`, a population scanned since 2026-10-20. Neither count is a resolution of the
    earlier ones; in general, the set changes with Trivy's database.)*
  - **UNKNOWN.** A valid `UNKNOWN` severity means unscored. The finding stays reported for
    triage (the summary lists it with the reported severities, tagged `[UNKNOWN]`); it is not
    LOW, resolved or proof of safety. A missing or unrecognized severity, a malformed report, an
    unknown scope and an incomplete analysis continue to fail closed. A finding later classified
    CRITICAL falls under the unconditional CRITICAL rule. `test_gate.py` pins each case.
- **Scope of the Trivy and OpenTofu executables — operator decision of 2026-10-21 (bounded).** "I adopt
  development/tooling scope for the Trivy and OpenTofu executables used as CI tools in this tranche, provided you verify
  that these classified artifacts are not shipped or executed as application runtime components." This is a new, dated
  decision. The order of 2026-10-19 did not explicitly settle it, and the coverage-gap tranche's sentence that placed the
  binaries in the adopted scope text was that tranche's reading. The condition was verified on 2026-10-21 with no
  discrepancy: no Dockerfile or task definition installs, copies or runs either executable; Trivy runs only on the CI
  runners, and OpenTofu only in Revision reader's IaC contract steps, never against real state. `policy.json` records it
  under `_operator_decisions` (the limbs verbatim) and enforces it as `vulnerabilities.binary_scope = development`; a
  missing or unrecognised value fails the policy closed (exit 2). Its limbs:
  - Trivy-selected CRITICAL findings continue to fail regardless of fix availability.
  - Development/tooling HIGH findings remain reported under the existing adopted severity rules.
  - OpenTofu's 28 reported HIGH findings remain unresolved findings, listed with their identities, affected versions,
    available fixes and limitations under "Coverage-gap tranche", repair 4, with the accountable owner.
  - It is not a finding-specific exception, a remediation claim or a declaration that the tools are safe.
  - It authorizes no AWS execution, IAM change, deployment or provider activation.
  - A component shipped or used at runtime keeps its image/runtime treatment. Vendored packages inherit the scope of
    the environment that contains them; there is no universal tooling exemption. The installed CI tooling environments
    are development scope; vendored or bundled code inside a shipped image is runtime content, and today it is not
    analysed (see the populations table: decision D4b).
- **Lock targets and report shape.** Every lock target Trivy reports must be
  declared runtime or development in the policy; an undeclared target is an incomplete
  analysis (exit 4), and a finding without a recognised severity is a malformed report
  (exit 2), as is a report whose Results, Packages, identifiers or findings do not have the
  scanner's shape (never read as "no findings"). The policy file is validated: a rule set with a
  gap, an overlap, an unknown scope or the superseded key shape is refused (exit 2).
- ~~**Vulnerabilities**: CRITICAL/HIGH fail, and only when a fixed version exists — this is
  the publish-time Trivy policy (`docs/operations/staging-publish-workflow.md` §5/§9: an
  INFRA-5-set threshold "pending an explicit repository-wide severity decision",
  `ignore-unfixed: true` "subject to review") carried onto the merge path. Every finding the
  fix-availability filter excludes is printed as `EXCLUDED` in the summary; nothing is
  hidden. Runtime-scope lock findings and image findings gate; development-scope lock
  findings (npm `dev`, `requirements-dev.txt`, `requirements-gate.txt`, the Semgrep lock) are
  `REPORTED` and do not gate (exit criterion 12 names runtime dependencies). Gating
  development scope too is an open operator choice.~~ *(Superseded 2026-10-19 by the adopted
  rules above; the HIGH part is unchanged.)*
- **IaC** *(threshold and the six named exceptions explicitly adopted, unchanged, 2026-10-19)*:
  CRITICAL/HIGH misconfigurations fail. No source sets an IaC threshold; this is by
  analogy with the vulnerability threshold. ~~No ignore file is tracked, so findings the
  infrastructure accepts by documented decision (for example the public ALB, or the WAF
  deferral recorded as `P6-INF-11`) currently FAIL this gate until an operator adopts a
  per-check exception file with written justifications, or remediates.~~ *(2026-10-18, operator order: the
  reviewed exceptions file `security/misconfig-exceptions.json` is adopted for the six class
  A/B/C findings and the four class D findings are remediated — see "IaC exceptions" below.
  No Trivy ignore file is used, the threshold is unchanged, and an excepted finding is still
  listed.)* *(Since 2026-10-19, `test_scanner_configuration.py` fails on any of:
  - an ignore or Trivy configuration file in the checkout;
  - such a file named in the workflow;
  - a trivy-action step that is not in a reviewed line shape.
  Every gate over a Trivy report also refuses one present in its working directory at run time.
  See "Closure tranche".)*
- **SAST** *(adopted, unchanged, 2026-10-19; so are scanner-error handling and the SBOM rules
  below)*: Semgrep `ERROR` severity fails; `WARNING`/`INFO` are reported. Any Semgrep
  scanner error (a rule timeout, a parse failure) makes the analysis incomplete and fails.
- **SBOM identity**: each identity record names the reviewed commit (`commit`: the pull
  request head, or the pushed commit on `main`) and the commit actually checked out
  (`checkout_commit`: on a pull_request event GitHub builds the ephemeral merge ref), the
  image id, the SBOM's serial and sha256 and both component counts.
- **SBOM**: CycloneDX 1.5–1.7 JSON with a `urn:uuid` serial, the expected described
  component, a non-empty **package-component** population (`library`/`application`/
  `framework` entries only; the total entry count is reported separately, so padding with
  `file` entries cannot satisfy the floor) above a per-artifact floor, exactly one
  `ImageID`/`RepoDigest` identity value and, for images, the same `ImageID` as the
  vulnerability report of the same job.

## Baseline on `main` at the time of wiring (measured, not adjudicated)

Measured locally on `main` `bd1e577b` with the same tool versions (images: no Docker on the
measuring host, so the image baseline is the first complete CI run of this branch, below):

- Locks: 5 npm advisories — 2 HIGH (`source-map-js` 1.2.1, fixed in 1.2.2; `braces` 3.0.3,
  no fix), 3 MEDIUM (`react-router` ×2, `postcss-selector-parser`); all development scope
  except `react-router` (runtime, MEDIUM). Python locks: 0. Gate verdict: PASS (HIGH
  development findings reported; the unfixed one excluded).
- IaC: 27 failed checks — 3 CRITICAL (`AWS-0104` unrestricted egress, `modules/ecs`),
  7 HIGH (`AWS-0053` public ALB, `AWS-0011` no WAF, `AWS-0015`/`AWS-0132` customer-managed
  keys ×5), 5 MEDIUM, 12 LOW. Gate verdict: **FAIL (policy violation)** — these are existing
  recorded infrastructure findings, not something this wiring introduces or resolves.
- SAST: 1 ERROR (`use-defused-xml`, `apps/api/app/connectors/rss.py:25`, confidence LOW),
  3 WARNING (SHA-1 in `app/infra/vector.py`, `exec()` in two governance scripts). Gate
  verdict: **FAIL (policy violation)** — an existing product finding.

- Images (CI run 39110119516 of this branch, Trivy DB of 2026-10-18; the Dockerfiles are
  unchanged from `main`): `signalnest-api:ci` and `signalnest-worker:ci` each carry 87 OS
  packages (Debian 13.7) and 66 Python packages; 44 HIGH OS findings with no fix are
  `EXCLUDED` and listed; 4 HIGH with a fix violate — `urllib3` 2.7.0, `msgpack` 1.1.2 and
  `setuptools` 70.3.0, all vendored inside pip 26.2.1, which the Dockerfile's
  `pip install --upgrade pip` installs into the runtime venv (the application's own
  `urllib3` is the locked 2.8.0). Gate verdict: **FAIL (policy violation)** — a Dockerfile
  finding (ship the venv without pip, or pin pip), not a lock finding.

- Reader image (CI run 39110876776, the first run that reached the reader gate): `signalnest-revision-reader:ci` on its digest-pinned
  `gcr.io/distroless/python3-debian12` base (Debian 12.13, Python 3.11.2) carries 43 HIGH OS
  findings with a fixed Debian version available (libexpat1, krb5, libpython3.11, libssl3)
  and 20 without (2 CRITICAL: libsqlite3-0 affected, zlib1g will-not-fix). Gate verdict:
  **FAIL (policy violation)**.

So with the proposed policy the `Container build and security` context was red on `main`
until an operator either adopts exceptions (with written justification), changes the
proposed thresholds, or the findings are remediated in their own tranches. The gate is not
relaxed to hide that.

## Remediation tranche (2026-10-18; policy, thresholds, filters, gates and controls unchanged)

- **SAST — fixed.** `apps/api/app/connectors/rss.py` parses with `defusedxml`
  (`forbid_dtd`, `forbid_entities`, `forbid_external`) behind the unchanged 2 MiB bound and
  DOCTYPE/entity byte screen; refusals map to the existing `UNSAFE_CONTENT` failure and
  `ParseError` to `PARSE_ERROR`; the stdlib `xml.etree` import is gone. Six regression tests
  (valid feed, a DOCTYPE past the 4 KiB entity screen, a UTF-16 DOCTYPE the byte screen
  cannot see, an external entity that is never resolved, a valid UTF-16 feed, an oversized
  feed). `defusedxml==0.7.1` joins both pip-tools locks; no other pin moved. With the same
  saved rulesets as the baseline CI run, the finding goes from 1 ERROR to 0 because the import
  is gone, not because a rule changed. The four SAST WARNINGs remain reported.
- **API and worker images — fixed.** The builder uninstalls pip from the venv after the last
  install, so pip 26.2.1 and its 18 vendored packages (including `urllib3` 2.7.0, `msgpack`
  1.1.2 and `setuptools` 70.3.0) no longer ship: Python packages per image 66 → 48, Python
  HIGH findings 4 → 0, image gates PASS (CI run 39120145845). The 44 unfixed Debian 13.7 HIGH
  findings are still `EXCLUDED` and listed, and the base image's own pip 25.0.1 under
  `/usr/local` is still present (no HIGH reported for it). Imports, migration actor,
  schema gate, bare migration command, non-root and no-secrets checks all still pass.
- **Reader image — NOT fixed (blocker).** Upstream `python3-debian12:latest` resolves to the
  pinned digest itself (same image id) and the in-family tags carry the same packages, so no
  compatible digest refresh exists today. Moving to Debian 13/Python 3.13 or a non-distroless
  base would change the frozen reader contracts (Python minor, ENTRYPOINT, containment) and
  is not done. The 43 fixable and 20 unfixed findings remain; the reader gate stays red.
  *(Superseded 2026-10-18, operator order: candidate A adopted — see "Adoption tranche" below.)*
- **IaC — unchanged (decision pending).** The 10 gating findings remain. A per-finding
  disposition matrix with proposed, unadopted exception wording is in the evidence record;
  no infrastructure file changed and no ignore file exists. *(Superseded 2026-10-18, operator order: see
  "Adoption tranche" below.)*
- **Ruleset fetcher — repaired.** The registry served `p/javascript` as JSON in one run and
  YAML in the next. `fetch_semgrep_rules.py` now parses either form structurally (JSON with
  the standard library, YAML with the hash-locked `ruamel.yaml` safe loader), checks every
  rule entry, writes the raw bytes unchanged under `.yml` (Semgrep's directory loader skips
  `.json` files while `--validate` still exits 0), records the detected format, and stages
  the population so an incomplete set is never left for the scan or the SAST control.
  YAML anchors and aliases are refused before anything is constructed: a follow-up review
  showed that a document of under 2 KB of nested aliases passed the structural checks and
  kept `semgrep --validate` at full CPU past a 30-second limit (the registry's five rulesets
  carry none). The fetch step also has a 10-minute timeout (it takes about 10 seconds), so a
  ruleset that still stalls validation fails the step instead of holding the runner for the
  six-hour job limit. Its twelve tests (`fetcher_selftest.py`) run in the fetch step under
  the hash-locked venv.

## Adoption tranche (2026-10-18, operator order; thresholds and filters unchanged)

- **Reader image — candidate A adopted.** The reader moves to
  `gcr.io/distroless/python3-debian13` (Python 3.13.5) with a `python:3.13-slim` builder;
  the Python minor, distribution, interpreter path (`/usr/bin/python3.13`, also in the
  reader publish workflow's band) and both base digests change, and every reader control is
  kept (`apps/revision-reader/`). Measured on the proposal's local build with the same Trivy
  database: 43 violating / 20 excluded → 0 violating / 26 excluded. The gate passes only
  because of the fix-availability filter: the image is **not** vulnerability-free (26 HIGH
  findings with no published fix stay `EXCLUDED` and listed). It now ships 17 executables
  instead of 4 (Debian 13 `libc-bin`; no shell), and a `PYTHONPATH`/`PYTHONHOME` override
  still stops the interpreter before the reader runs (exit 1, as before). Neither is changed
  here, and no executable allow-list is added.
- **IaC class D — remediated** in the IaC (customer-managed keys for the SPA bucket, the
  audit trail and bucket, and the application bucket, which reuses the secrets key because
  the live role boundary admits no other; see the module READMEs under `infra/aws/modules/`).
  Nothing is applied.
- **IaC classes A, B and C — excepted** by `security/misconfig-exceptions.json` (below).

## IaC exceptions (`security/misconfig-exceptions.json`)

Four entries cover the six findings: `A1` (AWS-0104, the three TCP 443 IPv4 task egress
rules), `A2` (AWS-0053, the public HTTPS-only ALB), `B1` (AWS-0011, no WAF on the SPA
distribution — the `P6-INF-11` deferral) and `C1` (AWS-0132, the ALB log bucket, which ELB
log delivery requires to be SSE-S3). Each entry's statement, review trigger and expiry are
the sealed disposition packet's wording, adopted verbatim. An exception lets this gate pass
**for that finding only**; it closes, satisfies or reduces no Phase 6 row (`P6-INF-11` and
`P6-INF-1` stay open) and may not be cited for production.

How `gate.py misconfig --exceptions` decides, for every finding at gating severity:

1. The finding's file and start line are resolved to the **enclosing resource block** in the
   checked-out source (Trivy reports only a module address). The report's code lines must
   equal the file's lines, or the analysis is incomplete (exit 4); a resource address in the
   report's occurrences must agree with the resolved block.
2. The finding is `EXCEPTED` only if an entry names the same check, file and resource **and**
   the number of matched findings equals `expected_instances` **and** every condition holds
   **and** today (UTC) is before `expires`. Excepted findings are printed, never dropped.
3. Conditions read **literal** values only. A variable, reference, function call, template,
   heredoc or `dynamic` block cannot be confirmed and fails closed. Resource conditions read
   the one declaration of the block; an override file (`override.tf`, `*_override.tf`) or a
   `.tofu` file in the same module directory, or a second declaration, fails closed, because
   Trivy reports such a change at the original block. `A2`'s tree-scoped condition reads
   **every** `aws_lb_listener` and `aws_alb_listener` in every `.tf`/`.tf.json`/`.tofu` file
   under `infra/aws` (root and modules), requires at least one, and requires each to declare
   the literal `protocol = "HTTPS"`. It also fails closed on anything a textual reading cannot
   see through anywhere in that tree: a `.tofu` file, a module whose source is not a local
   path or is a local path that lies outside `infra/aws` as OpenTofu cleans it lexically or as it
   resolves on disk, any symlink under `infra/aws`, a provisioner, a generic provisioning resource (CloudFormation, Cloud Control,
   `awscc_*`) or `data "external"`.
4. An expired entry, an entry that matches nothing (stale), a count mismatch or a failed
   condition is itself a policy violation, and the findings it would have covered are listed
   as violations.

The exceptions file is schema-checked: unknown or missing keys, a duplicate entry, a target
outside `infra/aws` or not a `.tf` file, a missing expiry on anything but a verified
limitation (`C1` alone has none, with its reason), or a malformed condition is `FAIL (report
missing or malformed)` (exit 2).

**Negative controls.** The existing inert IaC control must still be a violation with the
exceptions loaded (it names its check, `AWS-0107`). `iac_exception_controls.py` builds 18
mutated copies of the real tree plus an unmutated baseline; one real Trivy scan covers them
all, and the real gate must report each mutation's own violation token (`--expect-fail
--expect-violation`) while the baseline passes (`--control-baseline`). The mutations: a
widened, IPv6, overridden or `.tofu`-shadowed egress rule, a fourth workload, an expired and
a stale entry, an HTTP listener in the alb module, in a new module file, in the root module
(attached through `module.alb.alb_arn`) and under the alias type, a variable protocol, a
non-local module source, a local module source that escapes `infra/aws` (`../`, or a symlink
followed by `..`), a `.tofu` listener, a provisioner and a non-SSE-S3 log bucket.
Several of these are caught **only** by the gate: in the local run of Trivy 0.76.0 on
2026-10-18, Trivy raised nothing for the root-module and alias-type listeners, the variable
protocol, the provisioner or the remote module.
`--report-prefix` (one copy of a multi-copy scan) and `--expect-violation` are refused
without `--expect-fail` or `--control-baseline`, so the real gate cannot be pointed at part
of a report. `test_gate_exceptions.py` and `test_iac_exception_controls.py` pin the logic
and the case list (against the completeness step of `ci.yml`).

**Residuals, disclosed.** The mechanism reads text. It cannot see a listener that another
Terraform state creates and attaches by looking the ALB up, and its opaque-construct list is
a denylist. `C1` has no expiry: the annual review is a written trigger, not a mechanical one.

## Closure tranche (2026-10-19, operator order; IaC, SAST, SBOM and scanner-error rules unchanged)

The P6-CI-6 closure assessment found a CRITICAL finding merging clean in four classes: a
CRITICAL with no published fix; a CRITICAL in development scope (npm development packages, the
API dev lock, the Semgrep lock); a CRITICAL in the reader image with no fix; and anything in the
reader job's installed tooling, which no scan covered. This tranche closes them on the merge path:

- **Severity first.** `gate.py vuln` applies the adopted per-severity rules above.
- **Installed tooling is scanned as installed.** `gate.py vuln --kind tooling` takes a Trivy
  `rootfs` report of an environment and that environment's `pip inspect` inventory
  (`--installed-inventory`, `--env-root`). Every installed distribution must appear in the
  scan at its own metadata path with the same name and version, or the analysis is incomplete
  (exit 4). An inventory of another environment or below the floor fails the same way. ~~Vendored
  copies the scan also reports, such as pip's own vendored packages, are evaluated as well.~~
  *(Corrected 2026-10-20: a vendored copy is evaluated only when the scan reports it, and an
  installed-environment scan reports pip's vendored packages only when that pip ships an SBOM of
  them: pip 26.2.1 does (18 packages), as does pip 26.2.2 (18, certifi 2026.10.19 among them; CERT-2); pip 25.0.1 and 25.1.1 do not, so the vendored packages of the
  `.security-venv` and `.locktools` pips and of the base image pip 25.0.1 were not analysed. Since the
  coverage-gap tranche every installed pip's own vendor list is analysed by a separate scan.)* Each
  environment is inventoried a second time just before the completeness step, and the
  final inventory is gated against the same scan, so a package installed after the scan fails
  completeness. The inventories are taken without `--local`, so a distribution a path entry
  brings in from outside the environment is listed and fails the location check instead of
  being left out. A real-scanner control in each job (`control-tooling-rootfs`) scans an inert
  installed environment, `security/controls/tooling-env` (one dist-info of PyYAML 5.3.1,
  metadata only), with Trivy `rootfs`; the tooling gate must fail on CVE-2020-14343, so a scan
  that silently stopped finding installed packages cannot stay green.
- **The reader job's installs are hash-locked.** `.reader-venv` is installed with
  `--require-hashes --no-deps` from `apps/revision-reader/requirements-dev.txt`, compiled with
  pip-tools from `pyproject.toml` (base and `[dev]`) plus `requirements-dev.in` (pip,
  setuptools, PyYAML). The editable install runs with `--no-deps --no-build-isolation`, so no
  unpinned build dependency is fetched. The unpinned `pip install --upgrade pip` and the later
  `pip install pytest` are gone, and `pip check` must pass. The interpreter's bundled pip
  performs the first hashed install and is then replaced by the locked pip. The system
  interpreter's Gate 4N PyYAML comes from `scripts/requirements-gate-hashes.txt`, compiled
  from the unchanged contract `scripts/requirements-gate.txt` (same pin, `--require-hashes
  --no-deps`). *(2026-10-21, GHSA-6q48-6c7w-9gw2: before that install, the prefix's own pip is
  installed from `apps/revision-reader/requirements-toolcache-pip.txt` — pip alone, at the reader
  lock's pin and hashes, `--isolated --require-hashes --only-binary :all: --no-deps` — over the
  toolcache's bundled pip, which vendors a certifi bundle of its own that no repository file
  controlled; the bundled pip performs that one install and is replaced. `test_tooling_locks.py`
  keeps the pin equal to the reader lock's, the lock pip-only, and the install exactly once and
  ahead of the Gate 4N PyYAML install and the first prefix inventory.)* The venv's own `pip install -r scripts/requirements-gate.txt` line (pinned by
  Gate 4N-I28BH-E2) is kept and is already satisfied. It runs unhashed, so it is a no-op only
  while every PyYAML pin agrees. `test_tooling_locks.py` fails if they ever differ. It also
  fails if:
  - a requirement in the two locks lacks a hash;
  - the reader lock misses a dependency or the build backend that `pyproject.toml` declares;
  - the two jobs' installer-shaped lines differ from the reviewed set *(every job's since 2026-10-20)*.

  An installer-shaped line, with backslash continuations joined, is any of:
  - pip under any version suffix (`pip3.12`), or pipx, uv, conda, poetry, pdm, npm, npx, yarn,
    pnpm, gem, cargo, apt, apt-get or brew. The match covers a command word, a word after a quote
    or `$(`, and a `bin/` path;
  - `-m` with or without a space before pip, ensurepip, venv, virtualenv, pipx or uv;
  - `pip._internal`, `import pip` or `from pip`;
  - `--upgrade-deps`, `setup.py install`, get-pip, `go install` or setup-python's `pip-install`
    input;
  - a curl or wget download piped into a shell.

  The check reads text. An installer reached another way (for example through a script the job
  runs) is not seen by it. ~~Such an installer lands either in an environment the final
  inventories reconcile, where completeness fails, or outside every scanned environment, which
  is the disclosed limit.~~ *(Corrected 2026-10-20: a package installed and removed again between two
  inventories lands in neither — both inventories agree and completeness passes. Since the coverage-gap
  tranche the API venvs and `.locktools` are read-only from the end of their install, which stops an
  ordinary such install there; see "What is observed, and when" under "Coverage-gap tranche".)*
- **Controls.** `vuln_policy_controls.py` takes this run's real reports and runs the real gate
  on 32 mutated copies in the container job and 18 in the reader job *(44 and 35 since 2026-10-20,
  plus 20 in each of Frontend quality, Migrations and API contract and Integration smoke and 31 in
  Backend quality: 170 in all)*, each listed by
  `--expect-control control-vulnpol-*` in its job's completeness step. *(Label corrected 2026-10-21, decision D7,
  label-only: these are 170 policy/control cases, not 170 detected policy violations. In run 39514455981 the gate's own
  verdicts were 49 policy violations, 85 incomplete analyses, 1 malformed report and 35 passes — 17 baselines, 14 findings
  reported, 4 findings excluded — each the case's stated outcome. Every control summary nevertheless ended in "NEGATIVE
  CONTROL DETECTED (policy violation raised as intended)". The success line is now "CONTROL BEHAVED AS INTENDED (the
  outcome stated for it was observed)", and each summary also records the gate's own verdict on the case, so a case meant
  to fail closed shows its FAIL. The cases, their expected outcomes, the exit codes and what completeness requires are
  unchanged.)* The cases cover:
  - CRITICAL with and without a fix in images, runtime locks, npm runtime and development
    packages, the security-tool lock, the reader tooling lock, both installed tooling venvs and
    the reader job's interpreter prefix;
  - the shapes real reports carry: the no-fix statuses `will_not_fix`, `fix_deferred` and
    `end_of_life`; a fix recorded without a `FixedVersion`; an advisory-style id; a package
    identifier matching no package; a vendored copy without a `FilePath`; a Go binary in an
    image; node packages in a tooling scan;
  - the retained HIGH behaviour (violation, excluded, reported, and `Status: fixed` without a
    `FixedVersion` still excluded);
  - an undeclared lock target, a missing severity, and five ways a tooling population can be
    incomplete.

  `test_vuln_policy_controls.py` pins the case lists, the gate arguments, each gate's own
  inventory, and the order inventory → scan → gate → controls → final inventory → final gate →
  completeness against `ci.yml`. It also shows that under the superseded policy exactly the 24
  pass-through cases fail *(44 since 2026-10-20)*.

  Scanner-side suppression is closed structurally and then guarded at two points:
  - **Structurally.** Every trivy-action step passes a reviewed configuration: `security/trivy.yaml`,
    `security/controls/trivy-control.yaml` or `security/trivy-scan.yaml`. Each pins `ignorefile` to the
    tracked, empty `security/empty-ignorefile`. With an explicit configuration and an explicit ignore
    file, Trivy reads neither an ignore file nor a configuration file from its working directory. Such a
    file therefore cannot filter a scan, whichever code creates it (a script, a test, a background
    process) and whenever it does so. The pinned files themselves remain workspace files; see the
    disclosed limit below. This was measured locally with Trivy 0.76.0 for `fs`, `rootfs` and
    `config` scans, with and without the pinned configuration.
  - **Statically.** `test_scanner_configuration.py` fails on:
    - a `.trivyignore*` or `trivy.y*ml` at the repository root, which is the scans' working
      directory;
    - any change to the two tracked Trivy configurations;
    - such a file named anywhere in the workflow outside the two reviewed configuration paths;
    - a `TRIVY_*` variable in the workflow;
    - a trivy-action step that passes no reviewed configuration, a configuration that does not pin the
      empty ignore file, or an ignore file that is not empty;
    - any trivy-action step line outside the reviewed shapes. The step check reads line shapes,
      so a quoted key, a flow mapping, another indentation, an `env` block or an unreviewed input
      or value is rejected, not interpreted. Unreviewed inputs include `ignore-unfixed`,
      `severity`, `trivyignores` and `skip-files`.
  - **At run time.** Every gate over a Trivy report (`vuln`, `misconfig`, `sbom`) refuses to
    certify it (exit 4) while a `.trivyignore*` or `trivy.y*ml` exists in its working directory,
    which the scans share, or while any `TRIVY_*` variable is set. trivy-action v0.36.0 keeps its
    own settings in a step-local file that it deletes, so it sets none.

  - **Where Trivy runs.** In the two security-scanned jobs *(every job since 2026-10-20)*, Trivy runs only through the reviewed
    trivy-action steps:
    - their number is pinned (13 and 5; *since 2026-10-20 every job: 6, 7, 6, 17, 6 and 9, in workflow order*);
    - every `uses:` reference is from a reviewed set, and no local action is allowed. A flow-style or
      quoted `uses` is read too;
    - no run line invokes `trivy` directly;
    - no scanning or gating step changes its working directory away from the one the run-time
      refusal checks, and no `defaults:` block sets one.
    The refusal's own list of file names is pinned literally in the test.

  Each check also runs against deliberately suppressed copies. The step and route checks read line
  shapes and fail closed. A legitimate edit, such as a version bump, a digest pin, a new input,
  `timeout-minutes` or a comment that names trivy-action inside another step, therefore fails until
  the test is updated with it, and is reviewed that way.

  **Disclosed limit:** no mechanical check here can see a change that defeats the scans by means its
  text does not reveal, for example:
  - an obfuscated scanner invocation;
  - a `TRIVY_*` variable set only in the environment of a scan step;
  - code that rewrites a report between a scan and its gate.

  Such a change can sit in `ci.yml` or `security/`, but also in ANY file the two jobs execute before
  their gates: the scripts, tests and application code they run. The control for those is review of
  every such change. A suppression file that such code plants in the working directory is closed
  structurally by the pinned configuration above. The pinned configurations and the empty ignore
  file are themselves workspace files, though. Code the jobs run could alter them during a run and
  restore them before the gates, and their run-time integrity rests on that same review.
- **Measured locally (2026-10-19, Trivy 0.76.0, macOS builds of the same locks):** 0 CRITICAL
  in every population. Locks: `source-map-js` HIGH `REPORTED` (development), `braces` HIGH
  `EXCLUDED` (no fix), five MEDIUM, including `mcp` 1.29.0 in the Semgrep lock, which remains a
  separate obligation. Reader tooling venv: 17 installed distributions all scanned; 4 HIGH
  `REPORTED` (pip 26.2.1's vendored `msgpack`, `setuptools` and `urllib3`). Security tooling
  venv: 67 of 67 scanned, 7 lower. ~~The toolcache interpreter of the runner holds pip 25.0.1
  only (MEDIUM/LOW).~~ *(Superseded 2026-10-20: that sentence came from a local scan of the
  `actions/python-versions` 3.12.15 Linux tarball, not from the runner. The CI measurement is
  the next bullet.)* CI on the pull request is the measurement of record.
- **Measured in CI (exact-head run 39337491390 on `f3fb4d59`; gate summary 2026-10-20T00:06Z):** the
  reader job's interpreter prefix (`pythonLocation`, `/opt/hostedtoolcache/Python/3.12.15/x64`) held 2
  installed distributions (~~pip 26.2.1~~ and PyYAML 6.0.3) and 21 scanned language packages,
  vendored copies included *(2026-10-21: true of that pip, 26.2.1, which ships an SBOM of its vendored packages; a pip
  without one, such as 25.0.1 or 25.1.1, contributes none of them to such a scan — see repair 3 under "Coverage-gap
  tranche")*. *(2026-10-21, GHSA-6q48-6c7w-9gw2 — certifi < 2026.10.19, CRITICAL,
  published 2026-10-20: the toolcache's pip 26.2.1 vendors certifi 2026.6.17, so this gate failed on
  every run after Trivy's database carried the advisory, and nothing in the repository controlled that
  pip. The prefix's pip is now installed first from `apps/revision-reader/requirements-toolcache-pip.txt`
  (pip 26.2.2, which vendors certifi 2026.10.19), so the prefix still holds 2 installed distributions,
  pip 26.2.2 and PyYAML 6.0.3; the first run on that change is its measurement of record.)*
  Under the adopted tooling policy its gate `REPORTED` 4 HIGH findings
  (development scope: reported, not failing): `GHSA-6v7p-g79w-8964` (msgpack 1.1.2),
  `CVE-2025-47273` (setuptools 70.3.0), and `CVE-2026-97687` and `CVE-2026-97689` (urllib3
  2.7.0). Package versions and findings change from run to run: each run's
  `reader-tooling-syspy.summary.txt` gate summary, in its `security-reports-revision-reader`
  artifact, is the record for that run.
- **Reproduced locally from `ci.yml`** (Trivy CLI with each step's own inputs; image scans
  substituted with merge run 39255973756's real reports, because this host has no container
  runtime): every security step of both jobs passes, completeness included.

**Residual tooling gaps, reported and not closed here** *(the list as the closure tranche left it,
kept as written; its state after the coverage-gap tranche of 2026-10-20 is the table under
"Coverage-gap tranche" below)*. Each of these is a repository-controlled
or runner-provided population that no merge-path scan covers:

- ~~The API venvs of `Frontend quality`, `Backend quality`, `Migrations and API contract` and
  `Integration smoke` run an unpinned `pip install --upgrade pip`. Their `-e apps/api` installs
  use build isolation, which fetches an unpinned setuptools (`apps/api/README-deps.md`). The
  hash-locked `apps/api/requirements-dev.txt` they install is scanned as a lock only, not as
  installed.~~ *(Addressed 2026-10-20: repair 2 under "Coverage-gap tranche".)*
- ~~`.locktools` (`pip==25.1.1`, `pip-tools==7.5.2`, Backend quality) is pinned but not hashed,
  and its transitive distributions float (below).~~ *(Addressed 2026-10-20: repair 1.)*
- `node_modules` from `npm ci` is integrity-checked against the scanned `package-lock.json`,
  but is not scanned as installed.
- ~~The interpreters and runtimes come from the runner and the setup actions: toolcache Python
  and Node, each venv's bundled bootstrap pip, the Container build job's system interpreter
  (stdlib-only scripts, not scanned) and the Trivy binary that `trivy-action` downloads.
  The OpenTofu binary and providers rely on the provider lock (one platform's checksums).~~
  *(Corrected 2026-10-20, by population; the table under "Coverage-gap tranche" is the state. The
  reader job's interpreter prefix's **installed distributions** were already scanned as installed by
  the closure tranche, so "not scanned" did not hold for them; the other jobs' prefixes are not
  scanned. **Interpreter binaries and standard library** (CPython with its bundled ensurepip wheel,
  Node.js) are analysed in no job. A venv's bundled bootstrap pip is replaced by a locked pip, or in
  `.security-venv` scanned as installed with its vendored packages. The reader job's toolcache pip is
  itself replaced from `apps/revision-reader/requirements-toolcache-pip.txt` since 2026-10-21, after
  performing that one hash-checked install. The Trivy and OpenTofu binaries
  are now analysed; the OpenTofu providers are not.)* GitHub Actions code is pinned by tag, not by
  digest (`P6-CI-9`).
- ~~The image's base pip 25.0.1 under `/usr/local` (API and worker images) is scanned in the
  image, as before.~~ *(Corrected 2026-10-20: its dist-info was scanned, but the packages it vendors
  were not; it is now removed from the runtime stage, with the bundled ensurepip wheel.)*
- Container images that required jobs run but do not build: the `postgres:16` service
  (Backend quality and Migrations and API contract; by tag, not digest) and the BuildKit image
  `docker/setup-buildx-action` starts. Also the Playwright Chromium build that
  `npm run e2e:install` downloads in Frontend quality.
- Docker build stages: only the final images are scanned.
  - `apps/api/Dockerfile` builds from `python:3.12-slim` by tag, not digest. Its builder stage
    installs `build-essential` and runs an unpinned `pip install --upgrade pip`.
  - The reader's builder stage runs `pip install --prefix=/install .`, pinned (`psycopg[binary]==3.2.3`)
    but unhashed, with build isolation.
  - Neither builder stage's tooling ships. The shipped image contracts are unchanged here.
- ~~`.locktools` installs pip-tools 7.5.2 by version only. Its transitive distributions (`build`,
  `click`, `packaging`, `pyproject_hooks`, `setuptools`, `wheel`) float.~~ *(Addressed 2026-10-20:
  every one is in `apps/api/requirements-locktools.txt`, with hashes.)*

## Coverage-gap tranche (2026-10-20, operator order; severity source, CRITICAL, HIGH and UNKNOWN rules and the six IaC exceptions unchanged)

The fresh closure assessment of the merged closure work found populations that a required context
installs or executes but that no scan analysed. The operator order of 2026-10-20 directs four repairs,
made here. No vulnerability exclusion is added, and nothing here decides whether P6-CI-6 is closable.
`policy.json` changes only by declaring the scope of the two new lock targets ~~and by stating that the
existing tooling scope applies to the scanner and tool binaries and to vendored packages~~. *(Corrected 2026-10-21: the
existing, adopted tooling scope names installed environments, not executables, so that sentence was this tranche's
reading, and it sat inside the adopted text. The binaries' scope is now the operator's bounded decision of 2026-10-21
(see "Policy" above), recorded apart from the adopted text and enforced as `binary_scope`. Vendored packages take the
scope of the environment that contains them.)*

1. **`.locktools` is scanned as installed.** Backend quality installs its lock toolchain from the
   hash lock `apps/api/requirements-locktools.txt` (~~pip 25.1.1, pip-tools 7.5.2~~ pip 26.2.2 and pip-tools 7.6.2
   since CERT-2, 2026-10-21 — the 7.5.2 toolchain cannot carry a fixed certifi and cannot run under pip 26 — and every transitive
   distribution, `--require-hashes --no-deps`, then `pip check`). It is scanned with Trivy `rootfs`,
   and the tooling gate reconciles it against its own `pip inspect` inventory and against the lock
   (`--locked-by`): installed == the lock's pins, at the locked versions. It is made read-only right
   after its install, before the lock check's own tests run, and it is inventoried and gated again at the
   end of the job. It is created with `--clear` and installed with `--only-binary :all:`. pip-tools reads the API project's static `[project]` metadata, so
   no build backend runs in the check; `tools/verify_locks.py` now also runs pip-compile with
   `--no-build-isolation`, so a backend that ever has to run (dynamic dependencies) is this lock's
   setuptools, never one fetched unpinned into a temporary build environment.
2. **The API venvs' bootstrap pip and build tooling.** In Frontend quality, Backend quality,
   Migrations and API contract and Integration smoke, the unpinned `pip install --upgrade pip` is
   replaced by `apps/api/requirements-bootstrap.txt` (~~pip 26.2.1~~ pip 26.2.2 since 2026-10-21 and setuptools 84.0.0, the reader
   tooling lock's pins and hashes), and `-e apps/api` runs with `--no-build-isolation`. ~~Every hash-locked
   install in every job now carries `--only-binary :all:`~~ *(corrected 2026-10-21: every hash-locked install that a
   `ci.yml` `run:` step performs carries it. The builder stage of `apps/api/Dockerfile`, which Container build and
   security's image builds run, does not: it runs an unpinned `pip install --upgrade pip`, then
   `pip install --require-hashes -r requirements-runtime.txt` without `--only-binary` or `--isolated`, then
   `pip install --no-deps .` with build isolation; the reader Dockerfile's builder installs unhashed. Both builder stages
   are decision D1 and are not scanned; only the final images are)*: a locked dependency built from an sdist would have
   its build requirements fetched unpinned and unhashed, run, and deleted, where no inventory or scan sees them
   (round-3 finding A3R-1, reproduced); every current pin has a Linux cp312 wheel, so the flag changes nothing
   today and makes such a lock line fail its install. Because pip applies option lines in a requirements file
   after its command line, every lock that ci.yml or the API Dockerfile installs from (the set is derived from
   the files) must hold only pins, `--hash=sha256:` continuations and comments (`gate.parse_lock`'s strict grammar,
   also used by `--locked-by`); a `--no-binary` continuation, `--only-binary=:none:`, `-c`, an index line or a
   URL requirement in a lock fails the security self-tests (round-4 finding A4R-1, reproduced). Every hash-locked
   install also runs `pip install --isolated`, so no `PIP_*` variable or user pip configuration (a constraint file
   naming `--no-binary`, for example) applies, and every venv is created with `--clear` *(2026-10-21: in the `ci.yml`
   `run:` steps, as above; not in the API builder stage)*. The API project's build
   backend is pinned by a test to `setuptools.build_meta`, with no in-tree backend, no `setup.py`/`setup.cfg`, no
   `cmdclass` and no `dynamic` fields, and no tracked top-level `pip`, `venv`, `ensurepip` or `piptools` module may
   shadow what the installs run with `python -m`. Each job scans
   its `apps/api/.venv` as installed, reconciles it against its inventory and against the bootstrap and
   dev locks plus the editable project (`--locked-extra signalnest-api`, which must be an editable
   install from a directory inside the working directory, per `pip inspect`'s `direct_url`), runs its
   own policy controls and both real-scanner controls, takes the inventory again after every other step
   and gates it, and ends with a completeness step and a report artifact.

   **What is observed, and when.** The API venv and `.locktools` are each observed at two points: the
   first inventory and scan, and the final inventory, gated against the same scan. Each is created with
   `--clear`, so nothing an earlier step left there survives, and the last line of its install
   (`stage_inputs.py readonly`, which directly follows the editable install, or for `.locktools` its `pip check`; a test pins each install's
   command list exactly) removes every write permission from it, so no later step runs with it writable. Both
   of its gates require it to be still read-only (`--require-read-only`). An ordinary install or uninstall by
   any later step therefore fails that step instead of slipping in and out unobserved, before or between the
   two observations.
   Every installer-shaped line of every job is pinned (`test_tooling_locks.py`), so a new install line in
   the workflow fails review. What none of this stops:
   - code that runs inside the install itself: pip, and the setuptools build backend that the editable
     install runs over the tracked `pyproject.toml` (its declarative configuration is pinned as above);
   - pip configuration that `--isolated` still reads: a site configuration in the venv (empty, as the venv
     was just cleared) and the global one, which only `sudo` can write;
   - any code the jobs run that undoes the measure: an unprivileged `chmod`, replacing the directory through
     its writable parent, or `sudo`, which GitHub-hosted runners grant the job user. It stops accidental
     installs, not deliberate ones;
   - installs outside every scanned environment.

   That is the disclosed limit above (decision packet D8), and its control is review. The other
   hash-locked environments (`.security-venv`, `.reader-venv`, the reader job's interpreter prefix) are not
   made read-only; they are inventoried after the code their jobs run.
3. **pip's vendored packages.** pip vendors its dependencies without dist-info, so a `rootfs` scan
   sees them only when pip ships an SBOM of them (26.2.1 and 26.2.2 do; 25.0.1 and 25.1.1 do not).
   `stage_inputs.py vendored` writes each installed pip's own `pip/_vendor/vendor.txt` pins to a staged
   requirements list, a Trivy `fs` scan analyses it, and the tooling gate re-reads `vendor.txt` from
   the environment and requires exactly those pins in that scan. Their findings are evaluated at the
   tooling scope *(2026-10-21: that is the scope of the installed CI tooling environment they sit in; vendored packages
   take their containing environment's scope and get no tooling exemption of their own)*. A pip without a readable vendor list, or with no vendored-pin scan, is an incomplete
   analysis. A real-scanner control (`control-vendored-pins`, every job) stages an inert
   environment whose pip vendors PyYAML 5.3.1, which exists nowhere else, and must fail on
   CVE-2020-14343 in the vendored-pin target. **Final images:** the API Dockerfile's runtime stage now
   removes the base image's pip 25.0.1 (with its launchers) and the interpreter's bundled ensurepip
   wheel; nothing at runtime installs packages or creates a virtualenv with pip. After the stage's last
   `COPY`, a build-time check fails the image build if any pip package, pip metadata, wheel file or pip
   launcher remains under `/usr/local`, `/opt/venv` or `/app`, or if either interpreter can import pip;
   the publish workflow builds the same Dockerfile, so it runs there too. The image gate also fails on
   any pip its scan lists. The reader image's distroless base carries neither.
4. **The Trivy and OpenTofu binaries.** *(2026-10-21: their vulnerability analysis is an operator requirement of the order of
   2026-10-20 — "Include vulnerability analysis of the actual Trivy and OpenTofu binaries as an explicit requirement of this
   new order" — not a finding about what an earlier source required. Their scope is the operator's bounded decision of
   2026-10-21; see "Policy" above.)* `stage_inputs.py binaries` copies the `trivy` (every job,
   since every job now runs Trivy) and `tofu` (Revision reader) executables the jobs resolve on PATH and records
   the path each resolved to and its sha256. Trivy `rootfs` analyses the copies as Go binaries, and the binary
   gate requires:
   - the copy and the file the binary resolved to on PATH at staging time to hash to the release digest in
     `scanner-binaries.json` (that the steps execute that same file is expected from how the setup actions
     put it on PATH; the exact-head CI run shows it);
   - one Go binary result per binary, naming the pinned main module and version and recovering the Go
     standard library version.

   Each digest was derived from the vendor's release asset after checking the asset against the
   release's published checksum file, which checks integrity against the release page, not a
   signature. A binary Trivy cannot analyse is an incomplete analysis, never a clean one: measured
   locally, Trivy 0.76.0 writes no Results at all for an executable it cannot analyse.

   What the analysis establishes: the module graph and Go version recorded in each binary's build
   information, matched against Trivy's database. Measured locally on the release binaries: Trivy
   v0.76.0 on Go 1.27.2, 377 modules; OpenTofu v1.12.5 on Go 1.26.5, 253 modules.

   What it does not establish: code outside that module graph, modules changed without a version
   change, vulnerabilities absent from Trivy's database, and build provenance beyond the release digest.

   **OpenTofu's unresolved HIGH findings (open; recorded 2026-10-21).** At `binary_scope = development` the binary gate
   REPORTS them and does not fail on them; that is the operator's decision of 2026-10-21, which states it is not an
   exception, a remediation claim or a declaration that the tool is safe. Measured in exact-head run 39514455981 (PR #233
   head `2d6e4724`) on OpenTofu v1.12.5 (Go standard library v1.26.5, 253 modules), as Trivy 0.76.0 selected them with its
   database of that run: 28 HIGH, each with a published fix (also 30 MEDIUM, 6 LOW and 1 UNKNOWN, `GO-2026-5932` in
   `golang.org/x/crypto` v0.52.0, no fix). Trivy v0.76.0 has no HIGH; its one UNKNOWN is the same advisory in
   `golang.org/x/crypto` v0.57.0.
   - **Accountable:** the repository owner (operator order of 2026-10-21, continuation 02, section 8).
   - **Working follow-up:** the coordinator's CI-6 and IAM lanes.
   - **Visibility:** every Revision reader run lists each finding as `REPORTED` in its `scanner-binaries` gate summary, so
     they stay open and visible until they are resolved there.
   - **Limitations:** the analysis covers only the module graph and Go version recorded in the binary's build information
     (see "What it does not establish" above). A fixed version applies to the module named, not to the OpenTofu release;
     the set changes with Trivy's database. None of them is resolved by this record.

   | Advisory | Module | Installed | Fixed in |
   |---|---|---|---|
   | `CVE-2026-39883` | `go.opentelemetry.io/otel/sdk` | v1.42.0 | 1.43.0 |
   | `CVE-2026-56854` | `golang.org/x/crypto` | v0.52.0 | 0.55.0 |
   | `CVE-2026-56864` | `golang.org/x/mod` | v0.35.0 | 0.40.0 |
   | `CVE-2026-56865` | `golang.org/x/mod` | v0.35.0 | 0.40.0 |
   | `CVE-2026-25681` | `golang.org/x/net` | v0.54.0 | 0.55.0 |
   | `CVE-2026-27136` | `golang.org/x/net` | v0.54.0 | 0.55.0 |
   | `CVE-2026-39821` | `golang.org/x/net` | v0.54.0 | 0.55.0 |
   | `CVE-2026-46600` | `golang.org/x/net` | v0.54.0 | 0.56.0 |
   | `CVE-2026-78669` | `golang.org/x/net` | v0.54.0 | 0.60.0 |
   | `CVE-2026-56851` | `golang.org/x/text` | v0.37.0 | 0.41.0 |
   | `CVE-2026-56852` | `golang.org/x/text` | v0.37.0 | 0.39.0 |
   | `CVE-2026-84304` | `google.golang.org/grpc` | v1.79.3 | 1.83.1 |
   | `CVE-2026-84445` | `google.golang.org/grpc` | v1.79.3 | 1.82.2, 1.83.2, 1.84.0-dev.0.20260825144003-d5a41119e0e3, 1.85.0-dev.0.20260825072537-93e31b48545e |
   | `GHSA-hrxh-6v49-42gf` | `google.golang.org/grpc` | v1.79.3 | 1.82.1 |
   | `CVE-2026-50151` | `oras.land/oras-go/v2` | v2.6.0 | 2.6.1 |
   | `CVE-2026-50163` | `oras.land/oras-go/v2` | v2.6.0 | 2.6.2 |
   | `CVE-2026-85731` | `oras.land/oras-go/v2` | v2.6.0 | 2.6.2 |
   | `CVE-2026-33818` | `stdlib` | v1.26.5 | 1.25.13, 1.26.6, 1.27.0-rc.3 |
   | `CVE-2026-39821` | `stdlib` | v1.26.5 | 1.25.13, 1.26.6, 1.27.0-rc.3 |
   | `CVE-2026-46600` | `stdlib` | v1.26.5 | 1.26.6, 1.27.0-rc.3 |
   | `CVE-2026-56853` | `stdlib` | v1.26.5 | 1.25.13, 1.26.6, 1.27.0-rc.3 |
   | `CVE-2026-56858` | `stdlib` | v1.26.5 | 1.25.13, 1.26.6, 1.27.0-rc.3 |
   | `CVE-2026-56859` | `stdlib` | v1.26.5 | 1.25.13, 1.26.6, 1.27.0-rc.3 |
   | `CVE-2026-56860` | `stdlib` | v1.26.5 | 1.25.13, 1.26.6, 1.27.0-rc.3 |
   | `CVE-2026-56862` | `stdlib` | v1.26.5 | 1.25.13, 1.26.6, 1.27.0-rc.3 |
   | `CVE-2026-78667` | `stdlib` | v1.26.5 | 1.26.9, 1.27.2 |
   | `CVE-2026-78669` | `stdlib` | v1.26.5 | 1.26.9, 1.27.2 |
   | `CVE-2026-97031` | `stdlib` | v1.26.5 | 1.26.9, 1.27.2 |

   *(Re-measured 2026-10-23 in push run 39859641559 on `main` `831cc2ea`, Trivy 0.76.0 with that run's database; the
   table above stays the record of run 39514455981.)* OpenTofu v1.12.5: 30 HIGH rows, 27 distinct advisories, each
   with a published fix — the 28 rows above and two more, `CVE-2026-63209` (`github.com/klauspost/compress` v1.18.5,
   fixed in 1.18.7) and `CVE-2026-111943` (`stdlib` v1.26.5, fixed in 1.26.10, 1.27.3); also 30 MEDIUM rows (23
   distinct), 6 LOW (3 distinct) and the same UNKNOWN. Trivy v0.76.0 now has one HIGH, `CVE-2026-111943` (`stdlib`
   v1.27.2, fixed in 1.26.10, 1.27.3), in every job, besides its one UNKNOWN. Each HIGH row is `REPORTED` at the
   development scope in that run's `scanner-binaries.summary.txt` gate summaries (MEDIUM, LOW and UNKNOWN rows are
   listed as `lower`), and all six end `RESULT: PASS`; none is resolved by this record.

**Population floors, re-derived from measured inventories** (local measurements on macOS builds of
the same locks, which carry no environment markers; the pull request's exact-head CI run is the
measurement of record). The floors are set somewhat below the measured counts, as the closure
tranche's were (60 of 67, 15 of 17, 40 of 48: 83 to 90 %; here 52 of 58 and 7 of 8), so that a routine
dependency removal does not trip them while an empty or truncated inventory does. For the two
hash-locked environments the binding check is the exact reconciliation against their locks; the floor
only guards the inventory before that check. ~~Every floor has a control that truncates the population
one below it and must fail.~~ *(Corrected 2026-10-21; the account below replaces that sentence.)*

**Which floors have a control, and what else protects population and identity** *(2026-10-21)*:
- **Dedicated truncation controls.** A case in `vuln_policy_controls.py` keeps one entry fewer than the floor in a copy of
  the job's real inventory or report, and the gate must fail it as incomplete. These floors have one:
  - `api-venv` 52, separately in each of the four API jobs;
  - `locktools` 7;
  - `security-tooling-venv` 60;
  - `reader-tooling-venv` 15;
  - `reader-tooling-syspy` 2;
  - the API image's 40 language packages;
  - the reader image's 1.

  The cases run on the first inventory. The `-final` gates use the same floor and code path over the final inventory and
  are not exercised separately.
- **API and worker images: shared code path, worker not exercised separately.** They have two gate steps, each over its
  own report, with the same floor (40) and the same code path, the image kind of `gate.py vuln`. Every case for these two
  images — truncation, an installer present, the planted CRITICAL findings — runs on the API report only.
- **SBOM floors: no dedicated truncation control.** These floors have none:
  - `sbom-image-api` 100 and `sbom-image-worker` 100 (134 package components each in run 39514455981);
  - `sbom-locks` 500 (764);
  - `sbom-image-reader` 10 (41).

  What protects them:
  - the gate's floor check itself, which counts only `library`, `application` and `framework` components, so padding
    with `file` entries does not count (`test_gate.py`: too few components, padded filesystem and image SBOMs);
  - an image SBOM must carry the same `ImageID` as the same job's vulnerability report, and must carry no conflicting
    identity values;
  - the described component must be the built artifact;
  - the positive control `control-sbom`: the inert lock's real Trivy SBOM must PASS the gate. That shows the gate reads a
    real CycloneDX document; it does not test truncation;
  - completeness requires every SBOM summary to PASS.
- **Vendored-pin scans have no floor.** Exact equality with each installed pip's `vendor.txt` is required, and the
  `*-vendored-pin-not-scanned` cases drop one pin and must fail.
- **Identity and completeness across all gates.**
  - Every report must describe the claimed artifact (name and type).
  - An image report must carry its image id and at least one OS package.
  - Every expected lock target must be present, populated and of declared scope.
  - Every installed distribution must sit in the scan at its own metadata path.
  - The hash-locked environments must equal their locks and stay read-only; the final inventories are gated.
  - The real-scanner controls (`control-vuln`, `control-tooling-rootfs`, `control-vendored-pins`) show that Trivy still
    finds a known CRITICAL.
  - The completeness step requires every expected summary, each with exactly one `RESULT` line and the expected verdict,
    and digests the directory.
- **Requirement check.** No existing requirement demands a dedicated control for each unchanged floor, so none was added:
  - The operator order of 2026-10-20 requires controls for a *changed* floor: "Any changed population floor must be
    re-derived from measured before/after inventories and independently reviewed, with controls proving that truncation
    still fails". The two changed floors, `api-venv` and `locktools`, have them.
  - The adopted SBOM requirement (order of 2026-10-19, retained) is a non-empty package-component population.

| Gate | Floor | Before | After | Truncation control |
|---|---|---|---|---|
| `api-venv`, `api-venv-final` (4 jobs) | 52 (new) | no gate; 57 installed (55 dev pins, pip 26.2.2 from the unpinned upgrade, the project) | 58 (55 dev pins, ~~pip 26.2.1~~ pip 26.2.2 since CERT-2 — 58 measured locally from the bootstrap and dev locks on 2026-10-21 — setuptools 84.0.0, the project) | `<job>-api-venv-truncated-inventory` |
| `locktools`, `locktools-final` | 7 (new) | no gate; 8 installed, transitive versions floating | 8, all from the lock | `backend-locktools-truncated-inventory` |
| `security-tooling-venv` | 60 (unchanged) | 67 | 67 | `security-tooling-truncated-inventory` |
| `reader-tooling-venv` | 15 (unchanged) | 17 | 17 | `reader-tooling-truncated-inventory` |
| `reader-tooling-syspy` | 2 (unchanged) | 2 | 2 | `reader-syspy-truncated-inventory` |
| `image-api-vuln`, `image-worker-vuln` | 40 language packages (unchanged) | 48 | 47 expected (pip removed) *(47 each, run 39514455981)* | `api-image-truncated-lang-packages` *(2026-10-21: on the API report only; the worker gate shares the code path and is not exercised separately)* |
| `image-reader-vuln` | 1 (unchanged) | 3 | 3 | `reader-image-truncated-lang-packages` |
| vendored-pin scans | none: exactly the pins of `vendor.txt` | — | ~~18 (pip 26.2.1), 20 (pip 25.1.1)*~~ 18 (pip 26.2.2, `apps/api/.venv` and `.locktools` alike since CERT-2, 2026-10-21, measured locally from the locks: certifi 2026.10.19 among them); ~~18 (pip 25.0.1, `.security-venv`), run 39514455981*~~ 18 (pip 26.2.2, `.security-venv`, since CERT-2 — its lock now carries pip; measured locally, certifi 2026.10.19 among them) | `*-vendored-pin-not-scanned` |
| `sbom-image-api`, `sbom-image-worker`, `sbom-locks`, `sbom-image-reader` *(row added 2026-10-21)* | 100, 100, 500, 10 package components (unchanged) | — | 134, 134, 764, 41 (run 39514455981) | **none** (see above) |

**Populations after this tranche.** The decision packet named below is evidence, not a tracked file;
"unresolved" means an operator decision is pending and nothing here decides it. *(2026-10-21: "D1"–"D8" and "D4b" name
the rows of the operator decision table of that date, kept in the coordination evidence `P6-programme-closeout-01`; a
population marked uncovered there is not covered by any merge-path scan.)*

| Population | Jobs | State |
|---|---|---|
| `apps/api/.venv`, installed distributions and pip's vendored packages | the four API jobs | scanned as installed; exactly the locks plus the project; final inventory gated |
| `.locktools` | Backend quality | the same, against its own lock |
| API and worker final images: the base pip 25.0.1 and the bundled ensurepip wheel | Container build and security | removed in the runtime stage; asserted at build time after the last layer; the image gate refuses pip |
| Trivy binary | every job (each runs Trivy since this tranche) | Go binary analysis in each job; release digest pinned |
| OpenTofu binary | Revision reader | the same |
| OpenTofu provider binaries (`hashicorp/aws` in the module caches) | Revision reader | **not analysed**; the provider lock verifies the download (one platform's checksums). Not named by the order; reported |
| The interpreter prefix's **installed distributions** (`pythonLocation`) | Revision reader | scanned as installed (pip and the Gate 4N PyYAML) with pip's vendored packages |
| The same, in the other five jobs | all but Revision reader | **not scanned**. No repository `run:` step installs into it; `actions/setup-python` with `cache: pip` runs its pip to locate the cache, and the jobs run stdlib-only security scripts with the interpreter and create venvs from it |
| **Interpreter binaries and standard library**: CPython, including its bundled ensurepip wheel, which seeds each `python -m venv` and whose pip performs that venv's first, hash-checked install; Node.js; *(2026-10-21)* the runner image's own `python3`, which Container build and security's self-test step runs before that job's Python set-up step | all jobs | **not analysed**; unresolved (decision packet, interpreter binaries). The bundled pip's own vendored packages are analysed only where such a pip stays installed (`.security-venv`) |
| `node_modules` from `npm ci` | Frontend quality; Migrations and API contract | integrity-checked against the scanned `package-lock.json`; not scanned as installed |
| Playwright Chromium and its `--with-deps` OS packages | Frontend quality | unresolved (decision packet) |
| `postgres:16` service, BuildKit image | Backend quality, Migrations and API contract; Container build and security | unresolved (decision packet) |
| Docker builder stages (the API builder's unpinned pip upgrade and build isolation; the reader builder) | Container build and security; Revision reader | unresolved (decision packet); only final images are scanned |
| GitHub Actions code | all jobs | unresolved (decision packet); tag pinning belongs to `P6-CI-9` |
| **Native libraries bundled inside wheels**, without dist-info: `psycopg-binary` 3.3.6 carries 15 shared libraries in `psycopg_binary.libs/`, among them OpenSSL 3.5.8 (`libssl`, `libcrypto`), libpq 5.18, Kerberos, OpenLDAP and SASL; `numpy` 2.5.3 carries OpenBLAS, libgfortran and libquadmath (measured in the Linux wheels of the locks); both are runtime pins | API venvs; API and worker images; the reader image's `psycopg-binary` 3.2.3 (same class, not measured) | **not analysed**: an installed-environment or image scan identifies the wheel by its own name and version only; under the adopted source the bundled libraries have no Trivy selection (decision packet D4 readings apply). Compiled extensions also carry third-party code linked in: marker strings of libuv (`uvloop`), libyaml (PyYAML's `_yaml`) and llhttp (`httptools`) are in their Linux extension modules (static linkage inferred, as these wheels carry no `.libs/`), and Rust crate graphs are named in embedded cargo registry paths: `bcrypt` 9, `pydantic-core` 26 and `watchfiles` 9 distinct crates (runtime pins, so in the images), `ruff` 100 (dev) (measured, lower bounds; Trivy reports nothing for these objects). *(2026-10-21: these are measured instances, not an enumeration; compiled code inside any other wheel of a scanned environment is the same class and was not inventoried. In the shipped images they are runtime content, **uncovered pending operator decision D4b**: no merge-path scan identifies them, and nothing here claims they are covered.)* |
| *(Row added 2026-10-21)* **The API and worker images' own CPython 3.12**: `python:3.12-slim`'s interpreter under `/usr/local`, built from source rather than installed as a Debian package | API and worker images (shipped) | **not identified, uncovered pending operator decision D4b**: the image scan lists 87 Debian packages and 47 Python packages and no entry for the interpreter (run 39514455981), so no CVE in it can be matched. The reader image's `python3.13` is a Debian package and is identified |
| Other vendored code without dist-info: `botocore/vendored` (`six`, a `requests` shim) and the C sources `httptools` compiles in | API venvs; API and worker images | not analysed by name and version (found locally in the API venv); reported |

*(Also corrected: the "Running locally" example below took the inventory with `pip inspect --local`,
which the closure tranche had already ruled out; it now matches CI.)*

## Failure behaviour (proved by `security/test_gate.py` and the CI negative controls)

- A scanner crash fails its own step; the gate then reports `FAIL (report missing or
  malformed)` (exit 2). An empty, truncated or non-JSON report is refused the same way.
- A report that describes a different artifact, lacks an image id, has an empty package
  population, an SBOM whose `ImageID` differs from the vulnerability report's or carries
  conflicting identity values, a gate summary with other than exactly one `RESULT` line, or
  a Semgrep run with recorded errors is `FAIL (scanner execution incomplete)` (exit 4).
- A lock target the policy does not declare, an installed tooling distribution absent from
  its scan, or Trivy suppression (an ignore or configuration file in the working directory, a
  `TRIVY_*` variable) is `FAIL (scanner execution incomplete)` (exit 4); a finding with no recognised
  severity, a malformed `pip inspect` inventory or a policy whose rules have a gap is `FAIL
  (report missing or malformed)` (exit 2).
- A finding at gating severity is `FAIL (policy violation)` (exit 3) — distinct from the two
  above in the summary and in the exit code. With the IaC exceptions file, so is an expired,
  stale, miscounted or condition-failing entry (`EXCEPTION_*` lines in the summary).
- Negative controls (`security/controls/`) run the real scanners over inert fixtures and pass
  only when the gate raises a policy violation (`--expect-fail`); a clean result, a crash or
  an incomplete analysis fails the control (exit 6).
- A control that behaved as intended ends in `RESULT: CONTROL BEHAVED AS INTENDED (the outcome stated for it was
  observed)`, and one that did not ends in `RESULT: CONTROL FAILED (...)`. *(2026-10-21, D7, label-only: until then these
  read `NEGATIVE CONTROL DETECTED (policy violation raised as intended)` and `NEGATIVE CONTROL FAILED` for every control,
  including the report-mutation cases meant to pass, to list a finding or to fail closed.)* The summary states the
  intended outcome, and for a report-mutation case also the gate's own verdict on it. `complete --expect-control` requires
  the success line.
- The completeness step fails if any expected summary is missing or does not carry the
  expected verdict, so an analysis that silently did not run cannot pass. The workflow has
  no path filters and the gate steps carry no conditions other than `if: always()`.

## Running locally

```
python3 -m unittest discover -s security -p "test_*.py"
mkdir -p security-reports
trivy fs --scanners vuln --list-all-pkgs --config security/trivy.yaml --skip-dirs security/controls --format json -o security-reports/trivy-locks-vuln.json .
python3 security/gate.py vuln security-reports/trivy-locks-vuln.json --name locks-vuln --kind filesystem --artifact . --expect-target package-lock.json
python3 -m venv .security-venv && .security-venv/bin/python -m pip install --require-hashes -r security/requirements-semgrep.txt
.security-venv/bin/python -m pip inspect > security-reports/security-tooling-venv-installed.json
trivy rootfs --scanners vuln --list-all-pkgs --config security/trivy-scan.yaml --format json -o security-reports/trivy-security-tooling-venv-vuln.json .security-venv
python3 security/stage_inputs.py vendored --out .vendored-pins --env security-venv .security-venv security-reports/security-tooling-venv-installed.json
trivy fs --scanners vuln --list-all-pkgs --config security/trivy.yaml --format json -o security-reports/trivy-vendored-pins-vuln.json .vendored-pins
python3 security/gate.py vuln security-reports/trivy-security-tooling-venv-vuln.json --name security-tooling-venv --kind tooling --artifact .security-venv --env-root .security-venv --min-lang-packages 60 --vendored-report security-reports/trivy-vendored-pins-vuln.json --vendored-target security-venv/requirements-vendored-pip.txt --installed-inventory security-reports/security-tooling-venv-installed.json
python3 security/stage_inputs.py binaries --pins security/scanner-binaries.json --job container-build --out .scanner-binaries --manifest security-reports/scanner-binaries-manifest.json
trivy rootfs --scanners vuln --list-all-pkgs --config security/trivy-scan.yaml --format json -o security-reports/trivy-scanner-binaries-vuln.json .scanner-binaries
python3 security/gate.py vuln security-reports/trivy-scanner-binaries-vuln.json --name scanner-binaries --kind binary --artifact .scanner-binaries --binary-job container-build --binary-pins security/scanner-binaries.json --binary-manifest security-reports/scanner-binaries-manifest.json
python3 security/vuln_policy_controls.py check container security-reports   # needs the API image report too
.security-venv/bin/python -m unittest discover -s security -p "fetcher_selftest.py"
.security-venv/bin/python security/fetch_semgrep_rules.py .security-venv/semgrep-rules
.security-venv/bin/semgrep scan --metrics=off --no-rewrite-rule-ids --timeout 60 --timeout-threshold 0 --exclude security/controls --config .security-venv/semgrep-rules --json --output security-reports/semgrep.json apps scripts security
python3 security/gate.py sast security-reports/semgrep.json --name sast --expect-prefix apps/api/app
trivy config --skip-dirs security/controls --format json -o security-reports/trivy-config.json .
python3 security/gate.py misconfig security-reports/trivy-config.json --name iac --expect-type terraform --exceptions security/misconfig-exceptions.json --source-root .
python3 security/iac_exception_controls.py build security-reports/iac-exception-controls
trivy config --format json -o security-reports/control-iac-exceptions.json security-reports/iac-exception-controls
python3 security/iac_exception_controls.py check security-reports/control-iac-exceptions.json security-reports/iac-exception-controls
```

`security/requirements-semgrep.txt` is regenerated like the API locks
(`pip-compile --allow-unsafe --generate-hashes --no-strip-extras security/requirements-semgrep.in` — `--allow-unsafe` since CERT-2 keeps pip in the lock, ~~pip
25.1.1 / pip-tools 7.5.2~~ pip 26.2.2 / pip-tools 7.6.2 since CERT-2, 2026-10-21); a manual edit breaks the hashes. The reader tooling lock and the
Gate 4N hash lock are regenerated the same way, from their own directories:
`pip-compile --generate-hashes --allow-unsafe --strip-extras --extra dev --output-file
requirements-dev.txt pyproject.toml requirements-dev.in` (in `apps/revision-reader/`) and
`pip-compile --generate-hashes --allow-unsafe --strip-extras --output-file
requirements-gate-hashes.txt requirements-gate.txt` (in `scripts/`), and `pip-compile
--generate-hashes --allow-unsafe --strip-extras --output-file requirements-toolcache-pip.txt
requirements-toolcache-pip.in` (in `apps/revision-reader/`, 2026-10-21). The API bootstrap and lock-toolchain locks are
regenerated in `apps/api/` with `pip-compile --allow-unsafe --generate-hashes --strip-extras --output-file
requirements-bootstrap.txt requirements-bootstrap.in` (and the same for `requirements-locktools`).
