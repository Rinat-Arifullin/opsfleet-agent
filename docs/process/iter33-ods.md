# Iteration 33: rename, Markdown export, retry report (ODs)

AC-21.9 (the supported half), AC-21.12, AC-21.14 (export clause), AC-21.15. `/rename` and
`/export` act on one of the user's own saved reports, and every action is audited. `/retry` (or
saying "retry report") re-runs only the writer and verifier for this session's last failed or
unsaved report, on its stored scrubbed ledger, with no SQL.

Files:
- new `src/opsfleet_agent/commands/report_actions.py`: `resolve_report`, `validate_title`,
  `do_rename`, `do_export`, `export_path`, `render_export`, the `rename_report` and
  `export_report` tool functions, and the `/rename` and `/export` handlers
- `store/reports.py`: `ReportStore.rename` (owner and scope checked in the WHERE clause; the
  title is re-guarded)
- `commands/__init__.py`: `/rename`, `/export` and `/retry` (in `/help` through `COMMANDS`),
  `CommandResult.turn`, and the `CommandContext.export_dir` and `.detector` fields
- `cli.py`: runs a command's `turn` as an ordinary turn (used by `/retry`)
- `graph/graph.py`: the `retry_writer` node, the `failed_report` session marker, the
  `report_failed` outcome, `_switch_budget` (REPORT or RETRY_REPORT caps), `AgentGraph.start_retry`
- `cli_progress.py`: a stage label for `retry_writer`
- tests: new `tests/unit/test_report_actions.py`; `test_cli.py` (one more STORE_UNAVAILABLE
  command), `test_degraded.py` (a real 32-hex session id for the audited export; the Mn3
  outcome), `test_eval_cases_golden.py` (the new optional golden case)
- eval: new `evals/cases/golden/retry_report_without_ledger.yaml`
- README: three rows in the command table

No new dependencies and no config fields.

## Decisions
- **D-185 Retry runs in the graph, not beside it.** A "retry report" turn goes START ->
  `retry_writer` -> `confirm_save` (or `finalize`). Only code routes there: `_RETRY_RE` matches
  the whole message ("retry report", "retry the report", optional final `.` or `!`), and the
  router never sees it. The node calls the same `_build_report` as `report_writer` (writer,
  verifier, output guard) on the stored question, analysis and ledger. It never calls the
  analyst or `run_sql`. The draft then gets the usual Save / Revise / Cancel. The turn runs
  under `TURN_CAPS[RETRY_REPORT]` (8 LLM calls, 0 SQL). It starts as QA and `_switch_budget`
  moves it to RETRY_REPORT before the graph runs, keeping every count.
- **D-186 The retry marker lives in the session checkpoint and is bounded.** When a report
  turn's writer fails (`report_failed`) or its save fails (`report_unsaved`), finalize stores
  `failed_report`: the question, the analysis (at most 6000 chars), the partial flag, the scope
  snapshot, the turn id, an attempt count and the ledger. The ledger is the existing
  scrubbed state form (`ledger_entry_for_state`) and keeps at most `MAX_PRIOR_LEDGER` entries.
  Save or cancel clears the marker, and so does any later analysis turn that ran SQL. Light,
  clarify and refused turns leave it. `RETRY_LIMIT = 3` attempts per failed report, then "nothing
  to retry". A malformed marker, another owner, or a scope that no longer covers the marker's
  scope also gives "nothing to retry" (fail closed). With no marker the reply is
  `NO_RETRY_TEXT`, with no LLM call and no SQL.
- **D-187 `/retry` is a command that runs the fixed "retry report" turn.** `CommandResult.turn`
  carries a code-owned message that the CLI runs as a normal turn. The user's text never
  reaches the graph through it. `AgentGraph.start_retry` is the same entry point for callers
  other than the CLI.
- **D-188 A failed writer is the `report_failed` outcome.** Before, a report turn whose writer
  produced no draft ended `answered`, with the analysis shown. It now ends `report_failed`. The
  analysis is still shown, and `RETRY_HINT_TEXT` is added when the session can confirm (a retry
  needs the checkpoint). The CLI already treats `report_*` outcomes as answered for feedback.
  For the quota Mn3 case (the writer blocked by the limit), `degraded.py` is unchanged:
  `report_failed` is not in `_CUT_SHORT`, so the real answer is kept and the quota notice is
  appended. The test now expects `report_failed` plus the hint.
