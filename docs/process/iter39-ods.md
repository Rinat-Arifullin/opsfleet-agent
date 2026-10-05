# Iteration 39: per-user preferences (ODs)

R4.1, AC-24.1..AC-24.4. `/prefs` views, sets, notes and resets per-user answer preferences;
they persist across sessions and reach the analyst and report-writer prompts as the
lowest-precedence block.

Files:
- new `src/opsfleet_agent/store/preferences.py`: `SQLitePreferenceStore`, the implementation of
  the `graph.memory.PreferenceStore` seam (`save`, `load`, `reset`, `delete_user`), table
  `user_preferences`, `ERASURE_TABLE`
- new `src/opsfleet_agent/commands/preferences.py`: `handle_prefs` (view, `set <key> <value>`,
  `note <text>`, `reset`)
- `commands/__init__.py`: the `/prefs` command (in `/help` through `COMMANDS`) and the
  `CommandContext.preference_store` field
- `graph/memory.py`: `render_preferences` (fixed sentences only), and `persistable()` now keeps
  each note's scope snapshot
- `persona.py`: `PREFERENCES_LABEL` and the `<user_preferences>` fence; `assemble_prompt` takes
  an optional `preferences` argument and appends the block after the persona
- `roles/analyst.py`, `roles/report_writer.py`: pass the rendered block to `assemble_prompt`
- `graph/graph.py`: `GraphServices.preferences`; `_assemble` loads the user's stored preferences
  every turn; the analyst and writer get the rendered block
- `graph/context.py`: the "Answer preferences for this session" line left the context section
  (the preferences now live in their own block)
- `cli.py`: builds the store on the existing SQLite connection and wires it into the graph and
  the command context
- tests: new `tests/unit/test_preferences.py`; `tests/unit/test_context.py` updated for the moved
  preferences line
- eval: new `evals/cases/adversarial/preference_policy_override.yaml`
- README: one row for `/prefs` in the command table

No new dependencies and no config fields.

## Decisions
- **D-176 One JSON row per user is the source of truth.** `user_preferences(user_id PRIMARY
  KEY, data, updated_at)` holds the `SessionMemory.persistable()` shape: format, depth, charts
  and at most 5 notes, each with its scope snapshot. Both `save` and `load` pass the document
  through `SessionMemory.from_state`, so only enumerated values and notes that still pass
  `sanitise_note` go in or come out. A row edited on disk cannot carry text into a prompt. The
  document is capped at 8 KiB. Saving empty preferences deletes the row. The graph reads the
  store at the start of every turn (`_assemble`) and overrides the checkpointed session
  preferences with it, so a `/prefs` change applies from the next question, in this session
  and in later ones. Restatements and pending clarifications stay session-only.
- **D-177 Preferences are a separate, last, code-rendered block.** The order is: the safety
  core, then the code rule sections, then the fenced persona, then `USER PREFERENCES (lowest
  precedence)` and `<user_preferences>`. The block holds only fixed sentences that
  `render_preferences` picks from the enumerated values (for example "Prefer a table for lists
  and comparisons."). It never holds user text. Its label says that preferences choose format
  and depth only, that safety, scope, the rules and the required report sections win over
  them, and that the persona decides tone (AC-24.4). The report writer gets the same block,
  and its required sections and code checks are unchanged. The old one-line preferences
  summary in the context section was removed, so preferences appear in one place only.
- **D-178 Notes stay fenced data in the context section.** A note is untrusted user text. It
  never goes into the preferences block. It stays a `PREFERENCE_NOTE` store item inside the
  fenced context section, scope-filtered with the existing rule (FR-76): a note written under
  another brand scope is not shown. `/prefs note` validates with `set_preference` and the
  note itself as the message (the same evidence check as the tool contract). It is rejected
  for PII, links, code, instruction-like wording or wider-scope requests, and when it would
  be a sixth note. A note needs a known current scope; with no scope it is refused.
- **D-179 `/prefs` is not audited, only traced.** Other user-state changes (`/feedback`) are not
  audited either, and a preference is the user's own UI choice. The trace records an
  allowlisted `tool` span with the action and the rejection code only, never a value or note
  text.
- **D-180 No natural-language preference hook.** "Remember I like tables" is routed by the router's
  `memory` label to the light path. Wiring `set_preference` into that path changes routing
  and the light-path prompt, which is not a low-risk change for this iteration. Changes go
  through `/prefs` only. The new adversarial eval case checks that an NL attempt to store a
  policy-widening "preference" is refused.
- **D-181 Erasure and residue hook.** `store.preferences.ERASURE_TABLE = ("user_preferences",
  "user_id")` names the table and its user key, so a per-user erasure (iteration 35) and the
  residue enumeration (iteration 23) can find it. `SQLitePreferenceStore.delete_user` is the
  erasure call. `/prefs reset` deletes the user's own row. It is a user clearing their own
  settings, not an erasure of stored analysis data, so it writes no audit record (D-179).
  Folding the table into the erasure and residue tests is left to those iterations.
- **D-182 A store read failure fails closed.** If `load` raises, the turn runs with no stored
  preferences and no notes, and the error type is logged (not the content). `/prefs` replies
  "Could not read or save preferences right now." on a store error, and is unavailable when no
  store is open.
- **D-183 Wiring outside the planned file list.** The plan listed only the command, the store
  and the test file. Applying preferences also needed `graph.py` (the per-turn load and the
  writer argument), `persona.py`, the two role modules, `context.py`, `memory.py`, `cli.py`
  and `commands/__init__.py`. The schema is created by `ensure_schema` when the store is
  built, and `PREFERENCES_MIGRATION` holds the same statement, so `store.db.MIGRATIONS` was
  not touched.
- **D-184 `golden/preference_table_vs_bullets` stays skipped.** It seeds `session.preferences`,
  which the live SUT does not support (iteration 40b OD-4: no seeding behind the agent's
  back). Its skip reason should change from "requires iteration 39" to the seeding gap when
  the seed is added. The unit test `test_preferences_persist_and_apply` covers the behaviour
  offline.

## Tests
`tests/unit/test_preferences.py` (all offline, synthetic data, SQLite under `tmp_path`):
- `test_preferences_view_reset`: view, set (format, depth, charts), note and reset; values
  survive a reopened store; keyed by user
- `test_preference_cannot_override_safety`: scope, PII, sections and safety keys, bad
  values, and notes with an injection, an email, a wider-scope request, a URL or too much
  text are not stored, and the reply says why
- `test_preference_notes_stored_injection`: a tampered row (an injection note, a bad enum, an
  unknown key, a bad scope) is dropped on load. A valid note appears only in the fenced
  context, never in the preferences block, and not under another scope. A garbage row loads
  as empty.
- `test_instruction_precedence`: safety, then rules, then persona, then the preferences
  label and block; fixed sentences only; no block when nothing is set
- `test_preferences_persist_and_apply`: the analyst prompt carries the block; a change applies
  on the next question; a new session over a reopened store applies it; the report writer
  gets the same block; `delete_user` erases it
- extra: `/prefs` dispatch, `/help` and the no-store case, too many notes, a note without a
  scope and trace content, and the load failure failing closed

Suite: 3933 passed, 6 deselected. The strict golden run and the offline fixture evals pass. The
new eval case passes offline from its recorded `fake` (it was not run live).

## Out of scope
- Natural-language preference changes (D-180)
- The library agent
- Adding the table to the iteration 23 residue test and the iteration 35 erasure flow (the
  hook is in place, D-181)
- Live seeding of `session.preferences` and un-skipping the golden case (D-184)
