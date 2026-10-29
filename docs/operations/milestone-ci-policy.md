# Milestone-based post-merge CI

**Status.** This page describes the workflow mechanics in this repository. The closeout policy they serve — what a
merge record must show before the integration slot moves — is an operator decision recorded outside the repository
and takes effect only at its activation checkpoint (below). Until then the earlier policy (the merge commit's own
six-check push CI) governs merges; after this change merges, that push CI no longer runs, which is why the
activation checkpoint holds the integration queue (see [Activation](#activation-checkpoint)).

## What runs when

| Event | `ci.yml` (the six required contexts) | `main-push.yml` | `milestone-validation.yml` |
| --- | --- | --- | --- |
| Pull request to `main` | **all six, unchanged** | only when the PR changes `main-push.yml` or `scripts/verify_milestone_evidence.py`: a *pr-measurement* run (not required, not evidence) | — |
| Push to `main` | not triggered | **Main short check** on the merge commit | — |
| Push to `feature/**` | all six, unchanged | — | — |
| Manual dispatch of `ci.yml` | all six on the chosen ref, unchanged — **never milestone evidence** | — | — |
| Manual dispatch of `milestone-validation.yml` on `main` | the six, called from the milestone run, named `Milestone full suite / <job>` — only after the binding passed | — | **Milestone binding** |

- **Before every merge into `main`** the six required checks run in full on the pull request (ruleset: strict,
  up to date; approval, last-push approval and thread resolution unchanged). A milestone run never substitutes for
  pull-request checks; its jobs carry the `Milestone full suite /` prefix, so they cannot satisfy or block a
  required context.
- **After every merge into `main`** the coordinator verifies the merge commit (sole parent, tree, patch) and the
  **Main short check** runs against that exact commit: the Integration smoke job's steps verbatim — a fresh API
  venv from the hash locks, scanned as installed and gated (CRITICAL fails, exactly the locks, vendored packages
  analysed, read-only), the scanner binary bound to its pinned digest, a disposable migrated and seeded SQLite
  database, the HTTP isolation smoke against the mock LLM and fixture connectors, the negative controls, the final
  inventory and the completeness gate — plus a binding record (`short-check-evidence-attempt-<n>` artifact) tying
  the run to the commit it checked out. The slot advances after both pass; **a failed short check holds further
  integration pending investigation.** The short check publishes no coverage or full-suite figures.
- **At a completed milestone** the full suite runs on the milestone's exact final `main` commit (below). Later
  `main` commits do not inherit a milestone's validation.
- **Before a deployment, launch or closure claim that requires full validation**, a passing milestone validation of
  the exact commit being used is required, checked with the verifier. Every separate security, infrastructure,
  deployment and live-demonstration gate still applies.

Concurrency: pull-request runs of `ci.yml` are grouped per PR and superseded by a newer push; every other run has a
group keyed by its run id (never cancelled, never queued). The milestone workflow declares no group of its own. The
called suite evaluates `ci.yml`'s group expression in the milestone run; whichever event name it sees there
(`workflow_dispatch` or `workflow_call`) it is not a pull request, so the group carries the run id and
`cancel-in-progress` is false — no pull-request or main-push run can cancel or queue it. The exact group string is
observed at the activation checkpoint.

## Creating a milestone

A milestone is a named, completed group of work — not automatically every small PR. When it is complete and the
integration queue is idle (no merge in flight), the operator (or the coordinator on the operator's instruction):

```bash
SHA=$(git ls-remote https://github.com/bolade04/signal_nest.git refs/heads/main | cut -f1)
gh workflow run milestone-validation.yml --ref main \
  -f milestone_id=M-2026-11-EXAMPLE \
  -f milestone_scope='PRs #280-#284: <what the milestone completes>' \
  -f milestone_sha="$SHA"
```

The run is named `Milestone <id> at <sha>`. **Milestone binding** refuses — and no suite job starts — unless the run
is a dispatch of `milestone-validation.yml` on `refs/heads/main` and `milestone_sha` equals the dispatched commit,
the checked-out commit and the `refs/heads/main` tip at bind time, with a valid id (`[A-Za-z0-9][A-Za-z0-9._-]`, at
most 64) and a one-line scope (at most 300 characters). A dispatch on another ref, for another commit, or after
`main` moved is refused; re-dispatch for the current tip. The binding record is uploaded as
`milestone-evidence-attempt-<n>` and shown in the job summary.

## Validating a milestone

```bash
gh run download <run-id> -n milestone-evidence-attempt-<n> -D /tmp/m   # the binding record
python3 scripts/verify_milestone_evidence.py verify --repo bolade04/signal_nest \
  --sha <tested-sha> --run <run-id> --milestone-id <id> \
  --binding-record /tmp/m/milestone-binding.json --record-out milestone-evidence.json
```

GET-only against the GitHub REST API (set `GH_TOKEN` to avoid anonymous rate limits); `--fixtures <file>` reads
captured responses instead. Exit 0 **PASS**, 1 **REJECT** (reasons listed), 2 cannot establish. It rejects: a run of
any workflow other than `milestone-validation.yml` (pull-request, push, short-check or plain `ci.yml` dispatch runs),
an event other than `workflow_dispatch`, a branch other than `main`; a run for another SHA (an earlier milestone
presented for a newer commit); a run name for another SHA or milestone; a run not completed successfully; a
presented attempt that is not the latest; any of the seven required jobs (`Milestone binding` and the six
`Milestone full suite / …`) missing, duplicated in the attempt, incomplete, or concluded failure, cancelled, skipped
or anything but success; a job result carried over from an earlier attempt; a job not bound to its own successful
check run on the SHA; incomplete or shifting pagination; and a binding record that disagrees with the run.

## Failure handling

- **Binding refused:** nothing ran; correct the inputs or re-dispatch on the current `main` tip.
- **A suite job failed or was cancelled:** the milestone is not validated. Investigate; a flaky job may be re-run
  with **Re-run all jobs** (the verifier refuses results carried over by *Re-run failed jobs*). A defect is fixed by
  an ordinary pull request (all six checks), and the milestone is then validated on the new final `main` commit —
  the earlier run never transfers to it.
- **A failed Main short check** holds further integration until investigated, as above.

## Records

- An intermediate merge record says **"merged; milestone validation pending"**, links the applicable milestone record
  once it exists, and states only the checks that actually ran on the merge commit (the short check) — never six
  post-merge checks when only the short check ran.
- A milestone record states: the **milestone id**, the **included scope**, the **tested SHA**, the **run ID**, the
  **attempt**, and the **actual results** of each of the seven required jobs (from the verifier's evidence record,
  `signalnest-milestone-evidence/1`), plus the verifier verdict. It validates that SHA only; later commits do not
  inherit it.

## Release eligibility and what is operator-controlled

A commit is eligible for a deployment, launch or full-validation closure claim only when the verifier PASSes a
milestone run for **that exact commit** and every separate gate passes. This check is **operator-controlled**: the
publish and run workflows (`staging-publish.yml`, `reader-publish.yml`, `reader-run.yml`) are manual dispatches behind
protected environments with human approval, and they do not read CI results — running the verifier before
dispatching them is the operator's step, not something those workflows enforce. Milestone creation, the decision
that a group of work is complete, and holding the integration queue are operator or coordinator actions.

## Activation checkpoint

1. Merge the approved change (separately authorized).
2. Verify the merge (sole parent, tree, patch) and its **Main short check** on the merge commit.
3. **Hold the integration queue** — no other merge into `main` — and run one milestone validation on that merge
   commit; verify it with the verifier.
4. Only after it passes is the new closeout policy activated (recorded outside the repository with the activation
   SHA and the milestone record). Until then the earlier policy governs. Because a push to `main` no longer runs the
   six checks once step 1 lands, the queue stays held from that merge until the milestone validation of step 3
   passes: no other change reaches `main` while neither the earlier six-check validation nor the new policy is in
   force.

## Rollback

Revert the change: `ci.yml` regains its push-to-`main` trigger and the two workflows and the verifier are removed.
No setting, ruleset or data changes are involved.
