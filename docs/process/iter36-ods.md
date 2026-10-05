# Iteration 36: feedback triage CLI with root-cause rules, add-eval and gated promote (ODs) [owner review pending]

Files:
- new `src/opsfleet_agent/commands/triage.py`: the maintainer CLI (`list`, `show`, `classify`,
  `dismiss`, `add-eval`, `promote`) and the root-cause rules
- new `config/maintainers.yaml`: the maintainer allowlist (synthetic id `support_demo`)
- `store/feedback.py`: `ROOT_CAUSES`, `DISMISS_REASONS`, `TRIAGE_GATES`, `list_items`, and a
  compare-and-set `set_state`
- `store/audit.py`: event types `feedback.triaged`, `feedback.dismissed`, `eval.case_added`; enum
  detail fields `root_cause`, `dismiss_reason`, `gate`, `triage_state`
- new tests: `tests/unit/test_triage.py`
- docs: README (R4.2 row, Not-built row, a "Feedback triage (maintainers)" section, the
  `OPSFLEET_MAINTAINERS_YAML` variable), `docs/technical.md` (commands row, gaps table)

No hot files, no new dependencies (`pyyaml` is already locked), no schema migration (the
`feedback.triage_state` column and its CHECK already exist). No eval case was added: the CLI is
a maintainer tool outside the chat, not a guardrail or a tool the model can call.

## Decisions
- **D-216 Entry point and the maintainer role.** The CLI is a module entry point,
  `python -m opsfleet_agent.commands.triage`, not a REPL command and not a `[project.scripts]`
  script: the chat never exposes it, and the README needs no reinstall step. A required
  `--as <id>` must be listed in `config/maintainers.yaml` (`OPSFLEET_MAINTAINERS_YAML` moves
  the file). The file is read fail-closed: a missing, unreadable or malformed file allows no
  one, and ids must match the audit actor pattern. A refused caller gets exit 2 and one fixed
  line, before the database or any trace is opened. Rev 4.2 has no application roles, so this
  is an allowlist for a local tool on the operator's machine, where infra access is the real
  boundary; `all_products` is a data scope and does not grant it.
- **D-217 Root-cause classes and rules.** FR-47's classes are kept as the code enum
  (`store.feedback.ROOT_CAUSES`), and the brief's names map onto them: `pii_leak` and
  `refused_wrongly` by a guard → `guardrail_block`; `wrong_number` → `verifier_fail` when the
  grounding or report guard flagged it, otherwise `intent_or_format`; `refused_wrongly` by the
  router → `misroute`; `formatting` and `other` → `intent_or_format`. The rules run over the
  rated turn's spans in a fixed order, first match wins: no spans → `no_trace`; an
  input/router/output/customer_id/echo guard with verdict block or refuse → `guardrail_block`;
  a SQL span in error or refused by policy, a `run_sql` tool error code, or a role that gave up
  → `sql_error`; an LLM span in error or on the fallback model → `model_down`; a report guard
  block or no draft, or grounding flags > 0 → `verifier_fail`; every successful SQL span
  returned 0 rows → `empty_result`; a down rating with route light/comment/refuse or reason
  `misunderstood` → `misroute`; reason `slow` or a turn over 30 s → `slow`; otherwise `clean`
  for an up rating and `intent_or_format` for a down one. The class is computed on read and
  never stored, so a rule change re-classifies old items; it is written into the audit row of
  each state change. Signals are span names and enum values only.
- **D-218 State machine, audit first, compare-and-set.** `classify`: new → triaged.
  `dismiss --reason {duplicate, not_a_bug, cannot_reproduce, out_of_scope}`: new or triaged →
  dismissed. `add-eval`: new or triaged → triaged. `promote`: new or triaged → promoted.
  Each change writes its audit row first (actor = the maintainer, the feedback's session and
  turn, `target_ids=[feedback_id]`, details = enums and hashes only); an `AuditError` or an
  ignored duplicate row aborts with nothing changed. Then the file is written in exclusive
  mode (never overwrites a reviewed file), then the state changes by compare-and-set on the
  expected states. If the write or the compare-and-set fails, the file is removed and an
  `outcome=failed` row is written best-effort. A refusal is audited best-effort with
  `outcome=refused` and the failing `gate`. Ids are accepted in full or as a unique prefix of
  8+ hex characters.
- **D-219 add-eval.** The feedback row stores no question (only a scrubbed comment), so the
  maintainer passes `--question`. It is secret-scrubbed and regex-scrubbed (redactions are
  kept as placeholders), then the PII detector runs on the result; anything it finds, or a
  detector failure, refuses the command. The case goes to `evals/cases/regression/
  triage_<id12>.yaml` with tags `[regression, triage, <cause>]`, the user's profile when it is
  a known one, `expect.outcome: answered` (or `refused` with `no_sql: true` under
  `--expect-refusal`), the golden suite's PII markers in `must_not_contain`, and a `skip`
  reason: it is a draft until a human reviews the expectations and records a `fake`. The test
  loads it with the eval runner's own `load_cases`.
- **D-220 promote writes a candidate, not the seed.** The plan goal says "adds a trio to the
  Golden seed"; the CLI instead writes `<data dir>/golden_candidates/<trio_id>.yaml` in the
  seed format, and a human copies it into `config/golden_seed.yaml`. The seed is a reviewed,
  committed file, and an automatic edit of it would bypass code review. Eligibility: an
  up-rated turn whose root cause is `clean`. The SQL comes from `--sql-file` or the turn's last
  successful SQL span; trace SQL has its literals replaced by `?`, so such SQL is refused and
  the reviewed SQL must be passed as a file. Gates in order, each fail-closed and each leaving
  the state and the files unchanged: (1) the Golden seed validator (SQL policy and scope,
  regex PII, injection, no figures, brand consistency); (2) the PII detector on the question
  and the summary; (3) a BigQuery dry run of the scoped SQL with the same job config and
  byte caps as `run_sql` (brands default from the user's profile: `all_products` → `agnostic`);
  (4) a green offline golden eval run (a subprocess with a 900 s timeout). The audit row
  carries the SQL's sha256, never the SQL text.
- **D-221 Display.** `list` and `show` print ids, enums, timestamps and the stored comment,
  which was scrubbed at `/feedback` time and is scrubbed again (secrets, length, PII regex)
  before display. The question, the summary and the SQL are never printed; the full trace is
  reached with `/trace`.
- **Deferred.** Auto-flagging failed turns that have no rating as triage inputs, clustering
  similar items, and a live NER test of the default detector wiring (the default gates are
  `pragma: no cover`; tests inject fakes and make no network call).
