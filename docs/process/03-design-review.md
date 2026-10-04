# Design review: HLD and ADR-001..008

- **Step:** SDLC Lite Step 3 (design review). Written on 2026-10-04.
- **Reviewer:** the `design-reviewer` role, run as a separate instance from the architect.
- **Reviewed:** `docs/architecture.md` (draft of 2026-10-04) and ADR-001..008 in `docs/decisions.md` (all marked Proposed).
- **Line references:** they point to the HLD revision reviewed at that time (the first draft of 2026-10-04), not to the current revision.
- **Checked against:**
  - `CLAUDE.md` (non-negotiables);
  - `.claude/ai-workflow/skills/reviewer.md` (Design Review mode);
  - the LLM section of `security-reviewer.md`;
  - `.claude/ai-workflow/config/human-gates.md`;
  - the assignment text;
  - `docs/process/01-requirements.md` (rev. 2, approved at G1);
  - `docs/process/02-design-digest.md` (approved owner decisions);
  - `docs/data-model.md`.
- **Not re-opened:** the owner decisions from the digest (LangGraph, self-hosted Langfuse, SQLite for the prototype, Gemini flash/flash-lite, CTE scope injection). Findings below only touch them where the design as written has a defect.

## Verdict

**READY, with conditions.**

There are no blockers. The design meets all eight requirements and every deliverable, and it enforces its guardrails in code. The four MAJOR findings are defects in the specification: two are in the safety core (scope enforcement and token handling) and two in the bounds and behaviour (retries and small-cell suppression). All four can be fixed by editing `architecture.md`, and none of them changes the architecture.

**Condition:** the architect resolves M1–M4 in `architecture.md` and the ADRs before 🔴 G2, or the owner explicitly accepts carrying them into the Step 4 plan as named tasks with tests. G2 is the owner's gate. This review does not approve it.

| Severity | Count |
|---|---|
| BLOCKER | 0 |
| MAJOR | 4 |
| MINOR | 10 |
| NIT | 4 |

---

## MAJOR

### M1. Scope and PII bypass through CTE-name shadowing and unlisted FROM sources

- **Location:** §5.3 steps 3 and 5; ADR-008.
- **Problem:**
  - Step 3 collects every `exp.Table`, **subtracts CTE aliases by name**, and checks the remainder against the 4-table allowlist. Step 5 rewrites allowlisted references to the code-built CTEs (`__p`, `__oi`, `__o`, `__u`). The model's own CTE names are kept, and only the `__` prefix is reserved.
  - The hole: a model can write `WITH order_items AS (SELECT 1) SELECT ... FROM \`bigquery-public-data.thelook_ecommerce.order_items\``. Name-based subtraction can treat the qualified base-table reference as the CTE. The check passes, step 5 leaves the reference un-rewritten, and BigQuery reads the raw table with no category filter. A similar path also reaches `users` with no PII projection.
  - The prototype has no second layer: authorized views exist only in production. A bypass here is a direct breach of R2: the scope, PII and the AC-12.x criteria.
  - The policy also does not list the FROM sources that are not `exp.Table` or that sqlglot parses unusually. Examples: table-valued functions (`ML.*`, `EXTERNAL_QUERY`, `APPENDS`, `VECTOR_SEARCH`), wildcard tables and `_TABLE_SUFFIX`, `FOR SYSTEM_TIME AS OF`, temporary functions and UDFs, `@@` system variables, and `INFORMATION_SCHEMA`. "Deny by default" is implied but never stated.
- **Fix:**
  1. Reject any model CTE whose name equals an allowlisted table's short or qualified name, or starts with `__`.
  2. Resolve references through sqlglot's scope analysis (`sqlglot.optimizer.scope`), not by name. Only an unqualified reference that resolves to a CTE in an enclosing scope counts as a CTE. Any qualified reference is a base table.
  3. Add a post-rewrite invariant. Re-parse the final SQL and assert that every base-table reference appears only inside the code-built CTEs. On failure, reject. This makes the rewrite fail-closed.
  4. State an explicit allowlist of AST node types for FROM/JOIN sources (a table, a subquery, UNNEST of a column), and deny everything else. Add unit tests for each bypass listed above, including the shadowing case.

### M2. The retry layers multiply past the turn deadline and the "6 retries per turn" cap

- **Location:** §4.4 (the retry configuration at about line 357 and the fallback flowchart); §4.1 bounds table ("Retries inside one turn: 6"); §8, Gemini row.
- **Problem:**
  - The chain as specified is `ChatGoogleGenerativeAI(max_retries=1, timeout=60)` → `.with_retry(stop_after_attempt=4)` → `.with_fallbacks([fallback])`.
  - One logical call can therefore make (1 + 1) × 4 = 8 primary requests, plus the fallback's own attempts. Each attempt has a 60 s timeout. The worst case is several minutes for a single call, against a 120 s turn deadline.
  - `with_retry` has no access to `TurnBudget`. The flowchart's "turn budget left?" decision and the 6-per-turn cap therefore cannot be enforced by this composition as written. This breaks the CLAUDE.md non-negotiable: "every loop and retry is bounded" in a way that respects the turn deadline.
- **Fix:**
  - Replace the stacked layers with one deadline-aware retry wrapper, a small custom Runnable or function, owned by `TurnBudget`. Each attempt:
    - checks the remaining wall time and the per-turn retry count;
    - sets the request timeout to `min(60 s, remaining − force_answer reserve)`;
    - decides between retrying the primary and going to the fallback.
  - Set the SDK retry to 0, or verify and document what `max_retries` does in `langchain-google-genai`.
  - Make sure the fallback model is not wrapped in its own retry.
  - Add a unit test with a fake model that always raises a transient error. It asserts the total attempts (≤ 6 per turn) and the simulated wall time (≤ the deadline), and that `force_answer` is reached.

### M3. The delete token reaches traces despite ADR-007's "never in a trace"

- **Location:** ADR-007; §6.3.3 (the pending action in checkpoint state, and the interrupt payload carrying the token); §7.2 ("The token is in state only"); §6.7 / §9 (CallbackHandler tracing).
- **Problem:**
  - The plaintext token is stored in graph state (`pending_action`) and sent in the `interrupt()` payload.
  - The Langfuse `CallbackHandler` and the JSONL tracer record node inputs and outputs, which include state.
  - The PII scrubber cannot recognise a random token, so masking will not catch it.
  - The result contradicts ADR-007 and §7.2. Anyone with trace access could replay the token within its 10-minute validity. The ID-hash binding and the same-session binding reduce the impact, but they do not remove the contradiction.
- **Fix:**
  - Keep only `sha256(token)` in `pending_action`. Compare the hash of the echoed token at confirm time.
  - Drop the `pending_action` and interrupt-payload keys from trace payloads explicitly (a key-level drop in the `mask_otel_spans` function and in the JSONL writer), rather than relying on pattern scrubbing.
  - Add a test that runs a full preview → confirm cycle and scans the JSONL trace and the captured Langfuse payloads for the token string.

### M4. The small-cell suppression rule is undefined at user_id grain and for nested queries

