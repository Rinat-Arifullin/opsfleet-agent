# Iteration 40: optional Langfuse observability (ODs)

Files: new `infra/langfuse/` (pinned self-host compose, `.env.example` with placeholders,
README), new `obs/langfuse_sink.py`, `obs/tracer.py` (an `extra_sink` hook, about 6 lines),
`cli.py` (sink build, invoke wrapping, per-turn wrap, bounded flush on exit, secret
registration), `commands/__init__.py` (`CommandContext.langfuse`, `/trace` prints the
Langfuse trace id), root README section, new `tests/unit/test_langfuse_sink.py`. No hot files,
no new dependencies (`langfuse` 4.16 is already locked), no config fields.

## Design
- One Langfuse trace per user turn, named `turn`. `session_id` is the CLI session, `user_id` is
  the profile id; both are set through `propagate_attributes`, so the **Sessions** and
  **Users** views group turns. Trace metadata: `turn_id`, `outcome`, `label`, `route`,
  `provider` (`gemini` or `lmstudio`).
- Two feeds, buffered per turn and emitted once at the end of the turn:
  1. every span the existing `Tracer` records, through the new `Tracer.extra_sink` hook. The
     hook receives the span after the allowlist, `sanitize_sql` and `drop_sensitive`, so the
     sink never sees more than the JSONL trace file does. A sink error is swallowed in
     `Tracer.record`.
  2. every LLM attempt, by wrapping the router invoke (`llm.chat`) and the tool-calling invoke
     (`llm.tools`) outside `health.wrap`, so each retry and fallback attempt is its own
     generation.
- Tree:

  ```
  turn (agent)                     input = question, output = answer
  ├─ guard:input (guardrail)       output = {verdict, rule, rule_hits, ...}
  ├─ router:router (agent)         metadata = label, route, model, escalated, ...
  │   └─ llm.chat (generation)     model, masked messages, output label, usage, latency
  ├─ role:analyst_quick (agent)    metadata = model, prompt_version, llm_calls, status
  │   ├─ llm.tools (generation)    attempt 1, level ERROR, status_message = TimeoutError
  │   ├─ llm.tools (generation)    attempt 2, output = {text, tool_calls}, usage
  │   ├─ sql:run_sql (tool)        sanitized SQL, rows, bytes billed, cache hit
  │   └─ tool:run_sql (tool)       outcome, rows count
  ├─ guard:grounding (guardrail)   verdict, grounding_flags
  ├─ guard:output (guardrail)      verdict, rule_hits, redaction_count
  └─ turn_summary (span)           outcome, path, label, LLM call and SQL query totals, persona
  ```

  A `router` or `role` span adopts the generations and tool/SQL spans recorded since the
  previous container (the Tracer writes the role span when the role ends). Its start time is
  moved back to its first child. LLM calls outside a role (light reply, report writer,
  verifier, forced answer) and every other span hang directly off `turn`. Generation latency
  and token usage come from the wrapped call, so the Langfuse cost/latency views work.
- Real start times use the SDK's OTel tracer (`_otel_tracer.start_span(start_time=...)`), a
  private API. If that API changes, it falls back to the public `start_observation`, and the
  only loss is accurate start times.
- Outcome: the turn result's `outcome`, `cancelled` on Ctrl-C, `error` on an exception (root
  level ERROR). The exception is always re-raised unchanged.

## What is sent, and what is not
PII is enforced in our code before the SDK sees anything, in two layers:
1. `_Cleaner` builds every input and output: the NER detector `mask()` (the same detector the
   output guard uses), then `pii_regex.scrub` (emails, phones, cards, ...), then `scrub_text`
   (registered secrets become `[secret]`), then a 4000-character cap. If the detector raises,
   the text becomes `[redacted]` (fail closed, never the raw text).
   - LLM messages: role plus cleaned content. Tool-result messages (they hold query rows) are
     replaced by `[tool result omitted: N chars]`.
   - Tool calls: `run_sql` arguments are replaced by `sanitize_sql` (literals become `?`) plus
     its hash; other arguments go through `drop_sensitive` and the scrubbers.
   - Tracer spans are already allowlisted, SQL-sanitized and key-filtered.
2. `make_mask()` is given to the `Langfuse(mask=...)` client, so everything is scrubbed again
   at export. It drops `pending_action`, `__interrupt__`, proofs, keys, tokens, secrets and
   passwords (`drop_sensitive`), and scrubs every string. If it fails, the payload becomes
   `[redacted]`.

Never sent: result rows, tool results, SQL literals, `GEMINI_API_KEY`, the Langfuse secret key
(registered with the secret scrubber at startup), the AES key, pending delete actions, delete
proofs, interrupts. Evidence: `test_redaction_question_answer_prompts_rows_secrets` runs a full
turn with a synthetic email, phone number, person name, secret, row sentinel and delete proof,
and asserts that none of them appears anywhere in the recorded client calls.
`test_mask_function_drops_sensitive_keys_and_scrubs_strings` and
`test_failing_detector_fails_closed` cover the second layer and the fail-closed path.

## Fail-open and bounds
- Off unless all three variables are set. When it is off, no client is built, nothing is
  wrapped and behaviour is identical (`test_disabled_without_all_keys_builds_no_client`).
- A client constructor error at startup logs at debug and runs without the sink.
- Any later Langfuse error logs once at debug (exception type only, never the message) and
  switches the sink off for the rest of the process. The turn result is unchanged.
- Bounds: client `timeout=5` s, `flush_interval=2` s, at most 500 buffered events per turn
  (extra events are counted in `dropped_events`), at most 64 remembered trace ids, at most 60
  messages per generation. Exit flush runs in `cli._bounded(..., 5.0)`.

## Open decisions
- OD-1 Most Tracer spans carry no `turn_id`, so the sink groups by "spans recorded while this
  turn ran", not by id. That is safe with one turn at a time in one process. Server mode would
  need a per-turn sink (or a contextvar) instead of the single active turn.
- OD-2 The `--resume` turn and the `/delete` start are not traced (they run outside
  `_Repl.turn`). The delete confirmation reply is a normal turn and is traced.
- OD-3 The `/trace` link uses `<host>/trace/<id>`, which Langfuse redirects to the project
  page. The project id is not known without an extra API call.
- OD-4 Prompt management (persona or prompt versions in Langfuse) and scores (for example
  `/feedback` as a Langfuse score) are follow-ups. `/feedback` still writes only to the local
  store.
- OD-5 Suggestion for the owner: in `tests/unit/conftest.py` (a hot file, not touched here),
  add the four `LANGFUSE_*` variables to the autouse fixture that already clears
  `OPSFLEET_LLM_*`, so a developer's real `.env` can never enable the sink in unit tests. The
  new test file clears them itself.
- OD-6 Pre-existing, not touched: `ruff format --check src/opsfleet_agent/commands/__init__.py`
  reports the `/export` tuple at about line 257. CI runs `ruff check` only.

## How to run
1. `cd infra/langfuse && cp .env.example .env`, replace every `change-me`, then
   `docker compose up -d` (see `infra/langfuse/README.md`).
2. Open http://localhost:3000 and sign in with the init user from that `.env`.
3. In the project root `.env`, set `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` (the
   `LANGFUSE_INIT_PROJECT_*` values) and `LANGFUSE_HOST=http://localhost:3000`.
4. `OPSFLEET_LLM_PROVIDER=lmstudio uv run opsfleet-agent --user <profile>` (or without the
   provider variable for Gemini), ask a question, then `/trace` for the trace id and link.
5. In Langfuse: **Tracing** shows one `turn` per question, and **Sessions** shows them grouped
   by CLI session.
