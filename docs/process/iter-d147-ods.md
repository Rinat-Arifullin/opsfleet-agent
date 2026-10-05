# D-147: CLI stage spinner (ODs)

Files: new `cli_progress.py` (Spinner, label table), new `obs/progress.py` (event seam),
`cli.py` (`_Repl.guarded`, about 10 lines), `graph/graph.py` (about 8 lines: one
`progress.report(name)` at node entry in `_safe`, one per requested tool in `on_tool_name`),
new `tests/unit/test_cli_progress.py`. No hot files, no new dependencies.

## Decisions
- OD-1 Global hook, not a contextvar. LangGraph may run nodes on worker threads, where a
  contextvar set in the CLI thread is not guaranteed to be visible. One REPL runs one turn at
  a time, so a single module-level hook (lock-guarded, restored in `finally`) is enough. If
  turns ever run concurrently in one process (server mode), switch to a per-turn callback in
  `GraphServices`.
- OD-2 Events are node names and `tool:<name>`; the CLI maps them to fixed labels. An unknown
  event (for example a tool name the model made up) keeps the current label, so model text is
  never shown. Wording is ours, not specified: `light` and `force_answer` show "Writing the
  answer…", `grounding` shows "Checking the numbers…", `finalize` shows "Checking the answer…".
  Owner may want different wording.
- OD-3 `/delete` start (`commands.dispatch` → `_delete_start`) is not wrapped: it is local and
  fast and runs outside `guarded`. The delete confirmation reply is a normal turn and does show
  the spinner, and so does `--resume`.
- OD-4 Log lines on stderr (warnings) can appear on the spinner line in a terminal, because
  stdout and stderr share it. The next frame redraws the line from column 0, and stop() erases
  it, so stdout stays clean. Not fixed: that would mean routing logging through the spinner.
- OD-5 The first frame is drawn after one interval (0.1 s), so fast turns (commands, refusals)
  draw nothing and do not flicker. `TERM=dumb` counts as non-TTY. Labels are cut to the
  terminal width so the line never wraps (a wrapped line cannot be erased with `\r`).
- OD-6 Pre-existing, not touched: `ruff format --check src/opsfleet_agent/graph/graph.py`
  reports two lambdas at about lines 1270-1282 (the iteration 22a delete edges) as unformatted.
  CI runs `ruff check` only, so nothing fails.
