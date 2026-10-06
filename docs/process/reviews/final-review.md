# Final Review: iteration 39b (HEAD 812b29d) — README, deliverables and overall verdict

Date: 2026-10-06 · Reviewer: T1 (multi-agent fan-out: README-vs-assignment, README-vs-code C1/C2, agent flow, security A/B; every HIGH/MEDIUM anchor re-verified in the main session) · **Verdict: BLOCKED** (recommendation to the owner; 🔴 G4 stays with the owner)

> Verdicts (final-reviewer.md): APPROVED · APPROVED WITH CONDITIONS (only LOW gaps) · BLOCKED (missing deliverable, unverified MUST, failed clean run, or an open HIGH/CRITICAL). Finding ids: `RA-n` = README vs the assignment, `RC-n` = README vs code, `AF-n` = agent flow (`code-review.md`), `SEC-An/Bn` = security (`security-review.md`).

## 0. Scope and method

| Reviewed revision | |
|---|---|
| Branch / commit | `step5-implementation` @ `812b29d` (== `main`, public repo verified), working tree clean |
| `README.md` | 1052 lines, md5 `7f45e45406d318dea8149c744d93b26e` |
| `docs/technical.md` | md5 `a21ceecb09db639e76bacc1b223f71cf` |
| `docs/architecture.md` | md5 `113f6583542ba57e5d0ad338101be3c1` |
| Assignment | the owner's local copy of the OpsFleet take-home (source of truth for R1–R8 and the deliverable list) |

| Reviewer | Scope | Claims checked | Mismatches kept |
|---|---|---|---|
| README vs assignment | deliverables, R1–R8 traceability, setup walk-through | checklist | RA-1..RA-13 |
| README vs code C1 | behaviour: SQL path, guards, delete, store, commands | 73 rows | 5 (RC-1..RC-5) |
| README vs code C2 | setup, config, tests/evals, CI, maintainer CLIs, Not built, LM Studio, Langfuse | 99 rows | 8 (RC-20..RC-27) |

## 1. Deliverables vs the assignment

| Deliverable | Status | Evidence |
|---|---|---|
| Production HLD with diagrams | ✅ | `docs/architecture.md`, Mermaid renders on GitHub (README mermaid fix in `0e63f4c`) |
| Detailed technical description | ✅ with stale anchors | `docs/technical.md` (AF-6, SEC-A1 / RC-4: `_pipeline` cited at `:701`, actual `:819`; §4 step order differs from code) |
| Working prototype, `pip install -r requirements.txt` + ADC | ✅ | `requirements.txt` in sync with `uv.lock` (export diff empty); setup section L519-616 matches `pyproject.toml` |
| Framework justification + experience level | ✅ / vague | "How I worked" present; experience statement at L932 is vague (RA-6) |
| Public GitHub repo | ✅ | `Rinat-Arifullin/opsfleet-agent`, `main == HEAD` |

No ❌, so dimension 1 does not block on its own.

## 2. Acceptance-criteria traceability (prototype-must R2, R3, R5, R7)

| Req | Built | Documented in README | Gap |
|---|---|---|---|
| R2 Safety / PII / per-user scope | ✅ `guards/sql_policy.py`, `scope.py`, `scope_ctes.py`, `small_cell.py`, `pii*.py` | ✅ | RC-4 step order (LOW) |
| R3 High-stakes delete with strict confirmation | ✅ `delete/flow.py`, `store/audit.py` audit-first | ✅ in substance | RC-2, RC-3, RC-5 wording (MEDIUM/LOW) |
| R5 Resilience / self-correction on syntax errors and empty results | ✅ `tools/run_sql.py:213,218,455`, HLD §6.5 | ❌ **not described** | **RA-1 HIGH** |
| R7 Observability with agent-level metrics | ⚠️ tracer + `obs/metrics.py` exist, but per-attempt LLM records never reach the tracer | ⚠️ metrics module unmentioned | **AF-1 HIGH**, RA-2 MEDIUM |
| R1 Golden bucket, R4 learning loop, R6 evals, R8 persona | ✅ | ✅ (evals under-reported: RC-20/21/23/26) | MEDIUM/LOW |

## 3. Clean-machine run

