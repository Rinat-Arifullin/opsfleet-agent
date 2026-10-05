# Iteration 46: Library agent role and node (ODs)

ADR-009 / HLD §4.0 and §4.2 (Library agent row), AC-21.10 (natural-language library turns).
Plan iteration 41 is already used (persona), so this one is numbered 46.

Before this iteration, a turn the router labelled `library` went to the Deep analyst (D-79),
which has `run_sql`. It now goes to a Library agent on flash-lite whose tool set holds the
report and preference tools only. A `report` turn still goes to the Deep analyst and then the
writer.

Files:
- new `src/opsfleet_agent/roles/library_agent.py`: `LIBRARY_TOOL_SPECS` (list, search, view,
  rename, export, delete_reports, set_preference), `make_library_executors`,
  `build_library_graph`, `run_library_agent`, the template texts. The same pattern as
  `roles/analyst.py`: a `call_model <-> run_tools` loop compiled with `checkpointer=False`,
  every model call through `LLMWrapper` (retries, the fallback model, the role sub-cap), the
  loop under `run_with_recursion_guard`, dispatch through the shared `_dispatch`
- new `prompts/library_agent.md` (`library-v1`)
- `graph/graph.py`: the `library` node, routing of the `library` label to it, `_after_library`
  (to `delete_preview` when the agent requested a delete, otherwise `finalize`), `library` in
  `_TEXT_ROUTES`, `GraphServices.audit` and `.export_dir`, `ctx.delete_turn`
- `cli.py`: passes the audit log into `GraphServices`
- `cli_progress.py`: a stage label for `library` and labels for the library tools
- tests: new `tests/unit/test_library_agent.py`; `test_graph.py` (the report/library routing
  test replaced); `test_eval_cases_golden.py` (the new optional golden case)
- eval: new `evals/cases/golden/library_agent.yaml`
- docs: `technical.md` (role table, §8 ADR drift, §9, D-79 row), `architecture.md` (the ADR
  drift pointer), README (the "Library agent" row of the built-vs-designed table)

No new dependencies and no config fields (`library_agent` was already in `config/models.yaml`).
No slash command changed, so `/help` is unchanged.

## Decisions
- **D-195 `set_preference` reuses the `/prefs` path.** The tool calls
  `graph.memory.set_preference` (enumerated values for format, depth and charts; notes through
  `sanitise_note`) and saves to the same per-user `PreferenceStore` as `/prefs`. A rejected
  change is a `PREFERENCE_REJECTED` envelope and nothing is stored. Routing is unchanged: a
  `memory` label still goes to the light path (D-180); only a message the router labels
  `library` reaches this tool. This is the smallest safe change: it adds no new route for
  preferences.
- **D-196 `GraphServices` carries `audit` and `export_dir`.** Rename and export through the
  agent call the iteration 33 tool functions, which write the audit record first (D-191). The
  graph therefore needs the audit log and the exports directory. When `audit` is `None` (tests
  that do not wire it), rename and export return `STORE_UNAVAILABLE` and change nothing.
- **D-197 An agent delete only produces the preview.** `delete_reports` runs
  `delete_flow.check_tool_request` (delete intent in the user's message, no tainted step after
  `view_report` or `search_reports`, no pending delete, a narrow selector) and also requires the
  selector words to come from the user's own message. It then hands the parsed request to the
  graph, which runs the existing `delete_preview` and pauses in `confirm_delete`. Only the
  user's next message can confirm. The agent has no path to `execute_delete`. A step that mixes
  `delete_reports` with any other call is refused as a whole (`gate_step`, `delete_not_alone`).
  A message that starts with a delete verb ("delete ...", "please delete ...") is still caught
  by the deterministic delete parser before the router and never reaches the agent.
- **D-198 Failure gives a template, never an analyst; no `save_report` tool.** If the primary
  and fallback models fail, or the loop ends without text, the turn answers
  `LIBRARY_UNAVAILABLE_TEXT` with no SQL. It never falls through to an analyst. The HLD lists
  `save_report(last_answer)` for this role; it is not exposed, because saving goes only through
  `confirm_save` (Save / Revise / Cancel), which already enforces the draft and ledger rules.

Enforced in code: the tool set is checked at import (a registry that gives the role
`run_sql`, `list_tables` or `get_schema` raises), `_dispatch` refuses any other tool name with
`TOOL_NOT_ALLOWED` and the output guard fails the turn closed; owner, scope, session and turn
are bound into the executors by code, never passed by the model.

## Tests
`tests/unit/test_library_agent.py` (mocked LLM, SQLite under `tmp_path`, synthetic data, no
network):
- `test_library_label_routes_to_library_agent`
- `test_library_tool_set_excludes_sql`
- `test_run_sql_is_not_bindable_by_library_agent`: `TOOL_NOT_ALLOWED`, no BigQuery call, the
  turn is not answered
- `test_list_and_view_via_tool_calls`
- `test_rename_and_export_via_tool_calls_are_audited`
- `test_owner_isolation`
- `test_delete_tool_only_previews_and_user_confirms`: the preview is shown, nothing is
  deleted, then "yes" deletes with no further model call
- `test_delete_tool_refusals`: no intent, a selector not in the user's words, tainted, and
  `delete_not_alone`
- `test_set_preference_uses_the_prefs_store_and_validation`
- `test_llm_failure_gives_template_never_analyst`

`tests/unit/test_graph.py`: `test_report_routes_to_deep_and_library_to_library_agent`
replaces `test_report_and_library_route_to_deep` (only `report` goes to deep now).

Eval: `golden/library_agent` (optional golden): "which of my saved reports are about
returns?" must list only the user's own matching report, with no SQL. It passes offline; it
was not run live.

## Out of scope
- `save_report` as an agent tool (D-198)
- Routing `memory` turns to the Library agent (D-180 kept, D-195)
- A live run of the eval case
