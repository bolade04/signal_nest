# Negative-control fixtures for the merge-path security gates (P6-CI-6)

Inert files that each required analysis category must DETECT in CI. They are never
executed, imported, built, installed or applied, and the real scans skip this directory
(`.semgrepignore`, `skip-dirs`). Each control is scanned on its own and the gate is run with
`--expect-fail`: the step passes only when the scanner found the intended finding AND the
gate classified it as a policy violation. A clean result, a crashed scanner or a malformed
report fails the control, so a scanner that silently stops finding things cannot stay green.

| Category | Fixture | Intended finding |
|---|---|---|
| Dependency / CVE | `vuln/pinned-vulnerable.txt` (`pyyaml==5.3.1`; a pip pin list deliberately not named like a manifest, bound to Trivy's pip analyser by `trivy-control.yaml`, so the GitHub dependency graph never indexes it and no Dependabot alert is raised for a fixture) | CVE-2020-14343, CRITICAL, fixed in 5.4 |
| IaC | `iac/main.tf` (security-group rule open to 0.0.0.0/0 on 22) | AVD AWS-0107, HIGH |
| SAST, Python | `sast/insecure_sample.py` (`subprocess` with `shell=True`) | ERROR severity |
| SAST, TypeScript | `sast/insecure_sample.ts` (`crypto.createCipher`) | ERROR severity |
| SBOM | `vuln/pinned-vulnerable.txt` | a CycloneDX inventory with the one pinned component |
| Installed environment (Trivy `rootfs`, tooling gate; 2026-10-19) | `tooling-env/lib/python3.12/site-packages/PyYAML-5.3.1.dist-info/METADATA` (metadata only, no code) with its `pip inspect`-shaped inventory `tooling-env-installed.json` (paths relative to the repository root) | CVE-2020-14343, CRITICAL, fixed in 5.4 |
