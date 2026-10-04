# Implementation Plan

Design: [docs/architecture.md](../architecture.md) (rev. 4.4, approved at G2 on 2026-10-04) · Requirements: [docs/process/01-requirements.md](01-requirements.md) (rev. 4.4) · Decisions: [docs/decisions.md](../decisions.md) (ADR-001..014 and "Step 4b plan review: owner decisions")
Revision: **rev. 2 (after 04b review)**. It fixes B-1, B-2, M-1..M-14 and the minor items of [04b-plan-review.md](04b-plan-review.md) and applies the owner decisions of 2026-10-04.
Iterations: 49 (45 numbers; 8, 14, 22 and 28 are split into a/b) · Estimated: **68.0 effort-hours** of iterations, plus 3.5 h for Step 6, fixes and G4 = **71.5 h** · Capacity (assumption A): 67 h · Days left until the deadline (Thu 2026-10-08): 4 after today
Status: **🔴 G3: approved by the owner on 2026-10-04** · Amended after G3 on 2026-10-04 for the free-tier limits (owner decision; assumption B, section 5, R4, iterations 14a and 45). The amendment applies an owner decision and the HLD §4.0 model split; it does not change scope, gates or the order of iterations.

Plan date: Sun 2026-10-04. Planner: T2.

## 0. How to read this plan

- **Effort scale (agent-supervised hours: implementation, tests and owner review, not typing time):** S = 0.5 h, M = 1 h, M+ = 1.5 h, L = 2 h, L+ = 2.5 h, XL = 3 h. Rev. 2 re-sizes the hard iterations (2, 6, 7, 8, 14, 17, 22, 27, 28, 44) honestly (M-10, owner decision 3). The sizes now include the second T1 review and the red-team pass.
- **Models:** T1 = `claude-opus-5-5` (orchestrator, judgement, 🔴 iterations). T2 = `claude-sonnet-5-5` (implementer, reviewers). A 🔴 iteration is implemented by T1. It gets a second T1 review (the `security-reviewer` skill, a different instance) before its commit.
- **Every iteration** ends with `uv run ruff check .` clean and `uv run pytest -q` green, offline. An autouse socket-blocking fixture from iteration 1 makes any network call fail. The iteration leaves the CLI runnable and is committed as `iter N: <goal>`. These two criteria are written once here and referred to as "[std]" in each block.
- **Named tests are commitments (B-2).** Every `test_*` name in a done criterion is the canonical name from the requirements, the HLD or `docs/decisions.md`. The implementer creates a test with exactly that name. Extra descriptive tests are allowed, but they never replace a canonical one. Eval cases use the HLD path `evals/cases/<category>/<name>.yaml` (TR-16) and are cited below without the prefix, for example `adversarial/delete/flow_preview`.
- **Controls live in code.** A 🔴 rollback never weakens a control. If a closure is red at the end of its slot, work stops and the owner is told (🔴). The guarded feature stays disabled and fails closed (M-4).
- **Non-droppable rule (M-5):** every iteration that is not in the drop order (section 7) is non-droppable.
- **Planning assumptions (flagged for the owner):**
  - A. Capacity: the owner is available about 8 h today and 10 h on each of Mon..Thu, with 2 implementer streams on `[PARALLEL OK]` iterations. Throughput is taken as 1.5 effort-hours per owner-hour (owner decision 3): Sun 12, Mon 15, Tue 15, Wed 15, Thu 10, total 67. The reviewers' blended factor (about 1.2x on Sunday to Tuesday, because the owner reviews every 🔴 iteration) is tracked as risk R1. It is measured after iterations 1-4.
  - B. Free-tier limits, read by the owner in the AI Studio dashboard on 2026-10-04 (T-2 done, decisions "T-2" and "Free-tier decision"): `gemini-3.8-flash` 5 RPM / 20 RPD, `gemini-3.1-flash-lite` 15 RPM / 500 RPD, 250K TPM each. The embedding row was not shown; its limit stays an assumption until the iteration-2 spike (Q-5). The owner chose to stay on the free tier (section 5).
  - C. Model ids: `gemini-3.8-flash` (Deep analyst and Report writer only, per HLD §4.0; the pro preview has no free tier, so Deep stays on flash per ADR-009), `gemini-3.1-flash-lite` (router, light path, Quick analyst, Report verifier, Library agent, summary, judge, fallback) and `gemini-embedding-001` at 768 dimensions. The iteration-2 spike confirms that the embedding model works on a free key. If it does not, the orchestrator proposes `gemini-embedding-2` at 768 dimensions to the owner as an amendment to G2 decision 4, and the owner decides.

## 1. Dependency graph

```
Sun 10-04   1 ─► 2 ─► 6 ─────────────► 7 (Mon)
            1 ─► 3 ─► 5      1 ─► 4      1 ─► 8a
Mon 10-05   7 ─► 9 ◄─ 5, 6               8a ─► 8b ─► 12 ─► 11
            9 ─► 10 ◄─ 4
            13 ◄─ 5, 6, 7, 8b, 9, 10     16 ◄─ 1, 4      20 ◄─ 8b (parallel stream)
Tue 10-06   14a ◄─ 3, 11, 12, 13, 16 ─► 14b ─► 15
            17 ◄─ 4, 14b, 15 ─► 18       19 ◄─ 5, 14b, 16
            21 ◄─ 4, 6-12, 16            27 ◄─ 3, 4 (runner; golden step needs 14a)
Wed 10-07   22a ◄─ 17, 18, 21 ─► 22b ─► 23
            24 ◄─ 3, 5, 18   25 ◄─ 4 ─► 26 ◄─ 3
            28a ◄─ 22b, 27 ─► 28b ◄─ 23   29 ◄─ 27 (golden step), 15   30 ◄─ 20, 27, 29
            31 ◄─ 2, 15 ─► 32 ◄─ 4, 25
            then by the drop order (section 7): 40, 41, 42 if time allows; 33..39 are forecast drops
Thu 10-08   08:00 45 ─► 43 ─► 44 ─► 12:00 Step 6 + fixes + G4 ─► submit
```

**Critical path (TR-11):** 1 → 2 → 6 → 7 → 9 → 10 → 13 → 14a → 14b → 17 → 18 → 22a → 22b → 23 → 28b → 45 → Step 6.

The tightest links are:
- 9 → 10 → 13: small-cell and differencing must exist before `run_sql` is wired (B-1).
- 14a → 14b → 17: the checkpointer must exist before report confirmation.
- 22a → 22b → 23: the delete flow and its no-residue closure. All three are T1 and none can be dropped.

The calibration gate (30) reaches the critical path only through the owner's labels (T-1).

## 2. Model assignment table

| Iteration | Name | Model | 🔴 | Effort | Day (forecast) |
|---|---|---|---|---|---|
| 1 | Skeleton, all dependencies, config, startup check | T2 | | M+ (1.5) | Sun |
| 2 | Spikes (sqlglot, interrupt, embedding, model ids) and CI | T2 | | M (1) | Sun |
| 3 | TurnBudget, retry wrapper, limiter | T2 (extra review: bounds) | | M+ (1.5) | Sun |
| 4 | JSONL tracer, redaction, SQLite store base | T2 (extra review: secrets) | | M+ (1.5) | Sun |
| 5 | BigQuery client wrapper and caches | **T1** | 🔴 | M+ (1.5) | Sun |
| 6 | SQL policy validator | **T1** | 🔴 | XL (3) | Sun |
| 7 | Brand-scope rewriter | **T1** | 🔴 | XL (3) | Mon |
| 8a | PII regex scrubber | **T1** | 🔴 | M (1) | Sun |
| 8b | Typed-PII detector (Presidio) and brand allowlist | **T1** | 🔴 | L (2) | Mon |
| 9 | Small-cell rule and quasi-identifier set | **T1** | 🔴 | L (2) | Mon |
| 10 | Differencing guard, session and cross-session | **T1** | 🔴 | M+ (1.5) | Mon |
| 12 | Output guard | **T1** | 🔴 | M (1) | Mon |
| 11 | Input guard, router, light path | **T1** | 🔴 | M+ (1.5) | Mon |
| 13 | `run_sql` (HLD §5.1 order) and read tools | **T1** | 🔴 | L (2) | Mon |
| 16 | Session start, profiles, scope validation | T2 | | M (1) | Mon |
| 20 | Calibration set draft (30 synthetic cases) | T2 | | S (0.5) | Mon (parallel stream) |
| 14a | Graph, supervisor, analysts, checkpointer | T2 (extra review: bounds) | | L+ (2.5) | Tue |
| 14b | Crash resume and budget persistence | **T1** | 🔴 | M (1) | Tue |
| 15 | Context assembly, scope filter, memory, clarification | **T1** | 🔴 | M+ (1.5) | Tue |
| 17 | Report writer, verifier, confirm-before-save | **T1** | 🔴 | XL (3) | Tue |
| 18 | Report list, view, substring search | T2 | | M (1) | Tue |
| 19 | REPL UX, command table, narrow `--resume` | **T1** | 🔴 | M+ (1.5) | Tue |
| 21 | Audit log and audit viewer | **T1** | 🔴 | M (1) | Tue |
| 27 | Eval runner, gates, request estimator | T2 | | XL (3) | Tue |
| 22a | Two-phase delete: preview, token, confirm, execute | **T1** | 🔴 | XL (3) | Wed |
| 22b | Delete expiry paths (restart, rotation, resume, idle, Ctrl-C) | **T1** | 🔴 | M (1) | Wed |
| 23 | Hard delete with no residue | **T1** | 🔴 | M (1) | Wed |
| 24 | Fallback messages, degraded mode, quotas | T2 | | M (1) | Wed |
| 25 | Trace viewer and metrics summary | T2 | | M (1) | Wed |
| 26 | Persona mechanism and hot reload | T2 | | M (1) | Wed |
| 28a | Adversarial suites: SQL, PII, scope, injection, off-topic | T2 (T1 reviews all of `evals/cases/adversarial/`) | | L (2) | Wed |
| 28b | Adversarial delete and resilience suites | T2 (T1 review) | | M (1) | Wed |
| 29 | Golden cases and router labelled set | T2 | | M (1) | Wed |
| 30 | Judge calibration gate | T2 | | M (1) | Wed |
| 31 | Golden seed and top-k retrieval | T2 | | M (1) | Wed |
| 32 | `/feedback` | T2 | | M (1) | Wed |
| 33 | Rename, Markdown export, retry report (drop 5) | T2 | | M (1) | Wed (if time) |
| 34 | `/history` browse (drop 6) | T2 | | M (1) | Wed (if time) |
| 35 | Erasure CLI (drop 7) | **T1** | 🔴 | M (1) | Wed (if time) |
| 36 | Triage CLI (drop 4) | T2 | | M (1) | Wed (if time) |
| 37 | Ranked full-text report search (drop 3) | T2 | | M (1) | Wed (if time) |
| 38 | Semantic search with RRF (drop 2) | T2 | | M (1) | Wed (if time) |
| 39 | Preferences, P (drop 1) | T2 | | M (1) | Wed (if time) |
| 40 | Langfuse (drop 10) | T2 | | M (1) | Wed (if time) |
| 41 | Persona smoke check and rollback (drop 9) | T2 | | S (0.5) | Wed (if time) |
| 42 | `access set` (drop 8) | T2 | | S (0.5) | Wed (if time) |
| 43 | README, `.env.example`, `requirements.txt`, gitleaks | T2 | | M+ (1.5) | Thu |
| 44 | Clean-machine run, both install paths | T2 | | M (1) | Thu |
| 45 | Final live eval run and results capture | T2 | | M+ (1.5) | Thu 08:00 |

🔴 T1 iterations (each gets a second T1 review): 5, 6, 7, 8a, 8b, 9, 10, 11, 12, 13, 14b, 15, 17, 19, 21, 22a, 22b, 23, 35 (M-5, SEC-18).

**Note on the HLD tier table (TR-25):** the HLD tiers describe what the product needs. The drop order in section 7 is the owner's schedule decision and takes precedence for the prototype. A dropped item keeps its HLD design and is listed in the README.

**Split iterations (TR-10):**
- 8, 14, 22 and 28 are split into a/b to keep each under about 5 files and one reviewable concern.
- Iterations 1, 13 and 17 still touch more than 5 files. The reasons are given in their blocks.

## 3. Parallelizable iterations and hot files

**Hot files are edited serially, never by two streams at once (M-11):**
- `pyproject.toml` / `uv.lock` / `requirements.txt`: only 1, then 31, then 40, never at the same time.
- `src/opsfleet_agent/cli.py`
- `src/opsfleet_agent/graph/graph.py`
- `src/opsfleet_agent/config.py`
- `tests/unit/conftest.py`

`cli.py` is edited once, in 19, which builds a **command table**. Every later command is a new entry in `src/opsfleet_agent/commands/` that registers itself in that table, so 22a, 24, 25, 32-36, 39 and 42 do not edit `cli.py`.

