# Iteration 35: user erasure with audit-first pseudonymisation (ODs) [owner review pending]

🔴 gate (SEC-18, AC-28.6, FR-59): deletion and the audit trail. Not self-approved; listed in
`docs/process/OWNER-QUEUE.md`.

Files:
- new `src/opsfleet_agent/commands/erase.py`: the maintainer CLI
  `python -m opsfleet_agent.commands.erase --as <maintainer> --user <id>` (preview), then
  `... --confirm <token> --retype <id>` (erase); the checkpoint, trace, export and
  golden-candidate scan and the post-commit file deletes
- `store/audit.py`: event types `erase.executed` and `erase.failed`, `ERASE_USER_TABLES`,
  `ErasePlan` (read-only scan with a stable digest), `audited_erase` (one transaction, audit
  first) and `_pseudonymise` (the one permitted audit mutation)
- `commands/__init__.py`: `/erase` in the REPL prints `ERASE_INFO_TEXT` only
- tests: new `tests/unit/test_erase.py` and `tests/unit/test_residue.py`, extended
  `tests/unit/test_audit.py`
- docs: README (commands table, a "User erasure (maintainers)" section, the Erasure row in
  "Not built" is now built, the retention paragraph), `docs/technical.md` (commands row, module
  map, gaps), `docs/process/04-plan.md`, `docs/process/OWNER-QUEUE.md`

No hot files, no new dependencies (stdlib `hmac`, `secrets`, `hashlib`), no schema migration.
No eval case was added: the CLI is a maintainer tool outside the chat, not a guardrail or a tool
the model can call.

## Decisions
- **D-222 Maintainer CLI and two-step confirmation.** Erasure is a module entry point with the
  same fail-closed maintainer allowlist as triage (D-216, `config/maintainers.yaml`). The REPL
  `/erase` only explains the process, whatever its arguments, so a chat user can never erase
  anyone, including another user. Refusals (not a maintainer, malformed id, the maintainer
  erasing themself) exit 2 before any write. The first run prints counts per store (never
  content or ids) and a token `<expires>.<32 hex>`: an HMAC-SHA256 keyed by a random
  `erase_confirm_key` in `meta`, over (actor, user, plan digest, expiry), valid for 300 s. The
  second run needs the token and `--retype <user id>` (exact match). The plan is re-scanned
  inside the write transaction and must match the digest, so a token from a preview that no
  longer describes the data is refused.
- **D-223 Pseudonymisation of the audit trail.** The audit log is append-only by trigger; the
  only permitted mutation is erasure. Inside the erase transaction the no-update trigger is
  dropped, `actor_user_id` and `details.target_user` equal to the user are rewritten to a fresh
  random `erased-<16 hex>` (not derived from the id, so it cannot be reversed or linked across
  erasures), the trigger is recreated from the canonical DDL and the schema is verified
  against the expected layout before COMMIT; a mismatch or any remaining row naming the user
  rolls everything back. `target_ids` keep the random object ids (report, feedback), which do
  not identify a person. The public audit record refuses to emit `erase.*` events.
- **D-224 One transaction, audit first.** `audited_erase` runs in `BEGIN IMMEDIATE`: scan,
  insert `erase.executed` (actor = maintainer, target = pseudonym, count = rows to delete);
  if that insert fails nothing is deleted (`AuditError`, exit 1). Then the FTS rows of the
  user's reports are deleted (verified gone, then `optimize`), each table in
  `ERASE_USER_TABLES` (report vectors, saved reports, preferences, feedback, quota,
  fingerprints) is deleted with its count checked against the plan and verified zero, audit
  rows are pseudonymised, the executed row is re-read, then COMMIT and
  `wal_checkpoint(TRUNCATE)` with `secure_delete` on. Any failure after the insert rolls back
  everything (including `erase.executed`) and appends `erase.failed` best effort. No `VACUUM`:
  `secure_delete` zeroes freed pages, and a VACUUM would lock the shared store. Every scan is
  bounded (`MAX_ERASE_IDS`); a second erase deletes nothing and is still audited (count 0).
- **D-225 Checkpoints and sessions.** Only handled when `checkpoints.db` exists (it is never
  created here). A missing or wrong checkpoint key refuses before any write, since erasing
  without it would silently miss threads. The scan is bounded (`MAX_THREADS = 10_000`). A
  thread is deleted when its latest checkpoint's owner is the user, or when it has no owner
  and its id is one of the user's sessions. Threads that cannot be decrypted are kept and
  counted in the output, as their owner is unknown.
- **D-226 Files after commit.** Local trace files of the user's sessions, exports of the user's
  reports and golden candidates of the user's feedback are deleted only after the DB commit.
  Each store that fails is audited as `erase.failed` (store and error type only), named in the
  output for manual removal, and the command exits 1; the DB erase stands. Regression-case
  drafts in the repository and the user's profile override entry are listed, not deleted:
  both are reviewed or operator-owned files. Scans skip symlinks and are bounded
  (`MAX_FILES`).

## Tests
- `test_residue.py`: builds a synthetic world (two users, every store populated), erases one
  user and scans every table, FTS index, WAL, checkpoint, trace, export and candidate for the
  user's id and marker strings; the other user is untouched.
- `test_erase.py`: preview deletes nothing; confirmation required, expiring and bound to user,
  actor and plan; retype must match; refusals before any write; a user session cannot erase;
  audit-insert failure aborts with nothing deleted; a failed delete rolls back including the
  audit row and leaves one `erase.failed`; a second erase is a no-op and audited; missing
  stores are tolerated and not created; bad checkpoint key refused; a file-delete failure is
  audited and reported.

## Not covered (stated in the CLI output and the README)
- Remote traces (Langfuse), backups and copies outside the data dir, `config/profiles.yaml`.
- A rerun cannot rediscover files whose delete failed (the DB rows linking them are gone); the
  CLI prints their names.
- Undecryptable checkpoint threads, trace files of sessions no longer linked to the user, and
  global `meta` rows.

## Open points for the owner
- A delete failure inside the transaction maps to exit 2 with "Refused: ... Nothing was
  deleted." although `erase.executed` was attempted (it was rolled back, and `erase.failed` is
  written). A distinct exit code may be clearer.
- `json_set` on `details.target_user` could in theory push a row past the 2048-byte details
  cap; pseudonyms are fixed length and usually shorter than ids, so this is not enforced.
- The preview writes the confirmation key into `meta` (a write before the confirmed run).