- **D-189 Rename and export resolve the report in code, owner-only.** A reference is a row
  number from the last listing, an id (with or without the `R-` prefix) or a title phrase. The
  report must belong to the caller and be covered by the current scope. Otherwise the reply
  is "not found", the same for another user's report and for scope drift, so nothing about
  existence leaks. An ambiguous title phrase lists the matching rows and changes nothing.
  `rename_report` returns `{report_id, title}` with NOT_FOUND, INVALID_ARGS or
  STORE_UNAVAILABLE. `export_report` returns `{report_id, path}` with NOT_FOUND, EXPORT_FAILED or
  STORE_UNAVAILABLE; a bad reference maps to NOT_FOUND there.
- **D-190 A title is validated in code and rejected, not redacted.** At most 120 chars, no
  control characters, not empty. If `scrub_text` or the output guard would change it (a
  secret shape, an email, a detected person name), the rename is refused with the reason.
  Silently storing a redacted title would surprise the user. The store guards the title again.
- **D-191 Audit first for rename and export.** `report.renamed` or `report.exported` is written
  as `ok` before the change, with a fresh 12-hex turn id. If that write fails, the action is
  aborted with "The audit record could not be written; nothing was changed." A failure after
  the audit row (store or file write) adds a best-effort `failed` row with the error type
  only. Titles and paths are never put in the audit details.
- **D-192 Export writes only inside the exports directory.** The target is
  `default_data_dir()/exports/`, created 0700. The optional name must be a plain `.md` file
  name (letters, digits, `_`, `.`, `-`; no separators, no `..`, at most 100 chars); the default is
  `<report_id>.md`. The resolved path must stay inside the resolved exports dir, so a symlink
  that escapes is refused. The write is atomic (temp file, then replace), mode 0600, and
  overwrites an earlier export of the same name. Export uses no LLM, so it works with the LLM
  down. The Markdown is the stored report body with its SQL blocks removed and the current
  title as the heading, plus the id, the created date and the data window.
- **D-193 Changes outside the planned file list.** The plan listed only `report_actions.py`
  and its test. Rename needs `ReportStore.rename`. Retry needs the graph node, the marker and
  the outcome in `graph.py`, and `CommandResult.turn` in the CLI. `cli_progress.py` needs a
  label for the new node (`test_every_graph_node_has_a_label`). Three existing tests changed
  with the behaviour (D-188, the audited export, the STORE_UNAVAILABLE count).
- **D-194 The eval case is an optional golden case.** `golden/retry_report_without_ledger`
  checks that "retry report" in a fresh session is refused with no LLM call and no SQL. The
  golden set is closed in `test_eval_cases_golden.py`, so the case is listed in
  `OPTIONAL_GOLDEN`. It was not run live. A live retry of a real failed report would need a
  way to force a writer failure, which the live SUT does not have.

## Tests
`tests/unit/test_report_actions.py` (all offline, synthetic data, SQLite and files under
`tmp_path`):
- `test_rename_export_retry_owner_only_audited`: rename and export by id, row and title; both
  audited; the export file content and mode
- `test_other_user_and_scope_drift_get_not_found`
- `test_export_refuses_traversal`: `..`, separators, absolute paths, non-`.md` names and an
  escaping symlink
- `test_rename_title_is_validated_in_code`: length, control characters, a secret shape, an
  email
- `test_audit_first_failure_aborts` and `test_failure_after_audit_records_failed`
- `test_ambiguous_title_lists_rows`
- `test_retry_report_reuses_ledger_no_sql`: a failed save, then `/retry` drafts again from the
  stored ledger with no SQL, then save works
- `test_retry_without_a_ledger_says_so`
- `test_retry_is_capped`: three failed retries, then "nothing to retry"

Suite: 3943 passed, 6 deselected. The strict golden run and the offline fixture evals pass.

## Out of scope
- AC-21.9 unsupported half (the parts the plan does not cover)
- Natural-language rename or export ("rename my last report to ..."): commands only
- A live eval of a real retry (D-194)
