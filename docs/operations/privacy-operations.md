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
  - **What counts as the same address.** Registration's own rule decides; it was measured, not
    assumed.
    - **Same address:** registration stores the NFC form, and maps the domain through UTS46
      to its lower-case U-label. So NFC/NFD spellings, A-/U-label domains, domain letter case
      and the domain spellings UTS46 maps (fullwidth letters and stops, the ideographic full
      stop `。`, mathematical letters) are one account. The matcher resolves a domain in text
      with that same mapping.
    - **Distinct accounts:** registration keeps `straße`/`strasse`, `weiß`/`weiss`, `ſ`/`s`,
      `ﬃ`/`ffi` and `ς`/`σ` apart. So does the matcher, which lower-cases letters rather than
      case-folding them (case-folding joined each pair, and let a verified account at one form
      reach another organization's records for the other).
    - **The one recorded exception:** registration keeps the local part's letter case
      (`Kate`/`kate` are two accounts). Erasure and export still match an account's own address
      in another letter case, gated on its verified address. This is a recorded engineering
      decision. The alternative, making account e-mail identity case-insensitive (`Kate` and
      `kate` one account; a migration and a sign-in change), is left open as operator
      decisions packet row 5.1 (F2).
    - **A letter case shared with another address decides only when that address exists.**
      Some upper-case spellings belong to two registrable addresses: `ΣΣ` is the upper case of
      `σσ` and of `σς`, `IA` of `ia` and `ıa`, `STRASSE` of `strasse` and `straße`, `OFFICE` of
      `office` and `oﬃce`, and ordinary capitals such as `JOSÉ`, `SALES`, `INFO` or `ИВАН` of
      `joſé`, `ſales`, `ınfo` and `иᲀан` too. The matcher computes which upper-case spellings
      Unicode lets two letters of different lower case share (`S`, `I`, `Σ`, `Μ`, Cyrillic `В`,
      `О`, `С`, `Т`, sequences such as `SS`, `FF`, `ST`).
      - **Capital positions are ambiguous, lower-case positions are exact** (fix pass 8). A
        spelling relates to the account when every lower-case letter in it is the account's
        letter at that position, and every capital is the upper case of the account's letter
        (or letters) there. One letter's upper case may be longer (`ß` is `SS`), and `σ` and
        `ς` share `Σ`.
      - It relates only through such a shared spelling when it is written with one (other than
        the account's own exact spelling), or when its lower case is another address's. For
        `straße@…` that is `STRASSE@…` or `StraSSe@…`; for `ırmak@…`, `Irmak@…`; for `σς@…`,
        `σΣ@…`; for `ſales@…`, `Sales@…` or `SaLES@…`.
      - A spelling whose lower-case letters contradict the account is another person's address
        and is never touched: for `straße@…`, `strasse@…` and `Strasse@…`.
      - Such an occurrence is `AMBIGUOUS` when a **twin** exists in the product when the
        deletion, export or verification runs: an account's e-mail or an invitation's address
        with the occurrence's upper case and another canonical form (NFC, lower-cased letter
        by letter, the domain as registration stores it). It is then never exported or erased
        for either twin, and the verifier flags it (exit 4).
      - When no twin exists, it is the account's (`WHOLE`): erased, exported and counted.
      - A stored spelling's canonical form is its own lower case: `ΣΣ` reads `σσ`, `IA` reads
        `ia`, `STRASSE` reads `strasse`. So an invitation to `ΣΣ@…` with no other address is the
        `σσ` account's; for a `σς` account it is itself a twin, and flagged.
      - The lookup runs only for such an occurrence, once per spelling. It selects with a
        `LIKE` pattern built from that upper case (`SALES` → `_ale_@domain`): 20,000 unrelated
        addresses, half at the same domain, select none.
      - The account's own exact spelling, a lower-case spelling, and capitals without such
        letters (`CAROL_X@…`) are `WHOLE` without a lookup.
      - Recorded consequences of this rule (adversarial review r6):
        - **R6-2.** The RFC 2142 role names (`info`, `marketing`, `sales`, `support`, `abuse`,
          `noc`, `security`, `postmaster`, `hostmaster`, `usenet`, `news`, `webmaster`, `www`,
          `uucp`, `ftp`) are one address to registration in any letter case: it stores
          `SALES@x` and `Sales@x` as `sales@x`. The twin rule does not know that. For a verified
          `ſales@x` or `ınfo@x`, with no `sales@x` or `info@x` account or invitation in the
          product, a role name written with a capital in text (`SALES@x`, `Sales@x`,
          `SaLES@x`, `INFO@x`, `Info@x`) is taken as that account's and erased.
        - **R6-3.** With no twin in the product, a spelling that relates through a shared
          capital is the account's. For a verified `straße@x`, `STRASSE@x` is erased although
          it is equally the upper case of `strasse@x`, a person who may exist only in text and
          hold no account or invitation. This is over-erasure of that person's mention, by the
          coordinator's rule; it is recorded for `D-PRIV-7` and the operator.
        - **R6-5.** The twin lookup runs inside the deletion's own transaction and locks neither
          `users` nor `organization_invitations`. A twin registered or invited after the lookup
          has read is not seen by that deletion: the occurrence is erased as the account's, as
          it would be had the twin appeared just after the deletion. Each verification decides
          again, as of its own read.
        - **R6-7.** The SQL pre-filter and the twin lookup rely on the database's `lower()`
          turning ASCII `A`–`Z` into `a`–`z`, and on nothing else (every other comparison is
          made in Python, whose case mappings ignore the locale). PostgreSQL's `lower()` follows the database's
          `LC_CTYPE`; under a Turkish locale it maps `I` to `ı`, so a fragment holding `i` would
          miss a row written in capitals. The repository sets no database locale anywhere; the
          deployed RDS default (`en_US.UTF-8`) is assumed, not asserted by code. Before an E10
          run, the operator confirms `SHOW lc_ctype;` is not a Turkish or Azerbaijani locale.
  - **Every form of the address is looked for:**
    - Unicode NFC and NFD;
    - another letter case, non-ASCII letters included (lower-cased; a letter case shared with
      a twin the product holds is flagged, see above). Each case image is compared in NFC, like
      the stored text: `ΐ` upper-cases to a decomposed `Ϊ́`, which NFC re-composes (fix pass 9);
    - inside JSON `\uXXXX` escapes, up to three layers;
    - percent-encoded, one layer (also on top of each JSON layer);
    - with the domain as the IDNA A-label (`xn--…`), as the U-label, or in any spelling the
      UTS46 mapping turns into it (fullwidth, `。`, mathematical letters).

    The SQL pre-filter compares only ASCII fragments that every one of those forms contains,
    so no database `lower()` decides anything for non-ASCII letters, on SQLite or on
    PostgreSQL. The comparison is made in Python. Invitations addressed to the account, and
    the verified-holder check of `erase-email`, compare the address the same way.
  - **Addresses without an ASCII fragment.** The local part may have no run of two ASCII
    characters (`иван@пример.рф`, `a@b.co`). Then, inside a request, the filter matches the
    address's whole forms instead:
    - raw text in lower, upper or capitalised case;
    - JSON-escaped with a lower-case domain;
    - percent-encoded in lower case.

    That keeps the request's work bounded. Measured on SQLite with 20,000 unrelated audit rows,
    the deletion selected 0 rows instead of all 20,010, and took 0.71 s, against 0.53 s for an
    ASCII address. It has a limit: other mixed casings of such an address (`иваН@…`) are not
    erased in the request on SQLite, nor in a JSON column on either engine. The operator's
    verification reads every row for such an address, so it reports any copy left as a
    finding (exit 1), or flags it (exit 4) when the casing is shared with a twin the product
    holds (`иВАН@…` with an `иᲀан@…` address); it is never passed. For an address with an ASCII run, the filter also selects
    the local part followed by `@`, `%40`, `＠` or `﹫` (raw or JSON-escaped), so domain
    spellings without an ASCII fragment are read too.
  - Each occurrence is judged by the product's own address grammar, the validator registration
    uses.
    - **`WHOLE`**: no registrable address could contain it. Examples: `mailto:a@b.co`,
      `<a@b.co>`, `"a@b.co"`, `(a@b.co)`, capitals, before a sentence-ending dot, or
      percent-encoded on its own. It is erased.
    - **`AMBIGUOUS`**: a registrable address could contain it. That includes other people's
      addresses (`xa@b.co`, `o'a@b.co`, `ía@b.co`, `a@b.co.org`). It also includes the
      account's own address after `email=`, `users/`, `#`, `{`, `|`, `'`, `’` or `«`, or before
      `.Word`, because registration accepts all of those characters in an address.
    - **Undecided** (also `AMBIGUOUS`): the address does not occur, but its local part and its
      domain are joined by an explicit separator written instead of `@`. The separators are
      `[at]`, `(at)`, `{at}`, `<at>`, `[@]`, `(@)`, `&#64;`, `&#x40;`, `&commat;`, the
      fullwidth `＠` and the small `﹫` (registration does not accept either in an address), and
      `at` or `@` between spaces. Examples are `a&#64;b.co`, `a [at] b.co` and `a＠b.co`. The
      domain after the separator is read with the same UTS46 mapping. Mere co-occurrence is
      not flagged: "John from Acme (acme.example)" is not about `john@acme.example`. "sales at
      shop.example" is flagged, because ` at ` is such a separator.

    An ambiguous or undecided occurrence is left, since erasing it could destroy another
    person's data, and the verifier reports it (§2).
  - **Erasure clears whole values.** A text or JSON string that names the account `WHOLE` is
    cleared as a whole, together with any other address in the same string. Separate list items
    are separate values.
  - JSON object keys are never rewritten, so a key that names the account makes the
    verification fail (a finding).
  - **Not detected, a recorded limit: exactly these pass the verification unseen.** These are
    texts that hold neither of two things in any of the forms listed above. One is the local
    part followed by `@` and a domain spelling that registration's UTS46 mapping turns into the
    account's domain. The other is the local part joined to such a domain by one of the listed
    separators. Measured examples:
    - a hash, base64 or other encoding of the address. Only one layer of percent-encoding, up
      to three layers of JSON escapes and the listed `@` entities are decoded. So a second
      percent layer (`%2540`), a fourth JSON layer, JSON escapes inside percent-encoding
      (`%5Cu0040`), other HTML entities (`&#46;`, `&#99;`) and quoted-printable (`=40`) pass
      unseen;
    - characters inserted into the address: a soft hyphen, a zero-width space or a line break,
      or quotes around the local part (`"carol"@…`);
    - the domain spelled out (`b [dot] co`), or a separator not in the list;
    - a local part in a compatibility spelling (fullwidth `ｃａｒｏｌ@…`). That is not a
      spelling of the address at all: registration keeps it as a distinct account.

    No product writer stores any of them for an address. Third-party or customer text (raw
    signals, notes) can contain the first three. Whole-text content hashes and the hashed
    signal embedding include an address's parts, but live in the same rows as that text and go
    with it. The verifier's `email_sha256` is an unsalted SHA-256: keep the report as
    pseudonymous evidence, not anonymous.

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

Exactly the texts described under "Not detected" in §1 pass unseen. Everything else is
erased, reported as a finding (exit 1), or flagged (exit 4). That includes a letter case shared
with a twin the product holds, and a copy of an address without an ASCII fragment in a casing the
request does not select (§1).

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

## 6. Reproducing a complete organization export (D-9f)

The organization export is complete but paged; the web download assembles it. To produce the
same file from a shell for an organization OWNER's session (`$API`, `$ORG` and `$TOKEN` are the
API base URL including `/api/v1`, the organization id and that OWNER's bearer token; keep the
token out of shell history and out of evidence):

