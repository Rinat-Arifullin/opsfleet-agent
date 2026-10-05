# Live eval iteration 1: answer-quality fixes (ODs)

Files:
- new `graph/assumptions.py`;
- `graph/graph.py` (`_assumptions`, `_finalize`, `_ledger` optional `model_sql`);
- `graph/context.py` (`shown_sql`, ledger keys);
- `roles/analyst.py` (`_fenced_queries`);
- `tools/run_sql.py` (`model_sql` on the ledger entry);
- `guards/sql_policy.py` (`SCOPE_PATTERN_HINT`, per-rejection hint);
- `guards/input.py` and `roles/light_path.py` (wording);
- `evals/run.py` (any-of `must_contain`);
- `evals/cases/golden/followup_breakdown.yaml`;
- tests: new `tests/unit/test_live1_answer_quality.py`, plus updates to `tests/unit/test_graph.py` and `tests/unit/test_eval_runner.py`.

No hot files, no new dependencies, no config fields.

## Root causes (from the live traces)

- **aov_by_traffic_source and churn_last_month.**
  - Both turns ended with deep_analyst `partial` (error_class `budget`), and the force answer then wrote the reply.
  - The prompt asks the model to state the scope and the definitions, but nothing in code made sure it did. So the answers had no "Calvin Klein", no "no order in" and no "restate".
- **followup_why_march.**
  - The scope rewrite is the scoped `trace_sql`, with `__p`/`__oi` CTEs and `brand IN UNNEST(@scope_brands)`. Turn 1 stored that rewrite in the ledger `sql`.
  - `PRIOR_QUERIES` showed it to the model on turn 2. The model copied the pattern, and the policy refused both attempts with `source_not_allowed`.
  - The refusal is correct, because parameters, UNNEST and `__` CTEs are not allowed in model SQL. But the generic hint did not tell the model what to change, so it gave up and the force answer replied.
- **followup_breakdown.** The scorer is a plain substring match. "12-month revenue" and "Oct 2025 - Sep 2026" are correct answers but did not match "12 months".
- **top_customers.** `must_not_contain: email` can be tripped by code-owned texts: the capabilities text and the PII refusal both said "emails".

## ODs

- **OD-1. The footer is code, appended in `_finalize`, only for what is missing.**
  - `assumptions_footer` is pure and bounded: case-folded substring checks over at most 20,000 characters. It adds lines under `Assumptions:` only for what the answer does not already state:
    - the scope line, when any profile brand (or "all products") is missing;
    - the revenue definition, when the question or answer is about revenue, sales, AOV, spend or GMV and the answer does not mention "cancel";
    - the AOV definition, when AOV is mentioned and the answer says neither "divided by" nor "per order";
    - the churn definition with the restate offer, unless the answer has both "no order in" and "restate". When the session has restated churn, a short acknowledgement replaces the definition.
  - It runs after the output guard and humanizer, on allowed answers only. The footer is constant text plus the profile's own brand names, with no digits, SQL or PII, so it needs no second guard pass and does not affect grounding.
  - Guard code: `assumptions_added`.
- **OD-2. Where the footer is not added.**
  - Not on comment replies.
  - Not on code-owned templates (`fixed_kind`).
  - Not on turns with no data. A turn has data when a query ran this turn or there is a prior ledger.
  - It stays in history, so the next turn's model sees the definitions that were stated. It is not registered as a static text.
  - It is not a separate notice, because the scorer and the user read only the answer text, and a notice could duplicate it.
- **OD-3. Prior queries show the model's own SQL.**
  - `run_sql` records `model_sql` on each ledger entry. `PRIOR_QUERIES` and the analyst's fenced queries use `shown_sql(entry)`, which falls back to `sql`.
  - `sql` still holds the scoped statement for grounding, the verifier, reports and the light path, so the audit trail is unchanged.
  - `model_sql` is an optional snapshot key, so checkpoints written before this change still resume. A non-string value is refused.
- **OD-4. The policy is unchanged; only the hint is.**
  - Parameters, placeholders, UNNEST, `__`-named tables and `__`-named CTEs are still refused, with the same rules (`source_not_allowed`, `cte_shadows_table`).
  - The hint for these cases is now `SCOPE_PATTERN_HINT`. It says the scope is applied automatically and shows the plain `order_items`/`products` join.
  - Other source refusals, such as LATERAL and TABLESAMPLE, keep their generic hint. The hint is still fixed text with no SQL echo.
  - The turn already ends with an answer through force_answer. OD-3 removes the cause, and OD-1 makes sure the scope is stated.
- **OD-5. `must_contain` items may be a list, meaning any-of, case-insensitive.**
  - A string item works as before. A list item passes if any one of its phrases is present.
  - An empty or non-string list is refused when the case loads (`CaseError`, exit 2), not partway through a run.
  - `followup_breakdown` now uses `[12 months, 12-month, twelve months]`.
- **OD-6. "Personal details" replaces "emails" in code-owned texts.**
  - The capabilities text now reads "customers' personal details such as names, contact details or addresses". The PII refusal uses the same wording.
  - The refusal's offer of anonymised customer IDs (D-157 OD-12) is unchanged.
  - The humanizer's column map, where `email` maps to "email", is not user prose and is unchanged.

## Not changed / follow-ups

- The deep-analyst budget exhaustion behind the aov and churn force answers is not addressed here. The footer makes the answer correct about scope and definitions, but it does not make the analysis complete.
- An offline `--suite golden` run still reports `RESULT: FAIL` because of the existing "judge uncalibrated (no calibration record)" gate. All 68 golden cases pass, including followup_breakdown for all three profiles.