- **Location:** §5.2 layer 5 (k = 5, a hidden `COUNT(DISTINCT user_id)` on "any aggregate that filters or groups on a quasi-identifier"); requirements A-3 and the AC at line 138 ("top 10 customers by total spend" returns user_id plus spend).
- **Problem:**
  - As written, "top 10 US customers by spend" filters on `country` (a quasi-identifier) and groups by `user_id`. Every group has exactly one distinct user, so every row is suppressed. A required capability returns nothing.
  - The opposite reading is also unsafe. If user_id-grain outputs are exempt, then `SELECT user_id, age, city, SUM(...) GROUP BY user_id, age, city` re-identifies people by joining opaque keys to quasi-identifiers.
  - Injecting a hidden count into arbitrary nesting, window functions or post-`LIMIT` queries is not specified. Filtering after a `LIMIT` silently drops groups. This is also a feasibility risk for Thursday.
- **Fix:** define the rule precisely in §5.2/§5.3 and add tests for each case.
  - (a) **Aggregates grouped by any quasi-identifier.** Add `HAVING COUNT(DISTINCT user_id) >= 5` at that aggregation level, before any outer `LIMIT`.
  - (b) **Outputs at user_id or order_id grain.** Allowed, but no quasi-identifier may appear in their projection or in any column derived from one. A filter on a quasi-identifier is allowed only when the filtered population is ≥ k, which a dry-run COUNT can check, or else reject.
  - (c) **Anything the rewriter cannot place safely** (windows over quasi-identifiers, nested aggregates over quasi-identifiers). Reject with a clear `POLICY_REJECTED` reason, so the agent can rephrase.

---

## MINOR

| # | Location | Problem | Fix |
|---|---|---|---|
| m1 | §1.2 table and the "P" note; no build order anywhere | The note that P items degrade to design-only is good. But the M scope alone is large for 4 days: the SQL policy with suppression, the delete flow, resilience, the JSONL viewer, the metrics summary and the eval runner. There is no build order or cut line inside M. Langfuse is optional, but the R8 persona path depends on it | Add a short build order in §1.2: R2 policy and R3 delete first, then R5, R7 JSONL, evals, and only then the P items (Golden, persona, preferences, Langfuse). Mark a minimum shippable set. The plan (Step 4) can then sequence tasks against it |
| m2 | §4.4 / §4.1 step 3 | `bind_tools` must be applied to each model **before** `.with_retry()` / `.with_fallbacks()`. `RunnableRetry` and `RunnableWithFallbacks` do not expose `bind_tools`, and an unbound fallback would answer without tools | State: "each model is bound to the same tool list, then wrapped". This also fits M2's single wrapper |
| m3 | §4.1 step 5 (grounding check) | "Every number in the answer must appear in the ledger" is brittle. Rounded values (12.3k vs 12,304), percentages, differences and dates will fail or be over-marked as derived | Specify a tolerance for rounding and a whitelist of derived operations computed by code (share, delta, growth). Otherwise the number must be labelled as an estimate. Add test cases |
| m4 | §2.3 / §6.3.3 | Routing is undefined when the model calls `delete_reports` alongside other tools in the same step, or calls it twice | Rule: `delete_reports` must be the only tool call in its step; otherwise the step returns an error envelope. One pending action per thread at a time |
| m5 | §6.3.3, confirm rule "same user turn" (about line 619) | "Same user turn" is not defined. Is it the turn that showed the preview, or the next user message? | Define it as a `turn_id` / turn counter stored in `pending_action`. Confirm is valid only on the next turn (counter + 1). Any other message cancels |
| m6 | §2.3 (cancel → input_guard edge) | After a cancelled delete, the graph routes the message to `input_guard` and skips `load_context`. Persona, preferences and the turn budget may be missing for that message | Route cancel → `load_context`, or document that the context is still in state from the paused turn and the budget is reset |
| m7 | §5.3 CTE SQL (`__o AS (SELECT o.* ...)`) | `__o` keeps every column of `orders`, including `num_of_item`, which counts all items in the order, including out-of-scope categories. A scoped user can sum it and learn about other categories' volumes, and an answer that uses it mixes scopes silently | Project an explicit column list in `__o` and either drop `num_of_item` or replace it with an in-scope count computed from `__oi`. Add a policy test |
| m8 | §3, §13.2 open question 6 | The model IDs `gemini-3.8-flash` and `gemini-3.1-flash-lite` are unverified against the reviewer's region and quota. The embedding model has no ID | Add a startup check that lists the models and fails with a clear message. Give the default embedding model ID in config. Keep the IDs in one config file |
| m9 | Header line 6; §8 setup rows | Reviewer-runnability is delegated to a future README. The HLD never mentions `requirements.txt` (the pip path in CLAUDE.md), the reviewer's minimum IAM role for the prototype (`roles/bigquery.jobUser` on the billing project; §10 names it only for the production service account), or the expected cost of a demo run | Add a short "Running the prototype" subsection: uv and pip paths, required roles, ADC, `.env` keys, and that Langfuse is optional. Then the README can follow it |
| m10 | §6.3.3 (`delete.previewed` appended inside the tool); §9 | The audit append happens inside a tool that runs before the checkpoint after the step. On a crash and resume, the step can re-run and append a second `delete.previewed` with a new token | Note that it is idempotent: either key the preview record by `(thread_id, step)`, or accept duplicate preview rows and state that only `confirmed`/`executed` matter for the audit trail |

## NIT

| # | Location | Note |
|---|---|---|
| n1 | §6.9 coverage table | FR-56 (audit viewer) and FR-19 (progress indicator) are covered in §1.2 and §6.7 but not cited by ID in §6.9. Add the IDs for traceability |
| n2 | §2.4 | A heading that only points elsewhere. Fold it into the section intro or drop it |
| n3 | §3, Agent framework row | The experience statement is present (as the assignment requires). Consider repeating it in one line in the README's tech section, where a reader looks for it |
| n4 | ADR-001..008 | All are Proposed. After G2, flip them to Accepted along with the gate record, so the decisions log is consistent |

---

## Requirements coverage matrix

### Assignment requirements R1–R8

| Req | Mechanism in the HLD | Verification in the HLD | Status |
|---|---|---|---|
| R1 Golden Bucket | §6.1: a YAML seed, embedding top-k with a scope filter, `trio_id@version` in the trace; production pgvector + FTS + RRF + re-ranker, curation and snapshots | Retrieval eval cases (§6.6); the trace field | Covered (prototype P) |
| R2 Safety and PII | §5: the 10-layer guardrail table, the sqlglot policy, PII-free CTEs, scope injection, dry-run + `maximum_bytes_billed`, result scrub, output guard, trace masking; production authorized views | Adversarial suite (§6.6); policy unit tests | Covered, **subject to M1, M3, M4** |
| R3 Destructive ops | §6.3: Saved Reports, two-phase delete, a code-issued token bound to the ID hash, audit first in one transaction, abort on audit failure | Delete unit tests; edge cases in §12 | Covered, **subject to M3**, m4, m5, m10 |
| R4 Learning | §6.4: explicit preferences, precedence, `/feedback`; production implicit learning and trio promotion | Preference tests; feedback scores | Covered (prototype P) |
| R5 Resilience | §4.4, §8: bounded self-correction, cost guard, retries, a fallback model, a rate limiter, a deadline, graceful messages; production circuit breaker and regional failover | Resilience suite; failure table §8 | Covered, **subject to M2** |
| R6 Quality | §6.6: golden, adversarial and resilience suites, an LLM judge, non-zero exit gates; production CI gate and canary | The eval runner itself | Covered |
| R7 Observability | §6.7, §9: a JSONL trace per turn, a viewer, a metrics summary, Langfuse when keys are set, a span model | Trace tests; the metrics summary | Covered, **subject to M3** |
| R8 Persona | §6.8: a Langfuse prompt with label and cache TTL, a local fallback, the version in each trace, a 4,000-char limit; production approval workflow | Persona fallback tests | Covered (prototype P) |

