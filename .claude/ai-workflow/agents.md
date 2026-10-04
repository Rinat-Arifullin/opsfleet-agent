# Agents Registry

Agents, roles, model tiers, I/O contracts and skill files for the lite workflow.

See also:
- [`README.md`](README.md): entry point and artifact map
- [`config/models.md`](config/models.md): model tiers and context compaction
- [`config/human-gates.md`](config/human-gates.md): gates and project risk areas
- [`config/orchestrator-pattern.md`](config/orchestrator-pattern.md): Orchestrator + Workers pattern

Roles map to Claude Code subagents: `Explore` (read-only search), `Plan` (read-only design critique) and `general-purpose` (scoped execution). A single Opus orchestrator session dispatches them.

---

## Orchestrator

### `orchestrator`
- **Model:** T1 (`opus`)
- **Role:** runs [`workflows/sdlc-lite.md`](workflows/sdlc-lite.md), writes Worker Briefs, aggregates worker output and surfaces gates
- **Does NOT:** self-approve 🔴 gates, or expand scope without a 🟡 surface

**Loop:**
```
1. Read the assignment, docs/, docs/decisions.md and the current code.
2. Decompose the current step into subtasks.
3. Give each worker a Worker Brief (scope, files, constraints, output format, do-NOT).
4. Fan out the independent subtasks in parallel.
5. Collect the outputs and run the reflection check ("did the worker answer its task?").
6. Resolve conflicts. A trade-off between them becomes a 🔴 gate.
7. Surface a gate if the step requires one, then proceed or loop.
```

---

## Workers

### `analyst`
- **Model:** T1. The input is ambiguous and it defines everything downstream.
- **Role:** turns the assignment into Prototype (M or P) / Platform adapter / Roadmap requirements, testable ACs, agent NFRs (latency, cost per question, rate limits) and client assumptions
- **Output:** `docs/process/01-requirements.md`
- **Skill:** [`skills/analyst.md`](skills/analyst.md)
- **Gate after:** 🔴 requirements approval

### `architect`
- **Model:** T1 (effort `xhigh`)
- **Role:** writes the production HLD and the prototype design. It covers the agent loop, tool contracts, the prompt/persona layers, the guardrail pipeline, stores, model routing and fallback, the Golden Bucket RAG, the learning loop, evals and observability.
- **Output:** `docs/architecture.md`, plus ADRs in `docs/decisions.md`
- **Skill:** [`skills/architect.md`](skills/architect.md)
- **Gate:** 🟡 decision digest before the full doc

### `design-reviewer`
- **Model:** T1, a different instance from the architect
- **Role:** critiques the design for requirement coverage, guardrail gaps, failure modes and cost blow-ups
- **Output:** `docs/process/03-design-review.md`, verdict READY or BLOCKED
- **Skill:** [`skills/reviewer.md`](skills/reviewer.md) (Design Review mode) plus the LLM section of [`skills/security-reviewer.md`](skills/security-reviewer.md)
- **Gate after:** 🔴 design approval (the reviewed HLD)

### `planner`
- **Model:** T2. Decomposing an approved design is mechanical work.
- **Role:** slices the design into small iterations in dependency order (config → BigQuery → guardrails → tools → agent → CLI → observability → evals)
- **Output:** `docs/process/04-plan.md`
- **Skill:** [`skills/planner.md`](skills/planner.md)
- **Gate after:** 🔴 plan approval

### `implementer`
- **Model:** T2, scoped to one iteration
- **Role:** Python engineer. Writes typed code and its pytest tests together.
- **Input:** a Worker Brief for iteration N
- **Output:** code + tests, one commit per iteration
- **Skill:** [`skills/implementer.md`](skills/implementer.md)
- **Scope-creep rule:** 🟡 surface to the orchestrator, never self-expand

### `tester` (tester / evaluator)
- **Model:** T2
- **Role:** writes the pytest unit tests and the **agent eval suites**: golden Q→SQL/answer, PII-leak red-team, prompt-injection red-team, scope-bypass, the delete-confirmation flow, and resilience with fault injection
- **Output:** `tests/`, `evals/`, `docs/process/reviews/eval-review.md`
- **Skill:** [`skills/tester.md`](skills/tester.md)

### `code-reviewer`
- **Model:** T2
- **Role:** reviews the diff for correctness, loop termination, error handling, retry and cost caps, and typing
- **Output:** `docs/process/reviews/code-review.md`
- **Skill:** [`skills/reviewer.md`](skills/reviewer.md)
- **Gate:** 🔴 for HIGH or CRITICAL fixes

### `security-reviewer`
- **Model:** T2
- **Role:** audits against the OWASP LLM Top 10: prompt injection, SQL safety, PII in output and logs, product-scope enforcement, excessive agency (delete), and secrets
- **Output:** `docs/process/reviews/security-review.md`
- **Skill:** [`skills/security-reviewer.md`](skills/security-reviewer.md)
- **Gate:** 🔴 for any HIGH or CRITICAL finding

### `final-reviewer`
- **Model:** T1
- **Role:** the pre-submit gate. Checks AC traceability, a clean-machine install, the demo script, deliverables vs the assignment, and trace completeness.
- **Output:** `docs/process/reviews/final-review.md`
- **Skill:** [`skills/final-reviewer.md`](skills/final-reviewer.md)
- **Gate:** 🔴 submit approval

### `debugger`
- **Model:** T1
- **Role:** root-cause analysis. Reads traces first, then reproduces with a pinned prompt and mocked dependencies.
- **Output:** a note in `docs/decisions.md`, or a section in the relevant review file
- **Skill:** [`skills/debugger.md`](skills/debugger.md)

---

## Routing

| Trigger | Route |
|---|---|
| New capability / the take-home itself | orchestrator → [`workflows/sdlc-lite.md`](workflows/sdlc-lite.md) |
| Failing test or eval, or wrong agent behaviour | orchestrator → [`workflows/bug-hunt.md`](workflows/bug-hunt.md) |
| Pre-submit | Step 6 of sdlc-lite (3 parallel reviewers + final-reviewer) |
| Context at 70% | `/compact` with the current step and open decisions as the hint |
