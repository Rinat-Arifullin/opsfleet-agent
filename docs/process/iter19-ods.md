# Iteration 19 — REPL UX, command table, narrow `--resume` 🔴: owner decisions

Status: implemented in a worktree, **owner review pending** (🔴 gate: session resume and
ownership; never self-approved). Coordinator review fixes applied. Files:
`src/opsfleet_agent/cli.py`, `src/opsfleet_agent/__main__.py`,
`src/opsfleet_agent/commands/__init__.py`, `src/opsfleet_agent/graph/resume.py` (additive),
`src/opsfleet_agent/bq/client.py` (`reset_cancel`), `tests/unit/test_cli.py`.

**Gate precondition:** the live smoke run of OD-16 must pass before the gate closes.

## How to try it by hand

Prerequisites:
- `.env` holds `GEMINI_API_KEY` and a `LANGGRAPH_AES_KEY` of 16, 24 or 32 characters (32 is
  recommended).
- The reviewer has run `gcloud auth application-default login`.

```
GOOGLE_CLOUD_PROJECT=<your-project> uv run python -m opsfleet_agent --user analyst_a
GOOGLE_CLOUD_PROJECT=<your-project> uv run python -m opsfleet_agent --user analyst_a --resume <session_id>
```

The session id is printed in the banner, at `/exit`, on EOF, and on a double Ctrl-C at the
prompt. Try `/help`, a question, Ctrl-C while it runs, then `/exit`.

## Decisions for the owner (one per item; default = what the code does now)

**OD-1 — Owner check first, one fixed refusal (no oracle).**
- `_resume_precheck` reads the encrypted checkpoint locally. An unknown id, another user's
  session and a checkpoint the current key cannot decrypt (MAC failure) all get the same
  `NO_SUCH_SESSION_TEXT` with exit 2. No model, BigQuery or lister call is made. Tests assert
  the stderr is byte-identical for unknown id, other user, and the wrong key (own and other
  user). Any other store error gets `STORE_REFUSED_TEXT`.
- `graph/resume.py` now checks the owner **before** "nothing pending" (the order is: no
  checkpoint, owner, nothing pending, scope, input guard, resume), so a direct caller has no
  existence oracle either. `resume_turn` stays as the backstop behind the CLI pre-check.
- Residual: the owner check is only as strong as `--user`, which is unauthenticated (accepted
  under FR-07: a local single-user CLI; session ids are uuid4, so not guessable).
- Owner: accept the fixed text, the MAC fold-in and the backstop?

**OD-2 — Scope drift is detected before any network call.**
- The pre-check compares the stored `scope_snapshot` with the current profile scope.
- On drift it prints `SCOPE_DRIFT_TEXT` and continues in a fresh session id. No replay happens.
- A profile that fails to parse counts as drift (fail closed).

**OD-3 — NOTHING_PENDING and ASK_AGAIN continue in the *same* session id.** The user gets the
history back. RESUMED finishes the interrupted turn. Only drift or an owner mismatch starts a new
session. Owner: OK?

**OD-4 — `/persona` is read-only.** It shows the active persona version. Editing stays out of the
REPL until the persona-edit flow has its own gate.

**OD-5 — Report commands are stubs.** `/reports`, `/open`, `/search` and `/export` print "not
available yet". `/help` marks them as such. They get wired once iterations 17, 18 and 22a land.

**OD-6 — Done: `BigQueryRunner.reset_cancel()`.** A public, locked method in `bq/client.py`
clears the stale cancel flag after a Ctrl-C; `build_runtime` passes it as `clear_cancel`. A test
drives the real runner: the cancel sets the flag, `reset_cancel` clears it before the next turn.

**OD-7 — Ctrl-C handling (revised) and the residual race.**
- The SIGINT handler only raises `KeyboardInterrupt`: no I/O, no network call in the handler.
  It is installed inside the `try` and the previous handler is restored before the cancel
  work, so a press at any point lands in one place.
- The cancel work (`cancel_inflight`, bounded at 1 s in a daemon thread; `reset_cancel`; closing
  the checkpointed turn; a `turn_cancelled` trace) runs in normal flow under an ignore-handler,
  so a second Ctrl-C during cleanup is swallowed; a loop-level backstop prints "Cancelled.".
- The same guard wraps the `--resume` turn (tested: Ctrl-C during resume cancels and the
  prompt continues).
- Residual: if a node ever ran BigQuery in a worker thread, a job started between the cancel
  and the reset could be cancelled spuriously. Acceptable for a single-user CLI; recorded.

**OD-8 — `load_settings()` runs before the checkpointer is built.** That way `.env` supplies
`LANGGRAPH_AES_KEY`. A missing or bad key gives one line naming the variable, never its value,
with exit 2, before any network call (fail closed). This is tested.