### Deliverables

| Deliverable | Where | Status |
|---|---|---|
| HLD with concrete services | §2.1 (Cloud Run, IAP, Vertex AI, BigQuery authorized views, Cloud SQL + pgvector, Pub/Sub, Secret Manager, VPC-SC, Cloud Build) | Covered |
| Mermaid diagrams | 7 diagrams: §2.1, §2.2, §2.3, §4.4, §5.1, §6.3.3, §7.1 | Covered (all render, see below) |
| Reasons for choices | §3 table with rejected alternatives; ADR-001..008 | Covered |
| Data flow | §2.3, §4.1, §5.1, §6.3.3 | Covered |
| Error handling and fallback | §4.4, §8 failure table, §12 edge cases | Covered, subject to M2 |
| Setup and example run | Delegated to the README (header) | Partial: see m9 |
| Framework and experience level | §3, Agent framework row; ADR-001 | Covered |

### Design Review checklist (reviewer.md)

| Check | Result |
|---|---|
| Each requirement has a mechanism and a verification | Yes (table above) |
| Guardrails are in code, not only in prompts | Yes: the SQL policy, scope CTEs, delete token and audit are all in code. M1, M3 and M4 are gaps inside that code design |
| A failure-mode table exists | Yes, §8 |
| Concrete services are named | Yes |
| Observability is designed | Yes, §9 |
| Prototype and production share interfaces | Yes, the §2.2 interface table |
| ≥ 8 edge cases | Yes, 16 in §12 |
| Loops and retries are bounded | Mostly. The `TurnBudget` table is good; the retry composition is not (M2) |
| Reviewer-runnable | `GOOGLE_CLOUD_PROJECT` comes from the environment and Docker Compose is optional. The setup details are deferred (m9) |

### FR traceability summary

- **Prototype (M) FRs:** all are mapped in §1.2 and in the sections for each requirement. FR-56 (audit viewer) is mentioned (§1.2 R3 row, lines 50, 83, 549) but not cited in §6.9 (n1).
- **Prototype (P) FRs:** FR-42–44, 46, 48, 51, 52. Mapped, with an explicit degrade-to-design-only rule.
- **HLD-only FRs:** FR-07–10, 14, 27, 37–41, 45, 47, 49, 50, 53, 57, 58, 63, 66–68. Mapped in §1.2 and in the §6.9 coverage table.
- **NFRs:** the security, privacy, cost, latency and reliability targets map to §4.1 bounds, §5, §8 and §10. No orphan NFR was found.

---

## Mermaid validation

All 7 diagrams were extracted and rendered with `@mermaid-js/mermaid-cli` (mmdc). **7 of 7 pass**, with no syntax errors.

| Diagram | Type | Result |
|---|---|---|
| §2.1 Production | flowchart LR | Pass |
| §2.2 Prototype | flowchart LR | Pass |
| §2.3 Agent graph | flowchart TD | Pass |
| §4.4 Fallback | flowchart TD | Pass |
| §5.1 Guardrails | sequenceDiagram | Pass |
| §6.3.3 Delete | sequenceDiagram | Pass |
| §7.1 Stores | erDiagram | Pass |

## API spot-check (context7, current docs)

| API used in the HLD | Result |
|---|---|
| LangGraph `interrupt()`, `Command(resume=...)`, `result["__interrupt__"]`, `recursion_limit`, `SqliteSaver` / `PostgresSaver` | Matches the current docs. The HLD's note that a node re-runs from the start on resume (line 234) is correct and handled |
| `Runnable.with_retry(stop_after_attempt, wait_exponential_jitter, retry_if_exception_type)`, `.with_fallbacks(exceptions_to_handle=...)` | They exist as described. See M2 for how they compose, and m2 for `bind_tools` |
| Langfuse v4: `Langfuse(mask_otel_spans=...)`, `CallbackHandler`, `propagate_attributes`, `get_prompt(label, cache_ttl_seconds, fallback)`, `create_score`, datasets / `run_experiment` | Exist. `mask_otel_spans` is the right export-time hook (`mask=` is the legacy form) |
| BigQuery `QueryJobConfig(dry_run, use_query_cache, maximum_bytes_billed, job_timeout_ms, labels)` | Exist as described |

---

## What is good

- Safety is enforced by code at several layers: the AST policy, code-built PII-free CTEs, BigQuery's own `maximum_bytes_billed`, a result scrub and an output guard. The model never writes the scope filter.
- The delete design is strong in principle: the token is issued by code and bound to the hash of the sorted IDs, the owner, the session and the expiry. Audit first in one transaction, with abort on audit failure, follows CLAUDE.md exactly. Preview and result messages are rendered from templates, so counts cannot be hallucinated.
- The `TurnBudget` with explicit caps and a reserved `force_answer` call is the right pattern. Once M2 is fixed, it makes every turn bounded in calls, queries, money and time.
- Langfuse is optional and fail-open, and JSONL is always written, so the prototype runs without Docker.
- No hard-coded project IDs: `GOOGLE_CLOUD_PROJECT` comes from the environment, and billing goes to the reviewer's project.
- The failure table, 16 edge cases, the OWASP LLM mapping and the prototype/production interface table are thorough and concrete.
- The ADRs record rejected alternatives honestly, and the experience statement the assignment requires is present.

## Gate

🔴 **G2 is the owner's gate.** This review recommends proceeding once M1–M4 are resolved (or explicitly carried into the plan with tests). It does not approve G2. The open questions in `architecture.md` §13.2 also go to the owner at G2.

## Architect response (2026-10-04)

All findings are accepted. There are no disagreements. One note on M2's arithmetic: per the current `langchain-google-genai` docs, `max_retries=1` already means a single SDK attempt (0 means the Google default of 5 retries). So the old worst case was 4 primary attempts of 60 s each, not 8. The composition defect still stood, because `with_retry` could not see the turn deadline or the per-turn cap. It is fixed as suggested. The ADRs stay Proposed until G2.

