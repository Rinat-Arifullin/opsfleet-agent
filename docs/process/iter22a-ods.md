# Iteration 22a: two-phase delete (preview, token, confirm, execute). Implementer decisions

Status: implemented and awaiting the 🔴 owner gate. Nothing is committed. Round-2 review fixes
(MJ-1, mn-1..mn-6) applied. Owner decisions of 2026-10-05: OD-1, OD-10 (strict) and D-133 accepted;
OD-14 changed to re-ask.

## What landed

| Area | Files |
|---|---|
| Token and proof | `src/opsfleet_agent/delete/token.py` (DeleteKey, derive_token, make_proof, verify_proof) |
| Flow service | `src/opsfleet_agent/delete/flow.py` (parser, DeleteService preview/confirm/execute, render_prompt, gate_step, check_tool_request, setup_delete) |
| Graph | `src/opsfleet_agent/graph/graph.py` (nodes `delete_preview`, `confirm_delete` (interrupt), `execute_delete`; `AgentGraph.start_delete`, `_answer_delete`; crash resume in `PendingTurn.finish`) |
| Commands | `src/opsfleet_agent/commands/delete.py` (`/delete`), `commands/__init__.py` (`register_command`/`unregister_command`, `CommandContext.delete_start`) |
| CLI | `src/opsfleet_agent/cli.py` (`wire_delete`: registers `/delete` only when `setup_delete` succeeds) |
| Tests | `tests/unit/test_delete_flow.py` (75), `tests/unit/test_delete_token.py` (17), both counts including parametrized cases; `tests/unit/test_tracer.py` (forget_secret) |
| Tracer | `src/opsfleet_agent/obs/tracer.py` (public `forget_secret`) |
| Resume | `src/opsfleet_agent/graph/resume.py` (`close_interrupted_turn` closes a pending delete through `PendingTurn.close_delete`) |

No migration, config field, pyproject or lockfile change was needed. The service registers
`saved_report` as a deletable kind at runtime through `register_deletable`.

## Owner decisions (OD-n)

- **OD-1. The LLM never gets a delete tool.** *Owner-accepted 2026-10-05* (`/delete` or the phrase, always confirmed). `delete_reports` is not in `tools_for(...)` for any role.
  A delete starts only from the user's own text: either `/delete <selector>`, or a natural-language request
  matched by a deterministic parser. `gate_step` ("delete_not_alone") and `check_tool_request`
  (no_intent, tainted, delete_pending, SELECTOR_EMPTY) are implemented and tested at the service level,
  ready for the day a tool is exposed.
- **OD-2. The natural-language parser runs before the input guard.** It is deterministic: verb, object and
  selector. It never calls a model and puts no raw text into graph state; the preview is rendered by code
  from store rows. A false positive only produces a preview, which any non-"yes" reply cancels.
  The grammar was tightened in the review round (see OD-13).
- **OD-3. The turn counter and K_delete live only in memory.** After a restart, a pending delete fails
  closed: the token no longer matches, so the result is `key_changed` and EXPIRED, and crash resume
  reports that it expired.
- **OD-4. REPL commands do not count as turns (m5).** `/help`, `/reports` or `/report open` between
  the preview and "yes" do not advance the turn counter, so they do not break next-turn binding
  (AC-12.7 counts user chat turns only). A slash command never resumes the graph, so it cannot
  confirm or cancel. `/delete` is dispatched through `commands.dispatch`, not the `guarded()`
  Ctrl-C wrapper.
- **OD-5. Superseded by OD-11.** (The proof used to be in the resume value. It was single use,
  and a reuse after any terminal audit row is still cancelled as `replayed` and audited.)
- **OD-6. Replays are detected from audit rows** (EXECUTED, CANCELLED or EXPIRED for the same
  pending_action_id). No separate "used" table is kept.
- **OD-7. The COMMANDS table is now mutable** through `register_command`/`unregister_command`, so the
  feature-off path leaves `/delete` unregistered.
