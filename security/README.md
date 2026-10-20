# Merge-path security scanning and SBOMs (P6-CI-6)

This directory wires CVE, dependency, SAST and IaC analysis plus SBOM generation into the
two existing **required** CI contexts that build artifacts — `Container build and security`
and `Revision reader (unit, IaC contract, in-image)` — so that every pull request to `main`
and every push to `main` is analysed before merge. The six required context names and the
repository ruleset are unchanged. No optional job, no manual workflow and no external
analysis service is involved; the publish workflows (`staging-publish.yml`,
`reader-publish.yml`) and their Trivy steps are untouched.

## What runs, where, over what

| Category | Tool | Job | Population analysed | Report |
|---|---|---|---|---|
| Vulnerabilities, locked dependencies | Trivy 0.76.0 (`aquasecurity/trivy-action@v0.36.0`, `trivy-config: security/trivy.yaml`) | Container build and security | `package-lock.json` (runtime and development npm packages), `apps/api/requirements-runtime.txt`, `apps/api/requirements-dev.txt`, `scripts/requirements-gate.txt`, `scripts/requirements-gate-hashes.txt`, `security/requirements-semgrep.txt`, `apps/revision-reader/requirements-dev.txt` | `trivy-locks-vuln.json` |
| Vulnerabilities, installed tooling (2026-10-19) | Trivy 0.76.0 `rootfs`, reconciled against the environment's own `pip inspect` | Container build and security; Revision reader | `.security-venv` (Semgrep and its hash-locked set, plus the interpreter's bundled pip); `.reader-venv` (the hash-locked reader tooling set); the reader job's interpreter prefix (`pythonLocation`: its pip and the Gate 4N PyYAML) — each as installed, inventoried again before completeness | `trivy-*-tooling-*-vuln.json`, `*-tooling-*-installed*.json` |
| Vulnerabilities, built images | Trivy 0.76.0 | Container build and security; Revision reader | OS packages and installed Python packages of `signalnest-api:ci`, `signalnest-worker:ci`, `signalnest-revision-reader:ci` as built in that run | `trivy-image-*-vuln.json` |
| SAST | Semgrep 1.181.0 (hash-locked install, `security/requirements-semgrep.txt`) with registry rulesets `p/python`, `p/typescript`, `p/javascript`, `p/react`, `p/security-audit` | Container build and security | `apps/`, `scripts/`, `security/` minus `.semgrepignore` (tests, e2e, generated and vendored paths) and `security/controls` | `semgrep.json`, `semgrep-rules-provenance.json` |
| IaC / configuration | Trivy 0.76.0 misconfiguration checks | Container build and security | `infra/aws` OpenTofu root and modules, `infra/aws/bootstrap`, both Dockerfiles (13 targets, 246 checks at the baseline) | `trivy-config.json` |
| SBOM | Trivy 0.76.0, CycloneDX 1.7 JSON | both jobs | the three images and the repository locks | `sbom-*.cdx.json` + `sbom-*.identity.json` |

