# D-162 / D-163: sticky aggregate-only mode and at least 5 customers per band (ODs)

Owner decisions 2026-10-05, answering `iter-d159-ods.md`:
- **D-162** (D-159 OD-5): `aggregate_only` stays on for follow-up turns after a bands answer. "Show their IDs", "list those customers" and "which customers are in the top band" are refused at customer grain in code.
- **D-163** (D-159 OD-3): every spend band holds at least 5 customers (k = `DEFAULT_K`), enforced in code, not only in the prompt.

Files:
- `guards/sql_policy.py`:
  - new rule `band_count_required` and its hint;
  - `AggregateOnlyPlan` and `aggregate_only_plan(sql)`, which return the decision plus the result columns that count customers per band. `check_aggregate_only` now delegates to it;
  - new taint tag `_USERKEY` for the customer keys (`users.id`, `orders.user_id`, `order_items.user_id`). Like `_IDKEY`, it is stripped by non-ID-preserving aggregates;
  - `_Analyzer.band_count_columns` and its helpers. All recursion is bounded by `MAX_DEPTH`;
- `tools/run_sql.py`:
  - `RunSqlSession.aggregate_only` (sticky);
  - in `_pipeline`, the aggregate-only check runs when the turn **or** the session flag is set;
  - `_suppress_small_bands` and `_customer_count`;
  - `SMALL_BANDS_HINT`;
  - `_account` keeps a hint the pipeline already set;
- `graph/graph.py`:
  - `TurnState.aggregate_only` (checkpointed, not in `_TURN_RESET`);
  - in `input_guard`, the sticky flag is applied first, a ranking sets it, and a follow-up about customers gets the notice;
  - the flag is restored from the checkpoint in `_run` (after the owner check), in `open_resume` and in `_restore_ctx`;
- `graph/intents.py`:
  - `mentions_customers`;
  - `CUSTOMER_BANDS_RULE` now asks for a `customers` count column and says that small bands are hidden by the tool (the "merge" wording is gone);
- tests: new `tests/unit/test_d162_d163_bands.py`.

No hot files, no new dependencies, no config fields.

## D-162: sticky flag

- **Where it lives.** It is kept in two places that never disagree:
  - `RunSqlSession.aggregate_only`, in memory, per session. run_sql reads it, so the SQL policy check does not depend on the graph having set the turn flag.
  - `TurnState["aggregate_only"]`, checkpointed per session thread. It survives a process restart and is copied back into the `RunSqlSession` in `_run` (only after the other-owner refusal) and in `open_resume`.
- **When it is set.** Any full-route turn that matches `is_customer_ranking_request` sets it. This is the same trigger as D-159.
- **What a later turn gets.** Every later turn of that session behaves like a ranking turn:
  - run_sql refuses id grain (`customer_grain`) and refuses banded queries without a customer count (`band_count_required`);
  - small bands are hidden;
  - the analyst gets the bands rule and the bounded customer-ID answer retry;
  - the force-answer path rejects IDs.
- **Reset.** The flag is **never reset within a session**. A new session (new session id, for example a new CLI run without `--resume`) starts clear. Nothing in code clears it (the CLI has no reset command); report and delete flows leave it set.

## D-163: k >= 5 per band

There are two layers. The result side is the guarantee; the SQL side makes it checkable.

1. **SQL policy (`aggregate_only_plan`).**
   - A query in aggregate-only mode is *banded* when:
     - a non-root scope groups by a customer key (or selects `DISTINCT` customer keys), which is the per-customer subquery or CTE;
     - or a window partitions by one.
   - A banded query must return, as a named column, a count that provably counts distinct customers:
     - `COUNT(DISTINCT <customer key>) AS x`;
     - or `COUNT(*)` / `COUNT(col)` `AS x` over a single source that is one row per customer: a scope grouped only by customer keys, or a plain passthrough CTE of one.
   - Otherwise the query is refused with `band_count_required`. The refusal is retryable, the hint names the shape, and no BigQuery call is made.
   - Queries that are not banded (`SIMPLE`, `COUNT(DISTINCT user_id)` over everything) need no count column.
2. **Result side (`_suppress_small_bands`).**
   - After the PII scrub and before the row cap, every row whose count column is below k is dropped. This also applies when the column is missing, null, boolean, non-integral or non-numeric. The column name is matched case-insensitively; when there are several count columns, every one must reach k.
   - `suppressed_groups` says how many bands were hidden.
   - If every band was hidden, the result is ok with zero rows and `SMALL_BANDS_HINT` ("do not query again"). It still counts against the turn's bounded empty-result budget.
   - The model sees only the remaining rows, so neither SQL nor the answer text can show a small band.

