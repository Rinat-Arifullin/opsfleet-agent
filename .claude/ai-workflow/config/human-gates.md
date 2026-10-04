# Human Approval Gates

The AI proposes and the human approves. A gate blocks the chain until the human explicitly approves.

## Philosophy

Gates exist only where:
- a wrong decision is expensive to reverse;
- the AI lacks the client or business context to decide;
- the change touches one of the **risk areas** below.

Everything else runs autonomously and is logged.

## Gate levels

| Level | Symbol | Meaning |
|---|---|---|
| HARD | 🔴 | The chain stops until the human approves. |
| SOFT | 🟡 | The AI proceeds on a stated default and surfaces the decision. |
| INFO | 🟢 | The AI decides and logs the decision in `docs/decisions.md`. |

## Gates in SDLC Lite

| # | After step | Level | What the human checks |
|---|---|---|---|
| G1 | 1 Requirements | 🔴 | Is the Prototype (M or P) / Platform adapter / Roadmap split right? Are the assumptions on the client's behalf acceptable, or should we ask the client? Are the ACs testable? |
| — | 2 Design (digest) | 🟡 | Framework choice and experience level, model routing, guardrail placement, stores |
| G2 | 3 Design review | 🔴 | The reviewed HLD: does it answer all 8 requirements? Is the production story credible? |
| G3 | 4 Plan | 🔴 | Iteration order, risky iterations, time budget against the deadline |
| — | 5 Implementation | 🟡 | Scope creep: add now, defer to "future work", or skip |
| G4 | 6 Final review | 🔴 | HIGH or CRITICAL fixes and the go/no-go for submission |

## Project risk areas (always at least 🟡; 🔴 if behaviour changes)

Any change touching these needs explicit human sign-off, even mid-iteration:

1. **PII masking:**
   - the column allowlist;
   - output post-filters;
   - what gets written to logs and traces.
2. **Product scoping:**
   - how the per-user product filter is injected and enforced in SQL;
   - any path that could bypass it.
3. **Destructive operations:**
   - the Saved Reports delete flow (two-phase preview → confirm);
   - the ownership check;
   - anything that lets the LLM itself trigger deletion.
4. **SQL execution policy:**
   - read-only enforcement (SELECT only, single statement);
   - the table allowlist;
   - `maximum_bytes_billed` and the dry-run cost check.
5. **Prompt and guardrail layers:**
   - the system prompt's safety section;
   - the injection/off-topic classifier;
   - the refusal policy.
6. **Cost and resilience caps:**
   - the retry and self-correction limits;
   - the model fallback chain;
   - timeouts.
7. **Secrets:** `.env`, credentials, anything that could end up in the public repo.

## Gate format

```
---
🔴 HUMAN GATE: [name]

**Decision needed**: [one sentence]
**AI recommendation**: [option] because [reason]
**Options**:
A) ... (trade-off)
B) ... (trade-off)
C) Custom
**Blocked while waiting**: [what]
---
```

## Override

The human may type `OVERRIDE: [reason]`. The override is logged in `docs/decisions.md` with a timestamp. It is an audit trail, not a shortcut.
