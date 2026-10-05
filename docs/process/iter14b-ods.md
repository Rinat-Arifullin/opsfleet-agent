# Iteration 14b: owner decisions (round 2 update)

## OD-1 (REVISED, replaces the round 1 "async durability" OD, which was rejected)
The parent turn graph runs with `durability="sync"` (`AgentGraph._durability()`, used by
`run_turn` and `PendingTurn.finish`; skipped when there is no checkpointer, where LangGraph
ignores it and warns). So each node's checkpoint is on disk before the next node starts. A hard
kill (os._exit, SIGKILL, power loss) loses at most the node that is running.
The role subgraphs (compiled with `checkpointer=False`) pass an explicit `durability="async"`.
Without it they inherit the parent's "sync" and fail in LangGraph 1.2.12
(`_put_checkpoint_fut`). LangGraph's "durability has no effect" UserWarning for that call is
silenced in analyst.py at import, filtered on that exact message.
Cost: one synchronous SQLite write per parent node, about 7 per turn. That is negligible next
to LLM and BigQuery latency.
Evidence: `test_resume_after_real_process_kill[deep|force_answer|grounding]` runs a child
process (sys.executable, synthetic key, tmp sqlite, fakes) that calls os._exit(9) inside the
node. The parent resumes at that node, runs no SQL, and does not re-run the router. A mutation
check with "async" on the parent fails all 3: the checkpoint is lost back to quick, or the turn
is lost entirely.
The round 1 tests missed this because a raised BaseException lets LangGraph flush pending
writes, and os._exit does not.

## OD-2 (unchanged, now tested)
Resume is bounded, not exact. A kill inside a node re-runs that node from its start, so its
LLM calls and queries can repeat once. Test: `test_resume_crash_inside_node_repeats_at_most_once`
(exactly one repeated query, then the turn finishes). A kill between nodes repeats nothing.

## OD-3 (new): owner binding
The state stores `owner` (the profile user_id) next to `scope_snapshot`, written by run_turn.
resume_turn compares it first and refuses (NEW_SESSION, OWNER_TEXT) when it differs or is
missing, so a missing owner counts as drift. Checkpoints written before this change have no
owner, so they start a new session (fail closed); there are no such checkpoints in production.
OWNER_TEXT does not say that another user owns the session.

## OD-4 (new): refusal texts and error classes
STORE_REFUSED is now one fixed text and never echoes exception text. KEY_REFUSED covers only:
- a ConfigError naming LANGGRAPH_AES_KEY (missing or invalid length);
- a ValueError whose message contains "MAC check failed" (pycryptodome AES-EAX, wrong key).

Any other ValueError from reading the checkpoint is STORE_REFUSED. Matching on the message is
tied to pycryptodome's wording. If that wording changes, a wrong key is reported as
STORE_REFUSED, which still refuses (fail closed). Test:
`test_resume_non_mac_value_error_is_store_refused`.

## OD-5 (new): restored-state validation scope
- Ledger entries must have exactly the keys run_sql writes: sql, query_id, sql_hash and
  executed_sql_hash as str, purpose as str or None, rows as a non-negative int (not bool).
  At most 64 entries. Otherwise the budget is exhausted and gave_up is set.
- A light or refuse route resumed with an empty final_text ends as outcome "error" with
  ERROR_TEXT. It never counts as "answered".
- Accepted risk: a well-formed but zeroed budget in decrypted state is accepted, because
  restore never lowers counters below the fresh context's (zero), and forging it requires the
  AES key. The same applies to forged seen/statements maps: anyone with the key can already
  write arbitrary state.

## OD-6 (round 3): local warning suppression, child import path, empty light reply

- **Warning scope.** The module-level `warnings.filterwarnings` in roles/analyst.py is gone. A new `_invoke_subgraph()` wraps only the role-subgraph `invoke(..., durability="async")` in `warnings.catch_warnings()` and ignores only LangGraph's "`durability` has no effect when no checkpointer" UserWarning. Process-wide filters are untouched, and the internal `__pregel_durability` config key is not used. Trade-off: `catch_warnings` is not thread-safe (it swaps global filter state). That is acceptable today because the CLI runs one turn at a time on one thread. A concurrent server deployment should revisit it, for example by passing durability through config once LangGraph exposes a public way to do so.
- **Guard test.** test_resume.py sets `pytestmark = filterwarnings("error::UserWarning")`, so every resume and graph path in that file fails if any UserWarning escapes a turn. Mutation check: removing the local filter fails 31 tests, including the real-kill stderr checks. `pytest -W error::UserWarning` over test_resume, test_graph* and test_input_guard_router passes (578).
- **Child import path.** The real-kill child gets `PYTHONPATH = src + parent sys.path`, so it imports exactly the code the parent imports.
- **Empty light reply.** When check_output allows a smalltalk reply but strips it to nothing (for example `<b></b>` or a lone ZWSP), run_light_path returns the label's template with source "template". The turn ends "answered", and the tracer's turn span records that outcome. A blocked (unexpected action) reply is unchanged. The `_finalize` "error" outcome for an empty light or refuse text stays as a backstop for resumed state.
