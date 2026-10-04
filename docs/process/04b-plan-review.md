# Step 4b: plan review of `04-plan.md` (before 🔴 G3)

Date: 2026-10-04. Three independent reviewers read the plan against `01-requirements.md` rev. 4.4, `architecture.md` (HLD) rev. 4.4, `decisions.md` and `CLAUDE.md`. No reviewer edited files. The orchestrator spot-checked the key findings and confirmed them (SEC-1 order at plan iteration 13; HLD test names absent; `9 [PARALLEL OK with 10]` while 10 depends on 9; memo keyed on "table modified time").

| Reviewer | Focus | Verdict | BLOCKER | MAJOR | MINOR | NIT |
|---|---|---|---|---|---|---|
| Security (SEC) | 🔴 risk areas, fail-closed controls | NOT READY | 1 | 9 | 8 | 2 |
| Traceability (TR) | AC/FR/ADR → test mapping, dependencies | NOT READY | 1 | 8 | 14 | 3 |
| Schedule (SCH) | sizing, capacity, parallelism, live budget | READY with conditions C1–C5 | 0 | 7 | 4 | 1 |

**Consolidated verdict: NOT READY for 🔴 G3.** One plan revision fixes it. Most of the work is mechanical (test names, dependency edges, ordering). Five items need an owner decision (§3).

## 1. Blockers

**B-1: the `run_sql` order contradicts the HLD** (SEC-1)
- Iteration 13 currently runs: validate → rewrite → dry run → execute → small-cell → differencing → scrub.
- HLD §5.1, §5.3 step 9 and §5.5 require: parse/validate → rewrite → re-resolve → QI/small-cell (HAVING injected, population check or reject) → post-rewrite invariant → dry run and cost caps → differencing → execute with `maximum_bytes_billed` → scrub → 200-row cap.
- Fix:
  - Follow the HLD order.
  - Rename the test to `test_run_sql_order_matches_hld_5_1`.
  - Add `test_run_sql_small_cell_and_differencing_before_execute`, where a fake BigQuery must never be called.

**B-2: done criteria do not use the named tests and evals** (TR-1, SEC-2, TR-2, TR-23)
- The requirements name 151 tests and the plan uses 13 of them. Of 74 named eval cases, the plan uses 7.
- Several tests appear under invented names, for example `test_memo_misses_after_refresh_date_change`, `test_delete_preview_includes_backup_notice` and `test_delete_token_derived_not_stored`.
- Missing entirely: the `test_resume_*` set, the bounded-retry tests, small-cell, differencing (session and cross-session), output guard, light path, and the HLD §4.5/§5.3/§5.4/§6.2/§6.3.3 security tests.
- The plan's own note (a) says that US-09..29 AC bodies were not re-read.
- This breaks the requirements §10 DoD.
- Fix:
  - Put the AC/HLD/ADR names verbatim into each iteration's done criteria. Descriptive extras may stay alongside them.
  - List every named eval case file in iterations 28 and 29.
  - Add an "AC → named test/eval" column to §9.
  - Re-read every AC body and drop note (a).

## 2. Major findings (deduplicated)

