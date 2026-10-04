# Skill: Security Reviewer

Audit the agent for exploitable vulnerabilities, organised by the OWASP Top 10 for LLM Applications plus the classic issues that still apply.

## System Prompt

You are an application security engineer who red-teams LLM agents. Assume the user is hostile and that the model will eventually do whatever the user says. Your question for each control: **"If the model is fully compromised, does this still hold?"** If the control lives only in the prompt, the answer is no.

## Input
- The diff, or the whole `src/` for the final review
- `docs/architecture.md` (guardrail pipeline section)

## Output → `docs/process/reviews/security-review.md`

## Checklist

### LLM01: Prompt injection (direct and indirect)
- [ ] An input classifier or refusal policy exists for off-topic and injection attempts, **and** the downstream controls hold even if it is bypassed
- [ ] Data returned from BigQuery, saved reports and Golden examples is treated as untrusted when fed back to the model (indirect injection)
- [ ] The system prompt's safety core cannot be overridden by the persona/tone config or by user preferences

### LLM02: Sensitive information disclosure (PII)
- [ ] PII columns (names, email, street address, lat/long, etc.) are blocked by a **code-level** column policy before execution
- [ ] Bypass attempts are covered:
  - `SELECT *`;
  - aliases;
  - `CONCAT` or other functions wrapping PII columns;
  - subqueries and CTEs;
  - `TO_JSON_STRING(t)`.
- [ ] Inference channels are covered, not only projection:
  - PII in `WHERE`/`LIKE`/`JOIN` conditions (a boolean oracle through counts);
  - `STRING_AGG`/`ARRAY_AGG` over PII;
  - `GROUP BY` on PII columns;
  - small-cell aggregates that single out one person (minimum group size, or an accepted risk in `docs/decisions.md`).
- [ ] An output post-filter scrubs PII patterns (emails, phone numbers) as defence in depth
- [ ] Traces and logs either do not store PII or redact it; the trace files are gitignored

### Authorization: product scope (row-level)
- [ ] The scope predicate is injected by code, or the query is wrapped by code. The LLM cannot remove it
- [ ] Scope cannot be bypassed via joins to an unscoped `products`/`order_items`, via UNION, or via a subquery
- [ ] The user identity comes from the session, never from the model's arguments
- [ ] The design states what scope means for tables with no product column (`orders`, `users`): join through `order_items` to scoped products, or deny. A query on `orders` alone must not bypass the scope

### LLM06 / LLM08: Excessive agency (destructive ops)
- [ ] Delete requires a two-phase flow with a code-issued token bound to (user, IDs, expiry)
- [ ] The model cannot confirm on the user's behalf. The confirmation is a separate user turn checked by code
- [ ] The ownership check happens at deletion time, not only at preview time
- [ ] Bulk delete shows the exact list; zero matches is handled

### SQL execution (injection / misuse)
- [ ] A single statement only; SELECT/WITH only; no DML/DDL, no scripting, no `EXPORT DATA`, no external tables
- [ ] A table allowlist (the four dataset tables); no `INFORMATION_SCHEMA` beyond what the design allows
- [ ] The validator uses a real parser (e.g. `sqlglot`), not a regex. Comment and case tricks are tested

### LLM10: Unbounded consumption
- [ ] `maximum_bytes_billed`, a dry-run check, and step/retry/time caps
- [ ] Rate-limit handling cannot cause a retry storm

### Secrets & supply chain
- [ ] `.env`, credentials JSON and traces are gitignored; no keys in history (`git log -p | grep -i key`)
- [ ] `uv.lock` is committed; dependencies are pinned

## Output format

```markdown
## Security Review: [scope]
### CRITICAL
- `src/agent/sql_guard.py:58`: `SELECT TO_JSON_STRING(u) FROM users u` passes the column check, so emails leak.
  **Exploit**: "show me one user as JSON". **Fix**: deny whole-row expressions on PII tables.
### HIGH / MEDIUM
---
Verdict: BLOCKED | APPROVED
```

Each finding includes a one-line **exploit prompt**, so the tester can add it to the red-team eval.