- **OD-8. Deletes by session or by ID can include reports that drifted out of scope.** They are still
  owned by the user; their titles are masked in the preview as "(created under a different product scope)".
- **OD-9. Superseded by OD-12.**
- **OD-10. Execute re-verifies, and runs only right after confirm (M1).** *Owner-accepted 2026-10-05
  (strict).* `execute_delete` checks the steps below in order:
  1. the CONFIRMED row is present, otherwise `not_confirmed` (unsafe, no new row);
  2. the binding holds (owner, confirmed set, ids_sha256), otherwise `binding_mismatch` (unsafe, no new row);
  3. an EXECUTED row for the action already exists: report its recorded count (no new row);
  4. re-derive the token and compare it with `token_sha256`; a mismatch closes as `key_changed`
     (a restart means a new K_delete) and writes `delete.expired`;
  5. the in-memory confirm time is present, otherwise `confirm_lost`, which writes `delete.expired`;
  6. the clock is within `EXECUTE_GRACE_S` = 60 s of that confirm, otherwise `timeout`, which writes
     `delete.expired`;
  7. owner re-check on the confirmed ids (`present`: still existing and still owned by the user).
     If any confirmed id is missing, the result is `set_changed`, which writes `delete.expired` and
     deletes nothing (OD-14). Otherwise one EXECUTED row records the whole confirmed set.

  So three steps write no row, four write `delete.expired`, and only a fully intact confirmed set
  writes `delete.executed`. Steps 4 to 7 delete nothing when they fail. The confirm time is kept in memory rather than parsed from
  the CONFIRMED row, because the audit log keeps its own clock.
  - **Interrupts and strands (MJ-1).** A Ctrl-C at the confirm or the execute stage is a cancel:
    `close_interrupted_turn` calls `PendingTurn.close_delete("interrupted")`, which drops the held
    proof and confirm time, writes `delete.cancelled` with reason `interrupted`, deletes nothing and
    closes the turn. It also recognises the turn that answered "yes" (confirm_delete stores that
    `turn_id`; the graph keeps an in-memory reply-turn map), and ignores any other turn id. If an
    EXECUTED row already exists, it reports the recorded count instead and writes no cancel row.
  - A stranded execute (a crash between the two nodes) runs only on an explicit resume
    (`PendingTurn.finish`), and then only if steps 1 to 6 pass. The next unrelated chat turn never
    runs it: `_close_stranded` closes it as `delete.expired` with reason `stranded`, tells the user
    ("A confirmed delete was interrupted before it ran, so nothing was deleted. ...") and then
    answers the turn.
  - **One terminal row per action (round 3).** `abandon`, `_confirm` and `_execute` (before the key
    check) look up any EXECUTED, CANCELLED or EXPIRED row first and report it without writing a
    new one. So a Ctrl-C close whose checkpoint write failed is not followed by a second
    `stranded` or `confirm_lost` row on the next turn or on `--resume`.
  - **A lapsed confirm stage on `--resume` (round 3).** `PendingTurn.finish` at confirm_delete
    with a changed key (restart) or an expired preview closes through `close_delete`: it writes
    `delete.expired` with reason `key_changed` or `timeout` and closes the turn, so the next delete
    request gets a fresh preview.
  - **AC-12.13 vs AC-12.15:** a rerun stays idempotent, and is retried only by an explicit resume
    inside the same live process and the grace window. After a restart or the window, the
    confirmation lapses, which satisfies AC-12.15's "no delete without a live confirmation".
  - Tests: test_restart_between_confirm_and_execute_deletes_nothing (the repro),
    test_restart_after_delete_committed_reports_recorded_count,
    test_stranded_execute_not_run_by_next_turn,
    test_stranded_execute_explicit_resume_in_grace_executes,
    test_stranded_execute_after_restart_closed_then_turn_answered,
    test_execute_after_grace_window_expires,
    test_ctrl_c_after_yes_cancels_and_deletes_nothing[execute, confirm],
    test_ctrl_c_after_commit_reports_recorded_count, test_ctrl_c_close_ignores_another_turn_id,
    test_resume_confirm_stage_lapsed_expires_then_fresh_preview[key_changed, timeout],
    test_close_state_write_fails_keeps_one_terminal_row[next_turn, finish].
