# Iteration 24: fallback messages, degraded mode, quotas (ODs, rev. 3 after review 2)

Files: `graph/degraded.py`, `store/quota.py`, `tests/unit/test_degraded.py`, `cli.py`
(`build_runtime`, a few lines, `pragma: no cover`), `graph/resume.py` (about 8 lines, see OD-6).
`graph.py`: 0 lines changed. Also fixed a pre-existing flake in `tests/unit/test_run_sql.py`
(`"300" not in raw` matched float timestamps; now the trace is parsed and values are compared).

## Decisions
- OD-1 Wrapper, not a graph edit. `DegradedGraph` has the same `run_turn` signature as `AgentGraph`
  and exposes `.inner`. A turn in which at least one provider call failed and none succeeded gets
  the AC-15.2 text and a notice; checkpointed state is untouched. A draft status line the graph
  already produced (`NOT_SAVED_TEXT`, `REVISING_TEXT`) is kept before the unavailable text.
- OD-2 Per-call gate, not per-turn. `LLMHealth.wrap` checks `QuotaStore.check` before EVERY
  provider call and raises `QuotaExceeded` (a `NonRetryableLLMError`: no retry, no fallback).
  The same wrapped invokes serve resume, report writer, verifier and retries, so all are gated.
  Each attempt is counted at attempt time (so Ctrl-C or an exception cannot erase it). At most
  `llm_per_hour` provider calls happen per hour. A turn cut short by quota ends as the quota text
  (`outcome="refused"`) only when the graph had nothing but its generic failure text
  (`ERROR_TEXT`, the force-answer `UNAVAILABLE_TEXT`, the partial-answer message). If it still
  produced a real answer (writer blocked: the analysis answer is shown) or a draft (verifier
  blocked: an unverified draft), the reply and outcome are kept and the notice carries the quota
  text, a "cut short, may be incomplete or unchecked" line and `QUOTA_NOTICE` (review 2 Mn3, Mn4).
  Deterministic turns (save, cancel, delete-while-pending refusal) make no
  provider call that matters, keep their own outcome and work at quota. Commands never reach the
  wrapper: /reports, /search, /open work at quota and with the LLM down.
- OD-3 Defaults: 300 LLM calls/hour, 2000/day, 100 GB BigQuery bytes/day per user, module
  constants in `store/quota.py`; fixed UTC windows; injectable clock.
- OD-4 Table `user_quota` is created idempotently by `QuotaStore.ensure_schema` (like
  `store/feedback.py`). The window column is named `win` (not the SQL keyword `window`), so no
  quoting is needed. `QUOTA_MIGRATION` is ready to fold into a numbered migration.
- OD-5 Bytes: before each turn `DegradedGraph.prepare` caps the session `SessionByteBudget.cap` at
  `used + remaining daily bytes` (the budget already refuses queries over its cap, before the
  query runs), reaching it through the guarded private `AgentGraph._sql_session`. Bytes are
  recorded in `finally` (the delta of `used`), so Ctrl-C and exceptions keep them. If the hook is
  missing the wrapper logs and carries on; it never raises.
- OD-6 Resume: `resume_turn` keeps the `DegradedGraph` it was given, opens the checkpoint on
  `.inner` and finishes the turn with `DegradedGraph.finish(pending, session)`: the same per-turn
  reset (no stale `quota_reason`), byte cap, bytes recorded in `finally` and reply degradation as
  `run_turn` (review 2 Mn1, Mn2). A plain `AgentGraph` still calls `pending.finish()`.
  `close_interrupted_turn` calls `unwrap(agent)`. `unwrap(agent, session)` stays as a cap-only
  helper.
