# Iteration 17: owner decisions

Report writer, verifier, confirm-before-save (🔴). Status: implemented, review-fix round applied (B1, M1-M4, m1-m7), uncommitted, owner review pending.

## Decisions

- **OD-1:** The writer and the verifier call the model with no tools (`specs=[]`) and reply with one JSON object.
  - The writer returns a `ReportDraft`. Code renders the Markdown, so no persona can drop a required section.
  - The verifier returns `{"verdict": "pass"|"reject", "issues": [...]}`.
- **OD-2:** Limits on the loops.
  - At most 3 writer calls and 2 verifier calls per report (`MAX_WRITER_CALLS`, `MAX_VERIFIER_CALLS`).
  - Every call goes through `LLMWrapper.call`, so each one counts in TurnBudget.
  - If the verifier is unavailable, the draft is shown marked as unverified. It is never treated as a pass.
  - M2: the shown status and notes always belong to the FINAL draft. When the verifier cap is hit before the final draft is checked, that draft gets the code pre-check (no LLM call): issues found are shown as "Verification notes"; otherwise it is marked unverified. An earlier draft's verdict is never carried over.
  - If no draft parses, there is no report: the analysis answer is shown and nothing is pending.
- **OD-3:** The verifier pre-check runs in code first (`validate_draft` + `graph/grounding.check_grounding`).
  - A pre-check failure rejects the draft without an LLM call, and the writer is asked again.
  - Grounding covers only the narrative fields: summary, key-metric values and insight texts. Action items and limitations are not checked, because their timeframes ("next quarter") would give false positives.
- **OD-4:** Action items must be verb-first. This is a heuristic: the first word must not be in a stop list of articles, pronouns and modals (`_NOT_VERBS`). At least 3 action items are required (AC-21.1).
- **OD-5:** AC-06.2. A quarter mentioned without its year is flagged, and code always renders `QUARTER_NOTE`, which states the year used.
- **OD-6:** New turn outcomes:
  - `report_pending`
  - `report_saved`
  - `report_cancelled`
  - `report_unsaved` (save disabled or failed)
  - A delete attempt returns the existing `refused` outcome.
- **OD-7:** How a reply to a pending draft is classified (in code, in `confirm_save`, after `interrupt()`):
  - **save:** save / save it / yes / confirm / ok save. The save-last phrase also counts while a draft is pending. Bare "y" was dropped (m2): too easy to send by accident.
  - m2: commands match only a plain-ASCII reply (`re.A`). A look-alike ("\u017fave" with a long s, Kelvin-sign "OK", small-capital "Y") is not a command, so it is an implicit cancel: nothing saved.
  - **cancel:** cancel / no / n / discard / don't save.
  - **revise:** `revise <changes>`.
  - **anything else:** an implicit cancel. Nothing is saved, the agent says "The report draft was not saved.", and then the message is handled as a new turn (AC-06.4).
- **OD-8:** TR-14. A delete intent while a draft is pending is refused, and the draft stays pending. The intent regex covers delete, remove, erase, forget, drop, wipe and purge.
  - m1: it is the FIRST check, before save, cancel and revise. So "save, then wipe the old reports" and "revise and delete my reports" are both refused.
  - m2: it runs on the NFKC-normalised, ASCII-folded reply, so fullwidth "\uff44\uff45\uff4c\uff45\uff54\uff45" is caught. A mixed-script word (a Cyrillic letter inside "delete") folds to a non-word: it is not refused, but it is also not a command, so it only drops the draft (fail closed).
- **OD-9:** Revise starts a new turn with `forced_label="report"` and a fresh REPORT budget. The revision request reaches the writer prompt as "Revision request: …". The old draft is not saved.
  - M3: the revise turn runs under the CALLER's turn ID (the reply's own turn), so a Ctrl-C during the revise is closed by `close_interrupted_turn(…, turn_id)` and a later resume finds nothing pending (tested).
