# Workflow: Bug Hunt

Use this for a failing test or eval, or for wrong agent behaviour found during manual testing.

## Trigger
- A red test or eval during Step 5 after 2 failed fix attempts
- A finding from a reviewer in Step 6
- Wrong behaviour seen in the CLI demo

## Steps

**1. Triage**: `debugger` (T1), using [`../skills/debugger.md`](../skills/debugger.md)
- Pull the trace, classify the bug (code / guardrail / prompt / retrieval / external / data semantics), and estimate the impact
- If it touches a risk area (`config/human-gates.md`), surface it to the human now (🟡)

**2. Hypotheses**: 2–4 hypotheses, ranked, each with its cheapest check. 🟡 Show the list to the human before a long investigation.

**3. Investigate**: cheapest check first: trace replay → fake-LLM unit test → a single live eval ×3.

**4. Fix**: `implementer` (T2)
- Write the regression test or eval case first, and confirm it fails
- Make the minimal fix in the right layer (code over prompt where possible)
- `uv run ruff check . && uv run pytest -q` must be green, plus the affected eval suite

**5. Verify**: `code-reviewer` (T2) does a quick pass on the fix diff. Does it fix the root cause? Does the regression test actually guard it?

**6. Log**: add a one-paragraph entry to `docs/decisions.md` if the fix changed a policy, prompt or cap.