```sh
# 1. The index: members, exact row counts, first pages, next cursors.
curl -sS -H "Authorization: Bearer $TOKEN" "$API/organizations/$ORG/export" > index.json

# 2. For each table whose next_cursor is not null, follow the cursors until it is null.
table=llm_usage_events
cursor=$(jq -r ".tables.$table.next_cursor // empty" index.json)
n=0
while [ -n "$cursor" ]; do
  n=$((n+1))
  curl -sS --fail -H "Authorization: Bearer $TOKEN" \
    --get --data-urlencode "cursor=$cursor" --data-urlencode "limit=5000" \
    "$API/organizations/$ORG/export/$table" > "$table.page-$n.json" || exit 1
  cursor=$(jq -r '.next_cursor // empty' "$table.page-$n.json")
done

# 3. The table is the index's rows followed by each page's rows, in page order.
jq -s '[.[].rows] | add' <(jq ".tables.$table" index.json) "$table".page-*.json
```

Check every table: the count of rows received must equal the index's `row_count`. A
difference means rows were created or deleted while you downloaded (there is no snapshot
isolation; see `data-inventory.md`, *Portability*); download again. A non-2xx answer stops the
loop: a 403 is a role (exactly OWNER) or membership problem, a 422 `invalid_cursor` a cursor
that is not the one the previous page returned, and `limit` above 5000 is refused. Nothing in
this procedure writes anything, and no usage or accounting figure is adjusted to match an
export.
