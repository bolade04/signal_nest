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
| A person with no account (an invitee), or the data an unverified, deleted account left under its address | operator, on out-of-band proof of the address: `python -m app.privacy.cli erase-email --email <address> --confirm` | the command's `verification` report |

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
- **Invitations the account accepted** are bound to it by id (`accepted_by_user_id`;
  acceptance required its exact address). They are erased with it, and listed in its export,
  whether or not the address is verified. Its address is also cleared from the audit records
  about exactly those invitations. An account that joined by invitation stays unverified
  until it verifies, so this is what keeps its address out of the organization it joined.
- **Other data found only by an email address** (invitations addressed to it, audit values
  naming it) is erased with an account, and listed in its export, only when the account has
  **verified** that address. Registration does not prove an address, and account identity is
  exact while these matches ignore letter case, so an unverified address may be another
  person's. An unverified account is erased by id (and by what it accepted), and the preview,
  the receipt and the export say so (`email_keyed_data`). To erase that data for a person whose
  account is gone, prove the address out of band and run `erase-email`. That command is refused
  only while an account with a **verified** address holds it (in any letter case or form).
  An unverified account holding the address does not block it, and the command's own
  verification reports that account (`users.email`).

  **That holder may be a live member.** Every account that joined by invitation is unverified
  until it verifies. If such an account holds the address, `erase-email` also deletes that
  account's **accepted invitation** and clears the address from the audit records about it.
  The account and its membership stay, and its export then lists no invitation. Before running
  it on an address a live account holds, ask the person to verify the address and delete the
  account themselves, or record that this history goes.
- **Which occurrences are erased** (*interim, pending the operator's decision `D-PRIV-7`* on
  ambiguous occurrences, decisions packet row 3.6a). The deletion and the verifier share one
  matcher (`app.privacy.matching`).
  - **Every form of the address is looked for:**
    - Unicode NFC and NFD;
    - any letter case, with non-ASCII letters fully case-folded;
    - inside JSON `\uXXXX` escapes, up to three layers;
    - percent-encoded;
    - with the domain as the IDNA A-label (`xn--…`) or as the U-label.

    The SQL pre-filter compares only ASCII fragments that every one of those forms contains,
    so no database `lower()` decides anything for non-ASCII letters, on SQLite or on
    PostgreSQL. The comparison is made in Python. Invitations addressed to the account, and
    the verified-holder check of `erase-email`, compare the address the same way.
  - Each occurrence is judged by the product's own address grammar, the validator registration
    uses.
    - **`WHOLE`**: no registrable address could contain it. Examples: `mailto:a@b.co`,
      `<a@b.co>`, `"a@b.co"`, `(a@b.co)`, capitals, before a sentence-ending dot, or
      percent-encoded on its own. It is erased.
    - **`AMBIGUOUS`**: a registrable address could contain it. That includes other people's
      addresses (`xa@b.co`, `o'a@b.co`, `ía@b.co`, `a@b.co.org`). It also includes the
      account's own address after `email=`, `users/`, `#`, `{`, `|`, `'`, `’` or `«`, or before
      `.Word`, because registration accepts all of those characters in an address.
    - **Undecided** (also `AMBIGUOUS`): the local part and the domain stand as separate tokens
      but the address does not occur, as in `a&#64;b.co` or `a [at] b.co`.

    An ambiguous or undecided occurrence is left, since erasing it could destroy another
    person's data, and the verifier reports it (§2).
  - **Erasure clears whole values.** A text or JSON string that names the account `WHOLE` is
    cleared as a whole, together with any other address in the same string. Separate list items
    are separate values.
  - JSON object keys are never rewritten, so a key that names the account makes the
    verification fail (a finding).
  - **Not detected, a recorded limit.** Transformations that keep neither the address nor both
    its local part and its domain as text are not found: a hash or base64 of it, or the domain
    spelled out (`b [dot] co`). No product writer produces any of them.

After a deletion, also: remove the address from the **SES account-level suppression list** if it
is there (AWS-side, operator), and note the request in the operator's own request log.

## 2. Independent verification (E10)

E10 — "A data-deletion request has been serviced end to end by an implemented mechanism" — must
be independently verifiable and must not rely on audit records (`P6-PRIV-5` is P1). The verifier
(`app.privacy.verification`) reflects the **live schema**. It reports every remaining
reference to the subject, and every text or JSON value that still names it `WHOLE` in any form,
as a finding. It uses the deletion's own matcher (§1); JSON is decoded first, keys included. It
lists, under `content_ambiguous`, every row with an `AMBIGUOUS` or undecided occurrence. It
does not read the audit trail or the inventory module, and it does not call the deletion code.
It shares only the matcher with it, so a record the deletion should have cleared can never pass
as clean.

Precisely, while any occurrence of the subject remains, the verifier never reports `verified`
without `needs_review`:
- a `WHOLE` one is a finding, so `verified` is false (exit `1`);
- an `AMBIGUOUS` or undecided one is left and flagged: `verified` with `needs_review` (exit
  `4`).

Only the transformations listed under "Not detected" in §1 pass unseen.

A deletion request runs only the verifier's **reference check** before it answers (one equality
count per column that can point at the subject; the receipt lists only rows that point at the
subject itself). The text/JSON scan reads every text column of every table, so it is not run in
the request: the operator runs it with the commands below.

```
python -m app.privacy.cli verify-account --user-id <id> [--email <address>]
python -m app.privacy.cli verify-email --email <address>
python -m app.privacy.cli verify-organization --organization-id <id> --workspace-id <id> ...
```

Exit codes:
- `0` = verified, nothing to review.
- `1` = findings (each names table, column and row count).
- `4` = verified, but `content_ambiguous` lists rows that may hold the subject's identifier in a
  context the address grammar cannot attribute, or where the matcher cannot decide. *Interim,
  pending `D-PRIV-7`:* an operator reads those rows and records whose they are, and erases a
  row by hand only if it is the subject's.

The report carries the SHA-256 of the email, not the email, so it can be kept as evidence. For an
organization, its audit records are reported under `retained_anonymised` (kept by `O-6`), and
each must have every identifying column cleared, or it is a finding.

**Which run applies to an account deletion.** The receipt's `email_keyed_data.status` says it,
and so does the `email_scope` field of the `privacy.account.deleted` log event.
- `included`: run `verify-account --user-id <id> --email <address>`. Exit `0`, or `4` after
  review (interim, pending `D-PRIV-7`), is the proof.
- `excluded_unverified`: run `verify-account --user-id <id>` (exit `0` or `4`). With `--email`,
  findings are expected: the address-keyed data that was deliberately left, such as an
  invitation the account never accepted. They stay until `erase-email` (§1) on out-of-band
  proof. Then `verify-account --user-id <id> --email <address>` exits `0` or `4`, or reports
  only another account that holds the address.

A recorded E10 demonstration needs, in an authorized environment:
- the request (self-serve);
- the receipt;
- an operator re-run of the verifier with exit `0`. Under the interim procedure pending
  `D-PRIV-7`, exit `4` with the reviewed rows recorded is also accepted.
- the time of each.

None has been recorded yet.

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
