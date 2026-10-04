# Plan Review 4 (independent): docs/process/04-plan.md (Step 4, before 🔴 G3)

Date: 2026-10-04 · Reviewer: T1 · Files reviewed: 15 (see §0.1) · **Verdict: READY WITH CONDITIONS** (recommendation to the owner; 🔴 G3 stays with the owner) · Covers plan rev. 1 (§§CRITICAL–LOW, as record) and rev. 2 as amended (§12, current)

> Severity scale is the house scale from `.claude/ai-workflow/skills/reviewer.md`: **CRITICAL** = PII leak, scope bypass, unconfirmed deletion, unbounded spend · **HIGH** = likely wrong behaviour under normal use, or a failed deliverable · **MEDIUM** = footgun or maintainability · **LOW** = minor
>
> Finding ids are `P4-H*` (HIGH), `P4-M*` (MEDIUM), `P4-L*` (LOW). Line numbers in §§CRITICAL–LOW refer to plan **rev. 1** (first row of §0.1). Rev. 1 was replaced on disk by **rev. 2** at 17:04, while this review was being written, and is not preserved anywhere else; the rev. 2 anchors and the per-finding status are in §12. This review is independent of `04b-plan-review.md` (the three-reviewer consolidation written the same afternoon); where the two agree, the 04b id is named so the planner fixes each point once.

---

## 0. Scope and method

### 0.1 Reviewed revision

| File | Lines | mtime (2026-10-04) | md5 (prefix) | Role in this review |
|---|---|---|---|---|
| `docs/process/04-plan.md` (rev. 1) | 1033 | 15:26:23 | `233573f9f5d8` | **subject of §§CRITICAL–LOW**; superseded on disk at 17:04, no copy kept |
| `docs/process/04-plan.md` (rev. 2, amended) | 1404 | 17:37:57 | `605fffe61a80` | **subject of §12**; current file. Written 17:04:08 (`36d8b45360b2`), G3 marked approved 17:07:38 (`c8aa25f8f86f`, 1402 lines), amended for the free-tier limits 17:37:57; §12 anchors refer to the 17:37 file |
| `docs/architecture.md` (HLD rev. 4.4) | 2006 | 15:19:23 | `f6eaba4b6b6b` | design the plan must implement |
| `docs/process/01-requirements.md` (rev. 4.4) | 1610 | 15:19:18 | `799459e3e7e1` | AC ids (147) |
| `docs/decisions.md` | 645 | 17:37:57 | `bdf5761f87e0` | ADRs, drop order `:526–550`, Step 4 inputs `:551–599`, owner decisions after 04b `:600–615`, G3 approval `:617–619`, T-2 limits `:621–629`, free-tier decision `:631–645` |
| `docs/process/03b-independent-review.md` | 395 | 14:14:05 | `80cdc1b382b0` | Review 3 fix list (R3-*) the plan must carry |
| `docs/process/04b-plan-review.md` | 110 | 16:33:30 | `24be7906bf81` | parallel consolidation; input, not overwritten |
| `docs/process/HANDOFF.md` | 70 | 15:19:07 | `5635470266d1` | hard rules |
| `.claude/ai-workflow/skills/planner.md` | 85 | 14:19:58 | `bb30bad1ac9e` | template, ordering, slicing, self-check |
| `.claude/ai-workflow/workflows/sdlc-lite.md` | 128 | 14:19:58 | `4004f8b89c00` | Step 4 `:72–79`, Step 5 `:83–98`, Step 6 `:102–118` |
| `.claude/ai-workflow/config/human-gates.md` | 80 | 14:19:58 | `e6bb7841ba23` | G3 criteria `:29`, risk areas `:33–60`, gate format `:62–76` |
| `.claude/ai-workflow/skills/reviewer.md` | 95 | 14:20:31 | `d624c34278b8` | house format |
| `pyproject.toml` | 11 | 08:05:25 | `8d5450a67075` | current repo state |
| `.gitignore` | 27 | 14:20:31 | `b607ca872d53` | hygiene |
| `.python-version` | 1 | 08:00:11 | — | hygiene |
| `CLAUDE.md` | — | — | — | `uv export` line `:14` |

Repo state: no commits, everything untracked; `pyproject.toml` is an 11-line stub (`requires-python = ">=3.12"`, five runtime deps, no dev group, no scripts entry, no `live` marker); no `src/`, `tests/` or `.github/`.

### 0.2 Method

1. Read the plan end to end against the G3 criteria in `human-gates.md:29` (iteration order, risky iterations, time budget against the deadline), the planner template and rules (`planner.md:22–85`) and the Step 5/6 contract (`sdlc-lite.md:83–118`).
2. Checked every design behaviour the HLD binds to a named test or a constant (§4.0.7 constants, §4.4 retry table, §5.3–5.5 guard order, §7 tests) for an owning iteration.
3. Two scoped audits ran in isolated contexts and their outputs were reconciled by hand: (A) files per iteration, shared files inside `[PARALLEL OK]` sets, dependency graph versus per-iteration `Depends on`, schedule arithmetic; (B) the test-name namespace of the HLD and requirements versus the plan. Every number quoted below was re-derived by grep on the files in §0.1.
4. Read `04b-plan-review.md` and `decisions.md:600–615` after forming the findings, then marked agreements. Both files were produced by another session at 16:33 and were not witnessed in this one; they are treated as the project's record, not as owner instructions received here.
5. Hygiene scan of the plan for absolute paths, e-mail addresses, real client names and secret-looking strings (counts only).
6. When the plan changed to rev. 2 mid-review, every finding was re-checked against rev. 2 by three scoped audits in isolated contexts (risk areas; test names and traceability; schedule and structure), reconciled by hand into §12. Rev. 2 was written by the planner session, not here; its "owner decisions" and "closed questions" are the project's record, not instructions received in this session.

Not done: no code exists yet, so nothing was executed. Live limits were not re-checked against the provider (decisions `:611–615` records that check).

---

## CRITICAL

None.

---

## HIGH

### P4-H1 — The retry ladder in iteration 3 contradicts the approved HLD (risk area 6: cost and resilience caps)

- **Where:** `04-plan.md:150` — "Retries: at most 3, backoff 1 s, 2 s, 4 s with jitter, then the fallback model, then a typed error" with `test_llm_retry_is_bounded`, `test_llm_fallback_then_typed_error`.
- **Design says:** one retry, fallback once, at most 6 retries per turn, SDK `max_retries=1`: `architecture.md:37`, `:606`, `:612`, `:666`, `:716`, `:798`. That is the R3-H4 / R3-M6 closure the owner approved at G2.
- **Why HIGH:** a 3-step ladder before fallback multiplies the worst-case per-role calls by about three against the budget line at `:149`, and the HLD tests that pin the cap (`test_role_subcap_counts_retries_and_fallback`, `test_retry_wrapper_bounded`, `test_sdk_single_attempt`, `test_retry_then_fallback`) do not appear anywhere in the plan. The plan also never mentions the per-role sub-cap, the recursion limit or the safety preamble (all HLD §4.0.7 / §5.2 items).
- **Process:** iteration 3 is `Risk: medium`, T2, no 🔴, no red-team criterion and no rollback note although `human-gates.md:56–59` lists exactly these caps and `:35` says any change needs explicit sign-off. Either the HLD is being changed silently or the plan is wrong; both need the owner.
- **Fix:** rewrite `:150` to the HLD ladder (primary → 1 retry → fallback once → typed `error_class`; SDK `max_retries=1`; per-turn cap 6), adopt the four HLD test names, add "the red-team cases for area 6 pass" and a rollback note, and mark 3 🔴 (or T2 with T1 review, as the plan does for 28).

### P4-H2 — The plan's test names and the HLD's test names are two different namespaces

