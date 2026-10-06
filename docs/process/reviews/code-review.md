# Code Review: agent and agentic flow (iteration 39b, HEAD 812b29d)

Date: 2026-10-06 · Reviewer: T1 (multi-agent fan-out, anchors re-verified in the main session) · Files reviewed: 22 under `src/opsfleet_agent/` (graph, roles, tools, delete, store, obs, cli) · **Verdict: CHANGES REQUESTED** (recommendation to the owner; 🔴 G4 stays with the owner)

> Severity scale (reviewer.md): CRITICAL = PII leak, scope bypass, unconfirmed deletion, unbounded spend · HIGH = likely wrong behaviour under normal use, or a failed deliverable · MEDIUM = footgun or maintainability · LOW = minor. Finding ids: `AF-n` (agent flow). HIGH items need the owner's approval of the fix approach (G4); MEDIUM and LOW may be fixed autonomously.

## 0. Scope and method

| Reviewed revision | |
|---|---|
| Branch / commit | `step5-implementation` @ `812b29d` (== `main`), working tree clean |
| `README.md` | 1052 lines, md5 `7f45e45406d318dea8149c744d93b26e` |
| `docs/technical.md` | md5 `a21ceecb09db639e76bacc1b223f71cf` |
| `docs/architecture.md` | md5 `113f6583542ba57e5d0ad338101be3c1` |

| Reviewer | Scope | Raw findings | Kept after verification |
|---|---|---|---|
| Agent-flow subagent | `graph/graph.py`, `graph/budget.py`, `graph/llm.py`, `graph/degraded.py`, `graph/resume.py`, `roles/*`, `tools/run_sql.py`, `tools/registry.py`, `cli.py` | 6 | 6 (AF-1..AF-6) |
| Main session | re-ran every HIGH/MEDIUM anchor with `sed -n`; ruff, pytest, offline evals (see `eval-review.md`) | — | — |

## CRITICAL

None.

## HIGH

### AF-1 — Per-attempt LLM records never reach the local tracer; `/trace`, metrics and triage are blind to model failures

- `graph/llm.py:308` is the only place a per-attempt record (`model`, `attempt`, `fallback_used`, error, tokens) is emitted: `self.budget.usage.record(rec)`.
- `graph/budget.py:108-111` defines the `UsageSink` protocol and `:125` the optional `sink` field, but both `TurnBudget(...)` call sites (`graph/graph.py:524-527`, `:693-699`) pass no `sink=`. `grep -rn 'record("llm"' src/` returns nothing.
- Consumers that expect an `llm` span: `obs/metrics.py:135` (`fallbacks = sum(1 for s in llm if s.get("fallback_used"))`), `commands/triage.py:172-178` (`model_down` from `by_type.get("llm", [])`), the `/trace` viewer. `graph.py:1730` records only the aggregate `llm_calls_total`; `graph.py:1066` only role-level `retries=attempts`.
- `docs/architecture.md:1438` promises retries/fallbacks per call in the trace. Existing tests (`tests/unit/test_tracer.py:78,81,114`, `test_trace_viewer.py`, `test_triage.py`) inject `llm` spans synthetically, so nothing catches the gap.
- **Impact:** the R7 "agent-level metrics" deliverable reports fallback rate 0 and retry count 0 regardless of what Gemini does; `triage --model-down` can never fire.
- **Fix (needs owner approval, G4):** add a `TracerUsageSink` adapter in `obs/tracer.py` that records an `llm` span (`model`, `outcome`, `attempt`, `retries`, `fallback_used`, `tokens_in/out`, `limiter_wait_ms`, `status`), pass `sink=services.usage_sink` at both `TurnBudget` call sites, and add a test that drives `LLMWrapper.call` with a failing fake provider and asserts an `llm` span with `fallback_used=True` / `status="error"` in the tracer output.

## MEDIUM

### AF-2 — The router still runs on forced-label turns (Revise, `/retry`) and can refuse a revise after the old draft was closed

- `graph/graph.py:788` calls `route(...)` unconditionally; `:794-795` overrides the result with `ctx.forced_label` only when the router did not return `refuse`. `resume("revise")` has already closed the pending draft by then (`:2177-2200`).
- Cost: 1–2 of the 8 (`RETRY_REPORT`) / 14 (`REPORT`) budgeted LLM calls spent on a decision that is ignored. Risk: a router `refuse` on a benign revise instruction drops the user's draft. Related accepted deviation D-141 (`docs/technical.md:354`) covers quota cut-off, not this path.
- `grep -rn forced_label tests/` → no test.
- **Fix:** skip `route()` when `ctx.forced_label` is set (keep the non-LLM input guards), or never let a router `refuse` win over a forced label; close the old draft only after the new turn passes the guard. Add a unit test with a stub router returning `refuse` on a forced `revise`.

