# D-159: customer rankings answered with spend bands only (ODs)

Owner decision 2026-10-05 (answer to OD-14 of `iter-d156-d157-ods.md`): top / best customers, or a customer ranking by spend, is answered with spend bands and customer counts plus aggregates such as each band's share of revenue. No individual customers, no customer IDs, no per-customer rows. PII variants keep the existing refusal. This replaces the D-157 answer shape (opaque customer IDs).

Files:
- `guards/sql_policy.py`: new rule `customer_grain`, its hint, `check_aggregate_only` and `_Analyzer.returns_id_grain`;
- `tools/run_sql.py`: `RunSqlTurn.aggregate_only`, checked in `_pipeline` after the base policy;
- `graph/intents.py`: `CUSTOMER_BANDS_NOTICE`, `CUSTOMER_BANDS_SECTION`, `CUSTOMER_BANDS_RULE` and `mentions_customer_id`. `CUSTOMER_ID_NOTICE` is removed;
- `graph/graph.py`:
  - `input_guard` marks a ranking turn and sets the notice;
  - `_analyst` adds the bands rule and one bounded ID retry;
  - `_force_text` rejects IDs;
  - constants `CUSTOMER_ID_ERROR_CLASS` and `CUSTOMER_ID_REJECTED`;
- tests: new `tests/unit/test_d159_customer_bands.py`, updated `tests/unit/test_d157_top_customers.py`;
- evals: `evals/cases/golden/top_customers.yaml`, and a comment in `evals/cases/router/labelled.yaml` (labels unchanged);
- docs: `docs/architecture.md` (§5.2 routing table, small-cell row 5, verification list, edge case 11b) and `docs/decisions.md` (ADR-004 item 5, ADR-013 note).

No hot files, no new dependencies, no config fields.

## Enforcement (code, not prompt)

1. **Marking the turn.** In `input_guard`, if the route is `full` and `is_customer_ranking_request` matches the scrubbed text, the turn's `RunSqlTurn.aggregate_only` is set and the notice is shown. This applies whatever the router label was. The D-157 relabel of `injection` / `off_topic` to `simple` is unchanged. PII variants never reach this point, because the PII refusal comes first.
2. **SQL.** On an `aggregate_only` turn, `run_sql` runs `check_aggregate_only` after the base policy and before any dry run.
   - It refuses `SQL_POLICY` / `customer_grain` if:
     - the root output is at id grain: GROUP BY holds an id key, or the query reads plain table rows without aggregating;
     - or any output column carries an id key. This includes `MAX(user_id)`, `ANY_VALUE(user_id)` and an id passed through a subquery.
   - `COUNT(DISTINCT user_id)` is allowed. So is a per-customer subquery or CTE that is then grouped into bands.
   - The refusal is retryable, and the turn's existing consecutive-failure limit (`gave_up`) bounds it. No BigQuery call is made.
3. **Answer.** If the analyst's answer on a ranking turn names a customer ID (`mentions_customer_id`), it is retried once with the bands rule (guard span `customer_id`, verdict `retry`).
   - If the second answer still names an ID, it is dropped (verdict `block`) and the bounded force-answer path runs.
   - If the force answer names an ID, the fixed template is used instead (verdict `block`, rule `customer_id_in_answer`).
   - Worst case is 3 model calls.

## Open decisions

- **OD-1. Scope: narrow (chosen) vs general.** ADR-013 option A and the base policy stay as they are, and id-grain blocking applies only to customer-ranking turns. A general rule ("no id grain ever") would also break legitimate order-level questions, for example "list the 10 latest orders with status". It would contradict ADR-013 and A-3 (`user_id` / `order_id` allowed as opaque keys), and it would need a new 🔴 G2 decision. **Owner to confirm narrow.**
- **OD-2. Notice wording (needs owner sign-off).** `CUSTOMER_BANDS_NOTICE` = "Note: I show customer spending as bands with customer counts, not individual customers."
  - It is shown on every detected ranking turn, including ones the router labelled `simple` correctly. Under D-157 only relabelled turns got a notice.
  - Constant naming follows the old one: `CUSTOMER_BANDS_NOTICE` / `_RULE` / `_SECTION`.
- **OD-3. Small-cell merge is a prompt rule, not code.**
  - What the code does: the small-cell rule (k = 5) applies only to quasi-identifier groups, and a band is derived from spend, not from a QI. Code therefore refuses customer-level output, but does not check that each band holds at least 5 customers.
  - What the prompt does: the analyst rule tells the model to merge a band with fewer than 5 customers into the next lower band, or else leave it out with a note.
  - Risk: low. A band count under 5 discloses no identity, because no IDs are shown and no QI is attached.
  - Alternative: inject `HAVING COUNT(*) >= 5` on the outer band aggregate. **Owner to decide whether that is needed.**
- **OD-4. Plain row lists are refused on a ranking turn.**
  - On an `aggregate_only` turn, any query that returns plain table rows (for example a product list) counts as id grain and is refused with the bands hint.
  - This is acceptable because the turn is about customers. A mixed question ("top customers and the products they buy") gets bands plus product aggregates.
- **OD-5. Follow-ups are not detected.**
  - A follow-up such as "and the top 5?" or "show me their IDs" does not match `is_customer_ranking_request`, so the turn is not marked `aggregate_only`.
  - Under the narrow scope (OD-1), that follow-up runs under the base policy and could return opaque `user_id`s, which is the pre-D-159 behaviour. PII stays blocked either way.
  - Closing this would mean carrying the flag across turns. **Owner to decide.**
- **OD-6. ID detector in the answer is a regex.**
  - `mentions_customer_id` matches:
    - "customer / user / client / buyer / shopper ID / # / no. / number" followed by a digit;
    - "user_id" / "userid";
    - a markdown header cell named "Customer ID".
  - A bare number list with no label ("1. 10234 — $5,120") is not caught.
  - The SQL rule (point 2) is the primary control: an executed query on this turn never returns IDs, so the model has none to repeat.
- **OD-7. Requirement AC-01.1 wording.** `01-requirements.md` AC-01.1 still describes a "≤10 rows, numeric spend column" answer. `golden/top_customers` now expects bands (`band`, `customers`, no `user_id`, no "customer id"). The requirement text belongs to its owner and is not edited here. **Owner to amend AC-01.1** (suggested: "answered as spend bands with customer counts and revenue share; no individual customers").

## Verification

- `uv run ruff check . && uv run pytest -q`
- `OPSFLEET_GOLDEN_STRICT=1 uv run pytest -q`
- `PYTHONHASHSEED=2 uv run pytest -q -p no:randomly`
- `uv run python evals/run.py --offline --yes --cases-dir evals/cases/_fixtures`: PASS.
- The full offline set passes `golden/top_customers`. It still reports FAIL overall, from the existing judge-calibration gate (no calibration record), which is unrelated to D-159.
