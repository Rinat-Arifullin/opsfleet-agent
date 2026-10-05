# Iteration 40b: golden evals as a Langfuse dataset, live SUT (ODs)

Files:
- new `evals/live_sut.py`: the live system under test, `evals.live_sut:live_harness`
- new `evals/langfuse_dataset.py`: the `upload` and `run` subcommands
- `cli.py`: the secret registration moved into `register_runtime_secrets(settings)`, so the
  harness registers exactly the same secrets as the CLI; no behaviour change
- new tests: `tests/unit/test_live_sut.py` and `tests/unit/test_langfuse_dataset.py`
- docs: a section in `infra/langfuse/README.md` and one line in the root README

No hot files, no new dependencies (`langfuse` 4.16 and `python-dotenv` are already locked), no
config fields.

## Decisions
- **OD-1 Dedicated eval data dir.** The live SUT keeps sessions, the checkpointer, saved
  reports and quota in `OPSFLEET_EVAL_DATA_DIR`, by default `<OPSFLEET_DATA_DIR or data>/eval-live`.
  Live evals never touch the user's own store. It uses the shipped profiles without
  `--profiles` overrides, so the cases' `profile` ids resolve the same on every machine.
- **OD-2 Stable item ids.** The item id is the case id with every character outside
  `[A-Za-z0-9_.-]` replaced by `__`. Golden ids are already safe, so they appear unchanged.
  The original id is always in `metadata.case_id`. Langfuse item ids are unique across a
  project's datasets, so a dataset other than `opsfleet-golden` prefixes its own name to the
  id. `create_dataset_item` with the same id updates the item in place, which makes `upload`
  idempotent. `upload` never deletes or archives items that have no local case: removing an
  item is a manual action in the UI.
- **OD-3 Runtime per profile, session per case.** Each profile gets one runtime, built
  lazily by `cli.build_runtime`, the same function the CLI uses, and then reused. Every case
  runs in a fresh session `<eval session id>-<random6>`, so the checkpointer never resumes an
  earlier run. JSONL traces go to the runner's trace dir. A case that times out has its
  BigQuery job cancelled, and its runtime is retired and never reused, because the turn may
  still be running. Bounds: at most 8 turns per case, and a per-case wall clock set by
  `OPSFLEET_EVAL_CASE_TIMEOUT_S` (default 600 s, clamped to 1..3600). Every flush or shutdown
  waits at most 5 s.
- **OD-4 Only the `profile` seed is supported.** A case that seeds `saved_reports`, `persona`
  or `preferences` fails with the clear reason "session seeds not supported by the live SUT".
  Seeding would mean writing into the eval store behind the agent's back, which is out of
  scope here. Affected cases: `discuss_saved_report`, `persona_tone_change`,
  `report_search`, `roadmap_actions_unsupported` and `preference_table_vs_bullets` (already
  `skip`).
- **OD-5 No LLM judge in live runs.** The configured judge is a hosted model. An uncalibrated
  judge contributes no pass or fail in `run_case` anyway, so judged cases are scored on their
  deterministic checks only.
- **OD-6 Which trace a dataset item links to.** The dataset run item links the **last**
  turn's trace, since that is the turn the checks score. Earlier turns of a multi-turn case
  share its Langfuse session. If the SUT failed before any trace existed (a timeout, an
  unsupported seed, an unknown profile), a minimal observation named `eval-case-failed`
  holds the link and the scores.
- **OD-7 Score mapping.** `pass` is a BOOLEAN score, 1 or 0. Its comment is the failure
  reasons, with secrets scrubbed and then passed through the sink's PII mask. Each check from
  `run_case` adds a `check:<name>` BOOLEAN score, so the run view can be filtered by which
  check failed. Not-applicable (`skip`) items appear in the table but are not run, linked or
  scored.
- **OD-8 No request-budget estimate in `run`.** `evals/run.py` estimates the LLM and BigQuery
  budget before a run; `langfuse_dataset.py run` does not. `--limit` and `--case` bound the
  run, and the runtime's own quota store and `maximum_bytes_billed` still enforce limits in
  code. Use `--limit 1` first.
- **OD-9 LLM call attribution.** `SutResult.llm_calls` counts calls per model from the `llm`
  spans of the JSONL trace. Calls that `TurnResult.llm_calls` reports but no span names go
  under `unattributed`, so totals still match the runtime.
- **OD-10 What the dataset holds.** Items carry the case turns, the session seed and the
  expectations. These are synthetic test data written for this repo, with no customer data.
  Scores carry only check names, booleans and masked reasons. Keys are never printed. A
  connection error prints the host and the exception class, never the exception message,
  which could echo a request.
- **OD-11 Run name and exit codes.** The default run name is
  `<git rev-parse --short HEAD>-<UTC %Y%m%dT%H%M%SZ>`, with `nogit` as a fallback after a 5 s
  timeout. Exit codes: 0 when every run case passed, 1 when any failed, 2 for configuration or
  connection problems (missing `LANGFUSE_*`, server unreachable, keys rejected, no matching
  items).