### AF-3 — HLD promises a 30-minute idle timeout for the prototype; none is implemented

- `docs/architecture.md:86`, `:133`, `:1389` (FR-10) describe a 30-minute idle timeout and name `test_idle_timeout_drops_pending_delete`. `cli.py:513-517` is a plain `input("> ")`; `grep -rn 'idle\|1800' src/` finds nothing; the named test does not exist.
- A pending delete is still protected by the 600 s token clock (`delete/flow.py` `is_live` / `lapse_reason`, `graph.py:2340`), so this is not a safety hole. A pending Save/Revise/Cancel draft stays answerable indefinitely.
- **Fix:** either implement it (read input on a helper thread, `join(IDLE_TIMEOUT_S)`, on expiry `resume("cancel")`, `abandon("expired")`, new `session_id`) with the named test, or amend `architecture.md` and FR-10 to "deferred to production" so the HLD matches the prototype.

## LOW

### AF-4 — Dead guard clause in the light path
`roles/light_path.py:240`: `if budget.subcap(LIGHT_ROLE) > 1 or budget.calls >= TURN_CAPS[TurnKind.LIGHT].llm_calls:` — the first clause compares the configured cap (always 1, `budget.py:195-196`) and is always false. Intended: `budget.role_calls[LIGHT_ROLE] >= budget.subcap(LIGHT_ROLE)`.

### AF-5 — `verifier_calls` counts verdicts that made no LLM call
`roles/report_writer.py:296`: `if verdict.source != "code":` also counts `"unavailable"` verdicts. Use `== "llm"`.

### AF-6 — Stale anchors and small code-quality nits
- `docs/technical.md` cites `graph.py` line numbers that have drifted (actual: `_make_nodes :759`, `_build :1914`, `AgentGraph :1966`, `run_turn :1998`, `_run :2016`, `PendingTurn :2323`, `_result :2439`).
- `graph.py:2142` is redundant with the check above it; `_finalize` is ~95 lines.
- Private-attribute coupling: `graph.py:694-698`; `degraded._budget` → `inner._sql_session`; `resume.py` reaches into `pending._built` and `agent._config`.

## Worst-case LLM calls per turn (vs budgets in `graph/budget.py`)

| Turn kind | Cap | Worst case observed in code | Note |
|---|---|---|---|
| Light | 3 | 3 | router + light role + fallback |
| Q&A | 10 | 15 nominal → stopped at 9 + reserved `force_answer` | cap enforced by `TurnBudget`, so bounded |
| Report | 14 | 14 | |
| Revise | 14 | 14 | 1–2 wasted on the router (AF-2) |
| `/retry` | 8 | 7 | |
| Library | 10 | 9 | |
| Delete confirm | 0 | 0 | code only |

SQL: ≤6 statements per turn; `MAX_CONSECUTIVE_FAILURES = 3` (`tools/run_sql.py:149`); `RECURSION_LIMIT = 60`; `LLMWrapper` ≤4 attempts per call with `BACKOFFS_S = (1.0, 2.0)`.

## Good

- Every BigQuery path is dry-run, capped with `maximum_bytes_billed` and retried a bounded number of times (`bq/client.py:395-440`); cancellation is correct (`:459-474`).
- Fail-closed everywhere it matters: `_run_locked`, `Prepared` identity, `degraded._gate`, the owner check in `resume.py`, the light path refusing to answer with figures, the verifier never passing by default, the router failing open only to `complex`.
- Pending actions (save / revise / cancel / delete) are code-owned (`graph.py:2176-2200`); all 10 router labels map to a node.
- No tracebacks or secrets reach the user (`MAX_LINE_CHARS = 4000`, `MAX_TURNS = 1000`, `format_error`).

## Observations (not findings)

- `_analyst` (`graph.py:939`) may call `run_analyst` up to 3× per node; with `time_bounded=False` (local provider, D-149) there is no deadline. A named `MAX_ANALYST_STEPS` would make the bound visible.

## Coverage gaps

- No test drives `LLMWrapper` with a failing provider end-to-end through the tracer (AF-1).
- No test for a forced-label turn meeting a router `refuse` (AF-2).
- `test_idle_timeout_drops_pending_delete` named in the HLD does not exist (AF-3).
- No test for the light-path cap clause (AF-4).

## Verdict

**CHANGES REQUESTED.** One HIGH (AF-1) blocks approval and needs the owner's decision on the fix approach (🔴 G4). AF-2..AF-6 can be fixed autonomously. Related reviews: `security-review.md` (APPROVED), `eval-review.md` (APPROVED), `final-review.md` (BLOCKED on AF-1, RA-1, RC-1).