- **OD-11. The proof stays in process memory (m1).** The resume value carries `reply`,
  `pending_action_id` and `proof_sha256`. The service holds the proof itself and compares both the
  hash and the HMAC in constant time. A missing held proof (evicted, or another process) is
  cancelled as `proof_mismatch`. test_checkpoint_holds_token_hash_only asserts that neither the
  token nor the proof (nor the key) appears in any checkpoint, `saver.storage`, `saver.writes` or
  their pickled bytes.
- **OD-12. Scrubber registration happens once per pending action, and is bounded (m4).** The token
  and the proof are registered on first derive per `pending_action_id`. The service keeps the newest
  `HELD_MAX` = 256 actions (an LRU); an evicted action's secrets are forgotten, and a later confirm
  of it fails closed. Forgetting goes through the public `tracer.forget_secret(value)` (mn-6);
  `token.py` no longer touches `tracer._secrets`. Tests:
  test_secrets_registered_once_per_action_and_bounded,
  test_forget_secret_stops_scrubbing_only_that_value.
- **OD-13. Natural-language grammar (M2).** The request must start with the verb (an optional
  "please" is allowed first), and the verb's direct object must be one of:
  - `[all] [my|the|these|those|our] [saved] report(s)`
  - `[the] this/current session['s] [saved] report(s)`
  - `[report(s)] <32-hex id>`

  So "remove the cancelled orders from the revenue report", "delete returned items in the report",
  "delete the outliers in report 2 and redo the chart" and similar requests are analysis, not a
  delete (test_nl_analysis_requests_are_not_deletes, 7 phrases). Positives:
  test_nl_report_deletes_still_parse plus the flow tests. A request that is not anchored at the
  start, such as "could you delete my reports about X", now needs `/delete`.

  Round-2 tightening:
  - mn-2: the report object must end the request or be followed by punctuation, a selector word
    (about, titled, named, called, from, with, on, matching, containing, created, that, which,
    "in this session"), a negation word, a 32-hex id or a wildcard. So "delete the report header",
    "remove the report's footnotes", "delete report-level totals" and "delete report sections with
    no data" are analysis. Trade-off: a bare noun after the object ("delete reports quarterly
    widgets") is also analysis and needs "about"/"titled" or `/delete`.
  - mn-1: a negated or exclusive selector (not, except, excluding, other than, but, without,
    besides, apart from, n't) is refused as an empty selector rather than inverted
    (test_nl_negated_selector_refused).
  - mn-3: hex ids are matched case-insensitively and stored lowercase
    (test_uppercase_report_id_recognised_and_stored_lowercase).
  - mn-4: Unicode separators (category Z*: NBSP, U+2028, U+2029, U+3000 and others) become a space
    before NFKC (test_fold_maps_unicode_separators_to_space).
- **OD-14. A changed set is re-asked, never partly deleted.** *Owner decision 2026-10-05: re-ask.*
  If any previewed report vanished or was re-owned between confirm and execute, nothing is deleted
  (not even the rest), `delete.expired` is written with reason `set_changed`, and the reply names
  the vanished ids (ids only; the rows and their titles are gone), says nothing was deleted, and,
  when other reports still match, asks the user to ask again for a fresh preview. The fresh preview
  is not rendered inside execute: re-asking goes through the normal preview, so it gets a new
  pending action, a new token and a new confirmation. The old confirmation is single use and never
  runs again. The former "Deleted N reports. M previewed reports no longer existed." path and its
  test are gone. A tampered `report_ids` after confirm is still `binding_mismatch`
  (test_execute_rejects_report_ids_changed_after_confirm[subset, superset]).
  Tests: test_execute_set_changed_deletes_nothing_and_reasks[reowned, deleted],
  test_set_changed_through_graph_then_fresh_preview, test_set_changed_text_when_nothing_left.
  Mutation check: removing the execute binding check (`exec_bind`) or the `set_changed` guard makes
  these tests fail.

## Deviations and residual risks

- Ctrl-C during a pending delete now writes `delete.cancelled` (reason `interrupted`) and deletes
  nothing (MJ-1, OD-10). Residual: a Ctrl-C after the delete committed but before the checkpoint
  write closes the turn with the recorded count and writes no cancel row (correct audit), but the CLI
  still prints its generic "Cancelled." line for that turn. `cli.py` was left unchanged; the next
  `/reports` shows the true state.
- A stranded execute is expired by the next unrelated turn and is executed only by an explicit
  resume within the grace window (MJ-1).
- Matching scans at most the owner's 200 newest rows (`owner_rows`; D-133, owner-accepted
  2026-10-05). More rows than that set the truncated flag, which is surfaced in the preview.