**OD-9 — README and `.env.example` (proposal only; neither file exists in this branch yet).**
Add to `.env.example`:
```
GEMINI_API_KEY=your_gemini_api_key_here
LANGGRAPH_AES_KEY=your_32_character_random_key_here
```
Add to the README run section:
- `--resume <session_id>` (own sessions only; scope change means a new session)
- the command list from `/help`
- "Ctrl-C cancels the running query"
- a key-generation hint: `python -c "import secrets; print(secrets.token_urlsafe(24))"`, which
  prints 32 characters

**OD-10 — D-96 `known_brands` and D-117 `golden_index` are not wired (`graph.py` is owned by
iteration 17).** Proposed `graph.py` diff:
- In `GraphServices`, add `known_brands: Collection[str] = ()` and
  `golden_index: GoldenIndex | None = None`.
- In `_assemble`, pass `known_brands=services.known_brands`.
- In `_retrieve_golden`, call `services.golden_index.retrieve(...)` when it is set. Otherwise
  keep the no-op.
- `build_runtime` would then pass the brand catalogue and a `GoldenIndex` built from
  `load_seed()`.

**OD-11 — D-114: golden seed strictness.** Proposal: `load_seed(strict=True)` in tests and CI,
so a bad seed fails the build. In production use `strict=False`, which skips bad entries with a
logged count and never blocks startup.

**OD-12 — `validate_scope_against_catalog` and the catalogue-fed PII brand allowlist are
deferred.** Both need a BigQuery brand query at startup. Today the allowlist is built from
profile brands only (D-90 `load_profiles_with_overrides`). Owner: accept a startup query
(dry-run, capped) in a later iteration?

**OD-13 — Done: a resumed turn is shown in full and sets `last_turn_id`.** `resume_turn` returns
the turn id (`ResumeOutcome.turn_id`); the CLI prints the answer and its notice through the same
path as a normal turn, so `/trace` and `/feedback` work right after a resume (tested).

**OD-14 — Bounds.**
- `MAX_TURNS=1000` per process. When it is reached, the CLI prints the session id and exits 0.
- `MAX_INPUT_CHARS=8000`: input is truncated before the graph.
- Command arguments: at most 4000 characters.
- Ctrl-C at the prompt: one press prints a hint; two in a row exit 130.

**OD-15 — The audit recorder in `build_runtime` is bound to `user_id` only, not `session_id`.**
After a drift or new-session switch, audit rows keep the user. Proposal: bind the session id
lazily through the tracer, or rebuild the recorder on `new_session()`.

**OD-16 — `build_runtime` and `_make_router_invoke` are `pragma: no cover`.** They are
production wiring for BigQuery and Gemini clients, so offline unit tests replace them with a
fake runtime (and with a real `AgentGraph` over fakes for the resume tests). A live smoke test
(`@pytest.mark.live`, or a manual run as above) is a **gate precondition**: the gate does
not close until it passes.

**OD-17 — Durable cancel: a cancelled turn is never replayed by `--resume`.**
- After a Ctrl-C the CLI calls `close_interrupted_turn` (`graph/resume.py`), which checks the
  owner and the turn id and then writes `{"outcome": "cancelled"}` with
  `update_state(..., as_node="finalize")`. The checkpoint then has nothing pending. Tested end to
  end: Ctrl-C mid-turn, exit, `--resume` prints "nothing pending" and runs no query, router or
  analyst call (the test fails if the close is disabled). It never raises (logs the type only).
- It touches `AgentGraph` internals (`open_resume(...)._built`, `_config`) because `graph.py` is
  owned by iteration 17. Proposal: after iteration 17 lands, add a public
  `AgentGraph.close_pending(session, turn_id)` and switch to it.

**OD-18 — Startup catch-all.**
- `main()` wraps everything: Ctrl-C at any point exits 130 with no traceback (also during the
  imports, in `__main__`).
- Any unexpected `Exception` logs only its type name, prints one fixed line
  (`UNEXPECTED_TEXT`) and exits 2: no traceback, message or secret on screen (tested).
- Owner: OK that the message is hidden (the type is in the log)?

**OD-19 — Smaller review fixes.** The `--resume` id uses `fullmatch` (so `"abc\n"` is refused,
tested). `terminal_safe` strips every Unicode `Cf` format character (incl. U+061C and U+180E)
plus `Cc`, `Zl` and `Zp` (tested). The `/audit` test uses a log with rows for two users and
asserts only the caller's rows appear and the session filter is passed.

## Owner-queue items handled here
- D-78, D-106: implemented in code (command table, REPL UX, Ctrl-C cancel, narrow `--resume`).
- D-90: implemented (`load_profiles_with_overrides` in the CLI).
- D-96, D-114, D-117: recorded as OD-10 and OD-11 only.