| Finding | Resolution | Where |
|---|---|---|
| M1 CTE shadowing and FROM sources | Policy steps added: CTE names equal to a table name or using `__` are rejected (`cte_shadows_table`); FROM sources are limited to tables, subqueries and UNNEST of a column (`source_not_allowed`); names resolve per scope (an unqualified name matching a visible CTE is that CTE, a qualified name is always the base table); the final SQL is re-validated after the rewrite and fails closed (`rewrite_invariant`). Tests listed | §5.2 layer 3, §5.3 steps 3–5 and 9, §6.2, ADR-004, ADR-008 |
| M2 retry composition | One deadline-aware `call_llm` wrapper owned by `TurnBudget`. SDK retries off (`max_retries=1`), `bind_tools` before wrapping, per-attempt timeout min(60 s, remaining time minus the `force_answer`/`finalize` reserve), 3 retries per call and 6 per turn, one fallback attempt with no retries of its own, then `force_answer` or the templated message. `test_retry_wrapper_bounded` (fake clock: attempt count and wall time) and `test_sdk_single_attempt` | §4.1 bounds, §4.4 (text and flowchart), §6.5, §8, ADR-003 |
| M3 delete token in traces | State and checkpoints hold only `sha256(token)`. The plaintext exists only in the interrupt and resume payloads and is compared in constant time. `pending_action`, `__interrupt__` and the resume token are dropped by key in one shared function used by OTel masking, the Langfuse callback filter and the JSONL writer. Tests `test_delete_token_never_in_traces` and `test_checkpoint_holds_token_hash_only` | §2.3, §6.3.3, §6.7, §7.1, §7.2, §9.1, ADR-007 |
| M4 small-cell rule | A three-case rule over the listed quasi-identifiers: (a) grouping by a quasi-identifier injects `HAVING COUNT(DISTINCT user_id) >= 5` at that level, before the outer ORDER BY or LIMIT; (b) at user or order grain, no quasi-identifier in the projection, ORDER BY or window partition, and a quasi-identifier filter needs a capped population check of at least k; (c) windows over a quasi-identifier, nested aggregates and correlated quasi-identifiers are rejected (`small_cell_unplaceable`). "Top customers by spend" stays allowed, with tests and an eval case | §5.2 layer 5, §6.2, §12 #11 and #11b, §13.2 Q1, ADR-004 |
| m1 build order | Build order with tiers 0–5 and a cut line (tiers 0–4 are the minimum shippable set) for the Thursday 2026-10-08 deadline | §1.2 |
| m2 `bind_tools` | Each model is bound to the same tool list, then wrapped | §4.1 step 3, §4.4, ADR-003 |
| m3 grounding tolerance | A number matches if it equals the ledger value after rounding to the displayed precision or within 0.5%, with k/M/% normalised. Dates must fall inside the data window. Derived operations (share, delta, growth) are recomputed by code. Anything else is labelled an estimate. Tests listed | §4.1 step 5 |
| m4 mixed delete routing | `delete_reports` must be the only tool call in its step (`delete_not_alone`). One pending delete per thread (`delete_pending`) | §2.3, §4.1, §4.2, §6.3.3, ADR-007 |
| m5 "same user turn" | Defined as `preview_turn + 1`, stored in `pending_action`. Any other turn cancels | §6.3.3, ADR-007 |
| m6 cancel routing | Cancel routes to `load_context` as a new turn with a fresh turn budget. *Later change:* the cancel target has since moved from `load_context` to `input_guard`, so the scope snapshot is re-checked on the cancel message (see the current HLD and the independent review 03b, R3-L7) | §2.3, §6.3.3, ADR-007 |
| m7 `__o` projection | Explicit column lists in `__oi` and `__o`. `__o` drops `num_of_item` (item counts come from `__oi`) and `orders.gender` (a duplicate quasi-identifier). Test `test_orders_num_of_item_not_exposed` | §5.3, ADR-008 |
| m8 model IDs | All IDs live in `config/models.yaml`, are marked "to verify", and are checked at startup with a clear error. The embedding ID is deliberately left to verify in Step 4 instead of naming an unverified one | §3, §1.3, §8, §13.2 Q6, ADR-003 |
| m9 reviewer runnability | New "Running the prototype" section: uv and pip paths (`requirements.txt` via `uv export`), `roles/bigquery.jobUser` on the reviewer's project, ADC, `.env.example` keys, Langfuse optional, expected demo cost | §1.3, header |
| m10 idempotent preview | `delete.previewed` is insert-or-ignore, keyed by `(thread_id, step)` | §6.3.3, ADR-007 |
| n1 FR IDs | FR-19 and FR-56 cited in the coverage table. The audit viewer and progress line are also described | §6.9, §6.3.3, §6.5 |
| n2 §2.4 | Folded into the §2 intro and the heading removed | §2 |
| n3 README tech line | Deferred to Step 5, when the README is written | Step 5 |
| n4 ADR status | The ADRs flip to Accepted at G2, together with the owner's gate record | G2 |

All Mermaid diagrams in `architecture.md` were re-validated with `@mermaid-js/mermaid-cli` after the edits. 🔴 G2 remains the owner's gate.

---

# Review 2 (2026-10-04): multi-agent revision (HLD rev. 3, ADR-009)