- OD-7 Revise at quota (review 2 Mn5). The graph closes the pending draft (`resume("revise")`)
  before the revise makes its first provider call. So `DegradedGraph.run_turn` checks quota
  BEFORE the graph runs for a plain revise reply to the user's own pending draft: when the user
  is already over quota the turn is refused (`QUOTA_TEXT`, notice "draft still pending, reply
  save"), the graph is not invoked and the draft stays pending and savable. A delete request is
  left to the graph (its own refusal). **Owner decision:** a quota hit AFTER the revise started
  (some calls left, not enough) still drops the old draft, the same as an outage mid-revise;
  keeping it needs a `graph.py` change (close the old draft only once the new one exists). Pinned
  by `test_revise_cut_short_mid_way_drops_draft_mn5_pinned`.

## Deviations
- AC-21.14 export is NOT implemented (iteration 33 has not shipped; `/export` is a stub, M-3).
  Listing, viewing and search under LLM-down are implemented and tested, with the degraded
  notice (`DEGRADED_NOTICE`) on the failing turn.
- (Resolved) Limits are now in Settings (D-138, below).
- AC-22.8 "export still works" is likewise deferred with export.

## Hot-file changes (owner-accepted D-137..D-139, APPLIED in this worktree)
- D-137 (resume.py wiring): `resume_turn` unwraps the wrapper to open the checkpoint and finishes
  through `DegradedGraph.finish`; `close_interrupted_turn` unwraps. About 8 lines in `resume.py`.
- D-138 (config): `Settings.quota_llm_per_hour=300`, `quota_llm_per_day=2000`,
  `quota_bq_bytes_per_day=100_000_000_000`, read by `config.parse_quota` from an optional
  `quota:` mapping in `config/models.yaml` (the section is added with these defaults). Values must
  be positive ints (bool rejected); an unknown key or a non-mapping raises `ConfigError`.
  `cli.build_runtime` passes `QuotaLimits(...)` from Settings to `QuotaStore`. Tests in
  `tests/unit/test_config.py` (`test_quota_*`).
- D-139 (db): migration `(4, QUOTA_MIGRATION)` in `store/db.py` (4 was the next free number on
  `step5-implementation`, which has 22a). The DDL moved to the dependency-free
  `store/quota_schema.py`, imported by `db.py` and `quota.py` (no cycle). `QuotaStore.ensure_schema`
  stays as the idempotent fallback. Tests in `tests/unit/test_store_db.py`
  (`test_quota_migration_*`: fresh DB, and a DB where `ensure_schema` made the table first).

## Rebase onto 22a (integration decisions)
- There is no local `main` branch; the base was `step5-implementation` (contains 22a, cb593d2).
  The three modified files (cli.py, resume.py, test_run_sql.py) were re-applied as a 3-way patch
  limited to those paths; the new files were kept as they were. The only conflict was the
  `resume.py` import block (merged by hand).
- No blanket `__getattr__`. Everything the CLI reaches on the runtime graph is explicit on
  `DegradedGraph`: `prepare`, `run_turn`, `finish`, plus the 22a `start_delete` (a `/delete`
  preview, same byte cap and accounting as a turn) and `delete_reply_turn` (pure delegation).
  Anything else goes through `unwrap()`.
- OD-8 Delete x quota x degraded mode:
  1. A typed delete request ("delete reports about ...") is parsed by regex in graph.py `_run`
     and goes from START straight to `delete_preview`: no router call, so no gate and no count
     (an exhausted quota does not refuse it; `test_typed_preview_at_quota_is_not_gated`).
     `/delete <selector>` and the confirm/cancel/execute steps are deterministic code too: the
     LLM quota never blocks or counts them, and an outage cannot change their text.
  2. Confirming ("yes") a pending delete works with an exhausted LLM quota (tested). Cancel and
     expiry write their `delete.cancelled` / `delete.expired` rows in the graph and service, never
     in the wrapper, so degraded mode cannot suppress them.
  3. A non-confirm reply cancels the delete and is then a normal gated turn. If that turn is
     cut short (quota or outage), `DegradedGraph._degrade` keeps the "nothing was deleted" text
     in front of the quota or outage message. The head is matched by prefix against the
     delete-flow texts (cancelled, expired, unsafe, pending, stranded), so it survives whatever
     follows it, including a clarification reply (`_closed_delete_head`).
  4. Ctrl-C after "yes": `close_interrupted_turn` unwraps and closes the delete as CANCELLED
     (`interrupted`), independent of quota. `--resume` of a pending delete goes through
     `DegradedGraph.finish` (capped, bytes recorded) but is not LLM-gated, since a delete resume
     makes no LLM call.
  Tests: `tests/unit/test_degraded_delete.py`.
- OD-9 A failing quota check fails closed, visibly: `LLMHealth._gate` catches `StoreError` /
  `sqlite3.Error` from `quota.check`, logs only the exception type name, sets the quota reason
  `check_failed` and raises `QuotaExceeded`, so the turn is refused with
  `QUOTA_CHECK_FAILED_TEXT` ("Usage limits could not be checked, so I didn't run this. Please
  try again.") and is not counted as a provider failure. Only gated LLM calls reach `_gate`, so
  confirm, cancel, execute and `/delete` are unaffected (same as OD-8).
  Test: `test_quota_check_failure_fails_closed_but_not_delete_steps`.

## Residual risks
- Known limit, cross-process check/record race (review 2 m2): `check` and `record_calls` are
  separate statements, not one transaction. Two processes for the same user on one data dir (two
  terminals) can each pass the check for the last call, so each may exceed a limit by one call.
  The hourly/daily LLM counters can end above the limit by the number of processes making a call at that moment (at most one call each, since
  every call is checked). Bytes behave the same: each process caps its session at the remaining
  bytes it read before the turn, so together they can overshoot by up to one turn's bytes per
  process. Within one process there is no race (calls are sequential). Accepted for a local
  CLI. Production: an atomic reserve (`UPDATE ... SET used = used + n WHERE used + n <= limit`,
  check the row count) in a shared store, then a refund of unused reservation.
- Bytes are recorded after the turn (including a `--resume` turn, OD-6), so the daily counter
  lags by the in-flight turn; within a process the session cap bounds it.
- Quotas are local to one data dir; they do not hold across machines.
- `DegradedGraph` reads the private `AgentGraph._sql_session`; a graph.py rename disables the
  byte cap and bytes accounting (logged, never raised); `test_bytes_cap_follows_remaining_daily_budget`
  fails on a rename. Production: a public budget accessor on `AgentGraph`.
- A turn where the router is blocked but a later step succeeds keeps the graph's fail-open route;
  a turn where only some calls fail keeps the graph's own `UNAVAILABLE_TEXT`, not the AC-15.2 text.

## AC coverage
- AC-15.1: model fallback pre-existing in `LLMWrapper`; exercised by the outage tests.
- AC-15.2: `test_all_models_down_graceful`, `test_draft_status_prefix_kept_when_llm_down_m3`.
- AC-21.14: `test_degraded_mode_lists_and_searches_reports_when_llm_down` (same store as the
  failing graph, notice asserted); export not implemented.
- AC-22.8: `test_quota_blocks_after_limit`, `test_quota_gates_each_call_not_each_turn_m1`,
  `test_save_cancel_and_delete_refusal_work_at_quota_m1`, `test_ctrl_c_mid_turn_keeps_accounting_m2`,
  `test_bytes_cap_follows_remaining_daily_budget`, `test_quota_store_bytes_cap`,
  `test_quota_keeps_real_answer_when_writer_blocked_mn3`, `test_quota_notice_on_unverified_draft_mn4`,
  `test_revise_at_quota_keeps_draft_pending_mn5`, `test_revise_cut_short_mid_way_drops_draft_mn5_pinned`.
- Resume/continuity: `test_resume_works_through_the_wrapper_b1`,
  `test_close_interrupted_turn_works_through_the_wrapper_b1`,
  `test_outage_then_recovery_keeps_session_m4`, `test_pending_draft_survives_outage_and_saves_m4`,
  `test_wrapper_never_raises_when_budget_hook_missing`, `test_resume_charges_bytes_to_daily_quota_mn1`,
  `test_quota_hit_during_resume_shows_quota_text_mn2`, `test_resume_does_not_leak_stale_quota_reason_mn2`,
  `test_resume_with_plain_agent_graph_still_works`.
- Delete through the wrapper (OD-8): `test_delete_preview_confirm_execute_through_wrapper`,
  `test_slash_delete_through_wrapper_has_no_llm_call`, `test_confirm_not_blocked_by_exhausted_llm_quota`,
  `test_typed_preview_at_quota_is_not_gated`, `test_cancel_text_kept_when_outage_reply_is_clarification`,
  `test_closed_delete_head_matches_any_delete_text_by_prefix`, `test_quota_check_failure_fails_closed_but_not_delete_steps`,
  `test_cancel_at_quota_writes_audit_and_keeps_text`, `test_cancel_during_outage_writes_audit_and_keeps_text`,
  `test_ctrl_c_after_yes_with_wrapper`, `test_resume_pending_delete_through_wrapper`,
  `test_resume_stranded_execute_through_wrapper`.