Executed in the main session on HEAD: `uv run ruff check .` → clean; `OPSFLEET_GOLDEN_STRICT=1 uv run pytest -q` → 4224 passed, 6 deselected (84.9 s); offline eval → `RESULT: PASS`, all seven gates ok (see `eval-review.md`). Not executed: a fresh-venv `pip install -r requirements.txt` and a live Gemini/BigQuery run (no live credentials used in this review).

**README correctness issue found here:** the startup check described at L272-278 overclaims (RC-1, below).

## 4. Findings on `README.md` by severity

### HIGH (🔴 G4: owner approves the fix approach)

- **RA-1 — R5 self-correction is implemented but never described.** The code retries on empty results with explicit instructions (`tools/run_sql.py:213` "The query returned no rows. Check the filters…", `:218` "The query again returned no rows. Do not query again…", `empty_results` counter `:455`) and on SQL errors; HLD §6.5 covers it. README has no sentence about it, although R5 is a prototype-must. Fix ≤10 lines: R5 row at L77, a bullet in the resilience block L472-484, one demo row.
- **RC-1 — Startup check overclaims.** L274-275 say "ADC credentials load" and "a dry run against BigQuery succeeds". `config.py:374-397 startup_check` only loads settings and lists Gemini models; the BigQuery client is built later at `cli.py:270`, and `dry_run` exists only in `bq/client.py:307 prepare` (per query) and `commands/triage.py`. Missing ADC surfaces on the first query. Fix: reword L274-275 ("the Gemini model list is reachable with the key and every configured model id exists; ADC and BigQuery are first exercised by the first query") **or** add a `SELECT 1` dry run to `startup_check`. The owner chooses.

### MEDIUM (fix autonomously, list in the iteration summary)

- **RC-2** — L74 and L441-442 "The model has no delete tool." `tools/registry.py:24-41` binds `delete_reports` to `library_agent`; `delete/flow.py:80`; `roles/library_agent.py:17` says it only REQUESTS the two-phase delete (D-197). Replace with: "The model cannot delete anything: the Library agent's `delete_reports` tool only requests the preview; the confirmation check and the delete itself run in code."
- **RC-3** — L500-501 audit log lists "saves". `store/audit.py:138-162 EVENT_TYPES` has no save event; the save node (`graph/graph.py:1446-1448`) only writes `report_saved` to the trace. Replace with the actual list: delete previews/confirmations/executions/cancellations/expiries, guard refusals, every `run_sql`, renames, exports; "saves are recorded in `saved_report` and the trace".
- **RC-20** — L741 "41" golden cases; `evals/cases/golden/` has 44 yaml (33 `input:` + 11 `turns:`); `docs/technical.md:287` repeats 41.
- **RC-21** — suite table L739-744 omits the gates in `evals/gates.py:81-150` (`adversarial/delete` any failure fails the run, `differencing/cross_session` 100%, `resilience` 100%, pii_typed brand false positives 0 with ≥1 case); README L900 Not-built row should say live cases for these suites are missing while the gates and offline fixtures exist.
- **RA-2** — R7 "agent-level metrics" not named; `obs/metrics.py` unmentioned (and see AF-1 for what it currently cannot see).
- **RA-3** — capabilities not mapped to the assignment; aggregate-only "top customers" (L368-370) missing from the coverage table.
- **RA-4** — pip step L567 redundant (`requirements.txt` line 3 is `-e .`); rationale L570-572 inaccurate.
- **RA-5** — no per-requirement demo script for the reviewer.

### LOW