Every report is produced with `exit-code: 0` and then evaluated by `security/gate.py`, which
fails the job itself. Each gate writes `security-reports/<name>.summary.txt`; a final
`complete` step refuses to pass unless every expected summary exists with the expected
verdict, and digests every file into `security-reports/MANIFEST.txt`. The whole directory is
uploaded as a workflow artifact (`security-reports-container-build`,
`security-reports-revision-reader`) with 30-day retention, on success and on failure.

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
    record.
  - **UNKNOWN.** A valid `UNKNOWN` severity means unscored. The finding stays reported for
    triage (the summary lists it with the reported severities, tagged `[UNKNOWN]`); it is not
    LOW, resolved or proof of safety. A missing or unrecognized severity, a malformed report, an
    unknown scope and an incomplete analysis continue to fail closed. A finding later classified
    CRITICAL falls under the unconditional CRITICAL rule. `test_gate.py` pins each case.
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
  (exit 4). An inventory of another environment or below the floor fails the same way. Vendored
  copies the scan also reports, such as pip's own vendored packages, are evaluated as well. Each
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
  --no-deps`). The venv's own `pip install -r scripts/requirements-gate.txt` line (pinned by
  Gate 4N-I28BH-E2) is kept and is already satisfied. It runs unhashed, so it is a no-op only
  while every PyYAML pin agrees. `test_tooling_locks.py` fails if they ever differ. It also
  fails if:
  - a requirement in the two locks lacks a hash;
  - the reader lock misses a dependency or the build backend that `pyproject.toml` declares;
  - the two jobs' installer-shaped lines differ from the reviewed set.

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
  runs) is not seen by it. Such an installer lands either in an environment the final
  inventories reconcile, where completeness fails, or outside every scanned environment, which
  is the disclosed limit.
- **Controls.** `vuln_policy_controls.py` takes this run's real reports and runs the real gate
  on 32 mutated copies in the container job and 18 in the reader job, each listed by
  `--expect-control control-vulnpol-*` in its job's completeness step. The cases cover:
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
  pass-through cases fail.

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

  - **Where Trivy runs.** In the two security-scanned jobs, Trivy runs only through the reviewed
    trivy-action steps:
    - their number is pinned (13 and 5);
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
  installed distributions (pip 26.2.1 and PyYAML 6.0.3) and 21 scanned language packages,
  vendored copies included. Under the adopted tooling policy its gate `REPORTED` 4 HIGH findings
  (development scope: reported, not failing): `GHSA-6v7p-g79w-8964` (msgpack 1.1.2),
  `CVE-2025-47273` (setuptools 70.3.0), and `CVE-2026-97687` and `CVE-2026-97689` (urllib3
  2.7.0). Package versions and findings change from run to run: each run's
  `reader-tooling-syspy.summary.txt` gate summary, in its `security-reports-revision-reader`
  artifact, is the record for that run.
- **Reproduced locally from `ci.yml`** (Trivy CLI with each step's own inputs; image scans
  substituted with merge run 39255973756's real reports, because this host has no container
  runtime): every security step of both jobs passes, completeness included.

**Residual tooling gaps, reported and not closed here.** Each of these is a repository-controlled
or runner-provided population that no merge-path scan covers:

- The API venvs of `Frontend quality`, `Backend quality`, `Migrations and API contract` and
  `Integration smoke` run an unpinned `pip install --upgrade pip`. Their `-e apps/api` installs
  use build isolation, which fetches an unpinned setuptools (`apps/api/README-deps.md`). The
  hash-locked `apps/api/requirements-dev.txt` they install is scanned as a lock only, not as
  installed.
- `.locktools` (`pip==25.1.1`, `pip-tools==7.5.2`, Backend quality) is pinned but not hashed,
  and its transitive distributions float (below).
- `node_modules` from `npm ci` is integrity-checked against the scanned `package-lock.json`,
  but is not scanned as installed.
- The interpreters and runtimes come from the runner and the setup actions: toolcache Python
  and Node, each venv's bundled bootstrap pip, the Container build job's system interpreter
  (stdlib-only scripts, not scanned) and the Trivy binary that `trivy-action` downloads.
  The OpenTofu binary and providers rely on the provider lock (one platform's checksums).
  GitHub Actions code is pinned by tag, not by digest (`P6-CI-9`).
- The image's base pip 25.0.1 under `/usr/local` (API and worker images) is scanned in the
  image, as before.
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
- `.locktools` installs pip-tools 7.5.2 by version only. Its transitive distributions (`build`,
  `click`, `packaging`, `pyproject_hooks`, `setuptools`, `wheel`) float.

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
.security-venv/bin/python -m pip inspect --local > security-reports/security-tooling-venv-installed.json
trivy rootfs --scanners vuln --list-all-pkgs --format json -o security-reports/trivy-security-tooling-venv-vuln.json .security-venv
python3 security/gate.py vuln security-reports/trivy-security-tooling-venv-vuln.json --name security-tooling-venv --kind tooling --artifact .security-venv --env-root .security-venv --min-lang-packages 60 --installed-inventory security-reports/security-tooling-venv-installed.json
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
(`pip-compile --generate-hashes --no-strip-extras security/requirements-semgrep.in`, pip
25.1.1 / pip-tools 7.5.2); a manual edit breaks the hashes. The reader tooling lock and the
Gate 4N hash lock are regenerated the same way, from their own directories:
`pip-compile --generate-hashes --allow-unsafe --strip-extras --extra dev --output-file
requirements-dev.txt pyproject.toml requirements-dev.in` (in `apps/revision-reader/`) and
`pip-compile --generate-hashes --allow-unsafe --strip-extras --output-file
requirements-gate-hashes.txt requirements-gate.txt` (in `scripts/`).