| ID | Finding | Sources | Fix |
|---|---|---|---|
| M-1 | SQL bypass classes are untested in iterations 6, 7, 9 and 13. | SEC-3, TR-7 | Add tests for: <br>• PII in WHERE/JOIN/GROUP/ORDER/LIKE <br>• CONCAT/SUBSTR/TO_JSON_STRING/STRING_AGG/ARRAY_AGG over PII <br>• source allowlist (EXPORT DATA, EXTERNAL_QUERY, ML.*, FOR SYSTEM_TIME AS OF, temp UDF, @@vars) <br>• scalar deny over QI <br>• id literal combined with QI <br>• QI lineage through CTEs; `created_at` as QI <br>• `test_cte_shadowing_rejected`, `test_nested_cte_scope_resolution` <br>• `test_rewrite_invariant_fail_closed`, empty brand list fails closed <br>• `test_orders_num_of_item_not_exposed` <br>• ADR-004 small-cell cases (b) and (c) plus the population query <br>• mapped BigQuery errors in the trace and audit legs. |
| M-2 | Delete controls are untested in iterations 21 and 22. | SEC-4, TR-6, TR-9 | Add the ADR-007 and HLD §6.3.3 tests: <br>• single use / replay; `test_proof_mismatch_cancels`; `test_confirm_only_on_next_turn` <br>• `test_delete_token_never_in_traces`, `test_checkpoint_holds_token_hash_only`, `test_delete_token_derived_not_stored` <br>• `test_confirm_delete_rerun_keeps_token`, `test_confirm_delete_on_other_instance_verifies` <br>• unique `(pending_action_id, event_type)` <br>• `test_execute_delete_requires_confirmed_record` <br>• `test_delete_refused_after_view_same_turn` (taint) <br>• `test_second_delete_while_pending_rejected` <br>• `test_large_delete_requires_typed_count`, `test_confirm_prompt_rendered_by_code` <br>• `test_resume_expires_pending_delete` <br>• recompute ownership/ids_sha256 at execute (AC-12.6) <br>• `K_delete` never persisted <br>• backup notice in the **preview** (AC-12.11, iteration 22). |
| M-3 | Iterations use components built later. | TR-3, TR-4, SEC-8, SEC-9 | • Build the checkpointer before 17 (in 14, or a new iteration). <br>• Move the delete parts of 16 and 19 (AC-20.5/20.7, AC-15.5, AC-22.6) into 22, or schedule them after 22. <br>• 15/16 before 14, or a test-only scope plus `test_scope_from_profile_only`; L1 live smoke only after 15/16. <br>• Parametrize the residue test: 23 covers existing tables; 37/38 re-run it with FTS `optimize` inside the delete transaction. <br>• Add the missing edges: 25→26, 27 golden step→29, 9→5, 11/12→14, 12→11, 20→8, 35→32/34/39 (or erasure tolerates absent stores). <br>• AC-21.14 export clause is conditional on 33. |
| M-4 | Rollback notes quietly weaken non-droppable closures: 8 narrows to PERSON only, 10 falls back to session-only, 23 leaves a residue gap. Iteration 7 cites a fallback ADR-008 does not have. | SEC-5, TR-13 | A red closure means stop and escalate to the owner (🔴). The guarded feature stays disabled and fails closed: cross-customer aggregates refused, delete tool unregistered, input refused. |
| M-5 | 🔴 iterations are marked T2, and the non-droppable list is incomplete. | SEC-7, TR-20 | • Mark 5, 11, 12 and 15 as 🔴 T1 with a second review. Same for 19 (SEC-14) and 35 (in the table, not only §R). <br>• Rule: every iteration not in the drop order is non-droppable. |
| M-6 | Eval gates for the rev. 4.4 closures are missing. | SEC-6 | Add: <br>• `adversarial/pii_typed/brand_false_positive` with `test_eval_gate_brand_false_positive_zero` <br>• `test_eval_gate_differencing_cross_session_100` <br>• calibration status wired into `gates.py` (an uncalibrated judge does not count). |
| M-7 | Secrets and trace redaction rely on a denylist. | SEC-10 | • `test_trace_redaction` (HLD) <br>• config logging by allowlist <br>• `test_secrets_never_in_traces_logs_or_errors` with sentinel values for the API key, AES key, `K_delete`, token and proof <br>• `.gitignore` check <br>• gitleaks in CI (iteration 43). |
| M-8 | Bounds required by ADR-003/009 are missing from 3 and 14 (CLAUDE.md: "every loop and retry is bounded"). | TR-5 | • `test_retry_wrapper_bounded`, `test_sdk_single_attempt`, `test_role_subcap_counts_retries_and_fallback` <br>• per-turn cap of 6; fallback gets one attempt, then `force_answer` <br>• `recursion_limit`; escalate once <br>• `test_turn_caps_enforced`, `test_turn_deadline`, `test_all_models_down_graceful`. |
| M-9 | The memo key differs from the HLD: "table modified time" is used instead of `(sql_hash, scope_key, refresh_date)`. ADR-012 caches have no iteration. | TR-8, SEC-11 | • Use the HLD key. <br>• `test_memo_misses_after_refresh_date_change`, `test_result_cache_key_includes_scope`, `test_repeated_query_reuses_result`. <br>• A memo hit still runs differencing. |
| M-10 | The Monday checkpoint (1–19 plus demo) cannot be reached. Hard iterations are under-sized by about 11.5 effort-hours. A 1.5x factor is not realistic for 🔴 work, where the owner is the bottleneck. | SCH-1, SCH-2, SCH-3, TR-19 | • Re-size iterations 2, 6, 7, 8, 14, 17, 22, 27, 28 and 44. <br>• Capacity at 1.0x for 🔴/T1 and 1.5x for T2 pairs (about 1.2x blended Sun–Tue); measure after iterations 1–4. <br>• Monday checkpoint = tiers 0–1 plus a live ask→answer through 14. <br>• Sunday-night tripwire. <br>• Checkpoints listed as iteration numbers plus a demo script. |
| M-11 | Some `[PARALLEL OK]` pairs are invalid, and hot files are edited in parallel. | SCH-4, TR-12 | • Remove 9∥10. <br>• 5 starts after 3. <br>• Serialize edits to `pyproject.toml`/`uv.lock`, `cli.py`, `graph/graph.py`, `config.py` and `conftest.py`. <br>• A command table so `cli.py` is edited once. <br>• 33/37 run in sequence. |
| M-12 | There is no dependency plan: langgraph, sqlglot, presidio, spacy plus the model, ruff and pytest are not added by any iteration; `requires-python` does not match; `requirements.txt` would drift. | SCH-5, TR-15 | • Iteration 1 adds every known dependency with pins and exports `requirements.txt`. <br>• Only 31 and 40 touch the lock later, one after the other. <br>• The CI sync check moves to iteration 2. |
| M-13 | The live budget is unconfirmed: limits and model ids are not verified, the Deep (pro) model has no row, and the final run uses 100% of Thursday's limit. | SCH-6, TR-21 | • Confirm limits for the exact ids, including Deep (owner task T-2), before G3. <br>• Add a Deep row, with fallback if its quota is about 0. <br>• Final full run Wednesday evening or Thursday 08:00, before 44. <br>• Reserve 20–30 calls on Thursday. <br>• Fix the L3/L6a counts. |
| M-14 | The drop order only frees Wednesday time. There is no minimum shippable product and no rule beyond item 10. | SCH-7 | Owner decision (§3): minimum scope plus a "volume cut" escalation that keeps the gates. 31/32 go first on Wednesday. |

