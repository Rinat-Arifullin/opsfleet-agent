# Workflow: SDLC Lite

A 6-step chain for a single-developer LLM-agent project. The orchestrator (Claude Code, T1) runs it and stops at every 🔴 gate.

```
[1] Requirements → 🔴 → [2] Design (🟡 digest) → [3] Design review → 🔴 → [4] Plan → 🔴
→ [5] Implementation (loop over iterations) → [6] Final review → 🔴
```

> To run it, the orchestrator reads this file and follows it. There is no `/workflow` command.

---

## Step 0: Context load (orchestrator)

Read:
- the assignment (the source of truth for scope and deliverables);
- the existing `docs/`;
- `docs/decisions.md`;
- the current code.

Produce a short internal state block: current step, open decisions and known constraints. The constraints here are:
- the Gemini free-tier rate limits;
- the BigQuery 1 TB/month free tier;
- the deadline.

---

## Step 1: Requirements

- **Worker:** analyst (T1), using [`skills/analyst.md`](../skills/analyst.md)
- **Input:** assignment text and recruiter clarifications
- **Output:** `docs/process/01-requirements.md`

The document must:
- split requirements into **Prototype** (M or P; implemented and tested), **Platform adapter** and **Roadmap** (both covered in the HLD);
- give every prototype-must requirement Given/When/Then ACs that can be turned into a pytest test or an eval case;
- list assumptions that we are making on the client's behalf, for example the user→product mapping, the PII definition and the Golden Bucket mock. These become questions for the client contact.

**🔴 Gate: requirements approval.** The human checks scope, assumptions and AC testability, and decides which open questions to send to the client's AI team lead. Work continues on the stated defaults while waiting for answers.

---

## Step 2: Design (HLD deliverable)

- **Worker:** architect (T1, effort `xhigh`), using [`skills/architect.md`](../skills/architect.md)
- **Input:** the approved requirements
- **Output:** `docs/architecture.md` (production HLD + detailed technical description + prototype scope). Each ADR also goes to `docs/decisions.md`.

The orchestrator first presents a **decision digest** (🟡 soft) before the full doc is written:
1. the agent framework choice, with the human's experience level stated;
2. LLM model routing and fallback;
3. the guardrail placement (pre-LLM, in-tool, post-LLM);
4. the stores (Saved Reports, preferences, traces, Golden Bucket index);
5. the production cloud services.

The human may redirect at the digest; otherwise the architect writes the full HLD. The hard approval comes after the design review, so the human approves the reviewed version.

---

## Step 3: Design review

- **Worker:** design-reviewer (T1), a **different instance** from the architect, using [`skills/reviewer.md`](../skills/reviewer.md) in Design Review mode, plus the LLM-specific part of [`skills/security-reviewer.md`](../skills/security-reviewer.md)
- **Output:** `docs/process/03-design-review.md` with the verdict READY or BLOCKED

The architect fixes CRITICAL and HIGH findings. MEDIUM and LOW findings are fixed, or logged as accepted in `docs/decisions.md`. If the verdict is still BLOCKED after one revision, the human decides.

**🔴 Gate: design approval.** The human approves the reviewed HLD: does it answer all 8 requirements, and is the production story credible?

---

## Step 4: Plan

- **Worker:** planner (T2), using [`skills/planner.md`](../skills/planner.md)
- **Output:** `docs/process/04-plan.md`

The plan contains iterations of at most 5 files each. Every iteration has done criteria that include `uv run ruff check`, `uv run pytest` and, where relevant, the eval subset it unlocks.

**🔴 Gate: plan approval.** This replaces the separate plan-review phase of the full framework. The human checks the order, the risky iterations and the time budget against the deadline.

---

## Step 5: Implementation (loop)

For each iteration N:

```
1. The orchestrator writes a Worker Brief (iteration scope, files, done criteria, do-NOT list).
2. The implementer (T2) writes the code and its tests together (skills/implementer.md).
3. If the iteration adds a guardrail or tool, the implementer also adds its eval case YAML (skills/tester.md). Writing a case is cheap; running live evals is not.
4. Run: uv run ruff check . && uv run pytest -q   (offline only; live eval subsets run only at the milestones marked in the plan).
5. Green → commit "iter N: <goal>" → mark it done in 04-plan.md.
   Red → fix it within the iteration; after 2 failed attempts, switch to the debugger (bug-hunt.md).
```

- Iterations with no shared files may run in parallel (marked `[PARALLEL OK]` in the plan).
- **Scope creep (🟡):** anything found outside the plan is surfaced to the human with the options *add now / defer to the README "future work" / skip*.
- **Live-API discipline:** unit tests mock Gemini and BigQuery. Only the eval suite and the smoke tests hit real APIs, and they are rate-limited. The plan states a live-call budget (Gemini requests per day, BigQuery bytes) so that full eval runs fit within the free tier.

---

## Step 6: Final review

The orchestrator fans out three workers **in parallel**:

| Worker | Skill | Output |
|---|---|---|
| code-reviewer (T2) | `reviewer.md` | `docs/process/reviews/code-review.md` |
| security-reviewer (T2) | `security-reviewer.md` | `docs/process/reviews/security-review.md` |
| tester / evaluator (T2) | `tester.md` (run the full evals) | `docs/process/reviews/eval-review.md` |

Then the final-reviewer (T1) uses [`skills/final-reviewer.md`](../skills/final-reviewer.md) and writes `docs/process/reviews/final-review.md`. That covers:
- traceability from each AC to its test or eval;
- a clean-clone install;
- the demo script;
- completeness of the deliverables against the assignment.

**🔴 Gate: submit approval.** Any HIGH or CRITICAL finding needs the human to approve the fix approach. MEDIUM and LOW findings are fixed autonomously and listed in the summary.

---

## Completion

- The README's "How I worked" section links to this folder and summarizes:
  - the gate decisions (from `docs/decisions.md`);
  - what the AI did and what I decided;
  - the time spent per step.
- Tag the submission commit.