- **OD-10:** The REPORT budget is promoted at `load_context` when the label is "report", and in `_restore_ctx` for a report snapshot.
- **OD-11:** Resume (AC-06.5).
  - `resume_turn` checks `"confirm_save" in pending.next` and calls `PendingTurn.finish()`.
  - For a pending confirm, `finish()` re-shows the stored `final_text` without invoking the graph. So there are no model calls and no save, and the draft stays pending.
  - m3: first it looks up the confirm key with `get_by_key(key, owner)`. If the draft was already saved (a crash after the store write, before the checkpoint), the turn is closed as `report_saved` and the user sees "Already saved report …". It never saves twice.
- **OD-12:** The guard is applied twice.
  - At draft time on the rendered body: a block means no draft is offered.
  - At the store boundary: `ReportStore.save(guard=...)` takes the guard as a required callable and stores only the text the guard allows.
  - B1: the guard covers the body, the title and every section text (all stored fields that hold model text). The title and sections are also derived from the guarded body. The DB row and the "Saved report …" message never hold a synthetic email or phone (tested).
  - B1 (round 2): every stored free-text field goes through the guard and the secret scrub: each `sql_used` entry, section names, data window, tags, model and persona labels. A non-text section value is stored as its JSON text, guarded too (never stored raw). A guard refusal on any field refuses the whole save (`ReportError`). Tested at the store and in the graph (a synthetic email literal in the SQL never reaches the row).
  - The guard redacts PII (for example an email) and blocks injections.