- Taint (the same turn as view_report or open_report) is tested at the service level only, because
  no LLM tool path exists (OD-1).
- Fixed the pre-existing flake in `tests/unit/test_library.py::test_view_report_scope_drift`. The
  test now asserts the scrubbed form of the id, because a random hex id with a long digit run is
  masked as `<ID>`.
- Everything in 22b (retention and purge) is out of scope.

## AC coverage

| AC | Tests |
|---|---|
| AC-12.1 | test_delete_requires_confirm |
| AC-12.2 | test_delete_confirm_deletes_exact_previewed_set |
| AC-12.3 | test_delete_cancel_on_non_confirm, test_cancelled_delete_reply_gets_fresh_context |
| AC-12.4 | test_delete_owner_only |
| AC-12.5 | test_delete_by_session_id |
| AC-12.6 | test_delete_confirm_deletes_exact_previewed_set (report created later), test_delete_expired_deletes_nothing, test_delete_confirmation_bound_to_preview_set, test_execute_rejects_report_ids_changed_after_confirm, test_execute_set_changed_deletes_nothing_and_reasks, test_set_changed_through_graph_then_fresh_preview (OD-14 re-ask: a changed set deletes nothing and needs a fresh preview) |
| AC-12.7 | test_llm_cannot_trigger_delete_without_user_turn, test_confirm_only_on_next_turn |
| AC-12.8 | test_delete_no_matches |
| AC-12.9 | test_delete_i_already_confirm_still_previews |
| AC-12.11 | test_delete_preview_includes_backup_notice |
| AC-12.12 | test_delete_refused_after_view_same_turn, test_delete_requires_intent_in_user_message (service level, see OD-1) |
| AC-12.13 | test_confirm_delete_rerun_keeps_token, test_previewed_audit_idempotent_on_replay, test_stranded_execute_explicit_resume_in_grace_executes, test_restart_after_delete_committed_reports_recorded_count (OD-10) |
| AC-12.14 | test_matcher_rejects_empty_and_wildcards |
| AC-21.6 | test_session_delete_excludes_viewed_reports |
| AC-21.7 | test_large_delete_requires_typed_count, test_large_delete_preview_truncated_but_bound |
| AC-28.1 | audit-row assertions across the flow tests (PREVIEWED, CONFIRMED, EXECUTED, CANCELLED, EXPIRED) |
| AC-28.2 | test_flow_audit_failure_aborts_delete |
| K_delete | test_delete_token_derived_not_stored, test_delete_token_never_in_traces, test_checkpoint_holds_token_hash_only, test_proof_mismatch_cancels[replayed], tests/unit/test_tracer.py:108 |

Structure: test_confirm_prompt_rendered_by_code, test_delete_not_alone_in_step,
test_execute_delete_requires_confirmed_record, test_second_delete_while_pending_rejected,
test_confirm_delete_never_deletes. Rollback: test_wire_delete_fails_closed,
test_slash_delete_command_previews_and_feature_off, test_delete_without_service_is_unavailable.
