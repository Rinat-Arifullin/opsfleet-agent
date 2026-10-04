# Model Tiers

Opus for the orchestrator and judgement-heavy roles, Sonnet for scoped workers.

**This file is the single source of truth for exact model IDs.** Everywhere else (`agents.md`, `workflows/*`, `skills/*`) refers to the tier (`T1`/`T2`).

| Tier | Model ID | Effort | Use when |
|---|---|---|---|
| **T1: Orchestrator / judgement** | `claude-opus-5-5` | `high` (`xhigh` for architect, debugger) | Decomposing work, design decisions, gating reviews, ambiguous or security-relevant reasoning |
| **T2: Worker** | `claude-sonnet-5-5` | `medium` | Implementing one iteration, writing tests and evals, one scoped review pass |

## Per-agent assignment

| Agent | Tier | Reason |
|---|---|---|
| orchestrator | T1 | Holds the full context, routes and aggregates |
| analyst | T1 | Ambiguous input; everything downstream depends on it |
| architect | T1 | High-stakes design across layers |
| design-reviewer | T1 | Must catch what the architect missed |
| final-reviewer | T1 | Holistic pre-submit judgement |
| debugger | T1 | Root-cause analysis is ambiguous |
| planner | T2 | Decomposes an approved design |
| implementer | T2 | Scoped per iteration |
| code-reviewer | T2 | One pass over a known diff |
| security-reviewer | T2 | Checklist-driven |
| tester / evaluator | T2 | Scoped and reproducible |

## Context hygiene

When the context gets long, `/compact` with the current step and the open decisions as the hint. Always keep `docs/process/01-requirements.md`, `docs/architecture.md` and the current state of `docs/process/04-plan.md`; completed iterations shrink to one line each.

## Note: these tiers are for *building* the project

The tiers above are the Claude models used by the development agents. The **product's** runtime models (Gemini primary and fallback) are a product decision. They are recorded in `docs/architecture.md` and `docs/decisions.md`, not here.
