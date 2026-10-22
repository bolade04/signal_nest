# Privacy operations — deletion requests, verification, retention (P6-PRIV-1)

**Status:** operating procedure for the mechanism added under `P6-PRIV-1`. Nothing here has
been executed against a deployed environment. Retention **durations** are undecided
(`O-5` / `P6-D05`); see [`data-inventory.md`](data-inventory.md#retention-durations-undecided).

## 1. Servicing a deletion request

| Requester | Path | Proof |
|---|---|---|
| An account holder who can sign in | Settings → Account → **Delete account** (current password, type `DELETE`); or `POST /api/v1/account/delete` | the receipt's reference check (`references_cleared: true`), then the operator's full verification (§2) |
| An account holder who cannot sign in | password reset first, then the self-serve path (the operator never deletes an account on someone's word alone) | as above |
| An organization (its OWNER) | Settings → **Delete organization** (current password, the organization's exact name); or `POST /api/v1/organizations/{id}/delete` | the receipt's reference check; keep `workspace_ids` for the operator's full verification (§2) |
| A person with no account (an invitee) | operator: `python -m app.privacy.cli erase-email --email <address> --confirm` | the command's `verification` report |

Rules the code enforces:

- **Sole owner.** The only OWNER of an organization that has other members cannot delete their
  account (409 `account_deletion_sole_owner`): they make another member an OWNER, or delete the
  organization, first. The preview (`GET /api/v1/account/deletion-preview`) says so in advance.
- **Sole member.** An organization in which the account is the **only** member is deleted with
  the account — left alone it would have no member who could ever delete it.
- **Running jobs.** An organization with a claimed or running job cannot be deleted (409
  `organization_jobs_active`); retry when the job ends.
- **Members of a deleted organization** keep their accounts; they lose access to that
  organization only.
- **Data found only by an email address** (invitations addressed to it, audit values naming
  it) is erased with an account, and listed in its export, only when the account has
  **verified** that address. Registration does not prove an address, and account identity is
  exact while these matches ignore letter case, so an unverified address may be another
  person's. An unverified account is erased by its id alone; the preview, the receipt and the
  export say so (`email_keyed_data`). To erase that data for a person whose account is gone,
  prove the address out of band and run `erase-email` (refused while any account holds the
  address in any letter case).
- **Whole values only.** An audit value is erased when it holds the account's id or address as
  a whole value, never because a longer address contains it (`xcarol@…`, `carol@….au`).

After a deletion, also: remove the address from the **SES account-level suppression list** if it
is there (AWS-side, operator), and note the request in the operator's own request log.

## 2. Independent verification (E10)

E10 — "A data-deletion request has been serviced end to end by an implemented mechanism" — must
be independently verifiable and must not rely on audit records (`P6-PRIV-5` is P1). The verifier
(`app.privacy.verification`) reflects the **live schema** and reports every remaining reference
to the subject and every text/JSON value that still contains its identifiers as a whole value.
It does not read the audit trail, the inventory module or the deletion code.

A deletion request runs only the verifier's **reference check** before it answers (one equality
count per column that can point at the subject; the receipt lists only rows that point at the
subject itself). The text/JSON scan reads every text column of every table, so it is not run in
the request: the operator runs it with the commands below.

```
python -m app.privacy.cli verify-account --user-id <id> [--email <address>]
python -m app.privacy.cli verify-email --email <address>
python -m app.privacy.cli verify-organization --organization-id <id> --workspace-id <id> ...
```

Exit `0` = verified; `1` = findings (each names table, column and row count). The report carries
the SHA-256 of the email, not the email, so it can be kept as evidence. For an organization, its
audit records are reported under `retained_anonymised` (kept by `O-6`) and each must have every
identifying column cleared, or it is a finding.

A recorded E10 demonstration needs, in an authorized environment: the request (self-serve), the
receipt, an operator re-run of the verifier with exit `0`, and the time of each. None has been
recorded yet.

## 3. Retention purge

```
python -m app.privacy.cli sweep [--class <class> ...] [--dry-run]
```

- A class with no configured duration is **refused** (`policy_unset`, nothing deleted) and the
  command exits `2`. `environment=production` does not start until all seven durations are set.
- Platform classes are purged by the command itself in bounded batches; workspace classes are
  enqueued as one `retention.purge` job per workspace (idempotent per day) and run by the worker.
- Run it daily. **No scheduler exists for it yet**: an ECS scheduled task (or equivalent) running
  this command is an infrastructure follow-up for the operations lane; until then it is run by
  hand.
- Workspace members can see a workspace's `retention.purge` jobs in the job list, as for any job
  of the workspace, but no customer role can cancel one (409 `job_not_cancellable`): the purge
  applies the operator's retention policy. The operator stops a purge by changing that policy
  (the job re-checks it when it runs).
- The sweep's output counts only the jobs it created (`jobs_enqueued`); a link the day already
  has is listed under `jobs_existing` by status, so a cancelled or failed link shows instead of
  being counted as enqueued.

## 4. After restoring a backup

Restored data predates later deletions. Before reopening a restored database to traffic,
re-apply every erasure completed after the backup's timestamp: search the application logs for
`privacy.account.deleted` and `privacy.organization.deleted` events after that time (they carry
the pseudonymous `user_id` / `organization_id`), repeat each deletion through the CLI or the
service functions, and run the verifier for each. This only works while log retention is at
least as long as backup retention — an operator item.

## 5. Relationship to P6-PRIV-3 and P6-PRIV-5 (P1, outside E0 for the pilot)

`P6-PRIV-3` (database-level audit immutability) and `P6-PRIV-5` (audit coverage) are P1 by the
recorded `P6-D02` narrowing. This mechanism adds **no** `record_audit` call and **does not**
depend on one: deletions are proven by the verifier, and are logged as structured events. Two
interactions are recorded rather than left implicit:

- Deletion **anonymises** audit records in place (account deletion: values naming the account;
  organization deletion: every identifying column of its records). Database-level immutability
  under `P6-PRIV-3` must therefore allow this one erasure path (for example a privileged
  anonymisation routine) or the two rows conflict.
- The retention purge deletes audit records older than the `audit_logs` duration; under
  `P6-PRIV-3` that must also remain possible for the retention role.