- **RC-4 / SEC-A1** — L378-379 and `technical.md:203-206` put the aggregate-only check before policy/scope; code runs `apply_scope` (`run_sql.py:824`) first, aggregate-only at `:831-834`; `technical.md:199` cites `:701` → `:819`.
- **RC-5** — L452 and L672 say the preview shows "expires in 600 s / 10 minutes"; `delete/flow.py:652-665` prints the backup notice (`:84`) and "Type yes to confirm. Any other reply cancels." (or "Type N to confirm." above 20); expiry is never shown.
- **RC-22** L848 flash-lite row omits `summary` and `fallback` roles (`models.yaml:14,16`). **RC-23** L743 "injection through data": only user-turn cases exist. **RC-24** L837 omits `LANGFUSE_BASE_URL`; `.env.example` lacks placeholders for `OPSFLEET_MAINTAINERS_YAML`, `OPSFLEET_GOLDEN_STRICT`, `OPSFLEET_EVAL_DATA_DIR`, `OPSFLEET_EVAL_CASE_TIMEOUT_S`, `LANGFUSE_BASE_URL`. **RC-25** `config.py:3` docstring "only module that reads the environment" is false (`session.py:54,61`, `cli.py:170-171`, `graph.py:299-300`, `golden/runtime.py:43-44`, `langfuse_sink.py:93,533`, `erase.py:346`, `triage.py:100,276`, `cli_progress.py:79`). **RC-26** L748-749 presents the calibrated judge as scoring, but `evals/live_sut.py:396-400` runs no LLM judge live (OD-5). **RC-27** L48 `prompts/` row omits `library_agent.md`.
- **RA-6..RA-13** — experience statement vague (L932); assignment vocabulary ("Golden Bucket", "Learning Loop") absent; example run illustrative, not real (L645); Unix-only commands (L559); minor structure nits.

### Undocumented but present (worth one line each)

Non-English input refused (`guards/input.py:109,452`); `MAX_INPUT_CHARS = 4000`; lexical retrieval fallback (`golden/seed.py:583`); duplicate SQL refused within a turn (`run_sql.py:845`); >20 matches require the typed count (`flow.py:664`); 7-day backup notice (`flow.py:84`); history window 12 turns / 4000 chars (`graph.py:179-181`); `/retry` caps 8/0/180 missing from L348-352; audit events `report.renamed`, `report.exported`, `tool.run_sql`, `differencing.suspected` and the append-only triggers; `--calibration-file/--calibration-cases` (`evals/run.py:621-622`); `MAX_THREADS = 10_000`; TPM/quota defaults in `models.yaml`.

## 5. Observability, resilience & cost, hygiene

- **Observability:** JSONL tracer, `/trace`, `/audit`, metrics and the Langfuse sink exist and scrub secrets/PII (`obs/tracer.py:150-214`, `langfuse_sink.py:104-155,180-184`). **Open HIGH AF-1:** per-attempt LLM records are never emitted, so fallback/retry metrics are always zero.
- **Resilience & cost:** every query dry-run and capped (1 GB/query, 10 GB/session, 60 s, 200 rows, 100 GB/day); every loop bounded (turn caps, SQL ≤6, `MAX_CONSECUTIVE_FAILURES = 3`, recursion 60, LLM ≤4 attempts). Idle timeout promised in the HLD is not implemented (AF-3).
- **Hygiene:** ruff clean; no secrets in the repo (`.env` never read in this review); `requirements.txt` in sync; `.claude/` committed as intended; no real people named in public docs.

## 6. Good

- Setup section matches `pyproject.toml` exactly (Python `>=3.12,<3.14`, spaCy wheel pin, console script); CI table mirrors `.github/workflows/ci.yml`; free-tier quota table and `local:` block match `models.yaml`.
- Every numeric claim on the SQL path and the delete flow maps to a named constant (`sql_policy.py:154-156,288-316`, `small_cell.py:103-104`, `bq/client.py:47-50`, `delete/flow.py:73-75,111`).
- "Not built" section is honest; links and anchors resolve; Mermaid renders.

## 7. Verdict and path forward

**BLOCKED** on three open HIGH findings, each needing the owner's decision (🔴 G4, not self-approved):

| Id | Where | Decision for the owner |
|---|---|---|
| AF-1 | `code-review.md` | approve the `TracerUsageSink` approach (adapter + `sink=` at both `TurnBudget` sites + test) |
| RA-1 | README | approve adding the R5 self-correction description (≤10 lines) |
| RC-1 | README L272-278 | reword the startup check **or** add a BigQuery `SELECT 1` dry run to `startup_check` |

After these three are fixed and MEDIUM items RC-2, RC-3, RC-20, RC-21, RA-2..RA-5, AF-2, AF-3 are applied, the expected verdict is **APPROVED WITH CONDITIONS** (remaining LOW: RC-4/5/22-27, RA-6..13, AF-4..6, SEC-A1/A2, SEC-B1).
