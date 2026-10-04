# ai-workflow (lite): how this project was built

This folder holds the AI-assisted development process used to build this project. It is committed on purpose so reviewers can see **how** the work was done, not only the result.

The framework is a port of my own `ai-workflow` SDLC framework. I originally wrote it for a classic NestJS/React backend+frontend stack. This copy is adapted in three ways:

- **Stack:** Python 3.12 / uv / pytest / ruff, LangGraph 1.x + `langchain-google-genai` (Gemini), BigQuery, and a CLI chat agent.
- **Risk profile:** an LLM agent that writes SQL. Its main risks are prompt injection, PII leakage, row-level scoping, destructive tool calls, runaway cost and non-deterministic output. Each skill's checklists target these risks instead of HTTP/DB/cache concerns.
- **Size:** a take-home is a one-person, roughly one-week project. The original 7 phases become a **6-step lite chain** with four human gates: the plan review is merged into the human plan gate, and the product-manager skill and the auxiliary workflows (hotfix, refactor, PR review and others) are dropped.

## Roles

| Role | Who |
|---|---|
| Human (product owner + approver) | Me. I approve every 🔴 gate. |
| Orchestrator | Claude Code (Opus, T1). It runs the chain, writes Worker Briefs and aggregates results. |
| Workers | Claude Code subagents: mostly Sonnet (T2); judgement-heavy roles (analyst, architect, reviewers of the design and the final submission, debugger) on Opus (T1). See [`config/models.md`](config/models.md). |

## The chain

```
[1] Requirements ──🔴──▶ [2] Design (HLD, 🟡 digest) ──▶ [3] Design review ──🔴──▶ [4] Plan ──🔴──▶
[5] Implementation (iterations, tests + evals per iteration) ──▶
[6] Final review (code ∥ security ∥ evals) ──🔴──▶ submit
```

Full description: [`workflows/sdlc-lite.md`](workflows/sdlc-lite.md). For a bug found after implementation, use [`workflows/bug-hunt.md`](workflows/bug-hunt.md).

## Layout

| Path | Contents |
|---|---|
| [`agents.md`](agents.md) | Agent registry: role, model tier, input/output, skill file |
| [`config/models.md`](config/models.md) | Model tiers (the single source of truth for model IDs) |
| [`config/human-gates.md`](config/human-gates.md) | Where the human approves, and the project risk areas |
| [`config/orchestrator-pattern.md`](config/orchestrator-pattern.md) | Orchestrator + Workers pattern, Worker Brief format |
| `skills/*.md` | One system prompt and checklist per role |
| `workflows/*.md` | Step-by-step chains |

## Artifact map

Each step leaves a file in the repo, so the decisions can be traced after the fact.

| Step | Artifact |
|---|---|
| 1 Requirements | `docs/process/01-requirements.md` |
| 2 Design | `docs/architecture.md`: **the HLD deliverable** (Mermaid diagram + detailed technical description) |
| 3 Design review | `docs/process/03-design-review.md` |
| 4 Plan | `docs/process/04-plan.md` |
| 5 Implementation | `src/`, `tests/`, `evals/` |
| 6 Final review | `docs/process/reviews/{code,security,eval,final}-review.md` |
| Decisions (any step) | `docs/decisions.md` (ADR log, including gate overrides) |

## What changed vs the original framework

| Original (NestJS/React) | Lite (Python LLM agent) |
|---|---|
| 7 phases, PM brief, separate plan review | 6 steps; the plan review is merged into the human plan gate |
| Architect: NestJS modules, DDL, REST contract, Redis caching | Architect: agent loop, tool contracts, prompt layers, guardrail pipeline, stores, model routing and fallbacks |
| Planner order: migration → entity → service → controller → UI | Planner order: config → BigQuery layer → guardrails → tools → agent loop → CLI → observability |
| Tester: Jest / Supertest / Playwright | Tester: pytest unit tests + **agent evals** (golden questions, SQL assertions, PII-leak and injection red-team suites) |
| Reviewer: TypeScript, TypeORM, React | Reviewer: Python typing, agent-loop termination, tool-error handling, cost and retry caps |
| Security: OWASP web top 10 | Security: OWASP **LLM** top 10: prompt injection, SQL injection via the LLM, PII output, scope bypass, excessive agency |
| Final review: staging, load tests, migrations | Final review: a clean-machine install, the run-through demo script, eval pass rate, trace completeness |