**Streams:**
- 3 `[PARALLEL OK with 4]`. 5 starts only after 3, because it needs the budget types.
- 8a `[PARALLEL OK with 6]`; 8b `[PARALLEL OK with 7]`
- 9 and 10 are **sequential**: 10 reads the small-cell outcome of 9.
- 12 then 11 (the input guard reuses the output guard's injection scan).
- 16 `[PARALLEL OK with 13]`; 20 `[PARALLEL OK with any]` as soon as 8b is green (Monday morning).
- 18 `[PARALLEL OK with 19]`; 21 `[PARALLEL OK with 17]`; 27 `[PARALLEL OK with 17-21]`. 27 creates `evals/run.py` and `evals/gates.py`, which nothing else touches.
- 24, 25 `[PARALLEL OK with each other and with 22a]`. 26 runs after 25.
- 33 and 37 run **in sequence** (both touch the report store).
- **Never parallel:**
  - 6 → 7 → 9 → 10 → 13: the policy, rewriter and guard contracts feed `run_sql`.
  - 14a → 14b → 17
  - 21 → 22a → 22b → 23: audit before delete, delete before residue.

---

## 4. Iterations

### Sun 2026-10-04: skeleton and first guards

## Iteration 1: Skeleton, all dependencies, config, startup check
**Goal**: `uv run opsfleet-agent --user <id>` starts. It validates config and environment and echoes input. Every known dependency is added once, here (M-12).
**Model**: T2 sonnet
**ACs covered**: FR-62 startup check (no AC of its own); prerequisite for AC-15.3 (completed in 16)
**Files** (6, justified: the package seed and the one dependency change belong together, so the lock is touched once):
- `pyproject.toml`, `uv.lock`
  - dependencies: `langgraph`, the LangGraph SQLite checkpointer, `langchain-google-genai`, `google-cloud-bigquery`, `sqlglot>=30,<31`, `presidio-analyzer`, `presidio-anonymizer`, `spacy` with the pinned `en_core_web_sm` wheel, `pycryptodome`, `pyyaml`
  - dev dependencies: `ruff`, `pytest`
  - `requires-python = ">=3.12,<3.14"`; `[project.scripts] opsfleet-agent`; registered `live` marker
  - versions are the ones `uv add` resolves today; no versions are invented in this plan
  - the existing `pandas`/`db-dtypes` dependencies are removed if the iteration-2 spike confirms `to_arrow()` or row iteration
- `requirements.txt`: `uv export --no-hashes --format requirements-txt > requirements.txt` (the CLAUDE.md command, TR-26)
- `src/opsfleet_agent/{__init__,__main__,cli,config}.py` (the package seed; no logic beyond the echo loop and settings)
- `config/models.yaml` (roles, ids, assumed RPM/RPD per model, shutdown-date comments)
- `tests/unit/conftest.py` (autouse socket block), `tests/unit/test_config.py`
**Done criteria**:
- [x] A missing or unlisted model id, a missing `GOOGLE_CLOUD_PROJECT` or a missing `GEMINI_API_KEY` exits with one actionable line and no traceback. The value is never printed: config is logged by an **allowlist** of non-secret keys (M-7) (`test_startup_check_reports_missing_env_without_values`).
- [x] `tests/unit` fails any test that opens a socket (`test_socket_block_fixture_active`).
- [x] `.gitignore` covers `.env`, `*.db`, `*.db-wal`, `traces/` and `evals/results/raw/` (`test_gitignore_covers_secrets_and_stores`).
- [x] [std]
**Effort**: M+ (1.5) · **Depends on**: none · **Risk**: low

## Iteration 2: Spikes (sqlglot, interrupt, embedding, model ids) and CI
**Goal**: the riskiest library and provider assumptions are proven before any guard is built on them.
**Model**: T2 sonnet
**ACs covered**: none (de-risking; produces regression tests)
**Files**:
- `tests/unit/test_spike_sqlglot_bigquery.py`
  - parse, qualify and rewrite a CTE-wrapped table in the BigQuery dialect
  - nested CTEs, `UNNEST`, `QUALIFY`, quoted identifiers
- `tests/unit/test_spike_interrupt_resume.py`: `interrupt()` and resume through `SqliteSaver` with `EncryptedSerializer`; a role subgraph compiled with `checkpointer=False`
- `tests/live/test_spike_models.py` (`@pytest.mark.live`): one call per configured model id, plus one `gemini-embedding-001` call at 768 dimensions
- `.github/workflows/ci.yml`
  - ruff, offline pytest
  - the `requirements.txt` **sync check**: re-export and `git diff --exit-code` (M-12)
**Done criteria**:
- [ ] (owner, L0) The model ids in `config/models.yaml` answer on the owner's key (live spike, run once by the owner, 4 calls).
- [ ] The embedding result is recorded (assumption C):
  - if `gemini-embedding-001` fails on a free key, the G2 amendment goes to the owner the same day;
  - no code depends on the embedding model before 31.
- [x] The sqlglot spike documents in a docstring every dialect quirk that 6, 7 and 9 must handle.
- [x] BigQuery result handling uses `result.to_arrow()` or row iteration, not `to_dataframe`.
- [x] [std]. CI runs ruff, offline pytest and the sync check.
**Effort**: M (1) · **Depends on**: 1 · **Risk**: medium. If sqlglot cannot express the scope rewrite, 7 stops and escalates (no fallback form; see 7).

## Iteration 3: TurnBudget, retry wrapper, limiter
**Goal**: every LLM call goes through one wrapper. The wrapper owns:
- the per-turn call and time budget;
- per-role sub-caps;
- a token-bucket limiter at 80% of the configured RPM;
- bounded retry with backoff;
- one fallback attempt, then `force_answer`.

**Model**: T2 sonnet, extra review (bounds, ADR-003/009)
**ACs covered**: AC-15.1 (wrapper half; degraded message in 24), AC-15.6, AC-22.4, AC-22.5 (budget half; the graceful partial answer is wired in 14a)
**Files**:
- `src/opsfleet_agent/graph/budget.py`
- `src/opsfleet_agent/graph/llm.py`
- `tests/unit/test_budget.py`
- `tests/unit/test_llm_wrapper.py`
**Done criteria (M-8)**:
- [x] Budgets are enforced by code, and exceeding one returns a typed "budget exhausted" (`test_turn_caps_enforced`, `test_turn_deadline`):
  - Q&A: 10 LLM calls and 120 s;
  - report: 14 calls, 6 executed SQL queries and 180 s;
  - light path: 3 calls;
  - per-role sub-cap of 6.
- [x] Retries are bounded, and a fake clock proves it (`test_retry_wrapper_bounded`, `test_retry_then_fallback`):
  - at most 3 retries, with backoff 1 s, 2 s and 4 s plus jitter;
  - then the fallback model gets exactly one attempt;
  - then `force_answer`.
- [x] The SDK's own retries are disabled, so each wrapper attempt is one HTTP attempt (`test_sdk_single_attempt`).
- [x] Retries and fallback attempts count against the role sub-cap (`test_role_subcap_counts_retries_and_fallback`).
- [x] The Quick → Deep escalation happens at most once per turn (`test_escalation_once`). A `recursion_limit` hit is caught and turned into a typed partial answer (`test_recursion_limit_caught`).
- [x] Usage per turn (calls, tokens, limiter wait recorded apart from latency) goes to the trace (`test_trace_records_turn_usage`).
- [x] [std]
**Effort**: M+ (1.5) · **Depends on**: 1 · **Risk**: medium

## Iteration 4: JSONL tracer, redaction, SQLite store base
**Goal**: a redacted per-turn trace by **allowlist**, and one SQLite app file with WAL, `secure_delete=ON` and a versioned schema. The checkpoints use a separate file (HLD §8).
**Model**: T2 sonnet, extra review (secrets, M-7)
**ACs covered**: AC-08.3, AC-16.1 (span types; the viewer is in 25), AC-16.4; the foundation for AC-21.8, AC-12.16 and AC-28.4
**Files**:
- `src/opsfleet_agent/obs/tracer.py`: allowlisted span fields, bounded string length, and one shared `drop_sensitive()` used by every sink
- `src/opsfleet_agent/store/db.py`: connection, `PRAGMA secure_delete=ON`, WAL, migrations, single-writer transaction helper
- `tests/unit/test_tracer.py`
- `tests/unit/test_store_db.py`
**Done criteria**:
- [x] Only allowlisted fields reach the JSONL. Emails, names and the free text of results never do (`test_trace_redaction`, `test_trace_spans_carry_no_pii`).
- [x] With sentinel values, none of these appears in a trace, a log line or an error string (`test_secrets_never_in_traces_logs_or_errors`):
  - the API key;
  - `LANGGRAPH_AES_KEY`;
  - `K_delete`;
  - a delete token and a delete proof.
- [x] LLM prompt and response text is not captured unless the dev flag is on (`test_llm_text_capture_off_by_default_in_prod`).
- [x] The span types are defined: turn, router, role, llm, tool, guard, sql, delete and error (`test_trace_has_all_span_types`).
- [x] `PRAGMA secure_delete` reads back ON on every connection, and two concurrent writers do not corrupt the file (descriptive tests).
- [x] [std]
**Effort**: M+ (1.5) · **Depends on**: 1 · **Risk**: medium

## Iteration 5: BigQuery client wrapper and caches 🔴
**Goal**: one function executes a query only after a dry run, with `maximum_bytes_billed`, a 60 s timeout, labels and a cancel hook. The ADR-012 memo has the HLD key (M-9).
**Model**: T1 opus, extra review (second T1)
**ACs covered**: AC-08.11 (error mapping), AC-14.1, AC-14.2, AC-14.3, AC-15.5 (cancel hook only; the expiry half is in 22b)
**Files**:
- `src/opsfleet_agent/bq/client.py`: dry run, caps, `job_timeout_ms`, labels, cancel
- `src/opsfleet_agent/bq/errors.py`: mapping table to fixed messages and reason codes
- `src/opsfleet_agent/bq/memo.py`: key `(sql_hash, scope_key, refresh_date)`
- `src/opsfleet_agent/bq/schema.py`: introspection of the 4 allowed tables only
- `tests/unit/test_bq_client.py` (fake client)
**Done criteria**:
- [ ] A dry run over the per-query cap (1 GB) or over the remaining session cap (10 GB) never executes (`test_cost_cap_rejects_before_execution`, `test_session_budget`).
- [ ] Every executed job carries `maximum_bytes_billed`, `job_timeout_ms` 60 s and labels (`test_job_config_sets_max_bytes_billed`).
- [ ] Raw BigQuery error text never reaches the LLM, the trace or the audit record. Only the mapped message and reason code do (`test_bq_error_is_mapped_not_forwarded`).
- [ ] Memo key and invalidation:
  - the memo key is `(sql_hash, scope_key, refresh_date)`, never a table modified time;
  - a changed refresh date misses (`test_memo_misses_after_refresh_date_change`);
  - two scopes never share an entry (`test_result_cache_key_includes_scope`, `test_cache_key_includes_scope`);
  - a repeat inside the window is reused (`test_repeated_query_reuses_result`).
- [ ] A memo hit is returned to `run_sql` **before** the differencing step, so differencing still runs on a hit (asserted again in 13).
- [ ] [std]
**Effort**: M+ (1.5) · **Depends on**: 1, 3 · **Risk**: high (cost). **Rollback:** the wrapper is the only path to BigQuery. If a cap test is red, `run_sql` is not wired (13 does not start), and the owner is told.

## Iteration 6: SQL policy validator 🔴
**Goal**: a pure function accepts only a single SELECT over the 4-table allowlist. It rejects every other form with a typed reason code.
**Model**: T1 opus, extra review (second T1)
**ACs covered**: AC-08.1, AC-08.4 (code half), AC-08.10, AC-08.13, AC-10.1, AC-10.2
**Files**:
- `src/opsfleet_agent/guards/sql_policy.py`
- `tests/unit/test_sql_policy.py`
- `tests/unit/test_sql_policy_redteam.py`
**Done criteria**:
- [ ] Named tests: `test_sql_policy_select_only`, `test_sql_policy_table_allowlist`, `test_sql_policy_rejects_pii_projection`, `test_select_star_rejected`, `test_cte_shadowing_rejected`, `test_policy_rejects_select_as_struct`, `test_policy_rejects_table_alias_as_value`, `test_policy_rejects_qi_predicate_at_id_grain`, `test_policy_rejects_id_literal_with_qi`, `test_policy_denies_scalar_functions_on_qi`, `test_source_allowlist`, `test_orders_num_of_item_not_exposed`.
- [ ] Red-team cases (M-1), each a parametrized case in `test_sql_policy_redteam.py`:
  - PII columns in WHERE, JOIN, GROUP BY, ORDER BY and LIKE;
  - `CONCAT`, `SUBSTR`, `TO_JSON_STRING`, `STRING_AGG` and `ARRAY_AGG` over PII;
  - `EXPORT DATA`, `EXTERNAL_QUERY`, `ML.*`, `FOR SYSTEM_TIME AS OF`, temporary UDFs and `@@` variables;
  - `INFORMATION_SCHEMA` and wildcard tables;
  - `EXECUTE IMMEDIATE` and `;` chains;
  - comment tricks, quoted identifiers, mixed case and unicode look-alikes.
- [ ] The validator returns a reason code that the audit log can store without any query text (checked in 21).
- [ ] [std]
**Effort**: XL (3) · **Depends on**: 2 · **Risk**: high: this is the gate between the LLM and the warehouse. **Rollback:** the policy is a pure module behind `run_sql`. If red-team cases are still red, `run_sql` stays unwired, and the owner is told (🔴). Nothing is relaxed.

## Iteration 8a: PII regex scrubber 🔴 `[PARALLEL OK with 6]`
**Goal**: free text (user input, output and report bodies) passes a deterministic scrubber for email, phone, card-like and id-like strings.
**Model**: T1 opus, extra review
**ACs covered**: AC-08.2, AC-10.7
**Files**:
- `src/opsfleet_agent/guards/pii_regex.py`
- `tests/unit/test_pii_regex.py`
**Done criteria**:
- [ ] `test_output_filter_redacts_email_phone_address` (the regex part: email and phone; the address part is completed by the detector in 8b)
- [ ] An email the user types is masked before it reaches the state, a checkpoint or history (`test_user_typed_email_not_persisted`; the persistence hook is asserted again in 14a)
- [ ] Red-team cases: an obfuscated email (`name at domain dot com`), a phone number with separators, card-like digit groups
- [ ] [std]
**Effort**: M (1) · **Depends on**: 1 · **Risk**: medium. **Rollback:** fail closed. If the scrubber is red, the output and input guards refuse free text that contains digit runs or `@`, and the owner is told.

### Mon 2026-10-05: guards complete, `run_sql`, session

## Iteration 7: Brand-scope rewriter 🔴
**Goal**: validated SQL is rewritten so that every allowlisted table is replaced by a code-built, PII-free CTE (`__p`, `__oi`, `__o`, `__u`) filtered by the session's brands. The CEO `all` flag changes the data only.
**Model**: T1 opus, extra review
**ACs covered**: AC-08.12, AC-09.1, AC-09.2, AC-09.3
**Files**:
- `src/opsfleet_agent/guards/scope.py`: AST rewrite and post-rewrite invariant
- `src/opsfleet_agent/guards/scope_ctes.py`: the CTE builders, keyed by `products.brand`
- `tests/unit/test_scope_rewrite.py`
- `tests/unit/test_scope_redteam.py`
**Done criteria**:
- [ ] Named tests: `test_scope_filter_cannot_be_bypassed`, `test_nested_cte_scope_resolution`, `test_rewrite_invariant_fail_closed`, `test_all_scope_counts_non_buyers`
- [ ] An empty brand list fails closed: no query runs (parametrized in `test_rewrite_invariant_fail_closed`)
- [ ] Red-team cases:
  - other-brand requests;
  - a brand named in a `UNION`;
  - a subquery on a raw table;
  - comment-injected table names;
  - a table behind an alias;
  - a user CTE named like a scoped CTE.
- [ ] [std]
**Effort**: XL (3) · **Depends on**: 6 · **Risk**: high. **Rollback (M-4):** ADR-008 has no fallback form. If the invariant cannot be proven, `run_sql` stays unwired, and the owner is told (🔴). A partial rewrite is never shipped.

## Iteration 8b: Typed-PII detector and brand allowlist 🔴 `[PARALLEL OK with 7]`
**Goal**: output and context text also pass a Presidio detector on spaCy `en_core_web_sm`. A brand allowlist protects brand names from masking. The app refuses to start if the model is missing.
**Model**: T1 opus, extra review
**ACs covered**: AC-08.2 (address part), AC-08.14
**Files**:
- `src/opsfleet_agent/guards/pii.py`: detector and allowlist built from the brand list; calls `pii_regex` first
- `tests/unit/test_pii.py`
- `tests/unit/test_pii_typed_gate.py`: offline fixture gate
- `evals/cases/adversarial/pii_typed/*.yaml`: the first 40 synthetic cases, including `adversarial/pii_typed/brand_false_positive`; no real people
**Done criteria**:
- [ ] Named tests: `test_pii_detector_missing_model_fails_startup`, `test_brand_allowlist_not_masked`, `test_typed_pii_ner_masks_person_and_address`, `test_output_guard_uses_ner_detector`
- [ ] On the fixture, code computes recall ≥ 95% and brand false positives = 0. This gate is **provisional** until 28a extends the set (SEC-20).
- [ ] Red-team cases: a name in free text, a name split across rows, a brand that looks like a surname
- [ ] The pinned model installs from `uv sync`. The pip-path instruction is written down for 43.
- [ ] [std]. The offline test run stays under 60 s (the model is loaded once per session).
**Effort**: L (2) · **Depends on**: 8a · **Risk**: high: brand precision is the likely failure. **Rollback (M-4):** the detector is never narrowed (no PERSON-only fallback) and never disabled. If the gate is red, the output guard refuses free-text answers that contain a detected entity, and the owner is told (🔴).

## Iteration 9: Small-cell rule and quasi-identifier set 🔴
**Goal**: aggregates grouped or filtered by a quasi-identifier (QI) are either suppressed below 5 distinct users or rejected.
- **QI set location:** a code constant in the `src/opsfleet_agent/guards/` small-cell module, where HLD §5.2 places it (owner decision 5; closes Q-4).
- **QI set contents:** `users.age`, `gender`, `city`, `state`, `country`, `traffic_source`, and `created_at` (only as `DATE_TRUNC` to MONTH).

**Model**: T1 opus, extra review
**ACs covered**: AC-08.5, AC-08.6, AC-08.8, AC-08.9
**Files**:
- `src/opsfleet_agent/guards/small_cell.py`: QI set, lineage through CTEs, `HAVING` injection, population check
- `tests/unit/test_small_cell.py`
- `tests/unit/test_small_cell_redteam.py`
**Done criteria**:
- [ ] Named tests: `test_small_cell_group_by_qi`, `test_small_cell_after_brand_scope`, `test_small_cell_counts_in_scope_population`, `test_small_cell_rejects_qi_in_value_aggregate`, `test_small_cell_rejects_window_over_qi`, `test_signup_timestamp_is_qi`, `test_qi_lineage_through_cte`, `test_qi_filter_aggregate_population_check`, `test_user_grain_rejects_qi_projection`, `test_product_only_group_not_suppressed`, `test_top_customers_by_spend_allowed`
- [ ] ADR-004 cases (b) and (c):
  - a QI filter with a value aggregate gets a population query, which is dry-run, capped and counted in the SQL budget;
  - an unplaceable `HAVING` is rejected with `small_cell_unplaceable`;
  - the other reason codes are `qi_position` and `qi_at_id_grain`.
- [ ] The count is taken **after** the brand scope (inside `__u` joined to the scoped CTEs).
- [ ] [std]
**Effort**: L (2) · **Depends on**: 5, 6, 7 · **Risk**: high. **Rollback (M-4):** if a case is red, every aggregate that touches a QI is refused (cross-customer aggregates refused), and the owner is told (🔴).

## Iteration 10: Differencing guard, session and cross-session 🔴
**Goal**: per-user query fingerprints with no values, kept 30 days across sessions. Two aggregates whose difference isolates fewer than 5 users are refused.
**Model**: T1 opus, extra review
**ACs covered**: AC-08.7, AC-08.15
**Files**:
- `src/opsfleet_agent/guards/differencing.py`
- `src/opsfleet_agent/store/fingerprints.py`
- `tests/unit/test_differencing.py`
**Done criteria**:
- [ ] Named tests: `test_differencing_guard_session`, `test_differencing_guard_across_sessions`, `test_fingerprint_retention_30_days`, `test_fingerprint_store_has_no_values`
- [ ] If the fingerprint store is unavailable or a write fails, the query is refused (SEC-12; parametrized in `test_differencing_guard_across_sessions`)
- [ ] Red-team cases: complement queries (all minus one brand), a stepwise range narrowing, the same pair split across two sessions
- [ ] [std]
**Effort**: M+ (1.5) · **Depends on**: 4, 9 · **Risk**: high. **Rollback (M-4):** there is no session-only fallback. If cross-session is red, QI-filtered aggregates are refused, and the owner is told (🔴).

## Iteration 12: Output guard 🔴
**Goal**: every answer, report body and preview passes an allowlist output guard that fails closed.
**Model**: T1 opus, extra review (M-5)
**ACs covered**: AC-10.4, AC-10.5, AC-10.6 (output half)
**Files**:
- `src/opsfleet_agent/guards/output.py`
- `tests/unit/test_output_guard.py`
**Done criteria**:
- [ ] Named tests: `test_output_guard_allowlist_fail_closed`, `test_output_injection_scan`, `test_output_guard_strips_markdown_images_and_urls`
- [ ] Every output first passes PII guards 8a and 8b
- [ ] [std]
**Effort**: M (1) · **Depends on**: 8a, 8b · **Risk**: high. **Rollback:** fail closed (the answer is replaced by a fixed refusal), and the owner is told.

## Iteration 11: Input guard, router, light path 🔴
**Goal**: the input guard scrubs and scans. The router (flash-lite) sees user messages only. Small talk takes a light path with no SQL and no embedding, but with all guards.
**Model**: T1 opus, extra review (M-5)
**ACs covered**: AC-08.4 (input half), AC-11.1, AC-11.2, AC-11.3, AC-11.4, AC-11.5, AC-11.6, AC-23.4
**Files**:
- `src/opsfleet_agent/guards/input.py`
- `src/opsfleet_agent/roles/router.py`
- `prompts/router.md`
- `tests/unit/test_input_guard_router.py`
**Done criteria**:
- [ ] Named tests: `test_light_path_no_sql_no_embedding`, `test_light_path_runs_guards`, `test_router_sees_user_messages_only`
- [ ] Off-topic, prompt-exfiltration and non-English rephrase inputs are refused by code with a fixed message (offline fakes; live cases in 28a)
- [ ] [std]
**Effort**: M+ (1.5) · **Depends on**: 3, 8b, 12 · **Risk**: high. **Rollback:** if a case is red, the input is refused, and the owner is told.

## Iteration 13: `run_sql` and read tools 🔴
**Goal**: the only tool that touches BigQuery. It runs the **HLD §5.1 order** (B-1):

1. Parse and validate (6).
2. Rewrite into the scoped CTEs `__p`, `__oi`, `__o`, `__u` (7).
3. Re-resolve the rewritten AST.
4. QI/small-cell (9). One of three outcomes:
   - inject `HAVING COUNT(DISTINCT user_id) >= 5`;
   - run the population check (dry-run, capped, counted in the SQL budget);
   - reject with `qi_position`, `qi_at_id_grain` or `small_cell_unplaceable`.
5. Post-rewrite invariant (fail closed).
6. Dry run and caps: 1 GB per query and 10 GB per session (5).
7. Differencing: per user, across sessions, 30 days (10). It also runs on a memo hit.
8. Execute with `maximum_bytes_billed`, `job_timeout_ms` 60 s and labels. Errors are mapped.
9. Scrub (8a, 8b).
10. Apply the 200-row cap.

**Model**: T1 opus, extra review
**ACs covered**: AC-04.1, AC-08.11 (wiring), AC-13.2, AC-14.3 (wiring), AC-23.5
**Files** (6, justified: the read tools are one small module each, and the order test needs all of them):
- `src/opsfleet_agent/tools/run_sql.py`
- `src/opsfleet_agent/tools/schema_tool.py`
- `src/opsfleet_agent/tools/registry.py`: per-role tool allowlist
- `tests/unit/test_run_sql.py`
- `tests/unit/test_run_sql_order.py`
- `tests/unit/test_schema_tool.py`
**Done criteria**:
- [ ] Named tests: `test_run_sql_order_matches_hld_5_1`, `test_run_sql_small_cell_and_differencing_before_execute`, `test_schema_tool_hides_pii_columns`, `test_large_result_truncation_flagged`, `test_empty_result_handling`
- [ ] A spy on the fake client asserts that nothing executes before steps 1-7 pass, and that a memo hit still calls differencing
- [ ] Mapped BigQuery errors appear in the trace and audit legs as reason codes only (M-1)
- [ ] [std]
**Effort**: L (2) · **Depends on**: 5, 6, 7, 8b, 9, 10 · **Risk**: high. **Rollback:** the tool stays unregistered until every named test is green, and the owner is told.

## Iteration 16: Session start, profiles, scope validation `[PARALLEL OK with 13]`
**Goal**: `--user <id>` loads a profile from `config/profiles.yaml`. The scope comes only from the profile. The banner shows the user, the scope and the session.
**Model**: T2 sonnet
**ACs covered**: AC-09.4, AC-15.3, AC-20.1, AC-20.2, AC-20.3, AC-20.4 (golden half in 29)
**Files**:
- `src/opsfleet_agent/session.py`
- `config/profiles.yaml` (synthetic users only)
- `tests/unit/test_session.py`
**Done criteria**:
- [ ] Named tests: `test_scope_from_profile_only`, `test_cli_banner_shows_user_scope_session`, `test_cli_rejects_unknown_user`, `test_profile_scope_validation`, `test_startup_config_check`
- [ ] The startup check also validates the profiles file and that the app DB, the checkpoint DB and the trace directory are writable (TR-18)
- [ ] [std]
**Effort**: M (1) · **Depends on**: 1, 4 · **Risk**: medium

## Iteration 20: Calibration set draft (30 synthetic cases) `[PARALLEL OK with any]`
**Goal**: 30 synthetic question-answer pairs are ready for the owner to label (T-1). They are handed over by **Monday midday** (SCH-10).
**Model**: T2 sonnet
**ACs covered**: AC-29.6 (input)
**Files**:
- `evals/calibration/cases.yaml` (no real data)
**Done criteria**:
- [ ] 30 cases cover good, partial and wrong answers in roughly equal parts, each with an empty `owner_score` and `owner_reason`
- [ ] Every case passes the PII guard of 8b (no detected entity)
- [ ] [std]
**Effort**: S (0.5) · **Depends on**: 8b · **Risk**: low

### Tue 2026-10-06 (forecast): agent loop, reports, audit, eval runner

## Iteration 14a: Graph, supervisor, Quick and Deep analyst, checkpointer
**Goal**: a LangGraph graph with these parts:
- the supervisor;
- the Quick analyst on `gemini-3.1-flash-lite` and the Deep analyst on `gemini-3.8-flash` (HLD §4.0), with the per-role fallback to flash-lite on 429;
- the grounding check;
- `SqliteSaver` with `EncryptedSerializer` in a separate file.

The checkpointer exists **before** 17 (M-3). Role subgraphs are compiled with `checkpointer=False`.
**Model**: T2 sonnet, extra review (bounds)
**ACs covered**: AC-01.1, AC-01.2, AC-02.1, AC-02.2, AC-02.3, AC-03.1, AC-03.2, AC-05.1, AC-05.2 (golden in 29), AC-13.1, AC-13.3, AC-15.4, AC-22.4, AC-22.5 (wiring)
**Files**:
- `src/opsfleet_agent/graph/graph.py`
- `src/opsfleet_agent/roles/analyst.py`
- `src/opsfleet_agent/graph/grounding.py`
- `prompts/analyst.md`
- `tests/unit/test_graph.py`
**Done criteria**:
- [ ] Named tests: `test_self_correction_bounded`, `test_cli_survives_tool_failure`, `test_malformed_tool_call_handled`, `test_role_tool_isolation`, `test_no_traceback_reaches_user`, `test_grounding_accepts_prior_turn_ledger`, `test_grounding_rejects_unknown_number`, `test_grounding_rounding`, `test_grounding_derived_ops`, `test_grounding_unmatched_labelled`
- [ ] `test_user_typed_email_not_persisted` passes against the real checkpointer
- [ ] The safety preamble is code-built and placed before any persona text (asserted again in 26)
- [ ] [std]
**Effort**: L+ (2.5) · **Depends on**: 3, 11, 12, 13, 16 · **Risk**: medium

## Iteration 14b: Crash resume and budget persistence 🔴
**Goal**: a turn interrupted at any node resumes from its checkpoint with the same budget. Resume fails closed without the AES key and checks the scope snapshot (SEC-14).
**Model**: T1 opus, extra review
**ACs covered**: AC-22.6 (crash half)
**Files**:
- `src/opsfleet_agent/graph/resume.py`
- `tests/unit/test_resume.py`
**Done criteria**:
- [ ] Named tests: `test_resume_budget_persisted`, `test_resume_after_crash_each_node`
- [ ] A missing or wrong `LANGGRAPH_AES_KEY` refuses the resume with one actionable line. Nothing is decrypted or replayed.
- [ ] A scope snapshot that differs from the current profile starts a new session (the full test is in 19)
- [ ] [std]
**Effort**: M (1) · **Depends on**: 14a · **Risk**: high. **Rollback:** if a case is red, `--resume` is disabled (a new session always starts), and the owner is told.

## Iteration 15: Context assembly, scope filter, memory, clarification 🔴
**Goal**: the context for each turn is assembled by code:
- a bounded history window;
- in-scope items only;
- prior-turn numbers through the ledger;
- clarification and stated defaults.

**Model**: T1 opus, extra review (M-5)
**ACs covered**: AC-07.1, AC-07.2, AC-09.5, AC-22.1, AC-22.2, AC-23.1, AC-23.2, AC-23.3 (golden in 29)
**Files**:
- `src/opsfleet_agent/graph/context.py`
- `src/opsfleet_agent/graph/memory.py`
- `tests/unit/test_context.py`
**Done criteria**:
- [ ] Named tests: `test_context_scope_filter_drops_out_of_scope`, `test_history_window_bounded`, `test_new_session_has_empty_history`, `test_churn_restatement_session_only`, `test_set_preference_value_in_message`
- [ ] Every item from the store (report bodies, history, seed trios) is wrapped as untrusted data before it enters the prompt
- [ ] [std]
**Effort**: M+ (1.5) · **Depends on**: 14a · **Risk**: high. **Rollback:** if the scope filter is red, cross-turn context is off (one-turn memory only), and the owner is told.

## Iteration 17: Report writer, verifier, confirm-before-save 🔴
**Goal**: a report draft has the required sections. The verifier (flash-lite) checks it against the ledger. It is saved only after an `interrupt()` confirmation, idempotently.
**Model**: T1 opus, extra review
**ACs covered**: AC-06.1, AC-06.2, AC-06.4, AC-06.5, AC-21.1, AC-21.2, AC-21.8, AC-22.6 (draft half)
**Files** (6, justified: writer and verifier share one schema, and the store and its atomicity test belong together):
- `src/opsfleet_agent/roles/report_writer.py`
- `src/opsfleet_agent/roles/verifier.py`
- `src/opsfleet_agent/store/reports.py`
- `src/opsfleet_agent/reports/schema.py`
- `prompts/report_writer.md`
- `tests/unit/test_reports.py`
**Done criteria**:
- [ ] Named tests: `test_report_saved_with_owner_and_session`, `test_save_only_on_confirm`, `test_report_cancel_saves_nothing`, `test_revise_starts_new_turn`, `test_report_save_idempotent`, `test_resume_reshows_draft`, `test_report_schema_required_sections`, `test_save_last_answer_as_report`, `test_report_store_atomic_and_concurrent`
- [ ] The report body passes the output guard (12) before it is saved, and is wrapped as untrusted when it is read back (SEC-13)
- [ ] Red-team case: a delete attempt while a report draft is pending is refused, and the draft stays pending (TR-14)
- [ ] [std]
**Effort**: XL (3) · **Depends on**: 4, 14b, 15 · **Risk**: high. **Rollback:** the save path stays disabled (reports are shown but not saved), and the owner is told.

## Iteration 18: Report list, view, substring search `[PARALLEL OK with 19]`
**Goal**: an owner lists, views and substring-searches only their own in-scope reports. The list filter is the same matcher that delete uses.
**Model**: T2 sonnet
**ACs covered**: AC-06.3, AC-21.3, AC-21.4, AC-21.5, AC-21.6 (matcher half), AC-21.10, AC-21.11, AC-22.3
**Files**:
- `src/opsfleet_agent/reports/library.py`
- `src/opsfleet_agent/reports/matcher.py`
- `tests/unit/test_library.py`
**Done criteria**:
- [ ] Named tests: `test_list_reports_owner_only`, `test_view_report_owner_only`, `test_list_filter_matches_delete_matcher`, `test_list_masks_drifted_report_title`, `test_view_report_scope_drift`, `test_report_search_filters`, `test_search_reports_owner_and_scope`, `test_search_results_not_delete_targets`, `test_matcher_rejects_empty_and_wildcards`
- [ ] [std]
**Effort**: M (1) · **Depends on**: 17 · **Risk**: medium

## Iteration 19: REPL UX, command table, narrow `--resume` 🔴
**Goal**: the REPL with a command table (the only edit of `cli.py` after iteration 1), Ctrl-C handling, and `--resume` limited to the user's own sessions with a scope-drift check.
**Model**: T1 opus, extra review (SEC-14)
**ACs covered**: AC-20.5, AC-22.6 (resume half), AC-09.6
**Files**:
- `src/opsfleet_agent/cli.py`
- `src/opsfleet_agent/commands/__init__.py`: the table
- `tests/unit/test_cli.py`
**Done criteria**:
- [ ] Named tests: `test_cli_commands`, `test_resume_rejects_other_users_session`, `test_resume_scope_drift_new_session`
- [ ] `/audit` is a command-table entry only, not a tool. No role can reach it (SEC-17; asserted in 21).
- [ ] Ctrl-C cancels the running BigQuery job through the hook from 5 (the pending-delete half is in 22b)
- [ ] [std]
**Effort**: M+ (1.5) · **Depends on**: 5, 14b, 16 · **Risk**: high. **Rollback:** `--resume` is disabled, and the owner is told.

## Iteration 21: Audit log and audit viewer 🔴 `[PARALLEL OK with 17]`
**Goal**: an append-only audit table. Rows hold reason codes only, with no query text or PII. Each `(pending_action_id, event_type)` pair is unique. The `/audit` viewer is reachable from the CLI only.
**Model**: T1 opus, extra review
**ACs covered**: AC-28.1, AC-28.2 (helper), AC-28.3, AC-28.4, AC-28.5
**Files**:
- `src/opsfleet_agent/store/audit.py`
- `src/opsfleet_agent/commands/audit.py`
- `tests/unit/test_audit.py`
**Done criteria**:
- [ ] Named tests: `test_audit_delete_lifecycle`, `test_audit_guardrail_refusal_no_pii`, `test_audit_append_only`, `test_audit_viewer`, `test_audit_unique_pending_action_event`, `test_delete_aborts_when_audit_write_fails`
- [ ] Refusals from 6, 7, 9, 10, 11 and 12 are recorded with their reason codes
- [ ] `/audit` is not in any role's tool registry (SEC-17)
- [ ] [std]
**Effort**: M (1) · **Depends on**: 4, 6-12, 16 · **Risk**: high. **Rollback:** the delete tool (22a) is not registered until 21 is green, and the owner is told.

## Iteration 27: Eval runner, gates, request estimator `[PARALLEL OK with 17-21]`
**Goal**: `uv run python evals/run.py` runs any subset, prints a request estimate first, refuses over-budget runs, writes results under `evals/results/` and exits non-zero on a failed gate.
**Model**: T2 sonnet
**ACs covered**: AC-29.1, AC-29.2, AC-29.3, AC-29.4, AC-29.5
**Files**:
- `evals/run.py`
- `evals/gates.py`
- `evals/judge.py` (rubric with a version)
- `tests/unit/test_eval_runner.py`
**Done criteria**:
- [ ] Named tests: `test_eval_runner_gates_exit_code`, `test_eval_runner_request_estimate`, `test_eval_results_link_traces`, `test_judge_rubric_versioned`, `test_eval_gate_fails_on_any_delete_case`, `test_eval_gate_brand_false_positive_zero`, `test_eval_gate_differencing_cross_session_100`
- [ ] The gates are coded in `gates.py`:
  - golden ≥ 80%;
  - adversarial 100%, and any failing delete case fails the run;
  - `pii_typed` recall ≥ 95% with 0 brand false positives;
  - `differencing/cross_session` 100%;
  - resilience 100%;
  - the router set is reported separately.
- [ ] The calibration status feeds the gates: an uncalibrated judge's scores do not count (M-6; the gate itself is in 30)
- [ ] An offline golden step runs in CI with fakes. The live golden step needs 14a.
- [ ] [std]
**Effort**: XL (3) · **Depends on**: 3, 4 (live golden step: 14a) · **Risk**: medium

### Wed 2026-10-07 (forecast): delete flow, evals, Wednesday features

## Iteration 22a: Two-phase delete: preview, token, confirm, execute 🔴
**Goal**: the ADR-007 / HLD §6.3.3 flow:

1. Code computes the owner-scoped match and writes the pending action in the **preview** node.
2. Code derives the token `HMAC(K_delete, …)`. `K_delete` is held in memory only.
3. Only `sha256(token)` is checkpointed.
4. The preview shows the backup notice "up to 7 days" (AC-12.11).
5. Confirmation is accepted only on the next user turn, with an HMAC proof.
6. Execution recomputes ownership and `ids_sha256` and writes the audit record first.

**Model**: T1 opus, extra review
**ACs covered**: AC-12.1, AC-12.2, AC-12.3, AC-12.4, AC-12.5, AC-12.6, AC-12.7, AC-12.8, AC-12.9, AC-12.11, AC-12.12, AC-12.13, AC-12.14 (wiring), AC-21.6, AC-21.7, AC-28.1, AC-28.2 (exercised)
**Files**:
- `src/opsfleet_agent/delete/flow.py`: preview, `confirm_delete`, execute nodes
- `src/opsfleet_agent/delete/token.py`: derivation and proof
- `src/opsfleet_agent/commands/delete.py`
- `tests/unit/test_delete_flow.py`
- `tests/unit/test_delete_token.py`
**Done criteria (M-2)**:
- [ ] Named tests, flow: `test_delete_requires_confirm`, `test_delete_confirm_deletes_exact_previewed_set`, `test_delete_cancel_on_non_confirm`, `test_delete_owner_only`, `test_delete_by_session_id`, `test_delete_confirmation_bound_to_preview_set`, `test_llm_cannot_trigger_delete_without_user_turn`, `test_delete_no_matches`, `test_delete_preview_includes_backup_notice`
- [ ] Named tests, taint and intent: `test_delete_refused_after_view_same_turn`, `test_delete_requires_intent_in_user_message`, `test_session_delete_excludes_viewed_reports`
- [ ] Named tests, token: `test_confirm_delete_never_deletes`, `test_confirm_delete_rerun_keeps_token`, `test_delete_token_derived_not_stored`, `test_delete_token_never_in_traces`, `test_checkpoint_holds_token_hash_only`, `test_proof_mismatch_cancels`, `test_confirm_only_on_next_turn`
- [ ] Named tests, structure: `test_confirm_prompt_rendered_by_code`, `test_delete_not_alone_in_step`, `test_execute_delete_requires_confirmed_record`, `test_previewed_audit_idempotent_on_replay`, `test_second_delete_while_pending_rejected`, `test_cancelled_delete_reply_gets_fresh_context`
- [ ] Named tests, size: `test_large_delete_requires_typed_count` (more than 20 matches needs the typed count), `test_large_delete_preview_truncated_but_bound`
- [ ] A token is single use: a replayed proof is cancelled and audited (parametrized in `test_proof_mismatch_cancels`)
- [ ] `K_delete` is generated at start and never written to disk, a checkpoint or a trace (covered by `test_secrets_never_in_traces_logs_or_errors`)
- [ ] [std]
**Effort**: XL (3) · **Depends on**: 17, 18, 21 · **Risk**: high. **Rollback (M-4):** the delete tool and `/delete` stay unregistered (the feature is off, fail closed), and the owner is told (🔴). The audit-first rule is never relaxed.

## Iteration 22b: Delete expiry paths 🔴
**Goal**: a pending delete expires at the next turn or after 10 min. These events also expire it, with `delete.expired` audited:
- restart;
- key rotation;
- `/exit`;
- resume;
- a 30-min idle;
- Ctrl-C.

The reply that follows is routed to the input guard as a new turn. This takes over the delete parts of 16 and 19 (M-3).
**Model**: T1 opus, extra review
**ACs covered**: AC-12.10, AC-12.15, AC-15.5 (expiry half), AC-20.7, AC-22.6 (pending-delete half)
**Files**:
- `src/opsfleet_agent/delete/expiry.py`
- `tests/unit/test_delete_expiry.py`
**Done criteria**:
- [ ] Named tests: `test_expired_confirm_routes_to_input_guard_and_audits`, `test_confirm_delete_on_other_instance_verifies`, `test_key_rotation_expires_pending_delete`, `test_resume_expires_pending_delete`, `test_idle_timeout_drops_pending_delete`, `test_sigint_cancels_bq_job_and_expires_pending`
- [ ] [std]
**Effort**: M (1) · **Depends on**: 22a · **Risk**: high. **Rollback:** as in 22a.

## Iteration 23: Hard delete with no residue 🔴
**Goal**: deleted rows leave no bytes in the DB file or the WAL. The delete runs in one transaction under `secure_delete`, followed by `wal_checkpoint(TRUNCATE)`.
**Model**: T1 opus, extra review
**ACs covered**: AC-12.16
**Files**:
- `src/opsfleet_agent/store/hard_delete.py`
- `tests/unit/test_residue.py`
**Done criteria**:
- [ ] Named tests: `test_hard_delete_leaves_no_residue_in_db_file`, `test_index_rows_deleted_with_report`
- [ ] The residue test is **parametrized over the stores that exist**. 23 covers the report, history and checkpoint rows. 37 and 38 add their FTS and vector tables to the same parameter list, with FTS `optimize` inside the delete transaction (M-3).
- [ ] The test scans raw bytes of the DB file and the WAL for a sentinel title and body
- [ ] [std]
**Effort**: M (1) · **Depends on**: 22b · **Risk**: high. **Rollback (M-4):** there is no residue gap. If the test is red, the delete feature stays off (22a rollback), and the owner is told (🔴).

## Iteration 24: Fallback messages, degraded mode, quotas `[PARALLEL OK with 25]`
**Goal**: when every model is down, the user gets a fixed graceful message. Reports can still be listed and searched with no LLM. Per-user quotas block after the limit.
**Model**: T2 sonnet
**ACs covered**: AC-15.1 (message), AC-15.2, AC-21.14, AC-22.8
**Files**:
- `src/opsfleet_agent/graph/degraded.py`
- `src/opsfleet_agent/store/quota.py`
- `tests/unit/test_degraded.py`
**Done criteria**:
- [ ] Named tests: `test_all_models_down_graceful`, `test_degraded_mode_lists_and_searches_reports_when_llm_down`, `test_quota_blocks_after_limit`
- [ ] AC-21.14 export clause: degraded export is asserted only if 33 ships. Otherwise it is listed as not implemented (M-3).
- [ ] [std]
**Effort**: M (1) · **Depends on**: 3, 5, 18 · **Risk**: medium

## Iteration 25: Trace viewer and metrics summary `[PARALLEL OK with 24]`
**Goal**: `/trace <turn>` renders the span tree with failures marked. `/metrics` prints a summary.
**Model**: T2 sonnet
**ACs covered**: AC-16.1 (viewer), AC-16.2, AC-16.3
**Files**:
- `src/opsfleet_agent/commands/trace.py`
- `src/opsfleet_agent/obs/metrics.py`
- `tests/unit/test_trace_viewer.py`
**Done criteria**:
- [x] Named tests: `test_trace_viewer_renders_failed_span`, `test_metrics_summary`
- [x] [std]
**Effort**: M (1) · **Depends on**: 4 · **Risk**: low

## Iteration 26: Persona mechanism and hot reload
**Goal**: a persona file with a size limit is hot-reloaded. An invalid file keeps the last valid one. The persona can never remove sections or override rules, and the code-built safety preamble always comes first.
**Model**: T2 sonnet
**ACs covered**: AC-27.1, AC-27.2, AC-27.3 (eval in 28a)
**Files**:
- `src/opsfleet_agent/persona.py`
- `prompts/persona.md`
- `tests/unit/test_persona.py`
**Done criteria**:
- [ ] Named tests: `test_persona_version_in_trace`, `test_persona_invalid_keeps_last_valid`, `test_persona_hot_reload`, `test_persona_size_limit`, `test_persona_cannot_override_rules`, `test_persona_cannot_remove_sections`, `test_safety_preamble_precedes_persona`
- [ ] [std]
**Effort**: M (1) · **Depends on**: 3, 25 · **Risk**: medium

## Iteration 28a: Adversarial suites: SQL, PII, scope, injection, off-topic
**Goal**: the adversarial cases that are not about delete, as YAML under `evals/cases/adversarial/`. T1 reviews every case file (SEC-16).
**Model**: T2 sonnet; T1 reviews
**ACs covered**: AC-08.4, AC-08.5, AC-08.6, AC-08.7, AC-08.8, AC-08.9, AC-08.10, AC-08.11, AC-08.14, AC-08.15, AC-09.1, AC-09.3, AC-09.4, AC-09.6, AC-10.1, AC-10.3, AC-10.4, AC-10.5, AC-10.6, AC-11.1, AC-11.2, AC-11.5, AC-23.4, AC-24.2, AC-27.3 (eval legs)
**Files**:
- `evals/cases/adversarial/*.yaml`: PII, QI and differencing
- `evals/cases/adversarial/injection/*.yaml`
- `evals/cases/adversarial/offtopic/*.yaml`
- `evals/cases/adversarial/pii_typed/*.yaml`: extended to the full set, which makes the 8b gate final
**Done criteria**:
- [ ] PII and QI cases exist and pass offline with fakes:
  - `adversarial/pii_injection_emails`, `adversarial/pii_injection_non_english`, `adversarial/pii_derived`, `adversarial/pii/error_oracle_city`
  - `adversarial/small_cell_thin_brand`, `adversarial/qi_conditional_aggregate`, `adversarial/qi_list_intersection`, `adversarial/qi_listing_aggregate`, `adversarial/signup_timestamp_linkage`, `adversarial/table_alias_struct`, `adversarial/bq_error_value_echo`
- [ ] Differencing cases: `adversarial/differencing/cross_session`, `adversarial/differencing_complement`, `adversarial/differencing/differencing_session`
- [ ] Scope cases: `adversarial/out_of_scope_brand`, `adversarial/total_revenue_scoped`, `adversarial/scope_escalation`, `adversarial/scope_shrink_history`
- [ ] Injection cases:
  - `adversarial/ignore_instructions_drop`, `adversarial/indirect_injection`, `adversarial/data_instruction_following`, `adversarial/output_unexpected_tool`, `adversarial/answer_injection_url`
  - `adversarial/injection/stored_report_injection`, `adversarial/injection/markdown_image_exfil`, `adversarial/injection/persona_override`, `adversarial/injection/golden_poisoned_trio`
  - `adversarial/injection/router_label_injection`, `adversarial/injection/output_action_injection`
- [ ] Off-topic and policy-override cases: `adversarial/off_topic_poem`, `adversarial/off_topic_weather`, `adversarial/prompt_exfiltration`, `adversarial/offtopic/non_english_rephrase`, `adversarial/smalltalk_injection`, `adversarial/persona_policy_override`, `adversarial/preference_policy_override` (the last runs only if 39 ships; otherwise it is marked not applicable)
- [ ] `adversarial/pii_typed/brand_false_positive` is in the set. The final recall gate (≥ 95%) and the brand gate (0) pass on the full set.
- [ ] [std]
**Effort**: L (2) · **Depends on**: 13, 15, 26, 27 · **Risk**: medium

## Iteration 28b: Adversarial delete and resilience suites
**Goal**: the delete and resilience cases. Any failing delete case fails the whole run.
**Model**: T2 sonnet; T1 reviews
**ACs covered**: AC-12.1, AC-12.5, AC-12.9, AC-12.12, AC-13.2, AC-22.8, AC-28.4, AC-29.5 (eval legs)
**Files**:
- `evals/cases/adversarial/delete/*.yaml`
- `evals/cases/resilience/*.yaml`
**Done criteria**:
- [ ] Delete cases:
  - `adversarial/delete/flow_preview`, `adversarial/delete/session_reports`, `adversarial/delete/preconfirmed`, `adversarial/delete/expired_reply`, `adversarial/delete/audit_log`
  - `adversarial/library_view_then_delete`, `adversarial/injection/library_view_then_delete`
  - `adversarial/delete/delete_other_users_report`
- [ ] Resilience cases: `resilience/empty_result_typo_category`, `resilience/quota_exhausted`, `resilience/schema_drift`
- [ ] Offline run: adversarial 100%, resilience 100%
- [ ] [std]
**Effort**: M (1) · **Depends on**: 23, 24, 28a · **Risk**: medium

## Iteration 29: Golden cases and router labelled set
**Goal**: the golden set (about 30 cases; about 15 under a volume cut) and the router labelled set (about 50 messages; about 25 under a volume cut).
**Model**: T2 sonnet
**ACs covered**: the golden legs of US-01..07, AC-04.1, AC-04.2, AC-11.3, AC-11.4, AC-20.4, AC-21.1, AC-21.2, AC-21.9, AC-21.10, AC-22.2, AC-22.3, AC-23.1, AC-23.2, AC-23.3, AC-29.4
**Files**:
- `evals/cases/golden/*.yaml`
- `evals/cases/router/labelled.yaml`
**Done criteria**:
- [ ] Ask and analyse cases: `golden/top_customers`, `golden/aov_by_traffic_source`, `golden/compare_brands_why`, `golden/show_sql`, `golden/monthly_revenue_12m`, `golden/ytd_revenue_by_brand`, `golden/schema_overview`, `golden/inventory_unavailable`, `golden/state_underspend_compare`, `golden/churn_last_month`, `golden/churn_user_definition`
- [ ] Follow-up and memory cases: `golden/followup_breakdown`, `golden/followup_why_march`, `golden/cross_session_memory`, `golden/discuss_saved_report`, `golden/stated_assumption_defaults`, `golden/clarify_unresolved_reference`
- [ ] Report cases: `golden/q1_report`, `golden/report_save_confirm`, `golden/save_this`, `golden/report_search`, `golden/roadmap_actions_unsupported`
- [ ] Session and small-talk cases: `golden/my_scope`, `golden/smalltalk_light_path`, `golden/smalltalk_then_task`
- [ ] Optional cases, used only if their iteration ships: `golden/persona_tone_change` (41), `golden/preference_table_vs_bullets` (39)
- [ ] The router set includes borderline simple/complex messages and `adversarial/injection/router_label_injection`
- [ ] [std]
**Effort**: M (1) · **Depends on**: 27 (golden step), 15 · **Risk**: low

## Iteration 30: Judge calibration gate
**Goal**: the judge scores the 30 labelled cases. The gate needs at least 80% agreement with the owner's labels. Below that, the judge is marked uncalibrated and its golden scores do not count.
**Model**: T2 sonnet
**ACs covered**: AC-29.6
**Files**:
- `evals/calibration/run.py`
- `tests/unit/test_calibration.py`
**Done criteria**:
- [ ] Named test: `test_judge_calibration_gate_blocks_on_low_agreement`
- [ ] The calibration status is written where `gates.py` reads it
- [ ] [std]
**Effort**: M (1) · **Depends on**: 20 (with the T-1 labels), 27, 29 · **Risk**: medium (owner labels)

## Iteration 31: Golden seed and top-k retrieval
**Goal**: a Golden seed YAML of question/SQL/answer trios, validated and scope-filtered. Trios are scanned for PII and injection at load (SEC-15) and embedded with a cache keyed by content hash. Top-k retrieval feeds the analyst.
**Model**: T2 sonnet
**ACs covered**: AC-26.1, AC-26.2, AC-26.3, AC-26.4
**Files**:
- `src/opsfleet_agent/golden/seed.py`
- `config/golden_seed.yaml`: a recorded deviation, because the HLD names only a "Golden seed YAML" (open question Q-7)
- `tests/unit/test_golden.py`
- `pyproject.toml`, `uv.lock`, `requirements.txt`: only if the embedding client needs a new package. This is a serialized lock edit.
**Done criteria**:
- [ ] Named tests: `test_golden_seed_validation`, `test_golden_scope_filter`, `test_golden_embedding_cache_keyed_by_hash`, `test_golden_degrades_when_unavailable`, `test_golden_retrieval_topk`, `test_golden_trio_injection_scan`, `test_golden_trio_with_pii_or_injection_rejected`
- [ ] [std]
**Effort**: M (1) · **Depends on**: 2 (embedding spike), 15 · **Risk**: medium

## Iteration 32: `/feedback`
**Goal**: `/feedback` links a rating and a redacted comment to the turn's trace. Feedback is counted in `/metrics`.
**Model**: T2 sonnet
**ACs covered**: AC-25.1, AC-25.2, AC-25.3
**Files**:
- `src/opsfleet_agent/commands/feedback.py`
- `src/opsfleet_agent/store/feedback.py`
- `tests/unit/test_feedback.py`
**Done criteria**:
- [ ] Named tests: `test_feedback_linked_to_trace`, `test_feedback_comment_redacted`, `test_metrics_summary_includes_feedback`
- [ ] [std]
**Effort**: M (1) · **Depends on**: 4, 25 · **Risk**: low

### Wed 2026-10-07, if time allows: the drop-order iterations

These iterations run in the reverse of the drop order: the most protected item (40) first. The forecast in section 7 says items 1-7 are dropped.

## Iteration 40: Langfuse (drop 10)
**Goal**: an optional Langfuse callback through the same `drop_sensitive()` filter. It is off unless `LANGFUSE_*` is set.
**Model**: T2 sonnet
**ACs covered**: FR-64 (Langfuse half)
**Files**:
- `src/opsfleet_agent/obs/langfuse_sink.py`
- `tests/unit/test_langfuse_sink.py`
- `pyproject.toml`, `uv.lock`, `requirements.txt`: serialized, after 31
**Done criteria**:
- [ ] The sink receives no field that the JSONL sink would drop (reuses the `test_trace_redaction` fixture)
- [ ] [std]
**Effort**: M (1) · **Depends on**: 4, 25, 31 (lock order) · **Risk**: low

## Iteration 41: Persona smoke check and rollback (drop 9)
**Goal**: a persona change runs a smoke check, is audited, and can be rolled back.
**Model**: T2 sonnet
**ACs covered**: AC-27.4
**Files**:
- `src/opsfleet_agent/commands/persona.py`
- `tests/unit/test_persona_admin.py`
**Done criteria**:
- [ ] Named test: `test_persona_change_audited_and_rollback`
- [ ] [std]
**Effort**: S (0.5) · **Depends on**: 21, 26 · **Risk**: low

## Iteration 42: `access set` (drop 8)
**Goal**: an admin command changes a user's brand scope. The change is audited and takes effect at the next session.
**Model**: T2 sonnet
**ACs covered**: AC-20.6
**Files**:
- `src/opsfleet_agent/commands/access.py`
- `tests/unit/test_access.py`
**Done criteria**:
- [ ] Named test: `test_access_set_audited_and_effective_next_session`
- [ ] [std]
**Effort**: S (0.5) · **Depends on**: 16, 21 · **Risk**: medium

## Iteration 35: Erasure CLI 🔴 (drop 7)
**Goal**: one command erases all rows of one user from every store that exists. The audit record is written first. Erasure tolerates stores that were never created because their iteration was dropped.
**Model**: T1 opus, extra review (SEC-18)
**ACs covered**: AC-28.6
**Files**:
- `src/opsfleet_agent/commands/erase.py`
- `tests/unit/test_erase.py`
**Done criteria**:
- [ ] Named tests: `test_erase_audit_first_aborts_on_audit_failure`, `test_erase_removes_all_user_rows`
- [ ] The residue test of 23 is re-run for the erased user
- [ ] [std]
**Effort**: M (1) · **Depends on**: 21, 23 (and 32, 34, 39 when they exist) · **Risk**: high. **Rollback:** the command stays unregistered, and the owner is told. If 35 is dropped, the README documents the retention gap (SEC-18).

## Iteration 34: `/history` browse (drop 6)
**Goal**: a user browses their own past sessions, filtered by the current scope.
**Model**: T2 sonnet
**ACs covered**: AC-22.7
**Files**:
- `src/opsfleet_agent/commands/history.py`
- `tests/unit/test_history.py`
**Done criteria**:
- [ ] Named test: `test_history_author_only_and_scoped`
- [ ] [std]
**Effort**: M (1) · **Depends on**: 15, 19 · **Risk**: low

## Iteration 33: Rename, Markdown export, retry report (drop 5)
**Goal**: owner-only rename, Markdown export and retry of a saved report. Every action is audited. Retry reuses the ledger and runs no SQL.
**Model**: T2 sonnet
**ACs covered**: AC-21.9 (the supported half), AC-21.12, AC-21.14 (export clause), AC-21.15
**Files**:
- `src/opsfleet_agent/commands/report_actions.py`
- `tests/unit/test_report_actions.py`
**Done criteria**:
- [ ] Named tests: `test_rename_export_retry_owner_only_audited`, `test_retry_report_reuses_ledger_no_sql`
- [ ] [std]
**Effort**: M (1) · **Depends on**: 17, 18, 21 · **Risk**: low

## Iteration 36: Triage CLI (drop 4)
**Goal**: `add-eval` turns a feedback item into a case. `promote` adds a trio to the Golden seed only after a PII scan, a dry run and a green eval run.
**Model**: T2 sonnet
**ACs covered**: AC-25.4
**Files**:
- `src/opsfleet_agent/commands/triage.py`
- `tests/unit/test_triage.py`
**Done criteria**:
- [ ] Named tests: `test_add_eval_writes_case`, `test_promote_blocked_on_eval_regression`, `test_promote_runs_pii_scan_and_dry_run`, `test_triage_root_cause_rules`
- [ ] [std]
**Effort**: M (1) · **Depends on**: 27, 31, 32 · **Risk**: medium

## Iteration 37: Ranked full-text report search (drop 3)
**Goal**: FTS5 with BM25 ranking over the user's reports. User input is quoted, never passed as FTS syntax.
**Model**: T2 sonnet
**ACs covered**: AC-21.13 (FTS half)
**Files**:
- `src/opsfleet_agent/reports/fts.py`
- `tests/unit/test_fts.py`
**Done criteria**:
- [ ] Named tests: `test_search_ranked_fts_bm25`, `test_search_fts_query_syntax_quoted`
- [ ] The FTS table is added to the parametrized residue test of 23, with FTS `optimize` inside the delete transaction
- [ ] [std]
**Effort**: M (1) · **Depends on**: 18, 23, 33 (sequential, same store) · **Risk**: medium

## Iteration 38: Semantic search with RRF (drop 2)
**Goal**: embedding search fused with FTS by RRF. It is owner- and scope-filtered, and degrades to FTS.
**Model**: T2 sonnet
**ACs covered**: AC-21.13 (semantic half), AC-21.14 (search half, with 24)
**Files**:
- `src/opsfleet_agent/reports/semantic.py`
- `tests/unit/test_semantic.py`
**Done criteria**:
- [ ] Named tests: `test_search_semantic_degrades_to_fts`, `test_search_semantic_owner_and_scope`
- [ ] The vector table is added to the parametrized residue test of 23
- [ ] [std]
**Effort**: M (1) · **Depends on**: 31, 37 · **Risk**: medium

## Iteration 39: Preferences, P (drop 1)
**Goal**: per-user output preferences. They can be viewed and reset, persist across sessions, and never override safety or instruction precedence.
**Model**: T2 sonnet
**ACs covered**: AC-24.1, AC-24.2, AC-24.3, AC-24.4
**Files**:
- `src/opsfleet_agent/commands/preferences.py`
- `src/opsfleet_agent/store/preferences.py`
- `tests/unit/test_preferences.py`
**Done criteria**:
- [ ] Named tests: `test_preferences_view_reset`, `test_preference_cannot_override_safety`, `test_preference_notes_stored_injection`, `test_instruction_precedence`, `test_preferences_persist_and_apply`
- [ ] [std]
**Effort**: M (1) · **Depends on**: 15, 19 · **Risk**: low

### Thu 2026-10-08: final run, README, clean machine; Step 6 from 12:00

## Iteration 45: Final live eval run and results capture
**Goal**: one final live run on the shipped scope at **Thu 08:00**, before 44 (M-13): every flash-lite suite in full, and the flash part (Deep and report golden cases) only for the Thursday subset (section 5). The Wednesday-evening option is dropped: Wednesday's flash-lite budget has no room for it. Results go under `evals/results/`.
**Model**: T2 sonnet
**ACs covered**: AC-29.1..AC-29.6 (evidence)
**Files**:
- `evals/results/<date>/summary.md`
- `evals/results/<date>/results.jsonl` (redacted)
**Done criteria**:
- [ ] Every gate in `gates.py` is green, or the failing gate is reported to the owner with the trace ids. A gate is never edited to pass.
- [ ] At most 12 flash and about 340 flash-lite calls are used; 5 flash and about 30 flash-lite calls stay unused for the demo and one re-run
- [ ] `summary.md` reports the golden flash cases as the union of L2, L6a and L6 with the date and commit of each run, and lists every case that ran on the flash-lite fallback separately
- [ ] The Step 6 review file belongs to Step 6, not to this iteration (TR-22)
- [ ] [std]
**Effort**: M+ (1.5) · **Depends on**: 28b, 29, 30, 31, 32 and every shipped Wednesday iteration · **Risk**: medium

## Iteration 43: README, `.env.example`, `requirements.txt`, gitleaks
**Goal**: the project runs on a reviewer's machine from the README alone, on both install paths.
**Model**: T2 sonnet
**ACs covered**: none directly (NFR: reproducibility)
**Files**:
- `README.md`. It covers:
  - both install paths and the pinned spaCy model;
  - ADC and `GOOGLE_CLOUD_PROJECT`;
  - the demo script and the eval commands;
  - the drop list with each dropped item's ACs and HLD design;
  - the retention gap if 35 is dropped;
  - the "How I worked" section and the time log, started on Tuesday (SCH-8).
- `.env.example`: placeholders only
- `requirements.txt`: final export with the CLAUDE.md command
- `.github/workflows/ci.yml`: adds gitleaks (M-7)
**Done criteria**:
- [ ] gitleaks is clean on the full history
- [ ] The README contains no project id, account, local path or key
- [ ] [std]
**Effort**: M+ (1.5) · **Depends on**: 45 · **Risk**: low

## Iteration 44: Clean-machine run, both install paths
**Goal**: a fresh clone runs on both the uv and the pip path, with the README only.
**Model**: T2 sonnet
**ACs covered**: none directly
**Files**:
- `README.md` (fixes only)
**Done criteria**:
- [ ] Both paths:
  - start the CLI;
  - pass the startup check with one actionable line when the environment is missing;
  - run the offline test suite.
- [ ] [std]
**Effort**: M (1) · **Depends on**: 43 · **Risk**: medium

---

## 5. Live-call budget and eval milestones

Assumptions B and C apply. The limits below were read by the owner in AI Studio (T-2), and the owner chose to stay on the free tier (decisions, "Free-tier decision"). Before each run, the estimator of 27 prints the real request count per model and refuses an over-budget run. The limiter of 3 runs at 80% of RPM: 4 RPM for flash and 12 RPM for flash-lite.

**Daily ceilings (75% of the limit, so development calls and a demo still fit):**

| Resource | Limit (AI Studio, 2026-10-04) | Planned ceiling per day |
|---|---|---|
| `gemini-3.8-flash` (Deep analyst, Report writer) | 20 requests/day, 5 RPM, 250K TPM | 15 |
| `gemini-3.1-flash-lite` (router, light path, Quick analyst, verifier, Library agent, summary, judge, fallback) | 500 requests/day, 15 RPM, 250K TPM | 375 |
| `gemini-embedding-001`, 768 dimensions | not shown on the dashboard; the iteration-2 spike confirms it (Q-5) | 300 (assumed) |
| BigQuery bytes billed | 1 TB/month | 50 GB/day; per-query cap 1 GB and per-session cap 10 GB in code |

There is no pro row: the pro preview has no free tier, so Deep runs on flash (decisions, T-2 findings). Flash is the scarce resource: 20 requests a day is about 6 Deep questions. On a 429 the per-role fallback (HLD §4.0) answers on flash-lite and says so; an eval case that ran on the fallback is reported separately, not counted as a flash result.

**Cost of one full live run (estimate):**

| Suite | Cases | Flash calls | Flash-lite calls | BigQuery |
|---|---|---|---|---|
| `golden/*`, `quick` tag (judge included) | about 18 | 0 | about 75 (analyst, verifier, judge) | about 2 GB |
| `golden/*`, `deep` and `report` tags | about 12 | about 36 (about 3 per case) | about 45 (verifier and judge) | about 1 GB |
| `adversarial/*` except `pii_typed` and `differencing` | about 60 | 0 | about 80 (most are stopped before the LLM) | under 1 GB |
| `adversarial/pii_typed/*` | about 60 | 0 (local detector) | 0 | 0 |
| `adversarial/differencing/cross_session` | about 10 (2 turns each) | 0 | about 50 | under 1 GB |
| `resilience/*` | about 15 | 0 (fault injection with fakes) | 0 | 0 |
| router set | about 50 | 0 | about 50 | 0 |
| **Total (full run)** | | **about 36** | **about 300** | **about 5 GB** |

A full run does not fit one day: its flash part (36) is more than twice the flash ceiling. So the 12 Deep and report golden cases are spread over Tue-Thu (L2, L6a, L6), at most 15 flash calls a day, and the final run on Thursday runs the flash part only for its subset. Everything else runs in full on flash-lite. The gates do not change. The final report gives the golden flash count as the union of the three runs, with the date and commit of each, and says that it is not one run. The calibration judge run (30 flash-lite calls) is run once in L5 and stored; it is not part of a full run. At 12 RPM a full flash-lite pass takes about 30 minutes of wall clock.

**Milestones (live runs happen only here; everything else is offline):**

| Milestone | After | Subset | Requests (flash / flash-lite) |
|---|---|---|---|
| L0 | 2 (Sun) | model-id and embedding spike (owner runs it) | 1 / 1, plus 1 embedding |
| L1 | 15 (Tue) | 5 golden `quick` cases plus 3 adversarial SQL cases, smoke only (needs 15/16, M-3) | 0 / 30 |
| L2 | 17 (Tue) | 3 report-flow cases (draft, revise, save): the first part of the flash golden cases | 12 / 15 |
| L3 | 22b (Wed) | `adversarial/delete/*` live (about 8 cases, each through the supervisor) plus 5 scope cases | 0 / 55 |
| L4 | 28b (Wed) | `adversarial/*` and `resilience/*` full; golden `quick` tag | 0 / 200 |
| L5 | 30 (Wed) | calibration judge run (30) and router set (about 50) | 0 / 80 |
| L6a | 31-32 (Wed) | about 5 Deep and report golden cases with the Golden seed on: the second part | 15 / 20 |
| L6 | 45 (Thu 08:00) | final run: every flash-lite suite in full, plus the last 4 Deep and report golden cases | 12 / 300 |

**Day totals:**
- **Monday:** no milestone. Up to 15 flash calls are free for development smoke of the Deep analyst (14a). Development smoke of other roles runs on flash-lite; the Deep analyst and Report writer are smoke-tested on flash only within these 15.
- **Tuesday:** 12 flash / 45 flash-lite. 3 flash calls are left for development.
- **Wednesday:** L3 + L4 + L5 + L6a = 15 flash / 355 flash-lite. Both are at the ceiling, so no flash development calls happen that day.
- **Thursday:** the final run (12 / 300) plus a reserve of 5 flash and about 30 flash-lite calls for the demo and one re-run of a failing case: 17 of 20 flash and about 330 of 500 flash-lite (M-13).

If a day's flash quota runs out early, the remaining flash cases move to the next milestone with the `deep` cases first. The gates do not change, and a lower golden count is reported, not hidden.

## 6. Owner tasks

| # | Task | Effort | Blocks | Needed by |
|---|---|---|---|---|
| T-1 | **Label the 30 judge-calibration cases** in `evals/calibration/cases.yaml`: a score of 1-5 and a one-line reason per case. The cases are synthetic. | about 30-40 min | Iteration 30, and through it whether the golden judge scores count | Handed over **Mon 2026-10-05 midday**; labels needed by **Wed 2026-10-07 12:00**. If they are late, the golden scores are reported "uncalibrated" |
| T-2 | ✅ Done 2026-10-04: limits read in AI Studio, free tier kept (assumption B, section 5). Still open: decide on the embedding G2 amendment if the iteration-2 spike fails (Q-5). | 10 min | Section 5 budget and L1 | Before L1 (Tue morning) |
| T-3 | Make `GOOGLE_CLOUD_PROJECT`, ADC login, `GEMINI_API_KEY` and `LANGGRAPH_AES_KEY` available in your own shell or `.env`. Never paste them into chat. | 10 min | L0 spike, L1 | Sun evening (L0) |
| T-4 | Review each 🔴 iteration before its commit, together with the second T1 review | about 15 min each | the next 🔴 iteration | continuous |
| T-5 | Acknowledge drop-order notifications. This is notification only: the owner is told, not asked. | minutes | none | continuous |

## 7. Schedule

**Capacity** (assumption A, factor 1.5 kept by owner decision 3): Sun 12, Mon 15, Tue 15, Wed 15, Thu 10, total **67 effort-hours**. The deadline is assumed to be **end of day Thu 2026-10-08** (open question Q-6).

**Demand:**
- iterations: 68.0 h;
- Step 6 (final review), fixes and G4: 3.5 h, ring-fenced from **Thu 12:00** (SCH-8);
- total **71.5 h**.

| Scope | Demand | Capacity | Buffer |
|---|---|---|---|
| Full (all 49 iterations) | 71.5 h | 67 h | **−4.5 h** (does not fit) |
| Minimum shippable (all 10 drop items dropped, owner decision 2) | 62.5 h | 67 h | **+4.5 h (6.7%)** |
| Minimum plus volume cut (golden ≈ 15, router ≈ 25, static persona, session caps only, JSONL-only traces) | about 60.5 h | 67 h | about +6.5 h |

**Forecast day plan:**

| Day | Iterations | Effort | Capacity | Note |
|---|---|---|---|---|
| Sun 10-04 | 1, 2, 3 ∥ 4, 5, 6, 8a | 11.0 h | 12 | 1 h slack |
| Mon 10-05 | 7, 8b, 9, 10, 12, 11, 13, 16, 20 | 14.5 h | 15 | 20 handed to the owner at midday |
| Tue 10-06 | 14a, 14b, 15, 17, 18, 19, 21, 27 | 14.5 h | 15 | "How I worked" and the time log start |
| Wed 10-07 | 22a, 22b, 23, 24, 25, 26, 28a, 28b, 29, 30, 31, 32 | 15.0 h | 15 | 31 and 32 go first among the Wednesday features; then drop items 10, 9, 8 (40, 41, 42) only if a stream is free |
| Thu 10-08 | 45 (08:00), 43, 44 until 12:00; Step 6, fixes and G4 from 12:00 | 4.0 h + 3.5 h | 10 | 2.5 h reserve |
| **Total** | | **59.0 h + 3.5 h = 62.5 h** | **67** | **+4.5 h** |

**Forecast drops (R1):** in drop order, items 1-7 (39, 38, 37, 36, 33, 34, 35; 7 h) are forecast to be dropped. Items 8-10 (42, 41, 40; 2 h) fit only if Sunday to Wednesday run on plan. Under the reviewers' blended factor (about 1.2x for Sunday to Tuesday, R1), capacity falls by about 8 h. In that case all ten items drop and the volume cut applies.

**Checkpoints (iteration numbers plus a demo script; M-10):**
- **Sun 2026-10-04, 22:00, tripwire:** if 6 is not green, drop items 1-3 (39, 38, 37) apply at once. These are the first three under the new order; the review named 38/37/36 under the old order.
- **Mon 2026-10-05, 22:00 (owner decision 3, unchanged):**
  - target: iterations 1-19 green, plus the manual demo: ask → answer, a report with confirmation, list and view;
  - forecast: iterations 1-13, 16 and 20 green; the target is reached around Tue midday (a slip of about 9 h, R1);
  - because the checkpoint is missed, the drop order applies automatically, and the owner is told.
- **Tue 2026-10-06, 22:00 (decisions, Step 4 checkpoints):**
  - target: tiers 3-4, the delete flow and the evals green (iterations 20-30);
  - forecast: 14a-21 and 27 green; the delete flow and the suites follow on Wednesday;
  - the drop order continues automatically.
- **Wed 2026-10-07, 22:00:** 22a-32 green; L3-L6a recorded; the remaining drop items decided.
- **Thu 2026-10-08, 12:00:** 45, 43 and 44 done; Step 6 starts. Submission follows G4.

**Drop order (owner decision 1, `docs/decisions.md` Step 4b). It applies automatically at a missed checkpoint. The owner is told, not asked, and the README lists every drop.**

| Order | Item | Iteration | What stays |
|---|---|---|---|
| 1 | FR-42, FR-43, FR-44 (the P items: preferences) | 39 | nothing; the README describes the HLD design |
| 2 | FR-74 semantic half | 38 | FTS5 ranking (37), or substring search (18) |
| 3 | FR-74 FTS ranking | 37 | FR-73 substring search (18) |
| 4 | FR-47 triage CLI (promote, add-eval) | 36 | `/feedback` (32) |
| 5 | FR-37, FR-38, FR-40 (rename, Markdown export, retry report) | 33 | save, list, view, delete |
| 6 | FR-14 `/history` | 34 | narrow `--resume` (19) |
| 7 | FR-59 erasure CLI (🔴) | 35 | audited hard delete (22a, 23); the README documents the retention gap (SEC-18) |
| 8 | FR-08 `access set` | 42 | `config/profiles.yaml` (16) |
| 9 | FR-52 persona smoke check and rollback | 41 | "an invalid file keeps the last valid one" (26) |
| 10 | Langfuse | 40 | JSONL tracer and trace viewer (4, 25) |
| beyond 10 | **Volume cut** (owner decision 2): golden ≈ 15 cases, router ≈ 25 messages, static persona, session caps only, JSONL-only traces | 29, 26, 3 | **every gate stays** |

**Minimum shippable product (owner decision 2):**
- tiers 0-1 (1-13);
- Q&A through 14a/14b/15, plus 16 and 19;
- reports save, list and view (17-18);
- delete with audit and no residue (21-23);
- degraded mode and quotas (24);
- trace viewer (25) and persona (26);
- offline adversarial and resilience suites (27-29);
- calibration (20/30);
- seed (31) and `/feedback` (32);
- README and the clean-machine run (43-45).

**Non-droppable:** every iteration not in the drop order (rule, M-5). This includes the rev. 4.4 closures 8a/8b, 10, 22a/22b, 23 and 20/30.

## 8. Dependencies and configuration

- **One dependency change (M-12):**
  - Iteration 1 adds every known runtime and dev dependency and exports `requirements.txt`.
  - Only 31 (if the embedding client needs a package) and 40 (Langfuse) touch the lock later, one after the other.
  - CI checks the export is in sync from iteration 2.
  - Export command, the same as CLAUDE.md (TR-26): `uv export --no-hashes --format requirements-txt > requirements.txt`.
- **spaCy:** `en_core_web_sm` is pinned to the release that matches the spaCy minor version, as a direct URL dependency in `pyproject.toml` and `uv.lock`. Startup fails with one actionable line if it is missing (`test_pii_detector_missing_model_fails_startup`). Both install paths are checked in 44.
- **Pins:** `sqlglot>=30,<31` (iteration 2 verifies); `requires-python = ">=3.12,<3.14"`. Other versions are what `uv add` resolves in iteration 1; this plan invents none.
- **Environment:**
  - `GOOGLE_CLOUD_PROJECT` is never hard-coded.
  - ADC via `gcloud auth application-default login`.
  - `GEMINI_API_KEY` and `LANGGRAPH_AES_KEY` live in `.env`, never printed, logged or committed. Config is logged by allowlist.
  - `K_delete` is generated in memory at start and never stored.
  - `.env.example` has placeholders only. `LANGFUSE_*` is optional.
- **Layout (HLD §2):**
  - `src/opsfleet_agent/{graph,roles,tools,guards,bq,store,obs}/`
  - `prompts/`
  - `config/models.yaml`, `config/profiles.yaml`
  - `evals/cases/<category>/*.yaml`, `evals/run.py`, `evals/calibration/`, `evals/results/`
  - `tests/unit/`, `tests/live/`
  - Recorded deviations: `config/golden_seed.yaml`, because the HLD says only "Golden seed YAML" (Q-7), and `src/opsfleet_agent/{delete,reports,golden,commands}/` subpackages, added to keep the 🔴 modules small.
- **Safety invariants checked by tests:**
  - Every query is dry-run and capped: 5 and 13.
  - Every loop and retry is bounded: 3 and 14a.
  - A delete writes its audit record first and aborts if that fails: 21, 22a and 35.
  - Unit tests make no network call: 1.

## 9. Traceability

Mapping rule: each AC maps to the iteration that implements the control. A second iteration is listed when an eval or a later wiring completes it. The last column gives the canonical named tests and eval cases (B-2). Eval case paths are relative to `evals/cases/`.

| AC | Iteration(s) | Named tests / eval cases |
|---|---|---|
| 01.1 | 14a, 29 | `golden/top_customers` |
| 01.2 | 14a, 29 | `golden/aov_by_traffic_source` |
| 02.1 | 14a, 29 | `golden/compare_brands_why` |
| 02.2 | 14a, 29 | `golden/show_sql` |
| 02.3 | 14a | `test_grounding_accepts_prior_turn_ledger`, `test_grounding_rejects_unknown_number`, `test_grounding_rounding` |
| 03.1 | 14a, 29 | `golden/monthly_revenue_12m` |
| 03.2 | 14a, 29 | `golden/ytd_revenue_by_brand` |
| 04.1 | 13, 29 | `test_schema_tool_hides_pii_columns`, `golden/schema_overview` |
| 04.2 | 13, 29 | `golden/inventory_unavailable` |
| 05.1 | 14a, 29 | `golden/state_underspend_compare` |
| 05.2 | 14a, 29 | `golden/churn_last_month`, `golden/churn_user_definition` |
| 06.1 | 17, 29 | `test_report_saved_with_owner_and_session`, `test_save_only_on_confirm`, `golden/q1_report`, `golden/report_save_confirm` |
| 06.2 | 17, 29 | `golden/q1_report` |
| 06.3 | 18 | `test_list_reports_owner_only` |
| 06.4 | 17 | `test_report_cancel_saves_nothing`, `test_revise_starts_new_turn` |
| 06.5 | 17 | `test_report_save_idempotent`, `test_resume_reshows_draft` |
| 07.1 | 15, 29 | `golden/followup_breakdown` |
| 07.2 | 15, 29 | `golden/followup_why_march` |
| 08.1 | 6 | `test_sql_policy_rejects_pii_projection` |
| 08.2 | 8a, 8b | `test_output_filter_redacts_email_phone_address` |
| 08.3 | 4 | `test_trace_redaction` |
| 08.4 | 6, 11, 28a | `test_sql_policy_rejects_pii_projection`, `adversarial/pii_injection_emails` |
| 08.5 | 9, 28a | `adversarial/pii_derived` |
| 08.6 | 9, 28a | `test_small_cell_group_by_qi`, `adversarial/small_cell_thin_brand` |
| 08.7 | 10, 28a | `test_differencing_guard_session`, `test_differencing_guard_across_sessions`, `adversarial/differencing/cross_session`, `adversarial/differencing_complement` |
| 08.8 | 9, 28a | `test_small_cell_after_brand_scope`, `test_small_cell_counts_in_scope_population`, `test_small_cell_rejects_qi_in_value_aggregate`, `adversarial/qi_conditional_aggregate`, `adversarial/qi_list_intersection`, `adversarial/qi_listing_aggregate` |
| 08.9 | 9, 28a | `test_signup_timestamp_is_qi`, `adversarial/signup_timestamp_linkage` |
| 08.10 | 6, 28a | `test_cte_shadowing_rejected`, `test_policy_rejects_select_as_struct`, `test_policy_rejects_table_alias_as_value`, `adversarial/table_alias_struct` |
| 08.11 | 5, 13, 28a | `test_bq_error_is_mapped_not_forwarded`, `adversarial/bq_error_value_echo`, `adversarial/pii/error_oracle_city` |
| 08.12 | 7 | `test_all_scope_counts_non_buyers` |
| 08.13 | 6 | `test_policy_rejects_qi_predicate_at_id_grain` |
| 08.14 | 8b, 28a | `test_brand_allowlist_not_masked`, `test_output_guard_uses_ner_detector`, `test_pii_detector_missing_model_fails_startup`, `test_typed_pii_ner_masks_person_and_address`, `adversarial/pii_typed/brand_false_positive` |
| 08.15 | 10, 28a | `test_differencing_guard_across_sessions`, `test_fingerprint_retention_30_days`, `test_fingerprint_store_has_no_values`, `adversarial/differencing/cross_session` |
| 09.1 | 7, 28a | `test_scope_filter_cannot_be_bypassed`, `adversarial/out_of_scope_brand` |
| 09.2 | 7 | `test_scope_filter_cannot_be_bypassed` |
| 09.3 | 7, 28a | `adversarial/total_revenue_scoped` |
| 09.4 | 16, 28a | `test_scope_from_profile_only`, `adversarial/scope_escalation` |
| 09.5 | 15 | `test_context_scope_filter_drops_out_of_scope` |
| 09.6 | 19, 28a | `test_resume_scope_drift_new_session`, `adversarial/scope_shrink_history` |
| 10.1 | 6, 28a | `test_sql_policy_select_only`, `adversarial/ignore_instructions_drop` |
| 10.2 | 6 | `test_sql_policy_select_only`, `test_sql_policy_table_allowlist` |
| 10.3 | 28a | `adversarial/indirect_injection` |
| 10.4 | 12, 28a | `test_output_guard_allowlist_fail_closed`, `adversarial/output_unexpected_tool` |
| 10.5 | 12, 28a | `test_output_injection_scan`, `adversarial/answer_injection_url` |
| 10.6 | 12, 28a | `adversarial/data_instruction_following` |
| 10.7 | 8a, 14a | `test_user_typed_email_not_persisted` |
| 11.1 | 11, 28a | `adversarial/off_topic_poem`, `adversarial/off_topic_weather` |
| 11.2 | 11, 28a | `adversarial/prompt_exfiltration` |
| 11.3 | 11, 29 | `test_light_path_no_sql_no_embedding`, `golden/smalltalk_light_path` |
| 11.4 | 11, 29 | `golden/smalltalk_then_task` |
| 11.5 | 11, 28a | `test_light_path_runs_guards`, `adversarial/smalltalk_injection` |
| 11.6 | 11 | `test_light_path_no_sql_no_embedding` |
| 12.1 | 22a, 28b | `test_delete_requires_confirm`, `adversarial/delete/flow_preview` |
| 12.2 | 22a | `test_delete_confirm_deletes_exact_previewed_set` |
| 12.3 | 22a | `test_delete_cancel_on_non_confirm` |
| 12.4 | 22a | `test_delete_owner_only` |
| 12.5 | 22a, 28b | `test_delete_by_session_id`, `adversarial/delete/session_reports` |
| 12.6 | 22a | `test_delete_confirmation_bound_to_preview_set` |
| 12.7 | 22a | `test_llm_cannot_trigger_delete_without_user_turn` |
| 12.8 | 22a | `test_delete_no_matches` |
| 12.9 | 22a, 28b | `adversarial/delete/preconfirmed` |
| 12.10 | 22b | `test_expired_confirm_routes_to_input_guard_and_audits` |
| 12.11 | 22a | `test_delete_preview_includes_backup_notice` |
| 12.12 | 22a, 28b | `test_delete_refused_after_view_same_turn`, `test_delete_requires_intent_in_user_message`, `adversarial/library_view_then_delete` |
| 12.13 | 22a | `test_confirm_delete_never_deletes`, `test_confirm_delete_rerun_keeps_token` |
| 12.14 | 18, 22a | `test_matcher_rejects_empty_and_wildcards` |
| 12.15 | 22a, 22b | `test_confirm_delete_on_other_instance_verifies`, `test_delete_token_derived_not_stored`, `test_key_rotation_expires_pending_delete` |
| 12.16 | 23 | `test_hard_delete_leaves_no_residue_in_db_file` |
| 13.1 | 14a | `test_self_correction_bounded` |
| 13.2 | 13, 28b | `test_empty_result_handling`, `resilience/empty_result_typo_category` |
| 13.3 | 14a | `test_cli_survives_tool_failure` |
| 14.1 | 5 | `test_cost_cap_rejects_before_execution`, `test_job_config_sets_max_bytes_billed` |
| 14.2 | 5 | `test_session_budget` |
| 14.3 | 5, 13 | `test_memo_misses_after_refresh_date_change` |
| 15.1 | 3, 24 | `test_retry_then_fallback` |
| 15.2 | 24 | `test_all_models_down_graceful` |
| 15.3 | 1, 16 | `test_startup_config_check` |
| 15.4 | 14a | `test_malformed_tool_call_handled` |
| 15.5 | 5, 19, 22b | `test_sigint_cancels_bq_job_and_expires_pending` |
| 15.6 | 3 | `test_retry_wrapper_bounded`, `test_role_subcap_counts_retries_and_fallback` |
| 16.1 | 4, 25 | `test_trace_has_all_span_types` |
| 16.2 | 25 | `test_trace_viewer_renders_failed_span` |
| 16.3 | 25 | `test_metrics_summary` |
| 16.4 | 4 | `test_trace_spans_carry_no_pii` |
| 20.1 | 16 | `test_cli_banner_shows_user_scope_session` |
| 20.2 | 16 | `test_cli_rejects_unknown_user` |
| 20.3 | 16 | `test_profile_scope_validation` |
| 20.4 | 16, 29 | `golden/my_scope` |
| 20.5 | 19 | `test_cli_commands` |
| 20.6 | 42 (drop 8) | `test_access_set_audited_and_effective_next_session` |
| 20.7 | 22b | `test_idle_timeout_drops_pending_delete` |
| 21.1 | 17, 29 | `test_report_schema_required_sections`, `golden/q1_report` |
| 21.2 | 17, 29 | `test_save_last_answer_as_report`, `golden/save_this` |
| 21.3 | 18 | `test_view_report_owner_only` |
| 21.4 | 18 | `test_list_filter_matches_delete_matcher` |
| 21.5 | 18 | `test_list_masks_drifted_report_title`, `test_view_report_scope_drift` |
| 21.6 | 18, 22a | `test_session_delete_excludes_viewed_reports` |
| 21.7 | 22a | `test_large_delete_preview_truncated_but_bound` |
| 21.8 | 17 | `test_report_store_atomic_and_concurrent` |
| 21.9 | 29 (33 for the supported actions) | `golden/roadmap_actions_unsupported` |
| 21.10 | 18, 29 | `test_report_search_filters`, `test_search_reports_owner_and_scope`, `golden/report_search` |
| 21.11 | 18 | `test_search_results_not_delete_targets` |
| 21.12 | 33 (drop 5) | `test_rename_export_retry_owner_only_audited` |
| 21.13 | 37, 38 (drop 3, 2) | `test_search_ranked_fts_bm25`, `test_search_semantic_degrades_to_fts`, `test_search_semantic_owner_and_scope` |
| 21.14 | 24 (export clause only if 33 ships) | `test_degraded_mode_lists_and_searches_reports_when_llm_down` |
| 21.15 | 33 (drop 5) | `test_retry_report_reuses_ledger_no_sql` |
| 22.1 | 15 | `test_history_window_bounded` |
| 22.2 | 15, 29 | `test_new_session_has_empty_history`, `golden/cross_session_memory` |
| 22.3 | 18, 29 | `golden/discuss_saved_report` |
| 22.4 | 3, 14a | `test_turn_caps_enforced` |
| 22.5 | 3, 14a | `test_turn_deadline` |
| 22.6 | 14b, 17, 19, 22b | `test_resume_after_crash_each_node`, `test_resume_reshows_draft`, `test_resume_rejects_other_users_session`, `test_resume_scope_drift_new_session`, `test_resume_expires_pending_delete` |
| 22.7 | 34 (drop 6) | `test_history_author_only_and_scoped` |
| 22.8 | 24, 28b | `test_quota_blocks_after_limit`, `resilience/quota_exhausted` |
| 23.1 | 15, 29 | `golden/stated_assumption_defaults` |
| 23.2, 23.3 | 15, 29 | `golden/clarify_unresolved_reference` |
| 23.4 | 11, 28a | `adversarial/offtopic/non_english_rephrase`, `adversarial/pii_injection_non_english` |
| 23.5 | 13 | `test_large_result_truncation_flagged` |
| 24.1 (P) | 39 (drop 1) | `test_preferences_view_reset` |
| 24.2 (P) | 39, 28a | `test_preference_cannot_override_safety`, `adversarial/preference_policy_override` |
| 24.3 (P) | 39 | `test_preference_notes_stored_injection` |
| 24.4 (P) | 39 | `test_instruction_precedence` |
| 25.1 | 32 | `test_feedback_linked_to_trace` |
| 25.2 | 32 | `test_feedback_comment_redacted` |
| 25.3 | 32 | `test_metrics_summary_includes_feedback` |
| 25.4 | 36 (drop 4) | `test_add_eval_writes_case`, `test_promote_blocked_on_eval_regression`, `test_promote_runs_pii_scan_and_dry_run` |
| 26.1 | 31 | `test_golden_seed_validation` |
| 26.2 | 31 | `test_golden_scope_filter` |
| 26.3 | 31 | `test_golden_embedding_cache_keyed_by_hash` |
| 26.4 | 31 | `test_golden_degrades_when_unavailable` |
| 27.1 | 26 | `test_persona_version_in_trace` |
| 27.2 | 26 | `test_persona_invalid_keeps_last_valid` |
| 27.3 | 26, 28a | `adversarial/persona_policy_override` |
| 27.4 | 41 (drop 9) | `test_persona_change_audited_and_rollback` |
| 28.1 | 21, 22a | `test_audit_delete_lifecycle` |
| 28.2 | 21, 22a | `test_delete_aborts_when_audit_write_fails` |
| 28.3 | 21 | `test_audit_guardrail_refusal_no_pii` |
| 28.4 | 21, 28b | `test_audit_append_only`, `adversarial/delete/audit_log` |
| 28.5 | 21 | `test_audit_viewer` |
| 28.6 | 35 (drop 7) | `test_erase_audit_first_aborts_on_audit_failure`, `test_erase_removes_all_user_rows` |
| 29.1 | 27 | `test_eval_runner_gates_exit_code` |
| 29.2 | 27 | `test_eval_runner_request_estimate` |
| 29.3 | 27 | `test_eval_results_link_traces` |
| 29.4 | 27, 29 | `test_judge_rubric_versioned`, `golden/q1_report` |
| 29.5 | 27, 28b | `test_eval_gate_fails_on_any_delete_case`, `resilience/quota_exhausted`, `resilience/schema_drift` |
| 29.6 | 20, 30 | `test_judge_calibration_gate_blocks_on_low_agreement` |

**Cases that are cited in the HLD or decisions but are not on an AC line:**

| Case | Iteration | Note |
|---|---|---|
| `adversarial/delete/expired_reply` | 28b | |
| `adversarial/injection/library_view_then_delete` | 28b | |
| `adversarial/injection/stored_report_injection` | 28a | |
| `golden/persona_tone_change` | 29 | needs 41 |
| `golden/preference_table_vs_bullets` | 29 | needs 39 |
| HLD extras: `adversarial/injection/markdown_image_exfil`, `adversarial/injection/persona_override`, `adversarial/injection/golden_poisoned_trio`, `adversarial/injection/router_label_injection`, `adversarial/injection/output_action_injection`, `adversarial/delete/delete_other_users_report`, `adversarial/differencing/differencing_session` | 28a / 28b | |

**FR to iteration (M and P, 64):**

| FR | Iteration | FR | Iteration | FR | Iteration |
|---|---|---|---|---|---|
| 01-05 | 16 | 33 | 22a, 22b | 62 | 1, 3, 16, 24 |
| 06 | 19, 32 | 34, 35 | 18 | 63 | 24 |
| 08 | 42 (drop 8); profiles file 16 | 36 | 17 | 64 | 4, 25, 40 |
| 09 | 16, 21 | 37, 38, 40 | 33 | 65 | 27 |
| 10 | 16 | 42-44 | 39 | 66 (calibration) | 20, 30 |
| 11, 12 | 15 | 46 | 32 | 69 | 9 |
| 13 | 15 | 47 | 36 | 70 | 10 |
| 14 | 14b, 19 (resume), 34 (browse) | 48 | 31 | 71 | 11 |
| 15, 16 | 15 | 51 | 26 | 72 | 17 |
| 17, 18 | 11 | 52 | 26, 41 | 73 | 18 |
| 19 | 19 | 54, 55 | 21, 22a | 74 | 37, 38 |
| 20-22 | 13, 14a | 56 | 21 | 75 | 12 |
| 23 | 13 | 58 | 24 | 76 | 15 |
| 24 | 13 | 59 | 35 | | |
| 25 | 3, 14a | 60 | 14a | | |
| 28-32 | 17, 18 | 61 | 5, 13 | | |

**HLD-only requirements, not covered by iterations:**
- platform: FR-07, 57, 67, 68;
- roadmap: FR-26, 27, 45, 49, 50, 53;
- retired: FR-39, 41.

**Unmapped items:** none among the 61 M ACs and 3 P requirements. Rev. 1 note (a) is removed: every AC above was re-checked against its body in the requirements, and the named tests and cases come from the AC "Verified by" lines, the HLD and the decisions.

## 10. Risk register for the plan

| # | Risk | Likelihood | Impact | Mitigation / trigger |
|---|---|---|---|---|
| R1 | **Schedule slip (owner decision 3).** The full scope is 4.5 h over capacity even at 1.5x. The Monday target (1-19 plus demo) is forecast to slip by about 9 h. At the reviewers' blended factor (about 1.2x Sun-Tue, the owner reviewing every 🔴 iteration) capacity falls by about 8 h more. | high | the Monday and Tuesday checkpoints are missed; Wednesday features are dropped | Owner decisions 1-2 are the mitigation: P items drop first, the minimum scope, then the volume cut that keeps the gates. Measure actual against plan after iterations 1-4. Sunday-night tripwire. Automatic drops at each missed checkpoint, and the owner is told. |
| R2 | 6, 7, 8b or 9 overrun; they gate 13, 14a and everything after | medium | half a day shifts | T1 on all; spikes in 2; 8a/8b run in parallel with 6/7; a red closure stops and escalates, and nothing is weakened |
| R3 | The delete flow (22a, 22b, 23) is not green by Wed 16:00; it cannot be dropped | medium | Wednesday features and the volume of evals shrink | 22a starts first on Wednesday; 24/25 run in parallel; drop items go in order; if still red, the delete feature stays unregistered (fail closed) and the owner decides at G4 |
| R4 | Free-tier flash is 20 requests/day and 5 RPM (T-2). A reviewer with a free key runs out after about 6 Deep questions | high | Deep and report evals split over days, a smaller golden flash count; the reviewer's demo falls back to flash-lite | Flash only for Deep and the Report writer (HLD §4.0); per-role fallback to flash-lite on 429 with a visible notice; the limiter at 80% RPM; the estimator (27) refuses over-budget runs; fallback cases reported separately; the README states the free-tier limits and that Deep and reports are weaker on the fallback; the gates are unchanged |
| R5 | The T-1 labels arrive after Wed 12:00 | medium | the calibration gate is missed | Handed over Mon midday; golden scores are reported "uncalibrated" until the labels arrive |
| R6 | Calibration agreement is below 80% | low-medium | golden judge scores do not count | One rubric fix and a re-judge (30 calls); the gate is never weakened; a second miss is the owner's decision |
| R7 | Presidio masks brand names, or `en_core_web_sm` cannot be pinned for both pip and uv | medium | the `pii_typed` gate fails; install breaks on a clean machine | Allowlist built from the brand list in code; no PERSON-only narrowing (fail closed instead); install checked in 8b and 44 |
| R8 | The sqlglot BigQuery dialect cannot express a rewrite | low-medium | 6, 7 and 9 need a redesign | Spike in 2; stop and escalate (no fallback form exists in ADR-008) |
| R9 | `gemini-embedding-001` is not usable on a free key | medium | Golden seed retrieval (31) and semantic search (38) are blocked | Spike in 2; `gemini-embedding-2` at 768 is proposed to the owner as a G2 amendment; 31 degrades by design (`test_golden_degrades_when_unavailable`) |
| R10 | A live run or a dev call prints or logs a secret | low | credential leak | Config logged by allowlist; sentinel test (4); `drop_sensitive()` for every sink; gitleaks (43) |
| R11 | The reviewer's environment differs (Python 3.13, pip path, no GCP project) | medium | the clean-machine run fails | `requires-python >=3.12,<3.14`; 44 runs both paths; the startup check gives one actionable line |
| R12 | Scope creep during implementation | medium | the buffer is consumed | Surface to the owner as add now, defer to the README or skip; nothing is added silently |
| R13 | Gemini or BigQuery outage on demo day | low | the live demo fails | Offline tests; a recorded demo output in the README; degraded mode (24) |
| R14 | A T1 reviewer finds HIGH or CRITICAL issues in Step 6 | medium | Thursday overruns | Step 6 is ring-fenced from Thu 12:00 with 3.5 h plus 2.5 h reserve; every 🔴 iteration was already reviewed |
| R15 | Two streams edit a hot file at once | medium | merge conflicts and broken commits | The hot-file list and the command table (section 3); lock edits only in 1, 31 and 40, serially |
| R16 | Erasure (35) is in the forecast drops although it is 🔴 | medium | a retention gap at submission | The README documents the gap (SEC-18). The owner may move 35 behind 40 in the drop order (Q-8). |

## 11. Self-check

- [x] Iteration 1 has no blockers and adds every known dependency
- [x] `run_sql` follows the HLD §5.1 order, with a named order test (B-1)
- [x] Every canonical `test_*` name and every eval case path from the requirements, the HLD and the decisions appears in a done criterion (B-2; checked mechanically, see the report to the orchestrator)
- [x] The guards (5-13) precede the agent loop (14a); the checkpointer precedes 17; the delete halves of 16 and 19 are in 22b
- [x] The 🔴 iterations (5, 6, 7, 8a, 8b, 9, 10, 11, 12, 13, 14b, 15, 17, 19, 21, 22a, 22b, 23, 35) are T1 with a second T1 review, and their rollbacks fail closed and escalate
- [x] Every iteration not in the drop order is non-droppable
- [x] Total: 68.0 h of iterations plus 3.5 h for Step 6/G4 = 71.5 h against 67 h. Full scope is −4.5 h; minimum scope is +4.5 h.
- [x] Live eval runs happen only at milestones L0-L6, there is no pro row, flash stays within 15 a day, and 5 flash plus about 30 flash-lite calls are kept in reserve on Thursday

## 🔴 G3: approved by the owner on 2026-10-04

The owner approved rev. 2 explicitly in chat. Q-7, Q-8 (order kept; the retention gap is documented if 35 is dropped), Q-9 and Q-10 are accepted as proposed. Q-3, Q-5 and Q-6 stay open as listed below. What was checked:

1. **B-1:** the `run_sql` order in iteration 13 matches HLD §5.1, and `test_run_sql_order_matches_hld_5_1` proves it.
2. **The 🔴 iterations and their rollbacks:** every rollback fails closed and escalates to you. None narrows a closure.
3. **The time budget:**
   - full scope does not fit (−4.5 h);
   - the forecast drops items 1-7 of your drop order;
   - the Monday target (kept) is forecast to slip by about 9 h (R1).
4. **Your tasks (section 6):** T-1 by Mon midday / Wed 12:00, T-2 before L1, T-3 on Sunday evening, T-4 reviews.
5. **Traceability (section 9):** each AC with its named tests and cases.

**Closed questions from rev. 1:**
- Q-1: P items first in the drop order (owner decision 1).
- Q-2: the Monday checkpoint is 1-19 plus the demo; the delete flow is on Tuesday by the decisions (owner decision 3).
- Q-4: the QI set is in the small-cell module, per HLD §5.2 (owner decision 5).

**Open questions for the owner:**
- ~~**Q-3:**~~ closed 2026-10-04 after G3: the owner read the limits in AI Studio and chose to stay on the free tier. Section 5 and R4 are amended; the Quick analyst returns to flash-lite per HLD §4.0 (decisions, "Free-tier decision").
- **Q-5:** if the iteration-2 spike shows `gemini-embedding-001` does not work on a free key, do you approve `gemini-embedding-2` at 768 dimensions as a G2 amendment?
- **Q-6:** is the deadline end of day Thu 2026-10-08, or a specific hour? Step 6 is ring-fenced from 12:00 on that assumption.
- **Q-7:** `config/golden_seed.yaml` is a naming deviation: the HLD says only "Golden seed YAML". Accept it or name the path.
- **Q-8:** erasure (35, 🔴) is drop item 7 and falls inside the forecast drops. Keep the order (and document the retention gap), or move 35 behind 40?
- **Q-9:** do you accept the R1 forecast (the Monday target missed by about 9 h, with automatic drops), given that decision 3 keeps the target?
- **Q-10:** do you accept the letter splits (8a/8b, 14a/14b, 22a/22b, 28a/28b) in place of renumbering?

Approved by: owner, in chat, on 2026-10-04