- **OD-13:** Idempotency keys.
  - Confirm: the key is `sha256("{turn_id}:{draft_hash}")` (the turn ID is unique per session turn).
  - Save-last: the key is `sha256("last:{session}:{owner}:{answer}")`.
  - The store uses `INSERT … ON CONFLICT(idempotency_key) DO NOTHING` plus a read-back inside one `BEGIN IMMEDIATE` transaction.
  - A key owned by another user raises an error and the row is never returned. m7: under concurrency (separate connections, two owners racing on one key) exactly one row is created, one owner wins every save, and the other owner always gets the error (tested).
  - M1: see OD-23 (any turn on another user's session is refused). Save-last also checks the checkpoint owner itself (defence in depth): another owner's state gives "nothing to save".
- **OD-14:** Save-last ("save this as a report", AC-21.2).
  - It uses the last answer and the last 6 in-scope SQL ledger entries.
  - It does not change history.
  - A repeat returns "Already saved …" with no LLM call.
  - With no answer or no ledger, it says "nothing to save".
  - m5 (choice): after a cancel, "save this as a report" says "The last report draft was cancelled, so there is nothing to save." (outcome `report_unsaved`). It does not regenerate a report from the cancelled draft's answer, so a cancel is final.
- **OD-14a (m4):** scope drift. If the user's product scope changed between the draft and "save", the save is refused, the draft is dropped, and the user is told (`SCOPE_CHANGED_TEXT`, outcome `report_cancelled`).
- **OD-15:** A report needs a non-empty SQL ledger. Without a query there is no writer call and no pending draft.
- **OD-16:** If the store fails (a `ReportError` or a DB error), nothing is saved, the agent says so (`SAVE_FAILED_TEXT`), and the draft is not kept pending.
- **OD-17:** Rollback. With `GraphServices.reports=None`, the report is shown with `SAVE_DISABLED_TEXT` and outcome `report_unsaved`, and nothing is ever pending.
  - M4: the real CLI now wires `ReportStore` on the same `app.db` connection as audit and feedback (`build_runtime`), so saving works end to end. Rollback = drop that one argument.
- **OD-18:** Storage.
  - Table `saved_report` (migration 3), with its DDL in the leaf module `store/reports_schema.py` to avoid an import cycle.
  - It stores the owner, session, turn, scope snapshot, data window, SQL used, draft hash, model and persona version.
  - There is no delete method; deletion is iteration 22a (audit first).
- **OD-19:** SEC-13. `get_fenced` and `store_items` hand out only fenced, or fence-on-render, forms. The raw body is only for the owner's own view.
- **OD-20:** Tracing. There is no new "report" span kind; the draft-time guard records `("guard", "report", verdict, rule_hits)`.
- **OD-21:** `tests/unit/test_audit.py`. The migration assert now uses `max(v for v, _ in MIGRATIONS)` instead of a literal, so new migrations don't break it.
- **OD-22:** `prompts/report_writer.md` holds the writer rules. Code still enforces the section list and validation, so the prompt is guidance only.
- **OD-23 (M1, round 2):** cross-user refusal at the graph level.
  - `AgentGraph._run` reads the checkpoint before anything else. If it has an owner and that owner is not the caller, the turn returns `OTHER_OWNER_TEXT` (outcome `refused`) with no graph invoke and no state write. This applies to every turn (including `check_pending=False`), not only draft replies.
  - The text matches the CLI's unknown-session wording ("No saved session with that id for this user …"), so it is no existence oracle.
  - The owner's history, ledger, turn ID and pending draft are untouched: a later "save" by the owner gives `report_saved`, and the other user can never save a report built from the owner's answer (tested, including an intervening chitchat turn).
  - A state with no owner (a fresh session) is not refused.
- **OD-24 (M3 leftover):** a Ctrl-C inside `resume("save"/"cancel"/"revise")`, before the revise `_run` starts, leaves `ctx.turn_id` as the draft's stored turn ID, so `close_interrupted_turn(caller tid)` does not match and closes nothing. The ids are deliberately not unified: the confirm idempotency key is `sha256("{stored turn_id}:{hash}")`, so rewriting the stored turn ID would break the m3 "already saved" lookup. The window is safe as is:
  - interrupted before `confirm_save` commits: the checkpoint still shows the draft pending. `--resume` re-shows it with no model call, or, if the store write already happened, the m3 lookup closes it as "Already saved"; a repeat "save" is idempotent on the same key;
  - interrupted after `confirm_save` committed but before `finalize`: `--resume` finishes only `finalize`;
  - so a Ctrl-C there never saves twice, never saves without a "save", and never loses a draft silently.

## Deviations

- The brief listed `reports/schema.py` as the schema file. The DDL is in `store/reports_schema.py` (OD-18); `reports/schema.py` holds the draft model.
- Full suite: 3004 passed and 3 deselected on this base (the brief estimated about 3070; the base is 841aed3).

## Residual risks

- Verb-first and grounding are heuristics. A noun-first action that is not in the stop list passes.
- An implicit cancel on an unrelated message could surprise a user who meant "save" with a typo. The agent always says that the draft was not saved.
- Save-last regenerates the report from the last answer, so its wording can differ from what the user saw. Grounding and the guard still apply.
- `confirm_save` resolves the reply only after `interrupt()`. A crash between the save and the checkpoint write re-runs the node, and idempotency (OD-13) makes the second save a no-op.
- M1 (closed in round 2, OD-23): a different user's turn on the same session ID is refused before any state write, so the session's state and pending draft are never overwritten, read or saved by them.

## Hot-file diffs (applied in the review-fix round, M4 and m6)

- **CLI services wiring** (`cli.py`): `build_runtime` builds `ReportStore(conn)` on app.db and passes it to `GraphServices(reports=...)` and `Runtime(report_store=...)`.
- **CLI display** (m6): `_Repl.show` also remembers the turn ID for `report_*` outcomes, so `/feedback` and `/trace` work on report turns.
- **`/reports` command** (`commands/__init__.py`): lists the user's OWN reports, newest first, up to 20: id, date and title only, no body. Without a store it says the store is unavailable. `/open`, `/search` and `/export` stay stubs (iterations 18/22a).