## 3. Owner decisions needed before G3

1. **Q-1 / SCH-9:** drop the P items (iteration 39) first in the drop order, ahead of M items. Recommended: yes. Record in `decisions.md`.
2. **SCH-7 minimum shippable scope:** tiers 0–1; Q&A through 14/15; reports save/list/view (17–18); delete with audit and no residue (21–23); offline adversarial and resilience suites (27–28); calibration (20/30); seed (31); `/feedback` (32); README plus the clean-machine run. Escalation beyond drop item 10 cuts volume but keeps the gates:
   - golden set about 15 cases;
   - router set about 25 messages;
   - static persona;
   - session caps only;
   - JSONL-only traces.
3. **SCH-1 Monday checkpoint redefinition** and the Sunday-night tripwire (if 6 is not green, drop 38/37/36 at once).
4. **T-2 (owner task):** confirm free-tier limits and model ids, including the Deep model.
5. **QI set location (Q-4 / SEC-19 / TR-17):** define it where HLD §5.2 places it. No new `config/qi.yaml` or `config/golden_seed.yaml` unless the HLD layout names them. Otherwise record the deviation.

**Owner answers (2026-10-04, recorded in `decisions.md` § Step 4b):**
1. Yes: P items (39) first in the drop order.
2. Accepted: the minimum scope and the volume-cut escalation.
3. Kept as in the plan: Monday = 1–19 plus demo at 1.5x. The slip forecast becomes risk R1; decisions 1–2 are the mitigation. The planner still re-sizes iterations (SCH-2) honestly and shows the buffer that results.
4. T-2: checked against the official docs. No published free-tier numbers (the AI Studio dashboard only). Flash and flash-lite have a free tier. The pro preview has none, so Deep stays on flash. `gemini-embedding-001` must be confirmed in the iteration-2 spike.
5. QI location: resolve per the HLD (planner).

## 4. Minor and nit findings (fold into the same revision)

- **Security:**
  - SEC-12: the differencing store fails closed when it is unavailable.
  - SEC-13: a saved report body passes the output guard and is wrapped as untrusted on read.
  - SEC-14: on resume, a missing `LANGGRAPH_AES_KEY` fails closed and the scope snapshot is checked.
  - SEC-15: Golden trios are scanned for PII and injection at load.
  - SEC-16: T1 review of `evals/adversarial/*`.
  - SEC-17: `/audit` is not reachable from chat or tools.
  - SEC-18: erasure is marked 🔴; if it is dropped, document the retention gap.
  - SEC-20: the recall gate in 8 stays provisional until 28.
- **Traceability:**
  - TR-10: iterations 1, 8, 13, 17 and 28 exceed 5 files; split them or justify.
  - TR-11: recompute the critical path through 8 → 9 → 10 → 13.
  - TR-14: replace the impossible "delete a draft" red-team case.
  - TR-16: eval layout per the HLD (`evals/cases/*.yaml`, `evals/run.py`), or record the deviation.
  - TR-18: the startup check also validates profiles and that the stores are writable.
  - TR-22: iteration 45 writes to `evals/results/`; Step 6 owns the review file.
  - TR-24: close Q-2 (already answered in `decisions.md`).
  - TR-25: one-line note on the HLD tier table vs the drop order.
  - TR-26: align the `uv export` command between `CLAUDE.md` and the plan.
- **Schedule:**
  - SCH-8: Thursday Step 6 plus fixes plus G4 takes about 3.5 h; start the README "How I worked" section and the time log on Tuesday; ring-fence Thursday from 12:00; state the deadline hour.
  - SCH-10: iteration 20 becomes a parallel stream on Sunday or Monday morning; T-1 goes to the owner by Monday midday.

## 5. What the reviewers confirmed as good

- All 64 prototype FRs are mapped.
- The drop table matches `decisions.md`, and the rev. 4.4 closures (8, 10, 22, 23, 30) are non-droppable.
- Every 🔴 iteration has a rollback. Iteration 22 keeps the delete tool unregistered until 21 is green.
- The CLAUDE.md non-negotiables are each tied to an iteration.
- The no-network fixture is in iteration 1, and the day-1 spikes come early.
- The residue test scans the raw DB and WAL bytes.

## 6. Next step

The planner revises `04-plan.md` (rev. 2) with B-1, B-2, M-1..M-14 and the minor items, using the owner's answers to §3. The orchestrator re-checks the result against this list. Then 🔴 G3 goes to the owner. G3 is not self-approved.