- **Numbers (by grep):** HLD 198 distinct `test_*` names; requirements 151 (a subset of the HLD's); plan 203. Intersection HLD ∩ plan = **9** (`test_cli_commands`, `test_eval_gate_fails_on_any_delete_case`, `test_hard_delete_leaves_no_residue_in_db_file`, `test_key_rotation_expires_pending_delete`, `test_persona_invalid_keeps_last_valid`, `test_pii_detector_missing_model_fails_startup`, `test_schema_tool_hides_pii_columns`, `test_self_correction_bounded`, `test_sql_policy_rejects_pii_projection`). 189 HLD names have no plan counterpart; 139 of them are the test an AC cites as its verification (112 AC ids). 63 of the 68 names in the HLD's R3-H10 table (`architecture.md:1405–1418`) are absent.
- **At least 25 are renames of the same check** (e.g. `test_delete_token_derived_not_stored` ↔ `test_delete_token_never_stored_anywhere`; `test_instruction_precedence` ↔ `test_preference_precedence_order`; `test_retry_wrapper_bounded` ↔ `test_llm_retry_is_bounded`; `test_turn_caps_enforced` + `test_turn_deadline` ↔ `test_turn_budget_caps_calls_and_deadline`; `test_judge_calibration_gate_blocks_on_low_agreement` ↔ `test_calibration_gate_blocks_judge_below_80`; `test_delete_aborts_when_audit_write_fails` ↔ `test_delete_aborted_when_audit_write_fails`; `test_audit_append_only` ↔ `test_audit_is_append_only`).
- **Why HIGH:** `04-plan.md:14` makes a test name a commitment, and Step 6's final reviewer must trace each AC to its test (`sdlc-lite.md:113`). With two namespaces that trace is manual guesswork on Thursday, and the 50 HLD names that are neither renamed nor AC-cited (grounding ledger, resume per node, sub-caps, memo refresh key) are silently lost. Concurs with 04b **B-2**.
- **Fix:** one pass over the plan replacing each renamed test with the HLD name (or a line in §9 mapping plan name → HLD name), and an explicit list of HLD tests the plan drops with a reason. The HLD is the source of truth here; where the plan's name is better, change the HLD in the same revision and say so.

### P4-H3 — Design behaviour with no owning iteration, or contradicted by its iteration

Each bullet is a bound HLD behaviour. Sub-items (a) and (b) alone would be HIGH; the rest are listed here so they are fixed in the same revision.

- **(a) `run_sql` order in iteration 13.** Goal `:304` and test `:313` (`test_run_sql_order_validate_rewrite_dryrun_execute_scrub`) chain "validate, scope-rewrite, dry run, execute, small-cell, differencing and scrub". The HLD puts the re-resolve, QI and small-cell rules and the post-rewrite invariant **before** the query (§5.3 steps 9–10, `architecture.md:987–988`) and the differencing guard "after the small-cell rules and before the query" (§5.5, `:1023`). Executing first and checking after means a BigQuery query is billed and a small-cell or differencing refusal happens on real data that has already left the warehouse. Concurs with 04b **B-1**.
- **(b) Memo key.** `:313` invalidates the memo "when table modified time changes"; the HLD key is `(sql_hash, scope_key, refresh_date)` with `as_of` pinned per turn (`architecture.md:40`, `:845`, test `test_memo_misses_after_refresh_date_change` `:854`). Different trigger, different test. Concurs with 04b **M-9**.
- **(c) Library agent.** The HLD has five roles plus the code supervisor (`:117`) and gives the Library agent the list/view/search/history work (`:300`, `:347`, `:483`, `:531`, `:572`, `:591`). The plan mentions it twice in passing (`:470`, `:567`) and iterations 18, 33, 34, 37, 38 build "tools" with no role, so the role-isolation design (which role may call which tool, `test_output_guard_blocks_unexpected_tool_call` in 12) has no Library row to check.
- **(d) User-typed PII on the input side.** HLD `:1126` (`test_user_typed_email_not_persisted`: masked in the checkpoint, summary, trace and router input) and §5.4 `:1009`. Iteration 12 scrubs the **final text** only (`:298`); iteration 11 (input guard) names no PII test; iteration 8 builds the scrubber but no iteration applies it to the incoming message before it reaches the checkpoint.
- **(e) Grounding ledger.** HLD §4 grounding tests at `:699` (`test_grounding_rounding`, `test_grounding_derived_ops`, `test_grounding_unmatched_labelled`, `test_grounding_accepts_prior_turn_ledger`, `test_grounding_rejects_unknown_number`) have no iteration; 14 and 17 do not name grounding.
- **(f) Resume semantics.** HLD `:655` tests (`test_resume_after_crash_each_node`, `test_resume_budget_persisted`, resume with an expired pending delete, re-showing a draft after resume) are split across 19 (`--resume`), 22 (pending delete) and 17 (draft). 19 names `test_resume` and `test_resume_only_own_session` (`:415`, `:418`) but not the per-node crash, budget-persistence, expired-pending-delete or draft-re-show tests; 22 and 17 name none.
- **Fix:** iteration 13 goal and test renamed to the HLD order (validate → rewrite → re-resolve/QI/small-cell → invariant → differencing → dry run → execute → scrub) and the memo test renamed to the refresh-date key; a Library role added to 18 (role file, allowlist row, one isolation test); input-side typed-PII masking added to 11 with the HLD test; grounding tests assigned to 14 (or a sixth file in 17 with the file cap noted); resume tests assigned to 19/22/17 by name.

### P4-H4 — Time budget: what is still undecided after the owner's decisions

The owner has already kept the 1.5× factor and the Monday checkpoint and accepted the minimum scope (decisions `:606–608`), so the capacity factor itself is not re-litigated here. Two things remain unaddressed by those decisions:

- **Sunday.** Iterations 1–8 are planned at 10.5 effort-hours (`:855`) and include three L-sized 🔴 iterations (6, 7, 8) that need T1 and an extra review each. G3 is still pending at about 17:00 on the Sunday in question. Under assumption A that is 15.75 owner-hours of work after the gate, which is not available today. The first checkpoint (`:865`) is therefore already behind before it starts, and R1 (`:989`) understates it.
- **Thursday.** `:859` allocates 5.0 h to iterations 43–45 **and** Step 6, with Step 6 at 1.5 h. Step 6 is three parallel reviewers, a T1 final review, 🔴 G4 and any HIGH fix (`sdlc-lite.md:102–118`); R14 (`:1002`) itself names it as a risk. The README (43) and the clean-machine run (44) are also on Thursday, so the submission depends on everything landing the same day.
- **Fix:** restate the Sunday row as what can realistically start today (1–2, maybe 3–4) and shift the rest; move 43 and 44 to Wednesday with a code freeze at Wednesday end of day, leaving Thursday for 45, Step 6 and fixes; give Step 6 at least 3 h; record in R1 that the first checkpoint is already at risk. Decision 2's minimum scope then becomes the explicit Plan B at the Monday checkpoint rather than an implicit one.

---

## MEDIUM

### P4-M1 — `9 [PARALLEL OK with 10]` but 10 depends on 9
`:95` versus `:268` (10 `Depends on: 4, 9`). Same for `20 [PARALLEL OK with any]` (`:96`) while 30 depends on 20 (`:590`), and the Wednesday chain `31 → 32` (`:98`) while 32 depends on 8 and 19, not 31 (`:622`). **Fix:** derive §3 from the `Depends on` lines, not by hand.

### P4-M2 — The Wednesday `[PARALLEL OK]` set shares files
`:98` declares 33, 34, 35, 37, 39, 40, 41, 42 mutually independent. 33 and 37 both edit `tools/report_tools.py` (`:629`, `:684`); `cli.py` is edited by 34 (`:643`), 35 (`:656`), 41 (`:736`), 42 (`:748`), and also by 32 (`:617`) and 36 (`:671`) on the same day. `sdlc-lite.md:96` allows parallel only with no shared files. `cli.py` appears in 10 iterations overall (1, 19, 21, 25, 32, 34, 35, 36, 41, 42), `report_tools.py` in 4, `.github/workflows/ci.yml` in 3, `config.py` in 3. **Fix:** either one `cli/` package with a module per command group (then the shared file disappears), or serialise the Wednesday set into two streams.

### P4-M3 — The dependency graph and critical path disagree with the iteration blocks
Graph `:23–35` versus `Depends on`: 28 (graph: 27; block: 22, 23, 27), 30 (27, 29 vs 20, 27), 34 (15, 19 vs 15, 21), 37 (18 vs 18, 23), 41 (26 vs 21, 26), 42 (16 vs 16, 21). The serial arrows 3→4→5 and 7→8 (`:23`) and 15→16 (`:28`) contradict the parallel pairs at `:93–96`. The critical path (`:38`) runs 17 → 21, but 21's block (`:453`) does not depend on 17. **Fix:** regenerate the graph from the blocks and re-derive the critical path; it probably passes through 13 → 14 → 17 → 22 rather than 21.

### P4-M4 — Risk-area iterations without the 🔴 marker, red-team criterion or rollback note
`planner.md:64` and `human-gates.md:33–35`. Iterations touching a risk area but marked medium/low with no red-team criterion: 3 (area 6), 5 (area 4: dry run, `maximum_bytes_billed`), 11 (area 5: injection classifier), 12 (area 1: output post-filter), 15 (area 2: scope filter in context), 16 (area 2: profile → scope), 19 (area 3: checkpoint holds a pending delete), 24 (area 6: fallback chain, degraded mode), 35 (area 3: erasure, marked "high-ish"), 42 (area 2: `access set`). **Fix:** add the criterion and a one-line rollback note to each, or state in §11 why an iteration is exempt. Keep the T2 assignment where the plan argues for it; the criterion is what matters.

### P4-M5 — No mid-iteration sign-off or scope-creep mechanism
`human-gates.md:35` requires explicit sign-off for any change to a risk area "even mid-iteration"; `sdlc-lite.md:97` defines the 🟡 scope-creep options. The plan has neither a paragraph nor a per-iteration note on how the implementer surfaces such a change (who is told, where it is logged). **Fix:** one paragraph in §11: "a risk-area deviation stops the iteration, is written to `docs/decisions.md` as a 🟡 entry and waits for the owner".

### P4-M6 — Golden seed (31) lands after the evals (27–30)
`planner.md:54–55` orders Golden Bucket before evals. L4 and L5 (`:833–834`) run with the seed off, so the golden quality numbers recorded on Tuesday do not measure R1 (Hybrid Intelligence); the first seeded run is L6a on Wednesday (`:835`). Acceptable only if stated as a baseline. **Fix:** either move 31 before 27 (it depends only on 14, 15, 6) or label L4 "seed off, baseline" and L6a "seed on, first R1 measurement" in §5.

### P4-M7 — Eval case YAML is deferred to iteration 28
`sdlc-lite.md:90`: an iteration that adds a guardrail or tool adds its eval case YAML in the same iteration. The plan's red-team criterion (`:200`, `:216`, `:233`, `:250`, `:265`, `:314`, `:384`, `:451`, `:467`, `:483`, `:661`) is satisfied by unit `test_*_redteam.py` files and the YAML suites arrive together in 28 (`:546`), which is then a consolidation iteration of the kind `planner.md:63` forbids. **Fix:** each 🔴 iteration lists its `evals/cases/adversarial/<area>.yaml` (the sixth file, with the cap noted) and 28 keeps only the resilience suite and the runner wiring.

### P4-M8 — Audit (21) retro-wires 6–12 through a file nobody creates, and carries three concepts
`:447` edits `guards/__init__.py` "wire refusals from 6-12 into the audit", but no earlier iteration lists that file. Refusals from the guards built on Sunday and Monday are unaudited until Tuesday, so L1 and L2 run with unaudited refusals. 21 also bundles the log, the viewer and the retro-wiring (one concept per iteration, `planner.md:61`). **Fix:** create a `GuardDecision` sink in iteration 4 (store base) with a no-op writer, have 6–12 emit to it from the start, and let 21 replace the writer and add the viewer.

### P4-M9 — Confirm-before-save (17) precedes the checkpointer (19)
17 implements Save / Revise / Cancel and `test_report_confirmation_survives_i…` (`:374–378`, `:382`), which in the HLD is an interrupt that survives a restart; the graph checkpointer with `EncryptedSerializer` and `LANGGRAPH_AES_KEY` is created in 19 (`:413`). The spike (2) compiles subgraphs with `checkpointer=False` (`:129`), so there is no checkpointer at all until 19. Either 17's survival test cannot pass, or 17 creates an unencrypted checkpointer that 19 later swaps (a risk-area change). **Fix:** move the checkpointer base (SQLite saver + encrypted serializer) to 14 or 16 and keep `--resume` UX in 19.

### P4-M10 — Iteration 23 tests FTS and embedding residue before those tables exist
`:473` and `:481` (`test_hard_delete_removes_fts_and_embedding_rows_same_transaction`) delete from the FTS index and the embeddings table, which are created in 37 and 38 (Wednesday, drop items 2 and 1). On Tuesday the test has nothing to delete, and if 37/38 are dropped the test is permanently vacuous while the non-droppable closure (`:886`) claims "no residue". **Fix:** 23 creates the two tables' schema (empty) with the residue test; 37/38 only populate them. Say so in 23's files.

### P4-M11 — `graph/precedence.py` lives in the first-to-drop iteration
`:710`: precedence (safety > report format > persona and preferences) is built in 39. Under decision 1 (decisions `:606`) 39 is now drop item 1, but precedence is an HLD invariant for persona (26) and the report format contract (17), not a P feature (`architecture.md:141`, `:766–772`, `:1410`). **Fix:** move `precedence.py` and `test_preference_precedence_order` (HLD name `test_instruction_precedence`) to 26; 39 keeps the store and the view/reset tests.

### P4-M12 — No `tests/live/` in the plan
HLD layout `:235` has `tests/live/` for the `@pytest.mark.live` tests. Iteration 1 registers the `live` marker (`:112`), but no iteration creates the directory or a single live test, and the milestones run through `evals/` only. **Fix:** 27 adds `tests/live/conftest.py` with the marker and the rate limiter and one smoke test, or §5 states that live checks live only in `evals/` and the HLD layout is amended.

### P4-M13 — L6 is planned at the ceiling
`:836` L6 = 150 / 300, which §5 describes as the full daily ceiling for the primary model. One failed case and a re-run is impossible on submission day. **Fix:** L6 at ≤ 70% of the ceiling (golden subset plus adversarial full) with the Wednesday L6a as the last full run, or move L6 to Wednesday evening.

### P4-M14 — Iteration 8's recall gate is measured on a self-authored fixture
`:231–233`: the `pii_typed` gate (recall ≥ 0.95 on the typed-PII fixture) and the fixture are written in the same iteration by the same implementer. **Fix:** the fixture is drawn from the requirements examples plus the Review 3 cases and is reviewed by the owner (T-1 slot), or the gate is marked "self-authored, re-checked at 30 with the owner's labels".

### P4-M15 — Package additions are not tracked per iteration
decisions `:551–599` (R3-M20, R3-L36) prescribe `requires-python >=3.12,<3.14`, a dev group, `[project.scripts]`, the `live` marker, `pycryptodome`, `sqlglot>=30,<31`. The repo has an 11-line stub. Iteration 1 lists `pyproject.toml` with the scripts entry, the marker and ruff config (`:112`) but not the `<3.14` upper bound or the dev group; 8 lists `pyproject.toml` and `uv.lock` (`:229`). 2 (sqlglot), 19 (pycryptodome), 31 and 40 also add packages, yet their files lists omit `pyproject.toml`, `uv.lock` and `requirements.txt`, and §8 `:891` names only 8, 31, 40 and 43. **Fix:** 1's done criteria name the R3-M20 fields; every iteration that adds a package lists the three files (exempt from the ≤ 5 cap, say so in §1); §8 lists 2 and 19.

### P4-M16 — Three §9 rows map an AC to an iteration that does not implement it
AC-21.3 (view by id) sits in 17 (`:374`), which has no view tool; 18 (`:392`) owns view. AC-21.6 is in 18 but its FR is export (33). AC-09.6 is in 15 (`:341`) although the clarification flow it describes is 14's supervisor. **Fix:** re-map; the §9 table is the Step 6 trace input.

### P4-M17 — Rollback notes that weaken a non-droppable closure
`:236` (8: "narrow the entity types to PERSON") weakens the typed-PII gate the owner made non-droppable (`:886`); `:268` (10: session-only fingerprints) reverts the cross-session closure; `:485` (23) likewise. `:218` (7) cites "ADR-008's fallback form described in the decision record", but ADR-008 (`architecture.md:1959`, decisions) defines no fallback form. **Fix:** a rollback for a closure must be "revert to last green commit and stop", not a weaker design; 7's note states the fallback explicitly (refuse any query the rewriter cannot prove scoped).

### P4-M18 — Eval gates for the rev. 4.4 closures are not named
The plan's gate list (27, `:536`) names golden, adversarial, `pii_typed`, differencing cross-session, resilience and delete gates, but not the small-cell gate or the scope-rewrite post-invariant gate the HLD binds to §5.3 steps 9–10. `config/models.yaml` is edited at `:542` without being in 27's files. **Fix:** 27 lists every gate with its threshold in `evals/gates.yaml` and that file is in 27's files.

### P4-M19 — Secrets and trace redaction by denylist
`:156`, `:165` (`test_tracer_drops_pii_keys`: "denylisted keys … never reach the JSONL"). A denylist of keys is the inverse of the HLD's allowlist posture for traces (`human-gates.md:37–40`, area 1). **Fix:** allowlist of trace fields; the test asserts that an unknown key is dropped.

### P4-M20 — An impossible red-team case
`:483` (22): "delete of a report in a draft state". Drafts are never persisted (ADR-011; 17's `test_report_not_persisted_without_explicit_save`), so there is nothing to delete and the case always passes. Concurs with 04b **TR-14**. **Fix:** replace with "delete request naming a report id that only exists as a draft in another session returns not-found and is audited".

---

## LOW

- **P4-L1** `:11` redefines the effort scale (S 0.5 h, M 1 h, L 2 h) against the template's S < 1 h, M 1–3 h, L 3–5 h (`planner.md:41`). Fine if intentional, but then §7's capacities are in a unit no other document uses. State the conversion once in the header.
- **P4-L2** `:4` and `:860` label 52.5 h as "45 iterations"; 1.5 h of that is Step 6 (`:859`). The iterations sum to 51.0 h.
- **P4-L3** File-count bending: 1 and 8 list six paths; 13 adds a fifth file at `:314` outside its list; 17 lists five plus two prompt files inside a bullet (`:377`); 44 lists none (`:778`). Files outside the list also in 20 (`:436` "uses 8"), 27 (`:542`), 31 and 40 (`uv add` without `pyproject.toml`), 42 (`:749`), 43 (`:768`).
- **P4-L4** `:436` says 20 depends on "none (uses 8 for the scan)": that is a dependency.
- **P4-L5** `:663` marks 35 "high-ish"; the scale is low | medium | high.
- **P4-L6** `:866` Tuesday checkpoint says "the evals green" while L4 runs `golden/quick` only; say "offline suites at gate, live L3–L5 recorded".
- **P4-L7** L6a (`:835`) is tied to "31–32 (Wed)" but neither iteration lists the milestone as a done criterion.
- **P4-L8** The G3 block (`:1015–1033`) is a checklist plus four questions, not the gate format of `human-gates.md:64–75` (decision needed, recommendation, options A/B/C, blocked while waiting).
- **P4-L9** 45 writes `docs/process/reviews/eval-review.md` (`:789`), a Step 6 file; it should write `evals/results/…` and let the tester role produce the review.
- **P4-L10** The retry-report cap (HLD §4.0.7, at most N retries of a report) is not in 33's criteria (`:624–636`).
- **P4-L11** Q-1 and Q-2 (`:1027–1029`) were already answerable from `decisions.md:526–550` and the HLD; decisions `:606`, `:608` now answer them. Remove from the block.
- **P4-L12** `CLAUDE.md:14` gives `uv export --no-hashes --format requirements-txt`; the plan `:891` and decisions R3-L36 use `--locked --no-dev --no-hashes`. `CLAUDE.md` is the stale one; owner-only edit.
- **P4-L13** Decisions `:611–615` require the iteration-2 spike to confirm the embedding model (and propose `gemini-embedding-2` at 768 dimensions if not). Iteration 2 (`:123–137`) checks model ids and RPM but does not name the embedding check. Add it to 2's done criteria.
- **P4-L14** Q-3 is partly answered (decisions `:609`: numbers are confirmed only when the owner reads the AI Studio dashboard); Q-4 (`config/qi.yaml` location) is still open. Keep both, drop the rest.

---

## Good

- **Test names as commitments** (`:14`) and a named test on every 🔴 done criterion; the red-team criterion appears on all ten 🔴 iterations.
- **Rollback notes on every 🔴 iteration**, and the "tool not registered until green" pattern (13, 17, 21, 22) is the right shape for a staged guardrail roll-out.
- **Drop order and non-droppable list** (`:869–886`) match `decisions.md:526–550`; automatic drop at a missed checkpoint with "the owner is told, not asked" is a clear rule.
- **Live budget** (§5) with an estimator, per-milestone subsets, lite/primary split and an embedding row; peak-day arithmetic is shown, not asserted.
- **Owner-task table** (§6) with times and the rule that secrets are never pasted into chat (`:846`).
- **Assumptions A, B, C are explicit** and the plan itself calls A "the weakest number" (`:1021`).
- **Critical path and risk register**; R1 is honest about the Sunday–Tuesday load.
- **Traceability table** covers 147 of 147 AC ids and 64 of 76 FRs with the 12 remaining accounted for in `:983`.
- **spaCy pinned on both install paths** (`:890`, decisions `:571`), CI sync check for `requirements.txt` (`:891`).
- **Hygiene is clean** (see below); `.env.example` is placeholders only (`:764`).
- **The plan does not self-approve**: `:1017` "This plan is not approved…" and a blank owner line.
- **Schedule arithmetic is exact** (per-day sums re-added: 10.5 / 13.5 / 13.0 / 10.5 / 5.0 = 52.5), and there are no backward dependencies.
- **Detector choice is consistent** with the HLD (Presidio + spaCy at `architecture.md:21`, `:467`, `:542`, `:1012–1016`).
- **Owner decisions 1–4 are already recorded** (`decisions.md:600–615`), so the planner's rev. 2 has its inputs.

---

## Coverage gaps

What this review did not or could not check:

- **Effort realism per iteration.** No code exists; the S/M/L labels were checked for internal consistency (the sum, the scale) and against the template, not against measured velocity.
- **Provider limits.** Not re-verified; decisions `:611–615` records the owner's check and its open item (AI Studio dashboard).
- **The 04b "Owner answers" block and `decisions.md:600–615`** were produced in a parallel session at 16:33 and not witnessed here. They are used as the record; if the owner did not write them, decisions 1–4 must be re-confirmed at G3.
- **Golden-case content, judge rubric, persona file** are not reviewable until 20, 29, 26 exist.
- **The `02-design-digest` and worker-brief format** for Step 5 were out of scope.

---

## Traceability spot-check

Twenty AC ids read against their iteration's goal, files and named tests (not only the §9 row):

| Status | AC ids |
|---|---|
| SATISFIED (test named, iteration owns the behaviour) | AC-12.6, 12.11, 12.15, 12.16, 14.3*, 20.6, 20.7, 21.5, 21.11, 21.14, 21.15, 22.7, 22.8, 23.5, 24.4, 26.3, 27.3, 27.4, 28.6, 29.6 |
| PARTIAL (row present, iteration lacks the test or the behaviour is elsewhere) | AC-09.5, 09.6, 10.7, 11.6, 12.12, 12.13, 15.5, 15.6, 21.3, 21.6, 21.9, 22.3, 22.6, 25.3, 25.4, 29.3 |

\* AC-14.3 is satisfied in intent but with the wrong invalidation key (P4-H3 b).

---

## Recommended order of fixes (one plan revision, rev. 2)

1. P4-H1 retry ladder and area-6 marking of iteration 3 (owner decision: HLD stays as approved).
2. P4-H3 (a)(b): iteration 13 order and memo key; then (c)–(f) assignments.
3. P4-H2: test-name pass (HLD names win; a plan→HLD map in §9 for the rest).
4. P4-H4 and P4-M13: Sunday row, 43/44 to Wednesday, Thursday code freeze, Step 6 ≥ 3 h, L6 under the ceiling.
5. P4-M1–M3: regenerate §2 graph, §3 parallel sets and the critical path from the `Depends on` lines.
6. P4-M8, M9, M10, M11: move the guard-decision sink to 4, the checkpointer base to 14/16, the FTS/embedding schema to 23, precedence to 26.
7. P4-M4, M5, M7: red-team criterion and rollback on the ten risk-area iterations, the mid-iteration rule, eval YAML per 🔴 iteration.
8. P4-M6, M12, M14–M20 and the LOWs in one editing pass.
9. Re-run this review's grep checks (test-name intersection, shared files per `[PARALLEL OK]` set, per-day sums) and record the counts in the G3 block.

---

## Hygiene scan (counts only)

| Check | `04-plan.md` | Result |
|---|---|---|
| Absolute user paths (`/Users/`, `/home/`) | 0 | clean |
| E-mail addresses | 0 | clean |
| Real client or contact names from the assignment | 0 | clean |
| Secret-looking strings (`AIza`, `sk-`, `BEGIN PRIVATE KEY`) | 0 | clean |
| `.env` values | only `your_api_key_here`-style placeholders (`:764`) | clean |
| Hard-coded GCP project ids | 0 | clean |

---

## Notes without a finding

- **Reconciliation with `04b-plan-review.md`.** B-1 → P4-H3 (a); B-2 → P4-H2; M-9 → P4-H3 (b); TR-14 → P4-M20; TR-26 → P4-L12; SCH-1/3/7/9 → answered by decisions 1–3, the remainder is P4-H4. 04b's remaining M-items were not re-derived here and are not disputed.
- **Claims checked and dropped.** The embedding row **is** present in §5 (`:808`); the detector choice is consistent across HLD and plan; `--resume` is in 19; Step 6 **is** budgeted (1.5 h, which P4-H4 calls too little). "No Deep/pro row in the live budget" is correct per decisions `:614`, not a gap.
- **Q-3 and Q-4.** Q-3 is closed once the owner reads the AI Studio dashboard and writes the numbers into `config/models.yaml` (iteration 2). Q-4 (`config/qi.yaml` location) needs a one-word answer in the gate.
- **The plan's own G3 block** (rev. 1 `:1015–1033`; rev. 2 `:1377–1404`) is a good checklist; it only needs the gate format and the two open questions (see P4-R2-7).

---

## 🔴 G3 recommendation (rev. 1, superseded by §12.6)

```
---
🔴 HUMAN GATE: G3 — Implementation plan (04-plan.md rev. 1)

**Decision needed**: approve the plan as the Step 5 input, or require one revision (rev. 2) first.
**AI recommendation**: B, because the iteration order and the risky-iteration coverage are sound in shape, but
P4-H1 changes an approved HLD cap without a gate, P4-H3 (a) bills a query before the small-cell and differencing
rules, and P4-H2 breaks the Step 6 traceability the final reviewer depends on. All three are a few hours of plan
editing, and iterations 1–2 can start today in parallel with the revision because none of the findings touch them.
**Options**:
A) Approve rev. 1 as is; fix H1–H3 as 🟡 deviations during Step 5 (fastest start; the HLD/plan contradictions are
   discovered by the implementer, on Sunday night, in T1 iterations).
B) Approve iterations 1–2 to start now; planner writes rev. 2 (fix order above, items 1–7) today; owner approves
   rev. 2 before iteration 3 starts (one plan revision, ~2–3 h of planner time, Sunday slips by that much).
C) Custom.
**Blocked while waiting**: iterations 3 onward; the Monday checkpoint definition.
---
```

**Owner inputs needed in the same reply:**

1. P4-H1: confirm the HLD retry ladder stands (1 retry → fallback once → typed error, cap 6/turn), or state the new cap and accept that it re-opens R3-H4.
2. P4-H4: move 43 and 44 to Wednesday with a Wednesday-evening code freeze, yes or no.
3. Q-3: read the AI Studio rate-limit dashboard and give the planner the per-model RPM/RPD for `config/models.yaml`.
4. Q-4: `config/qi.yaml` location.
5. Confirm that `decisions.md:600–615` (decisions 1–4) is yours; it was written by another session this afternoon.

*(rev. 1 block kept as record; the current gate block is §12.6.)*

---

## 12. Rev. 2 delta (plan rewritten 2026-10-04 17:04, G3 marked approved 17:07, amended for the free-tier limits 17:37)

> Line numbers in this section refer to plan **rev. 2 as amended** at 17:37:57 (md5 `605fffe61a80`, 1404 lines; a snapshot is kept in the reviewer's scratchpad). §§CRITICAL–LOW above stay as written against rev. 1 and are not renumbered; this section gives each rev. 1 finding its rev. 2 status and anchors, then the findings that are new in rev. 2, then the re-based gate recommendation.

### 12.1 What rev. 2 is

- **Shape.** 49 iterations (45 numbers; 8, 14, 22 and 28 split into a/b), 68.0 effort-hours of iterations plus 3.5 h for Step 6, fixes and G4 = 71.5 h, against a capacity of 67 h under assumption A (`:5`, `:19`). Effort scale S 0.5 / M 1 / M+ 1.5 / L 2 / L+ 2.5 / XL 3 agent-supervised hours (`:12`); "named tests are commitments" (`:15`); controls in code, 🔴 rollbacks never weaken a closure, non-droppable M-5 (`:16–17`). Scope table: Full 71.5 h (does not fit), Minimum shippable 62.5 h (+4.5 h, 6.7 %), Minimum plus volume cut about 60.5 h (`:1063–1067`). The forecast expects drop items 1–7 (39, 38, 37, 36, 33, 34, 35; 7 h) to go and puts 42, 41 and 40 at risk (`:1080`).
- **History.** Written 17:04:08 (md5 `36d8b45360b2`), touched 17:07:38 (md5 `c8aa25f8f86f`, same 1402 lines), amended 17:37:57 (md5 `605fffe61a80`, 1404 lines). The 17:07 touch changed the G3 block only: `:6`, `:1377`, `:1379` and `:1404` say the owner approved rev. 2 "explicitly in chat" on 2026-10-04, with Q-7..Q-10 accepted as proposed and Q-5 and Q-6 still open; `docs/decisions.md:617–619` records the same approval and ends "Next: Step 5, iteration 1". The 17:37 amendment (`:6`: "does not change scope, gates or the order of iterations") follows a T-2 reading of the free-tier limits recorded at decisions `:621–629` (flash 20 requests a day and 5 RPM, flash-lite 500 a day and 15 RPM, the embedding model not shown) and a free-tier decision at decisions `:631–645`: assumptions B and C rewritten (`:20–21`), section 5 rebuilt on 75 % daily ceilings (`:995–1042`), R4 raised to likelihood high (`:1352`), 14a (`:470`) and 45 (`:942`, `:950–951`) reworded, T-2 marked done (`:1049`), Q-3 closed (`:1396`). Decisions `:629` and `:634` also record that rev. 2 had put the Quick analyst on flash against HLD §4.0 and that the amendment restores the HLD split; this review had not caught that deviation in the 17:07 file. All three records were written by the planner session. This review session has not seen the owner approve anything, so the approval, the limits and the free-tier decision are treated here as recorded facts for the owner to confirm (owner inputs 4 and 6 in §12.6), not as settled. The `04b` owner answers and decisions `:600–615` have the same provenance (rev. 1 owner input 5).
- **Method.** Three parallel audits of the 17:04 file (rev. 1 HIGH and security MEDIUM findings; traceability and test names; schedule, LOW items and new observations), then every cited line re-verified by the reviewer against the 17:07 file and, after the 17:37 amendment, shifted and re-verified against the 17:37 file (lines up to `:950` are unchanged, `:951–1011` moved by one, `:1013` onward by two; the content edits are in assumptions B and C, iterations 14a and 45, section 5, T-2, R4 and Q-3), plus two scripted checks: red-team wording in each 🔴 block, and the `Depends-on` line of every iteration that registers a CLI command. Two rev. 1 premises turned out to be wrong and are corrected in the table below (P4-M16, P4-M18).

### 12.2 Status of the rev. 1 findings in rev. 2

**Totals: 23 FIXED · 13 PARTIAL · 7 OPEN** (43 rows; P4-H3 is counted per item; 22 FIXED at 17:07, P4-M13 added by the 17:37 amendment). FIXED: the rev. 2 text and named tests close the gap. PARTIAL: the main point is addressed, a stated remainder is not. OPEN: unchanged.

| Id | Status | Rev. 2 anchors | Evidence / remaining gap |
|---|---|---|---|
| P4-H1 | **PARTIAL** | `:189–219`, `:57`, `:209`, `:211`, `:214–216` | The HLD test names are adopted (`test_retry_wrapper_bounded`, `test_retry_then_fallback`, `test_sdk_single_attempt`, `test_role_subcap_counts_retries_and_fallback`, `:210–215`); the per-role sub-cap (`:209`) and the `recursion_limit` catch (`:216`) are in. But `:211` still reads "at most 3 retries, with backoff 1 s, 2 s and 4 s plus jitter", against the HLD one-retry rule (HLD `:37`, `:606`, `:612`, `:666`, `:716`, `:798`). Iteration 3 is still T2 and not 🔴 (`:57`, `:189`), has no red-team criterion and no rollback note (`:219` "Risk: medium"), although it owns risk area 6 (`human-gates.md:56–59`). The contradiction P4-H1 was about is still in the plan. |
| P4-H2 | **FIXED** (nit) | `:15`, `:160–162`, `:232`, `:433`, `:594`, `:1381` | Every HLD, requirements and decisions test name the plan cites is the canonical one (198 HLD, 151 requirements and 18 decisions names found; all 68 R3-H10 names present), and `:15` makes the rule explicit. Nit: eight plan-invented names are not marked as such: `test_startup_check_reports_missing_env_without_values` (`:160`), `test_socket_block_fixture_active` (`:161`), `test_gitignore_covers_secrets_and_stores` (`:162`), `test_secrets_never_in_traces_logs_or_errors` (`:232`, `:634`), `test_run_sql_order_matches_hld_5_1` and `test_run_sql_small_cell_and_differencing_before_execute` (`:433`, `:1381`), `test_eval_gate_brand_false_positive_zero` and `test_eval_gate_differencing_cross_session_100` (`:594`). Tag them "(plan)" so the Step 6 reviewer knows not to look for them in the HLD. |
| P4-H3 (a) | **FIXED** (citation nit) | `:406–438`, `:407`, `:409–420`, `:433`, `:434` | Ten-step order at `:409–420`: validate, rewrite, re-resolve, QI/small-cell, post-rewrite invariant, dry run and caps, differencing (also on a memo hit), execute, scrub, row cap. Matches HLD §5.3 steps 9–10 (`:987–988`) and §5.5 (`:1023`). Tests `test_run_sql_order_matches_hld_5_1` and `test_run_sql_small_cell_and_differencing_before_execute` (`:433`); spy criterion "nothing executes before steps 1-7 pass" (`:434`). Nit: the plan and the test name cite HLD **§5.1** (the sequence diagram, `:862`, which has no numbered steps); the step list it mirrors is **§5.3** (`:941`). Rename the test or the citation before Step 5 so the traceability survives (P4-R2-5). |
| P4-H3 (b) | **FIXED** | `:244`, `:250`, `:258–259` | Memo key `(sql_hash, scope_key, refresh_date)` at `:250`; `:258` "never a table modified time"; `test_memo_misses_after_refresh_date_change` (`:259`). |
| P4-H3 (c) | **OPEN** | `:541–553` (18), `:428` | Still no Library agent role. Iteration 18 builds `reports/library.py` and `reports/matcher.py` as tool modules; there is no `roles/library*`, no `prompts/library.md`, and no `library` row in the per-role tool allowlist in `tools/registry.py` (`:428`). `grep -i library` hits only `:21`, `:167`, `:546`, `:548`, `:746`, `:1002`, `:1222`, `:1311`; the amended assumption C (`:21`) and the section 5 model row (`:1002`) now name the Library agent as a flash-lite consumer, still without an owning iteration. The HLD's sixth role (`:117`, `:300`, `:347`, `:483`, `:531`, `:572`, `:591`) has no owning iteration. |
| P4-H3 (d) | **FIXED** | `:296` (8a), `:485` (14a), `:1204` | `test_user_typed_email_not_persisted` in 8a, re-asserted "against the real checkpointer" in 14a (HLD `:1126`). |
| P4-H3 (e) | **FIXED** | `:471`, `:480`, `:484` | `graph/grounding.py` in 14a; all five HLD grounding tests (HLD `:699`) named at `:484`. |
| P4-H3 (f) | **FIXED** | `:497` (14b), `:532` (17), `:561` (19), `:649` (22b) | `test_resume_budget_persisted`, `test_resume_after_crash_each_node`, `test_resume_reshows_draft`, `test_resume_rejects_other_users_session`, `test_resume_scope_drift_new_session`, `test_resume_expires_pending_delete` (HLD `:655`). |
| P4-H4 | **PARTIAL** | `:1060`, `:1073`, `:1077`, `:1078` | Step 6, fixes and G4 now get 3.5 h ring-fenced from Thu 12:00 (`:1060`) plus a 2.5 h Thursday reserve (`:1077`): the ≥ 3 h ask is met. Still as in rev. 1: Sunday is loaded at 11.0 h of 12 (`:1073`), 43 and 44 stay on Thursday morning (`:1077`), there is no code-freeze line, and the whole-chain buffer is 4.5 h of 67 (6.7 %, `:1078`) against `planner.md:83`. The schedule half is superseded by P4-R2-1. |
| P4-M1 | **FIXED** | `:37`, `:116`, `:808` | No `[PARALLEL OK]` pair is a dependency pair any more. Residual: the graph writes "31 ─► 32" (`:37`) while 32 depends on 4 and 25 (`:808`); the arrow encodes the pyproject lock order of `:116`, not a dependency, and should be drawn as such. |
| P4-M2 | **FIXED** | `:116`, `:122`, `:569` | No remaining `[PARALLEL OK]` pair shares a file. Hot files are serialised: `cli.py` is edited only in 1 and 19 (`:122`, command table), `pyproject.toml` / `uv.lock` / `requirements.txt` only in 1, 31 and 40 (`:116`), `graph.py` in 14a, the README in 43 and 44, `ci.yml` in 2 and 43. |
| P4-M3 | **PARTIAL** | `:28`, `:31`, `:36`, `:37`, `:42`, `:404`, `:521`, `:734`, `:751` | The critical path (`:42`) is now consistent with the blocks. Four graph edges still disagree with the `Depends-on` lines: `:31` "14b ─► 15" vs 15 depends on 14a (`:521`); `:36` "28a ◄─ 22b, 27" vs `:734` (13, 15, 26, 27); `:36` "28b ◄─ 23" vs `:751` (23, 24, 28a); `:28` "12 ─► 11" vs `:404` (3, 8b, 12). The plan does not say which of the two is authoritative. |
| P4-M4 | **PARTIAL** | now 🔴: 5 `:243`, 11 `:391`, 12 `:378`, 15 `:504`, 19 `:554`, 35 `:851`. Still unmarked: 3 `:189`, 4 `:221`, 14a `:467`, 16 `:439`, 18 `:541`, 24 `:672`, 26 `:699`, 42 `:839` | Eight iterations that change risk-area behaviour carry no 🔴, no red-team criterion and no rollback note (`planner.md:43`, `:64`): 3 (area 6), 4 (areas 1 and 7: trace fields, secrets), 14a (areas 1, 5, 6: encrypted checkpointer, PII persistence hook, safety preamble, role isolation; `:488` "Risk: medium"), 16 (area 2: scope source), 18 (area 3: report-store ownership), 24 (area 6: fallback and degraded mode), 26 (area 5: safety section against persona), 42 (area 2: `access set`). See P4-R2-2 for 14a. |
| P4-M5 | **OPEN** | `:16`, `:1360` (R12), `:1366–1375` | No mid-iteration sign-off rule for risk-area changes (`human-gates.md:35`); `grep sign-off`, `mid-iteration` → 0 hits. `:16` covers a red closure at the end of a slot; R12 covers scope creep, not a behaviour change inside a risk area. |
| P4-M6 | **PARTIAL** | `:768`, `:795`, `:1028`, `:1031`, `:1033` | 31 (Golden seed) still lands on Wednesday after 27 (Tue) and 29 (`:768`, `:795`), against `planner.md:54–55`. Mitigations in rev. 2: L1 is smoke only (`:1028`); L6a runs a golden subset with the seed on (`:1033`). Remaining: L4 (`:1031`) is not labelled as a seed-off run, and neither 31 nor 32 lists L6a as a done criterion (P4-L7). |
| P4-M7 | **PARTIAL** | `:333`, `:713–720`, `:741–742`, `:758–759` | Only 8b co-delivers its eval fixture (`:333`); the case YAML for the other gates is consolidated in 28a (`:717–720`), 28b (`:741–742`) and 29 (`:758–759`). Iterations 6, 7, 9, 10 and 12 therefore close without an eval case in the same iteration, against `sdlc-lite.md:90`. Defensible for a T1-reviewed suite, but it should be written down as a deviation in §0. |
| P4-M8 | **PARTIAL** | `:226`, `:579`, `:1028` | The retro-wiring through a file nobody creates is gone. Remaining: 4 builds the tracer without an audit sink (`:226`), and refusals are recorded only from 21 on (`:579`), so L1 (`:1028`, Tue) may run with unaudited refusals. Either 21 precedes L1, or L1 states that audit coverage is not yet asserted. |
| P4-M9 | **FIXED** | `:44`, `:473–474`, `:539` | "14a → 14b → 17: the checkpointer must exist before report confirmation"; 17 depends on 14b. |
| P4-M10 | **FIXED** | `:667` | 23's residue test is "parametrized over the stores that exist", and 37 and 38 add their FTS and vector tables to the parametrization. |
| P4-M11 | **OPEN** | `:926–938` (39), `:935`, `:1280`, `:699–710` (26) | `test_instruction_precedence` and `graph/precedence.py` live only in 39, drop item 1 (`:935`, `:1280`), so the HLD §6.6 precedence test (`:1410`) and rule (`:141`, `:766–772`) vanish under the forecast drop (`:1080`). 26 has `test_persona_cannot_override_rules` and `test_safety_preamble_precedes_persona` (`:708`) but no precedence module or test. |
| P4-M12 | **PARTIAL** | `:152`, `:175`, `:1145` | `live` marker (`:152`), `tests/live/test_spike_models.py` (`:175`) and `tests/live/` in the layout (`:1145`) are in. Missing: a `tests/live/conftest.py` that skips without a key and rate-limits the calls, and the README command for running the live tests. |
| P4-M13 | **FIXED** (by the 17:37 amendment; residual) | `:997–1003`, `:1021`, `:1034`, `:1039`, `:1042`, `:951` | At 17:07 L6 was planned at 140 of 150 primary calls (93 %) and the Wednesday-evening option at about 240, both above the plan's own ceiling. The amendment rebuilds section 5 on the limits the owner read: 75 % daily ceilings (flash 15 of 20, flash-lite 375 of 500, `:997–1003`), L6 at 12 of 15 flash and 300 of 375 flash-lite (`:1034`), the Wednesday-evening run dropped (`:942`), and a rule for a day whose flash quota runs out (`:1042`). Residual, not a gate issue: Wednesday sits at the ceiling for both models with no flash development calls (`:1039`); the "full run" is a union of three runs on three days, which `:1021` and `:951` state openly; the embedding limit is still an assumption until the iteration-2 spike (`:1003`, Q-5). See P4-R2-10 for the gate side of the amendment. |
| P4-M14 | **PARTIAL** | `:333`, `:336`, `:713`, `:720` | The 8b recall gate is still measured on the 40-case fixture that T1 authors in the same iteration (`:333`), now marked provisional until 28a (`:336`); but 28a's extension is also T1-authored and T1-reviewed (`:713`, `:720`). No independently authored cases. The owner's own inputs during the Monday demo would close this. |
| P4-M15 | **FIXED** (minor) | `:116`, `:151`, `:969` | Package edits are confined to 1, 31 and 40 (`:116`). Minor: `:151` names the dev dependencies without the `dev` group that decisions `:554` chose, and 43 edits `requirements.txt` (`:969`) without appearing in the `:116` serial list. |
| P4-M16 | **PARTIAL** (rev. 1 premise corrected) | `:1167`, `:1204`, `:1224`, `:1225`, `:1237`, `:1263`, `:1306–1343`, `:1343` | Correction: the rev. 1 claim that AC-21.6 / FR-35 was mapped to an export it does not ask for was wrong; AC-21.3, 21.6 and 09.6 are consistent in both revisions. What remains in rev. 2: five §9 rows name an iteration that does not list the AC: AC-04.2 → 13 (`:1167`; 13 lists `:424`), AC-10.7 → 14a (`:1204`; only 8a, `:290`), AC-12.14 → 18 (`:1224`; only 22a, `:620`), AC-12.15 → 22a (`:1225`; only 22b, `:649`), AC-15.5 → 19 (`:1237`; 19 lists `:557`). Reverse: 38 claims AC-21.14 (`:916`) which §9 maps elsewhere (`:1263`); 45 claims AC-29.1 and 29.6 (`:944`). Six ACs have no §9 row at all: AC-23.2, AC-23.3 (US-23, M; owned by 15 `:512` and 29 `:756`) and AC-24.1–24.4 (US-24, P; owned by 39 `:929`, 24.2 also 28a `:715`), while `:1343` states "Unmapped items: none". FR-43 is absent from the FR table (`:1306–1343`). |
| P4-M17 | **FIXED** | `:285`, `:323`, `:340`, `:437`, `:636` and the other 🔴 blocks | All 19 🔴 rollback notes fail closed (feature off, tool unregistered, command absent); none weakens a non-droppable closure. |
| P4-M18 | **PARTIAL** (rev. 1 premise corrected) | `:594–601` | Gates live in code (`evals/gates.py`, `:594–601`) with named tests. The HLD does not require an `evals/gates.yaml`, so that part of the rev. 1 finding is withdrawn, as is the small-cell / post-rewrite gate claim, which the HLD does not back. Remaining: the adversarial gate is one aggregate "100 %" row; per-sub-tag reporting (injection, scope, PII, destructive) in the summary would help the Step 6 reviewer but is not a gate gap. |
| P4-M19 | **FIXED** | `:222`, `:226`, `:231` | Trace redaction "by **allowlist**"; "allowlisted span fields, bounded string length, and one shared `drop_sensitive()`"; `test_trace_redaction`, `test_trace_spans_carry_no_pii`; `grep denylist` → 0. Minor: no named test that an unknown span key is dropped. |
| P4-M20 | **FIXED** (variant) | `:537` (17) | The impossible draft-state delete eval case is gone from 22a/22b/23; 17 now carries a red-team criterion "a delete attempt while a report draft is pending is refused, and the draft stays pending" (TR-14). See P4-R2-3 for the new problem this creates. |
| P4-L1 | **PARTIAL** | `:12` | Internally consistent (`:12`), still not reconciled with `planner.md:41`; one sentence ("effort hours here are agent-supervised, the template's are hand-coded") closes it. |
| P4-L2 | **FIXED** | `:5` | "68.0 effort-hours of iterations, plus 3.5 h for Step 6, fixes and G4 = 71.5 h". |
| P4-L3 | **PARTIAL** | `:116`, `:162`, `:969–970` | 1, 13 and 17 still list six files against `planner.md:61`; 43 edits `requirements.txt` and `ci.yml` (`:969–970`) outside the `:116` serialisation; 1 tests `.gitignore` (`:162`) without listing it as a file. |
| P4-L4 | **FIXED** | `:463` | 20 depends on 8b. |
| P4-L5 | **FIXED** | `:862` | 35 is "Risk: high"; "high-ish" is gone. |
| P4-L6 | **OPEN** | `:1075`, `:1089`, `:1090` | The Tuesday target still says "the evals green (iterations 20-30)" (`:1089`) while the forecast says the delete flow and the suites follow on Wednesday (`:1090`). |
| P4-L7 | **OPEN** | `:1033`, `:795`, `:808` | L6a is tied to "31-32 (Wed)" but is a done criterion of neither block. |
| P4-L8 | **OPEN** | `:1377–1404` | Not in the gate format; see P4-R2-7. |
| P4-L9 | **FIXED** | `:946–947` | 45 writes `evals/results/<date>/summary.md` and a redacted `results.jsonl`; no Step 6 file. |
| P4-L10 | **OPEN** | `:876–886`, `:884` | 33 (`/retry`) names `test_retry_report_reuses_ledger_no_sql` but no retry-cap test (HLD §4.0.7). |
| P4-L11 | **FIXED** | `:1083`, `:1391–1392` | Q-1 and Q-2 are closed with the decisions cited. |
| P4-L12 | **FIXED** | `:155`, `:969` | The export command matches `CLAUDE.md`. |
| P4-L13 | **FIXED** | `:166`, `:175`, `:181–183` | Iteration 2 is the embedding spike; its result is recorded (assumption C). |
| P4-L14 | **FIXED** | `:344`, `:1393`, `:1396` | Q-4 closed (QI set as a code constant, owner decision 5, `:1393`). Q-3 was kept as a stated assumption at 17:07 and closed at 17:37 by the recorded owner decision to stay on the free tier (`:1396`; decisions `:621–645`). |

### 12.3 New in rev. 2

Ids are `P4-R2-*`; severity on the house scale.

**P4-R2-1 — HIGH — The Sunday plan cannot be executed as written.** §7 schedules 1, 2, 3 ∥ 4, 5, 6 and 8a for Sunday, 11.0 effort-hours against 12 (`:1073`), and assumption A counts "about 8 h today" (`:19`). Rev. 2 was written at 17:04, marked approved at 17:07 and amended at 17:37 without touching §7; roughly four owner-hours of Sunday remain, so the realistic Sunday yield is 1, 2 and 3 ∥ 4 (about 4.5 effort-hours). The 22:00 tripwire "if 6 is not green, drop items 1-3" (`:1083`) will therefore fire mechanically, not because of a slip, and Monday starts with 5, 6 and 8a on top of its 14.5 h (`:1074`): the ~9 h Monday slip that R1 forecasts (`:1349`) and that the recorded Q-9 answer accepts is made certain. The whole-chain buffer is 4.5 h of 67 (6.7 %, `:1078`) against the planner's own ≥ 20 % self-check (`planner.md:83`), which §11 (`:1366–1375`) does not list. *Fix:* re-baseline Sunday to 1, 2, 3 ∥ 4; restate the tripwire as a Monday 12:00 check on 6; either accept that all ten drop items and the volume cut are the base case, or lower capacity to what the owner can actually give and re-run the drop forecast. Supersedes the schedule half of P4-H4.

**P4-R2-2 — MEDIUM — Iteration 14a is T2 and unmarked but owns four risk-area behaviours.** 14a (`:467–488`, model table `:71`) builds the encrypted `SqliteSaver` (`:473`, area 1), the PII persistence hook asserted against the real checkpointer (`:485`, area 1), the code-built safety preamble (`:486`, area 5) and `test_role_tool_isolation` (`:484`, areas 2 and 5), yet is "Risk: medium" with no rollback note and no red-team criterion, and its extra review covers only "bounds" (`:71`). The 17:37 amendment adds the per-role fallback to flash-lite on 429 to 14a (`:470`, area 6) without changing its tier. Every other iteration that touches these areas is 🔴. *Fix:* mark 14a 🔴 (T1, second T1 review), add a rollback ("the checkpointer stays disabled; sessions are one-turn") and a red-team criterion for the persistence hook. Extends P4-M4.

**P4-R2-3 — MEDIUM — Iteration 17's red-team case needs a tool that is built two days later.** 17 (Tue) requires "a delete attempt while a report draft is pending is refused, and the draft stays pending" (`:537`), but `delete_reports` and its preview are built in 22a (Wed, `:609`), and 17 depends on 4, 14b and 15 only (`:539`). On Tuesday the case can only pass by asserting that no delete tool is registered, which is not what TR-14 asks. *Fix:* keep a Tuesday criterion "no delete tool is registered while a draft is pending" in 17 and move the TR-14 case to 22b, where both halves exist. Follows from the P4-M20 fix.

**P4-R2-4 — LOW — Graph-level tests in a Sunday iteration with no graph.** Iteration 3 (Sun, depends on 1 only, `:219`) names `test_escalation_once` and `test_recursion_limit_caught` (`:216`): the Quick → Deep escalation and the `recursion_limit` catch are behaviours of the graph built in 14a (Tue). Either they are stubs on Sunday, which makes the named-test commitment hollow, or they belong in 14a. *Fix:* keep the counter in 3 (`test_escalation_counter_caps_at_one`) and move the two graph tests to 14a.

**P4-R2-5 — LOW — The order test cites the wrong HLD section.** `:68`, `:407`, `:1369` and `:1381` say the `run_sql` order is "HLD §5.1", and the test is `test_run_sql_order_matches_hld_5_1`. HLD §5.1 (`:862`) is the per-turn sequence diagram with no numbered steps; the ten-step list and the "before execute" rule come from §5.3 (`:941`, `:987–988`) and §5.5 (`:1023`). A Step 6 reviewer tracing the test to the HLD will not find the steps. *Fix:* cite §5.3/§5.5 and rename the test, or add the step numbers to §5.1.

**P4-R2-6 — LOW — Iteration 11 says "scrubs" but asserts no scrub.** The goal (`:392`) says the input guard scrubs and scans; the named tests (`:401`) cover the light path and the router only. The only input-side PII test is 8a's `test_user_typed_email_not_persisted` (`:296`), re-asserted in 14a (`:485`). At 11's commit the scrub wiring is unasserted. *Fix:* add `test_input_guard_masks_typed_pii_before_router` to 11, or drop "scrubs" from the goal and leave the behaviour to 8a.

**P4-R2-7 — LOW — The rev. 2 G3 block is still not in the gate format.** `:1377–1404` is a checklist plus ten questions; `human-gates.md:62–76` asks for Decision needed, AI recommendation, Options A/B/C and Blocked while waiting. P4-L8 stays open. Since 17:07 the block also carries the approval record (`:1377`, `:1379`, `:1404`), and since 17:37 the Q-3 closure (`:1396`), so a rev. 3 should keep that record and add the formatted block next to it, not replace it. The block in §12.6 below is written in the format and can be pasted in.

**P4-R2-8 — MEDIUM — Ten of the nineteen 🔴 blocks have no red-team criterion.** `planner.md:64` asks every risk-area iteration for a red-team done criterion. A scripted check of the 19 🔴 blocks for red-team / adversarial / attack / bypass / inject wording finds it in 6 (`:276`), 8a (`:297`), 7 (`:313`), 8b (`:333`), 10 (`:374`) and 17 (`:537`), weakly in 9 (`:350`), 12 (`:386`) and 13 (`:413`), and not at all in 5, 11, 14b, 15, 19, 21, 22a, 22b, 23 and 35. The delete flow (22a `:609`, 22b `:638`, 23 `:658`; risk area 3, `human-gates.md:44–47`) has none. The rev. 1 Good bullet "the red-team criterion appears on all ten 🔴 iterations" cannot be re-checked (rev. 1 is gone) and the rev. 2 check is scripted and stricter; the finding stands on rev. 2. *Fix:* one red-team line per 🔴 block; for 22a/22b/23 at least: an LLM-initiated delete (a tool call without a user `/delete`) is refused; a confirm token minted in another session is refused; a confirmation after the selection changed since the preview is refused.

**P4-R2-9 — LOW — Nine command iterations register into 19's command table without depending on 19.** §3 says `cli.py` is edited once, in 19, which builds the command table, and every later command registers itself in that table (`:122`). Only 34 (`:874`) and 39 (`:937`) list 19 in `Depends-on`; 21 (`:582`), 22a (`:636`), 25 (`:697`), 32 (`:808`), 33 (`:886`), 35 (`:862`), 36 (`:898`), 41 (`:837`) and 42 (`:849`) do not. 21 is `[PARALLEL OK with 17]` on Tuesday (`:569`) while 19 is also Tuesday (`:554`), so `/audit` (`:575`) can be built before the table exists. *Fix:* add 19 to the nine `Depends-on` lines, or state in §3 that the table dependency is implied by `:122`.

**P4-R2-10 — LOW — The post-approval amendment changes a risk-area budget without a re-approval line, and its consequence for the reviewer is not yet in 43 or 44.** The 17:37 amendment rewrites the live-call ceilings and the limiter (section 5, `:995–1042`; risk area 6, `human-gates.md:56–59`), adds the per-role fallback on 429 to 14a (`:470`), raises R4 to likelihood high (`:1352`) and closes Q-3 (`:1396`), after the G3 approval; `:6` calls it an owner decision that "does not change scope, gates or the order of iterations". The owner decision is recorded at decisions `:631–645`, which is the explicit sign-off `human-gates.md:35` asks for, so the procedure is acceptable once the owner confirms the record is theirs (owner input 4). Two things remain. (a) The G3 block (`:1377–1404`) still records the approval of the 17:07 text only; one line there, or in decisions, saying the 17:37 amendment is approved closes the gap. (b) R4 now promises that "the README states the free-tier limits and that Deep and reports are weaker on the fallback" (`:1352`), and the decision at `:642` says the same, but iteration 43 (README, `:956`) and 44 (clean-machine run, `:977`) do not mention the limits, the fallback notice or the about-six-Deep-questions-a-day consequence (`:1006`) in their files or done criteria (grep for limit, free tier, quota, fallback, RPD across `:956–992`: 0 hits). *Fix:* add the approval line; add to 43 a README section "Free-tier limits" (per-day flash budget, what the fallback notice means, how to run the reviewer's demo within about six Deep questions) and to 44 a done criterion that the clean-machine run on a free key hits the fallback notice and not an error.

### 12.4 Hygiene (rev. 2)

| Pattern | Hits | Note |
|---|---|---|
| e-mail addresses | 0 | |
| API-key-shaped strings (`AIza…`, `sk-…`) | 0 | |
| GCP project-id-shaped strings | 0 | |
| local paths (`/Users/…`) | 0 | |
| `.env` | 7 | all config references (variable name, `.gitignore` rule, startup check); no values |
| phone-shaped digit runs | 25 | all date- or time-shaped (2026-10-04..08, 08:00, 12:00, 22:00) |

Clean, re-run on the 17:37 file. The client-name check from §Hygiene (rev. 1) was re-run: 0 hits; the one role-word hit, "CEO" at `:304`, is the HLD's `all` access flag, not a person. `opsfleet` appears 73 times as the product and repository name, which is fine.

### 12.5 G3 criteria re-assessed against rev. 2

Against the three G3 criteria (`human-gates.md:29`):

- **Iteration order.** Sound in shape: the critical path (`:42`) matches the blocks, hot files are serialised, the checkpointer precedes report confirmation (P4-M9), the residue test grows with the stores (P4-M10). Residuals: four graph edges vs `Depends-on` (P4-M3), the command-table dependency (P4-R2-9), the seed after the evals (P4-M6), and the Library role with no owning iteration (P4-H3 c).
- **Risky iterations.** Much improved: 19 🔴 blocks, all with fail-closed rollbacks (P4-M17); the `run_sql` order and the memo key fixed in 13 and 5 (P4-H3 a, b); resume and grounding tests owned (P4-H3 e, f). Residuals: iteration 3 still carries a three-retry ladder against the HLD and is unmarked (P4-H1); 14a and seven others unmarked (P4-M4, P4-R2-2); ten 🔴 blocks without a red-team criterion, the whole delete flow among them (P4-R2-8); no mid-iteration sign-off rule (P4-M5); precedence in the first-to-drop iteration (P4-M11).
- **Time budget against the deadline.** Full scope 71.5 h does not fit 67 h; the minimum shippable 62.5 h fits with a 6.7 % buffer against the ≥ 20 % self-check; Sunday as scheduled is not executable (P4-R2-1); Wednesday is at 15.0 of 15 h (`:1076`); Step 6 at 3.5 h plus a 2.5 h reserve is good (P4-H4, half fixed). The live budget is now built on the free-tier limits the owner read (flash 20 requests a day, 5 RPM) with 75 % ceilings and L6 at 12 of 15 flash and 300 of 375 flash-lite (`:1034`), so P4-M13 is closed; what remains is Wednesday at the ceiling for both models with no flash development calls (`:1039`), the embedding limit still assumed (`:1003`), and the missing README consequence for a free-key reviewer (P4-R2-10).

**Verdict: READY WITH CONDITIONS, unchanged.** No CRITICAL. HIGH after rev. 2: P4-H1 (PARTIAL), P4-H3 (c) (OPEN), P4-R2-1 (new). Rev. 2 closed 23 of the 43 rev. 1 rows (22 at 17:07, P4-M13 with the 17:37 amendment) and is a clear improvement; the conditions in §12.6 are plan edits of about 1.5 h, and none of them touches iterations 1, 2 or 4. Iteration 3 needs only the ladder settled (condition 2).

### 12.6 🔴 G3 recommendation (rev. 2 as amended, the 17:37 file)

Written for the state the plan is in: it records the owner's approval at `:1379`/`:1404` and in decisions `:617–619`, and a post-approval amendment recorded as an owner decision at decisions `:621–645`; this session has not seen that approval or that decision, and the gate is not self-approved here.

```
---
🔴 HUMAN GATE: G3 — Implementation plan (04-plan.md rev. 2 as amended, 17:37 file)

**Decision needed**: confirm in this session that the approval recorded at 04-plan.md:1379/:1404 and
decisions.md:617–619, and the free-tier decision at decisions.md:631–645, are yours; and decide whether
conditions 1–11 below go into a rev. 3 before iteration 5 starts, or into Step 5 as 🟡 deviations.
**AI recommendation**: B, because rev. 2 fixes the order and most of the risky-iteration coverage, but the three
HIGH items left (P4-H1: retry ladder vs the approved HLD; P4-H3 (c): a design role with no owning iteration;
P4-R2-1: a Sunday schedule that cannot run) are cheaper to fix in the plan tonight than to discover in Step 5,
and none of them blocks iterations 1, 2 and 4; iteration 3 only needs the ladder settled (owner input 2).
**Options**:
A) The approval stands as recorded; conditions 1–11 become Step 5 🟡 deviations logged in decisions.md
   (fastest; the schedule is re-baselined implicitly on Monday; the red-team gaps are found by the implementer
   in T1 iterations).
B) The approval stands for iterations 1, 2, 3 ∥ 4 starting now (3 after owner input 2); the planner folds
   conditions 1–11 into rev. 3 this evening (about 1.5 h); the owner approves rev. 3 before iteration 5 starts
   on Monday (one more plan pass; the Sunday yield is the same under A and B, see P4-R2-1).
C) Custom.
**Blocked while waiting**: iteration 5 onward; the Monday checkpoint definition; the drop forecast.
---
```

**Conditions for rev. 3 (option B) or for the deviation log (option A):**

1. Re-baseline Sunday to 1, 2, 3 ∥ 4; move the tripwire to Monday 12:00 on iteration 6; redo the drop forecast on real Sunday capacity (P4-R2-1, P4-H4).
2. Iteration 3: the HLD one-retry ladder (1 retry → fallback once → typed error), or a new cap approved as an HLD amendment; mark 🔴, add a rollback and a red-team criterion (P4-H1).
3. A Library agent role with an owning iteration (role, prompt, allowlist row), or explicit descoping with an HLD amendment (P4-H3 c).
4. Mark 14a 🔴 with a rollback and a red-team criterion (P4-R2-2); decide 4, 16, 18, 24, 26 and 42 (P4-M4).
5. A red-team line in every 🔴 block; for 22a/22b/23 at least the three delete cases (P4-R2-8).
6. Iteration 17: the Tuesday criterion "no delete tool registered while a draft is pending"; the TR-14 case moves to 22b (P4-R2-3).
7. Cite HLD §5.3/§5.5 and rename the order test (P4-R2-5, P4-H3 a); tag the eight plan-only test names "(plan)" (P4-H2).
8. Add 19 to the nine `Depends-on` lines (P4-R2-9); fix the five §9 rows, the six missing AC rows and FR-43 (P4-M16).
9. A mid-iteration sign-off rule for risk-area changes in §0 (P4-M5, `human-gates.md:35`).
10. The G3 block in the gate format, keeping the approval record (P4-R2-7, P4-L8).
11. One line recording the approval of the 17:37 amendment in the G3 block or in decisions.md; a "Free-tier limits" README section in 43 and a free-key done criterion in 44 (P4-R2-10).

**Owner inputs needed in the same reply:**

1. Real capacity for the rest of today, in hours, for the Sunday re-baseline.
2. P4-H1: confirm the HLD retry ladder stands (cap 6 per turn), or state the new cap and accept that it re-opens R3-H4.
3. Library agent role: in (an owning iteration) or out (an HLD amendment).
4. Q-3: confirm that the AI Studio limits recorded at decisions `:621–629` (flash 20 requests a day and 5 RPM, flash-lite 500 and 15 RPM) and the free-tier decision at `:631–645` are yours; the embedding row stays an assumption until the iteration-2 spike (`:1003`, Q-5).
5. Q-5 and Q-6 answers. Q-7..Q-10 are recorded as accepted at `:1379`; confirm.
6. Confirm that the G3 approval at `04-plan.md:1379`/`:1404` and decisions `:600–645` are yours. They were written by another session; this review did not witness them and does not treat the gate as passed until you confirm.

Approved by: ______ (owner) on ______
