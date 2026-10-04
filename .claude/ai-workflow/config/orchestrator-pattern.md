# Orchestrator + Workers Pattern

One orchestrator plans and routes; scoped workers execute; the orchestrator aggregates the results and surfaces human gates.

---

## Pattern Structure

```
              ┌──────────────────────────┐
              │ ORCHESTRATOR (Opus)      │  plans + writes Worker Briefs
              └──┬──────────┬─────────┬──┘
                 │          │         │   fan out (parallel)
          ┌──────▼───┐ ┌────▼─────┐ ┌─▼────────┐
          │ code     │ │ security │ │ evals    │  ← Sonnet workers
          └──────┬───┘ └────┬─────┘ └─┬────────┘
                 │ reflect  │ reflect │ reflect
              ┌──▼──────────▼─────────▼──┐
              │ ORCHESTRATOR: aggregate, │
              │ resolve conflicts        │
              └────────────┬─────────────┘
                     Human gate 🔴 (if needed)
```

---

## Orchestrator Responsibilities

1. **Decompose** — break the incoming task into independent subtasks
2. **Route** — assign each subtask to the right worker with full context
3. **Fan out** — launch workers in parallel where there are no dependencies
4. **Monitor** — detect if a worker is blocked or diverging
5. **Aggregate** — merge worker outputs, resolve conflicts
6. **Gate** — surface human decisions when needed, never self-approve high-stakes choices

**Model**: Opus — must hold the full task context and reason across worker outputs.

---

## Worker Responsibilities

1. **Accept scoped task** — one clearly defined subtask with all needed context
2. **Execute** — implement, review, or test the specific thing
3. **Reflect** — before returning, self-check: "Does my output actually answer the task?"
4. **Return structured output** — in the format the orchestrator expects

**Model**: Sonnet — well-defined input, single domain, reversible.

**Worker reflection prompt** (each worker runs this before returning):
```
Before returning my output, I verify:
- Does my output directly answer the task I was given?
- Are there any assumptions I made that the orchestrator should know about?
- Did I stay within my assigned scope? (if not, flag it, don't self-expand)
- Is my output format what the orchestrator expects?
```

---

## Context Passing

Workers receive a **Worker Brief** — not the full conversation, only what they need:

```markdown
## Worker Brief

**Your task**: [specific subtask description]
**Relevant files**: [list of files to read]
**Constraints**: [specific rules for this task]
**Output format**: [exactly what to return]
**Do NOT**: [explicit scope boundaries]
```

This keeps worker context small and focused. The orchestrator owns the full context.

---

## Fan-out Strategy

| Workers are independent | Run in **parallel** |
| Worker B needs Worker A's output | Run **sequentially** |
| Workers touch the same file | Run **sequentially** |

Example for Step 6 (final review) of sdlc-lite:
```
Orchestrator launches simultaneously:
  → code-reviewer (reads diff)
  → security-reviewer (reads diff)
  → tester/evaluator (runs tests + eval suites)

All three run in parallel → orchestrator aggregates 3 reports.
```

---

## Conflict Resolution

When two workers return conflicting findings (e.g., code-reviewer says "retry the failed SQL with the error fed back", security-reviewer says "the raw BigQuery error echoes column values into the prompt"):

Orchestrator:
1. Identifies the conflict explicitly
2. Proposes resolution that satisfies both
3. If resolution requires a tradeoff → **Human Gate** 🔴

---

## Applying to Each Workflow

| Workflow | Orchestrator | Workers |
|---|---|---|
| `sdlc-lite.md` | Runs the 6-step chain, holds the step state | analyst, architect, design-reviewer, planner, implementer, tester, reviewers |
| `sdlc-lite.md` Step 6 | Launches the parallel reviews, aggregates | code-reviewer, security-reviewer, tester/evaluator → final-reviewer |
| `bug-hunt.md` | Drives the hypothesis loop | debugger, implementer, code-reviewer |