**Why result side rather than injecting `HAVING COUNT(*) >= 5`:**
- The model writes the SQL. A HAVING clause would have to be placed on the right aggregate in arbitrary SQL: CTE chains, UNION branches, `GROUP BY 1`, windows. A wrong placement would silently filter nothing.
- Filtering the returned rows needs only the name of a column the policy has proved counts customers, so it cannot be bypassed by query shape.
- The SQL-side requirement exists only so that this column always exists. Without it, the result-side filter would have nothing to check.

**Why hide rather than merge:**
- Bands carry non-additive columns (share of revenue, averages, medians). Merging two rows in code would print wrong numbers.
- The prompt now tells the model that small bands are hidden and that it may use wider bands.

## Open decisions

- **OD-1. Reset policy.** The flag never resets within a session (the task default). Possible relaxations, none implemented:
  - reset on an explicit `/new` or `/clear`;
  - reset after N turns with no customer topic;
  - reset on a scope change.

  Each would reopen the "ask for bands, then ask for IDs two turns later" path. **Owner to confirm "never".**
- **OD-2. All later turns are aggregate-only (D-159 OD-4 made sticky).** After a ranking, any query in the session that returns plain table rows or an id column is refused for the rest of the session. That includes "list the 10 latest orders" or a product list. Aggregates (revenue by category, counts) are unaffected. Narrower option: stay sticky only for customer-topic follow-ups (`mentions_customers`). That is weaker, because "and the top 5?" has no customer word. **Owner to decide whether the broad stickiness is acceptable.**
- **OD-3. Hide, not merge.** A band under k is dropped from the result, not merged into a neighbour (see above). The answer may therefore show fewer bands than the model planned. The bands rule tells it to say so or to widen bands. **Owner to confirm.**
- **OD-4. Notice on follow-ups.** `CUSTOMER_BANDS_NOTICE` is shown again on a sticky follow-up only when it is a full-route turn whose text matches `mentions_customers`, that is, a customer word, "id(s)", "identifier(s)" or "who". Other turns of a sticky session show no notice, even though the mode applies. **Owner to confirm the wording and trigger** (D-161 accepted the text for ranking turns).
- **OD-5. Band detection is customer-keyed.**
  - "Banded" means grouped per customer (`user_id` / `users.id`) somewhere below the root.
  - Bands over orders ("orders by value band") are not customer bands, so they get no count requirement and no suppression. Their counts are order counts, not people.
  - The existing QI small-cell rule still covers groups by quasi-identifiers.
- **OD-6. Stricter shapes now refused in aggregate-only mode.** A banded query is refused with `band_count_required` (retryable) when:
  - the count is unaliased;
  - the per-customer subquery also groups by another key, for example customer and month. There `COUNT(*)` counts customer-months, and the model must use `COUNT(DISTINCT user_id)`;
  - it joins the per-customer subquery to another table before counting.

  This is fail-closed by design. The worst case is one bounded retry with the hint.
- **OD-7. Window-based bands are refused as `customer_grain`.** `SUM(...) OVER (PARTITION BY user_id)` keeps the id taint, so the analyser treats it as id grain. This was already true under D-159. The hint steers the model to the subquery shape. Accepting windows would need taint changes in the base policy. **No change proposed.**
- **OD-8. Whole-population aggregates in a sticky session.** A query with an IN-filter over a per-customer subquery (for example "revenue from customers with 3+ orders") counts as banded and needs a customer count column. The analyst may need one retry. Acceptable under fail-closed.
- **OD-9. D-163 applies only in aggregate-only mode.** Outside a ranking or sticky session, band-shaped queries are not filtered (`test_small_bands_are_not_filtered_outside_aggregate_only_mode`). D-163 is worded for spend bands, which only exist on ranking turns. Making it global would mean classifying every query as banded or not on every turn. **Owner to confirm.**

## Verification

- `uv run ruff check . && uv run pytest -q`
- `OPSFLEET_GOLDEN_STRICT=1 uv run pytest -q`
- `PYTHONHASHSEED=2 uv run pytest -q -p no:randomly`
- `uv run python evals/run.py --offline --yes --cases-dir evals/cases/_fixtures`