- **Step:** SDLC Lite Step 3 (design re-review), requested by the owner after the topology change. Written on 2026-10-04.
- **Reviewer:** the `design-reviewer` role, run as a separate instance from the architect.
- **Reviewed:** `docs/architecture.md` rev. 3 (1438 lines, read in full); ADR-009, plus the 2026-10-04 revision notes of ADR-001, ADR-003, ADR-004 and ADR-007 in `docs/decisions.md`; the budget rows of `docs/process/01-requirements.md` (C2, AC-22.4, FR-14, FR-21, the NFR "LLM calls per turn" row, A-24).
- **Line references:** they point to the HLD revision reviewed at that time (rev. 3), not to the current revision.
- **Focus (owner's request):** multi-agent behaviour, what happens when a role fails, and how failures are tracked. Also: budgets, leftover single-loop wording, the security boundary (including router-label injection), delete and save idempotency on resume, prototype feasibility by Thursday 2026-10-08, and Mermaid validity.
- **API check:** LangGraph behaviour was checked against the current docs through context7 (`/websites/langchain_oss_python_langgraph`, `/websites/reference_langchain_python_langgraph`).

## Verdict

**READY, with conditions.**

The topology is sound: a deterministic code supervisor, tool-less writer and verifier, one envelope type, one shared `TurnBudget`, and guardrails that stay in shared code. Nothing found makes the multi-agent design unworkable, so there is no blocker. But the revision's two new promises, "every failure has a defined outcome" and "a resumed turn is safe", are not yet true as written. The delete path describes two different execution owners, and the resume story does not survive a crash at the interrupt. The plaintext delete token is persisted in checkpoint pending writes, which contradicts the Review-1 M3 fix. The report-turn budget does not add up once the writer's fallback and the repair-after-rewrite edge are counted. Several role failures have no metric or alert. All six MAJOR findings are specification fixes in `architecture.md` and the ADRs, plus a scope decision on `--resume`.

**Condition:** the architect resolves R2-M1..M6 before 🔴 G2, or the owner explicitly carries them into the Step 4 plan as named tasks with tests. G2 is the owner's gate. This review does not approve it.

| Severity | Count |
|---|---|
| BLOCKER | 0 |
| MAJOR | 6 |
| MINOR | 7 |
| NIT | 2 |

---

## BLOCKER

None.

## MAJOR

### R2-M1. Two owners for the delete, and no storable idempotency key

- **Location:** §2.3 graph (`confirm → exec`) vs §2.3 note 3 ("the delete itself runs after `interrupt()` returns"), §6.3.3 sequence (participant K executes the DELETE) and its rule "Execution happens only in `confirm_delete`"; §4.0.6, §6.5 line 927 and ADR-009 name `execute_delete`
- **Problem:** The graph and the resume design put the delete in a separate `execute_delete` node. The sequence diagram and the rules execute it inside `confirm_delete`, after `interrupt()` returns. Also, the idempotency check "audit log has a `delete.executed` record for the token hash" has nowhere to live: `AUDIT_EVENT` (§7.1) has no token-hash or pending-action key and no unique constraint, only a free `details` json.
- **Why it matters:** LangGraph re-runs a node from its first line after a crash or resume (current docs: "Code and side effects before the pause run again"). If the delete sits inside `confirm_delete`, a crash between COMMIT and the node's checkpoint re-runs the whole node, and the only guard is an unindexed lookup in json. This is a 🔴 deletion area and the claimed test (`test_resume_after_crash_each_node`) cannot be written against an ambiguous spec.
- **Fix:** One owner: `confirm_delete` only validates the reply and writes `delete.confirmed`, then routes to `execute_delete`. `execute_delete` is the only node that deletes. Add `pending_action_id` (or `token_sha256`) as a column on `AUDIT_EVENT` with a unique index on `(pending_action_id, event_type)`, so `delete.executed` can exist once, and the check is an indexed read inside the same transaction. Fix the §6.3.3 sequence and rules to match.

### R2-M2. The plaintext delete token is persisted in checkpoint pending writes

- **Location:** §2.3 note 4, §6.3.3 "Token, hash only in state" ("checkpoints cannot leak it"), §6.7, ADR-007; Review-1 M3 test `test_checkpoint_holds_token_hash_only`
- **Problem:** The design puts the plaintext token in the `interrupt()` payload and in `Command(resume={"text", "token"})`. LangGraph persists both as pending writes of the checkpoint: that is how `get_state(config)` shows the interrupts and how `interrupt()` returns the resume value when the node re-runs. `SqliteSaver` in the prototype stores them unencrypted.
- **Why it matters:** The Review-1 M3 resolution claims state and checkpoints hold only `sha256(token)`, and its test asserts this. As written, that test fails, or passes only because it checks the state channels and misses the pending writes. The token is a 🔴 deletion-area control.
- **Fix:** Take the token out of the LangGraph payloads. Keep the plaintext in CLI memory only, keyed by the interrupt id, and resume with `Command(resume={"text": reply, "proof": hmac(token, interrupt_id)})`. Or drop the token and bind on interrupt id + `ids_sha256` + `preview_turn` + expiry. Extend `test_checkpoint_holds_token_hash_only` to scan pending writes (`get_state(...).tasks[*].interrupts` and the saver's writes table).

### R2-M3. Resume is undefined at the delete interrupt and conflicts with the requirements

- **Location:** §4.0.5 last row, §4.0.6, §2.3 note 1, §8 "Process crash mid-turn", §12 #5; requirements FR-14 (HLD-only), A-24, FR-05
- **Problem:** (a) After a crash while paused at `confirm_delete`, `graph.invoke(None, config)` re-runs to the same `interrupt()` and pauses again. The new CLI process does not have the plaintext token (it lived only in the old process's memory), so the delete can never be confirmed. `turn = preview_turn + 1` after a restart is not defined. (b) `--resume <session>` contradicts FR-14 "HLD-only", A-24 "no transcript resume in the prototype" and §12 #5 "a restart is a new session (FR-05)".
- **Why it matters:** The owner asked how failures are handled. A crash at the most sensitive point (a pending delete) has no stated outcome, and the prototype scope now silently includes a feature the approved requirements exclude.
- **Fix:** Define it: on `--resume`, a pending delete is always expired (audit `delete.expired`, message "That deletion request expired after a restart; nothing was deleted. Ask again to see a new preview"). Scope `--resume` to "finish the one in-flight turn of the last session" and record it as a requirements change (FR-14 split, A-24 and §12 #5 updated), or mark general resume HLD-only and keep only the idempotency guarantees and their unit tests in the prototype.

### R2-M4. The report-turn call budget does not add up

- **Location:** §4.1 bounds table (report row), §4.0.4, §4.4 table (router and writer rows), §8 "Report validation failure" ("then the writer's fallback model once"), §8 "Loop that does not converge" ("9 of 14"), §2.3 edges `val → writer` and `ver → writer`
- **Problem:** The phase is fixed at 5 calls (draft, repair, verify, rewrite, re-verify). But: the writer's fallback-model attempt (§8, §4.4) is a 6th call; a rewrite goes back through the validator, and the `val → writer` edge allows a second repair; the router's flash fallback makes "router 1" into 2; the §8 row's "9 of 14" (total used) and §4.1's "analysis ≤ 8" (per phase) use different counters; and `force_answer` on a report turn is outside "analysis ≤ 8". The walk-through below reaches 16 calls on a cap of 14.
- **Why it matters:** The 14-call cap is the cost model and an acceptance criterion (AC-22.4). If the phases cannot be summed, the cap is enforced only by the global counter, so the outcome on hitting it mid-phase (verify skipped? unverified save? report failed?) is decided by accident.
- **Fix:** Write one accounting rule: every attempt that reaches a provider counts, including retries and fallback attempts. Give per-phase sub-caps that sum to the cap: for example router ≤ 2 (primary + fallback), analysis ≤ 6 + `force_answer` 1, writer ≤ 3 attempts in total (draft, one repair or one fallback, rewrite; the repair is available once per turn) and verifier ≤ 2, which sums to 14. Or raise the cap through a requirements change to AC-22.4. Map "report phase at its cap" to concrete outcomes: no draft → `report_failed`; draft but no verify → save `unverified`. State that the history summary is skipped on report turns.

### R2-M5. Failure tracking is incomplete: missing matrix rows, metrics and alerts

- **Location:** §4.0.5 failure matrix, §8, §9.1, §9.2 "Agents (per role)", §9.4 alerts; ADR-009 Consequences ("the escalation rate (> 30%) is an alert")
- **Problem:** (a) Missing rows in the matrix: checkpointer write failure; `force_answer` itself failing; report-phase deadline or cap reached mid-phase; `save_generated_report` store failure on a report turn (only a Library-agent row exists); Library-agent model failure; an unexpected exception or unknown `status` from a role subgraph; `GraphRecursionError`. (b) Only `output_guard_error` has an alert. ADR-009 calls the escalation rate > 30% "an alert", but §9.4 has no such alert, and `router_unavailable`, `verifier_unavailable`, `report_failed` and `turn_resumed` appear in the matrix but not as named metrics in §9.2 or spans in §9.1.
- **Why it matters:** The owner's focus is "things failing and how failures are tracked". A role failure with no metric is invisible, and a metric with no alert or review path does not get acted on. An exception from a subgraph with no mapping ends the turn with a stack trace instead of the templated message.
- **Fix:** Add the missing rows, each with behaviour, user message, metric and alert. Add one supervisor rule: any exception or unknown status from a role becomes `{status: failed, error_class: internal}` and goes to that role's failure outcome. Add one canonical metric list (`agent_outcome_total{agent, status, error_class}` covers most rows) and list it in §9.2. Add §9.4 ticket alerts: escalation rate > 30% (1 h), `router_unavailable` > 5% (15 min), `verifier_unavailable` or `report_failed` > 10% (1 h), any checkpointer failure. Prototype: show these counts in the metrics summary.

### R2-M6. New injection paths through the router context and saved reports

- **Location:** §4.1 step 2 (router sees the last 2 turns), §4.0 Library agent tools (`view_report`, `set_preference`), §6.4 / §6.7 indirect injection, §6.6 adversarial suite
- **Problem:** (a) The router reads the last 2 turns, including assistant text derived from warehouse values. Indirect injection in data can steer the label: a forced `report` costs 14 calls and 180 s per turn (cost amplification), a forced `library` hands the turn to the agent holding `set_preference`. (b) Stored injection: writer output built from warehouse text is saved, then re-read by the Library agent via `view_report` in a later turn, in the same step loop that can call `set_preference`. The PII, scope and SQL boundary is unchanged, and the delete still needs a user reply, so impact is cost and preferences, not data.
- **Why it matters:** ADR-009 states that the router and the tool-less roles "cannot act on injected text". That holds for writer and verifier, not for the router (whose label is an action) or the Library agent. This is a 🔴 prompt and guardrail area.
- **Fix:** The router sees user messages only (current + previous user turn), never assistant or tool text. `view_report` output is delimited as untrusted data. `set_preference` is accepted only when the value appears in the current user message (code check). Add a rate rule: at most N `report` labels per user per hour. Add eval cases `router_label_injection` and `stored_report_injection` to the 100% adversarial gate.

## MINOR

| ID | Location | Problem | Why it matters | Suggested fix |
|---|---|---|---|---|
| R2-m1 | §4.1 last paragraph ("`recursion_limit` is set to 40"), §8 "Loop that does not converge" | `recursion_limit` counts supersteps, not LLM calls. The HLD does not say whether it applies per graph level or across subgraphs, and gives no derivation. When it fires, LangGraph raises `GraphRecursionError`: the run stops, so `force_answer` (listed as the §8 behaviour) cannot run. Current docs: the default is 1000 since 1.0.6, and the recommended graceful path is the `RemainingSteps` managed value. | A backstop that aborts the run gives the user a stack trace instead of a partial answer, and 40 is close to the worst-case step count (see the walk-through). | Derive the limit from the budget (2 × calls + parent nodes + margin, for example 60) and state it per level. Route on `RemainingSteps` to `force_answer`. Catch `GraphRecursionError` in the CLI and map it to the templated partial plus a metric. |
| R2-m2 | architecture.md §12 #2 ("from the classifier"), §12 #8 ("The classifier flags" twice), §4.1 heading "The loop", line 588 (§5.1 "loop at most 6 SQL and 10 LLM calls"), line 782 (§6.3.1 `save_report(mode=generate)`; generation is now code-only after the verifier), line 1368 ("The agent loop does not change"), §4.1 step 3 (describes `delete_reports` inside an analyst subgraph; it is now a Library-agent tool); decisions.md line 87 ("input classifier ... fallback for the agent"), line 98 ("**Classifier:** fails open"), line 118 ("a flash-lite classifier") | Leftover single-loop and classifier wording. The revision notes at the end of the ADRs say what changed, but the ADR bodies still state the old decision. | A reader of the ADR body or the edge cases gets the old design, and a Step 4 planner may build a `save_report(mode=generate)` tool that the model can call. | Replace with "router" and role names. Rewrite the ADR-003 and ADR-004 bullets in place (keep the revision note). Rename §4.1 to "Turn flow". Remove `mode=generate` from §6.3.1. |
| R2-m3 | §4.0.5 writer row and §4.4 ("retry report reuses the ledger") | The ledger is per-turn state. "Retry report" is a new turn, so where the previous turn's ledger lives, for how long, and which budget the retry gets are not defined. | Without it, the retry either re-runs SQL (contradicting the user message) or reads a ledger that the next unrelated turn has replaced. | Keep the last turn's scrubbed ledger in session state until the next analysis turn. A `retry report` turn has the router 1 + report phase budget and no analysis budget. Or mark retry as HLD-only. |
| R2-m4 | §7.4 cost envelope ("at about 6 calls") | The cost model was not refreshed for roles: report turns are 14 calls with 3 flash calls, and the router adds a flash-lite call to every turn. | The ≤ $0.10 per question target is checked against a stale call mix. | Give the cost per turn type: simple, complex, report (worst case), refused. *(Note, rev. 4.1: the owner removed all money figures at G2; the fix is usage per turn type in calls, tokens and bytes.)* |
| R2-m5 | §4.0.6, §3 checkpointer row | The subgraph persistence mode is not stated. Current LangGraph docs give three modes (`checkpointer=None` per invocation, the default; `True` per thread; `False` stateless). Per-thread mode keeps a role's messages across turns and conflicts with "each role starts from the ledger". | The "checkpoint after every node, parent and subgraph" claim depends on the mode, and resume tests need it. | State `compile()` with the default (per-invocation) for all role subgraphs, and why. Use `get_state(config, subgraphs=True)` in the resume test. |
| R2-m6 | §4.0.6 ("`TurnBudget` counters live in graph state") | A node that crashes after an LLM call but before returning loses that call's counter update. On replay, the call is made again and counted once. | Bounded (at most one node's calls per crash) but it means the cap is not exact under crash, and `test_resume_budget_persisted` should not assert exact equality. | Say so, and reconcile counts from the trace on resume, or accept the bound explicitly. |
| R2-m7 | §2.1 production diagram (line 144: flash is the "fallback for Deep") vs §4.4 role table (Deep falls back to flash-lite) | The Deep analyst's fallback model is stated two ways. | The fallback is part of the retry and cost model and the per-role eval gate. | Pick one (flash is the safer fallback for multi-step SQL) and align the diagram, the table and ADR-009. |

## NIT

| ID | Location | Problem | Suggested fix |
|---|---|---|---|
| R2-n1 | §2 runtime step 3 (line 202): "The API and reads BigQuery only through ..." | Typo. | "The API reads BigQuery only through ...". |
| R2-n2 | §4.1 step 1 (Golden retrieval: 1 embedding call) and the budget table | Whether the embedding call counts against the LLM-call cap is not stated. | Add "embedding calls are outside the LLM-call cap; they are bounded at 1 per turn". |

---

## Review-1 regression check

| Review-1 finding | Still resolved in rev. 3? | Note |
|---|---|---|
| M1 CTE shadowing and FROM sources | Yes | §5.2 layer 3 and §5.3 steps 3–5 and 9 unchanged. Both analysts use the same `run_sql`. |
| M2 retry composition | Yes for one call; partly for the turn | `call_llm` is unchanged and shared by all roles. The role-level accounting gap is R2-M4. |
| M3 delete token in traces | Traces: yes. Checkpoints: no | Drop-by-key in traces is intact. The "checkpoints hold only the hash" claim is false because of pending writes (R2-M2). This was not caused by rev. 3, but rev. 3 relies on it for resume. |
| M4 small-cell rule | Yes | §5.2 layer 5 unchanged and role-independent. |
| m4 `delete_not_alone` | Yes, wording stale | Still enforced; §4.1 step 3 describes it in the analyst context (R2-m2). |
| m5 `preview_turn + 1` | Undefined after a restart | R2-M3. |
| m6 cancel → `load_context` | Yes | Edge present in §2.3; fresh `TurnBudget`. *Later change:* cancel now routes to `input_guard` instead (scope snapshot re-check; R3-L7). |
| m10 idempotent `delete.previewed` | Yes | Insert-or-ignore on `(thread_id, step)` unchanged. |
| m1 build order and cut line | Needs refresh | The tiers predate the five roles. See the feasibility section. |
| Other Review-1 items (m2, m3, m7–m9, n1–n4) | Yes | No regression found. |

## Budget walk-through

Counted as "attempts that reach a provider". "As written" uses the rev. 3 text.

| # | Scenario | Calls (as written) | Cap | Result |
|---|---|---|---|---|
| 1 | Simple Q&A, happy path: router 1 + Quick 2 + summary 1 | 4 | 10 | OK |
| 2 | Simple → escalate at Quick's 4-call limit: router 1 + Quick 4 + Deep 4 + `force_answer` 1 | 10 | 10 | OK. No summary, consistent with "if a call is left". |
| 3 | As #2 with the router on its flash fallback | 11, or analysis silently shrinks to 8 | 10 | **Undefined** (R2-M4) |
| 4 | Q&A with 3 transient retries + 1 fallback on the first Quick call | 1 + 5 + up to 4 more | 10 | OK, bounded. Retries consume the analysis budget, as ADR-003 intends. |
| 5 | Report, happy: router 1 + Deep 5 + draft 1 + verify 1 | 8 | 14 | OK |
| 6 | Report, worst case per §4.1: router 1 + analysis 8 + draft, repair, verify, rewrite, re-verify | 14 | 14 | OK on paper |
| 7 | Report, worst case per §8 and §2.3 edges: router 1 + analysis 8 + draft + repair + writer fallback + verify + rewrite + repair of the rewrite + re-verify | 16 | 14 | **Exceeds** (R2-M4) |
| 8 | Report with the analysis at its cap: router 1 + 8 → `force_answer` | 10 | 14 | Consistent with "no report from a partial analysis", but `force_answer` is outside "analysis ≤ 8" (R2-M4) |
| 9 | SQL: Quick 2 failed → escalate; Deep corrections | ≤ 6 SQL | 6 | OK. The duplicate-hash block prevents Deep repeating Quick's queries. |
| 10 | Time, Q&A: per-attempt min(60 s, remaining), 10 s minimum for a retry | ≤ 120 s | 120 s | OK |
| 11 | Time, report phase: 5 sequential calls in the ≥ 45 s reserve (draft ≈ 10–20 s on flash) | ≈ 30–70 s | ≥ 45 s | **Tight.** The outcome on hitting the deadline mid-phase is not mapped (R2-M4, R2-M5). |
| 12 | Supersteps: parent worst ≈ 13 (load_context, input_guard, quick, deep, writer, val, ver, writer, val, ver, save, og, finalize; +3 per cancel loop); Deep subgraph ≈ 2 × 8 + 1 | ≈ 17 per level; ≈ 39 if counted across levels | 40 | OK per level, at the edge if cumulative (R2-m1) |

## Mermaid validation

**Method:** all ```` ```mermaid ```` blocks in `docs/architecture.md` were extracted with `awk` into separate files in a scratch directory. Each was rendered with `npx -y @mermaid-js/mermaid-cli -i <file>.mmd -o <file>.svg`, and each run exited 0 and produced an SVG.

| Block (first line) | Section | Type | Result |
|---|---|---|---|
| L123 | §2.1 Production | flowchart | Pass |
| L209 | §2.2 Prototype | flowchart | Pass |
| L270 | §2.3 Turn graph (supervisor + roles) | flowchart | Pass |
| L535 | §4.4 Retry and fallback chain | flowchart | Pass |
| L569 | §5.1 Turn sequence | sequenceDiagram | Pass |
| L800 | §6.3.3 Delete | sequenceDiagram | Pass (content conflicts with §2.3, R2-M1) |
| L1054 | §7.1 Stores | erDiagram | Pass |

## Prototype feasibility by Thursday 2026-10-08

Four working days remain. Five roles, five prompts, five eval suites, resume and per-role alerts do not all fit. Suggested cuts (the owner decides at G2):

| Keep in the prototype | Cut to HLD-only (design and tests stay documented) |
|---|---|
| Code supervisor, router inside `input_guard`, Quick and Deep analysts with escalation | Pro-class Deep model (production-only already) |
| Writer + code validation + a simple verifier (one prompt, `{verdict, issues}`) | "retry report" (R2-m3) |
| One shared `call_llm`, `TurnBudget` with the R2-M4 sub-caps, unit-tested | General `--resume <session>` (R2-M3). Keep the idempotency guarantees (save key, delete audit check, budget in state) with unit tests on a fake checkpointer |
| Delete with the R2-M1 node split and R2-M2 token handling | Per-role alerts (production §9.4); the prototype shows per-role counts in the metrics summary |
| One golden eval suite tagged by role; a router labelled set of about 50 (not ≥ 150); the adversarial suite at 100% | Separate per-role suites and gates (§6.6) |
| Library agent | Optionally replace it with code-routed commands (`list`, `view`, `delete`, `save`) behind the router's `library` label. This removes one prompt and the R2-M6(b) path |

## What is good

- The supervisor is code: conditional edges over typed state, no LLM hop to route, and the router is folded into a call that already existed. This is the cheapest, most testable multi-agent shape and avoids the LLM-supervisor routing-injection risk, which ADR-009 rejects for the right reasons.
- The writer and verifier have no tools, and the verifier has its own prompt and model, so report checking is independent of report writing.
- One envelope type, with routing on `status` and `error_class` only, keeps role failures uniform and testable.
- One `TurnBudget` across call, agent and flow levels (§4.0.4) means retries cannot multiply, the lesson of Review-1 M2.
- "No role runs twice in a turn" and "Quick → Deep at most once" make hand-offs bounded by construction.
- A failure matrix exists, with a user-facing message per row, and `output_guard` fails closed.
- Report saves have an idempotency key, the delete remains a parent-graph two-phase action that the Library agent cannot complete, and the guardrails stay in shared code that every role passes through.
- Per-role eval gates make a model swap per role a config change behind a measured gate.

## Gate

🔴 **G2 is the owner's gate.** This review recommends proceeding once R2-M1..M6 are resolved in `architecture.md` and the ADRs, or explicitly carried into the Step 4 plan as named tasks with tests. The `--resume` scope (R2-M3) and the feasibility cuts above are owner decisions for G2. This review does not approve G2.

## Architect response (Review 2, 2026-10-04)

All Review 2 findings are accepted, with no disagreements. They are fixed in HLD revision 4 together with requirements rev. 4 (brand scope, author-only reports, hard delete, light path, confirm-before-save, report search, output guard, context scope filter, frugality). The router label question is settled with a separate `smalltalk` label (ADR-010): merging it into `meta` would mix greetings and capability questions in traces and router metrics. The ADRs stay Proposed until G2.

| Finding | Resolution | Where |
|---|---|---|
| R2-M1 two delete owners | `confirm_delete` only validates and commits `delete.confirmed`; `execute_delete` is the single owner of the DELETE, committing `delete.executed` and the DELETE in one transaction, audit row first. Audit rows are unique on `(pending_action_id, event_type)`, so a replayed node is a no-op | §2.3, §6.3.3, §7.1, ADR-005, ADR-007 |
| R2-M2 plaintext token in checkpoints | The token lives only in a process-local vault keyed by `pending_action_id`. State and checkpoints hold its hash; the CLI resumes with `Command(resume={reply, pending_action_id, proof})`, where the proof is an HMAC; the proof is dropped by key from every trace sink | §2.3, §6.3.3, §6.7, §7.1, §7.2, §9.1, ADR-007 |
| R2-M3 resume at the delete interrupt | Narrow `--resume` finishes only the interrupted turn. A pending delete always expires (`delete.expired`, nothing deleted); an unconfirmed draft is shown again, never saved; a scope shrink starts a new session (FR-76). Tests `test_resume_expires_pending_delete`, `test_resume_reshows_draft`, `test_resume_scope_drift_new_session` | §4.0.6, §8, §12 #5, ADR-009 |
| R2-M4 report budget does not add up | Exact sub-caps. Q&A 10 = router ≤ 2 + analysis ≤ 7 (Quick ≤ 4) + `force_answer` 1. Report 14 = router ≤ 2 + analysis ≤ 6 + `force_answer` 1 + writer ≤ 3 + verifier ≤ 2. Light 3. Every provider attempt counts against the role sub-cap and the turn cap. The deadline outcome in the report phase is mapped | §4.0.4, §4.1, §8, ADR-003, ADR-009 |
| R2-M5 failure tracking | Missing failure rows added (unknown exception → `internal`, unexpected tool call, injection text in the answer, checkpointer write failure, light reply failure, report rate limit, store failure on Save). One metric `agent_outcome_total{agent,status,error_class}`. Alerts: page on `unexpected_action` and checkpointer failure; tickets on escalation > 30%/1 h, `router_unavailable` > 5%/15 min, verifier or `report_failed` > 10%/1 h, `output_injection` | §4.0.5, §8, §9.2, §9.4 |
| R2-M6 router and stored-report injection | The router sees user messages only. `view_report` and search output are wrapped as untrusted data. `set_preference` needs the value in the current user message (code check). Per-user rate limit on `report` labels (default 20/h, §13.2 Q9). Output guard with action allowlist per role and label and an answer injection scan (FR-75); context scope filter (FR-76). Eval cases `router_label_injection`, `stored_report_injection`, `output_action_injection` in the 100% adversarial gate | §4.0.2, §4.1, §4.2, §5.2, §6.4, §6.6, §10.1, ADR-004 |
| R2-m1 `recursion_limit` | About 60 (2 × 14 calls + parent nodes + margin); routers check `RemainingSteps` and go to `force_answer`; the CLI catches `GraphRecursionError` (`test_recursion_limit_caught`) | §4.1, §6.5, §8 |
| R2-m2 stale wording | "Classifier" replaced by "router" in the HLD and in the ADR-003 and ADR-004 bodies; §4.1 renamed "Turn flow"; `mode=generate` removed; `delete_reports` described as a Library-agent tool | §4.1, §6.3.1, §12, ADR-003, ADR-004 |
| R2-m3 retry report | The last scrubbed ledger stays in session state until the next analysis turn; "retry report" uses only the router and report-phase budget, with no SQL re-run | §4.0.5, §4.4 |
| R2-m4 stale cost model | §7.4 rewritten as usage per turn type (light, Quick Q&A, Deep Q&A, report; a refusal makes at most the router call) for the new load (A-8, A-35). Rev. 4.1: no money figures at all (owner decision at G2) | §7.4 |
| R2-m5 subgraph persistence | Role subgraphs compile with the default per-invocation checkpointer; the resume test reads state with `get_state(config, subgraphs=True)` | §4.0.6 |
| R2-m6 budget under crash | Stated: at most one node's calls can repeat after a crash; the trace reconciles the count; the test asserts the bound, not equality | §4.0.6 |
| R2-m7 Deep fallback | Prototype flash → flash-lite; production pro-class → flash. Diagram, role table and ADR-009 aligned | §2.1, §4.4 |
| R2-n1 typo | Fixed: "The API reads BigQuery only through …" | §2.1 |
| R2-n2 embedding calls | ≤ 1 per turn, outside the LLM-call cap, cached | §4.1, §4.4 |

New in revision 4: §1.4 Key decisions and alternatives, §2.0 Overview (stores and failure handling), §4.5 Speed and tokens, §6.2a Frugality, §9.6 Traces vs the Golden Bucket, and ADR-010 (router labels and light path), ADR-011 (confirm before save, unlimited Revise, author-only), ADR-012 (caches that never loosen a guardrail). §7.4 is now "Sizing and cost estimate (production, non-binding)". The feasibility cuts suggested by Review 2 are left to the owner at G2.

**Mermaid re-validation:** all 8 blocks in `architecture.md` (5 flowcharts, 2 sequence diagrams, 1 ER diagram) were extracted and rendered with `npx -y @mermaid-js/mermaid-cli`; all pass. One parse error was found and fixed during validation: semicolons inside the delete sequence messages (§6.3.3).

🔴 G2 remains the owner's gate.

### Owner decisions on the Review 2 feasibility cuts (2026-10-04, HLD rev. 4.1)

At the G2 discussion the owner committed Golden seed (FR-48, tier 5) and extended report search (FR-74: FTS5 bm25 plus semantic search with RRF, tier 6) to the prototype, and asked for an explicit list of what is cut. HLD §1.2 now has tiers 0–7, with tiers 0–6 as the minimum, and the "Cut from the prototype (design-only)" list. Deadline risk grows: if tier 6 runs late, its semantic half is dropped first. Details: `docs/decisions.md`, "Owner G2 decisions on HLD open questions and prototype scope".

### Owner decisions rev. 4.2 (2026-10-04): parity reverses two cuts

The owner chose parity between the prototype and production (option C). Two Review 2 feasibility cuts are reversed: "retry report" (R2-m3) is in the prototype again, re-running only the writer and verifier on the last scrubbed ledger with no SQL; the pro-class Deep model (R2-m7) is a choice in `config/models.yaml` in either environment, with the fallback chain unchanged. The fixes listed above for R2-m3 and R2-m7 still apply. Details: `docs/decisions.md`, "Owner G2 decisions rev. 4.2".
