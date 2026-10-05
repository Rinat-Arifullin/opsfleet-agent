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
  - `_merge_small_bands` (was `_suppress_small_bands`, D-172) and `_customer_count`;
  - `SMALL_BANDS_HINT` and, since D-172, `MERGED_BANDS_HINT`;
  - `_account` keeps a hint the pipeline already set;
- `graph/graph.py`:
  - `TurnState.aggregate_only` (checkpointed, not in `_TURN_RESET`);
  - in `input_guard`, the sticky flag is applied first, a ranking sets it, and a follow-up about customers gets the notice;
  - the flag is restored from the checkpoint in `_run` (after the owner check), in `open_resume` and in `_restore_ctx`;
- `graph/intents.py`:
  - `mentions_customers`;
  - `CUSTOMER_BANDS_RULE` now asks for a `customers` count column and says how the tool handles small bands (since D-172: merged together, or hidden when they cannot be merged);
- tests: new `tests/unit/test_d162_d163_bands.py`.

No hot files, no new dependencies, no config fields.

## D-162: sticky flag

- **Where it lives.** It is kept in two places that never disagree:
  - `RunSqlSession.aggregate_only`, in memory, per session. run_sql reads it, so the SQL policy check does not depend on the graph having set the turn flag.
  - `TurnState["aggregate_only"]`, checkpointed per session thread. It survives a process restart and is copied back into the `RunSqlSession` in `_run` (only after the other-owner refusal) and in `open_resume`.
- **When it is set.** Any full-route turn that matches `is_customer_ranking_request` sets it. This is the same trigger as D-159.
- **What a later turn gets.** Every later turn of that session behaves like a ranking turn:
  - run_sql refuses id grain (`customer_grain`) and refuses banded queries without a customer count (`band_count_required`);
  - small bands are merged or hidden (D-172);
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
2. **Result side (`_merge_small_bands`, D-172).**
   - It runs after the PII scrub and before the row cap. A row whose count column is missing, null, boolean, non-integral or non-numeric is dropped first. The column name is matched case-insensitively; when there are several count columns, every one must reach k.
   - **Merge** (the plan is `mergeable`): every band whose count is below k goes into one merged row. If that row is still below k, the smallest band at or above k joins it (ties broken by the label, then by the row text). The outcome does not depend on the result order, so re-sorting the same query (`ORDER BY band` vs `ORDER BY customers DESC`) cannot show a different split and let the user subtract one band from another. The merged row gets the fixed label `other bands` (`MERGED_LABEL`): joining the small bands' own labels would show which narrow bands (`CASE WHEN spend BETWEEN 10000 AND 10001 ...`) hold any customer. It adds the count columns and the plain `SUM` / `COUNT` / `COUNTIF` columns, and leaves every other column (shares, averages, medians, `SUM(DISTINCT)`) empty. A sum over mixed or invalid types is left empty, not guessed. The result gets `MERGED_BANDS_HINT`. The notes say that bands were merged or hidden but not how many, for the same reason.
   - A plan is mergeable only when the bands are provably disjoint and named by fixed labels: the root is one `SELECT` with `GROUP BY`, no joins, no `ROLLUP` / `CUBE` / `GROUPING SETS`, over a single source that is one row per customer, and every group key and every non-aggregate column is a `CASE` / `IF` whose results are all constants (at least one such label). Otherwise (for example `COUNT(DISTINCT user_id)` over a source that is not one row per customer, or `UNION` branches) a customer may be in two bands, so adding counts could overcount and expose a small cell; and a raw value as the label (`GROUP BY spend`, `ROUND(spend)`) would list each small band's own value in the merged label.
   - "One row per customer" means grouped by the bare customer key. A key derived from it (`user_id * 100 + month`, a `CONCAT` with the month) is a customer-month key, not a customer key, so `COUNT(*)` or `COUNT(DISTINCT key)` over it is not a customer count (a pre-existing gap closed here).
   - **Windows.** A window column reads other rows, so `LEAD(COUNT(*)) OVER (...)` or a running total could still show a merged or hidden band. Once a band is merged, every window column is emptied on every row except grand-total shares (`SUM(<SUM / COUNT / COUNTIF>) OVER ()` with no partition or order, `AggregateOnlyPlan.totals`), because merging keeps the total of an additive column. A total of anything else (`SUM(MIN(x)) OVER ()`, `SUM(IF(COUNT(*) = 1, MIN(x), 0)) OVER ()`) could single out a small band and is emptied. Once a band is hidden, every window column is emptied, shares too, since the total includes the hidden band. Columns computed from their own row only (`row_local`) stay.
   - **Subqueries and row gates.** A banded query is refused with `band_count_required` when the outer query has a subquery in its SELECT list, a `QUALIFY`, or a subquery in `WHERE` / `HAVING` / `ORDER BY` other than the population filter `<column> IN (SELECT ...)`. A SELECT-list subquery could count a small set of customers outside every band; the others decide which rows exist (`QUALIFY LAG(COUNT(*)) OVER (...) = 3`, `HAVING (SELECT COUNT(*) ... ) = 3`) before the result-side check runs. Residual: a subquery inside the per-customer CTE, or inside an allowed `IN (...)`, can still gate the whole result (all rows or none); that is pre-existing (OD-11).
   - **Hide** (not mergeable, or no merge reaches k): rows below k are dropped. The notes say how many bands were merged and how many hidden.
   - If no band is left, the result is ok with zero rows and `SMALL_BANDS_HINT` ("do not query again"). It still counts against the turn's bounded empty-result budget.
   - The model sees only the merged or remaining rows, so neither SQL nor the answer text can show a small band.
   - Residual: a merged band reveals N + small. Differencing it against a *different* query (other band edges) is the D-63 residual; the differencing guard fingerprints the query before merging. Re-running the same query in another order yields the same merge (see above). With `HAVING customers >= 5` the partner band shows on its own, so `other bands` minus the partner gives the small bands' total: one number for 1 to 4 customers in all, the same D-63 differencing class, now reachable in two queries.

**Why result side rather than injecting `HAVING COUNT(*) >= 5`:**
- The model writes the SQL. A HAVING clause would have to be placed on the right aggregate in arbitrary SQL: CTE chains, UNION branches, `GROUP BY 1`, windows. A wrong placement would silently filter nothing.
- Filtering the returned rows needs only the name of a column the policy has proved counts customers, so it cannot be bypassed by query shape.
- The SQL-side requirement exists only so that this column always exists. Without it, the result-side filter would have nothing to check.

**Merge, and when to hide (D-172):**
- Bands carry non-additive columns (share of revenue, averages, medians). The merged row therefore keeps only values that add up exactly and leaves the others empty; the hint tells the model not to compute them.
- Hiding stays as the fail-closed fallback when merging could be wrong.

## Open decisions

- **OD-1. Reset policy.** The flag never resets within a session (the task default). Possible relaxations, none implemented:
  - reset on an explicit `/new` or `/clear`;
  - reset after N turns with no customer topic;
  - reset on a scope change.

  Each would reopen the "ask for bands, then ask for IDs two turns later" path. **Owner to confirm "never".**
- **OD-2. All later turns are aggregate-only (D-159 OD-4 made sticky).** After a ranking, any query in the session that returns plain table rows or an id column is refused for the rest of the session. That includes "list the 10 latest orders" or a product list. Aggregates (revenue by category, counts) are unaffected. Narrower option: stay sticky only for customer-topic follow-ups (`mentions_customers`). That is weaker, because "and the top 5?" has no customer word. **Owner to decide whether the broad stickiness is acceptable.** **Resolved (D-170, owner 2026-10-05: "the whole session"):** the broad stickiness stays, and OD-1 (never reset within a session) is confirmed; no code change.
- **OD-3. Hide, not merge.** A band under k is dropped from the result, not merged into a neighbour (see above). The answer may therefore show fewer bands than the model planned. The bands rule tells it to say so or to widen bands. **Owner to confirm.** **Resolved (D-172, owner 2026-10-05: "merge"):** all small bands are merged into one row (then with the smallest band at or above k if still too small) when the bands are provably disjoint and fixed-name, otherwise hidden (see the result side above).
- **OD-4. Notice on follow-ups.** `CUSTOMER_BANDS_NOTICE` is shown again on a sticky follow-up only when it is a full-route turn whose text matches `mentions_customers`, that is, a customer word, "id(s)", "identifier(s)" or "who". Other turns of a sticky session show no notice, even though the mode applies. **Owner to confirm the wording and trigger** (D-161 accepted the text for ranking turns).
- **OD-5. Band detection is customer-keyed.**
  - "Banded" means grouped per customer (`user_id` / `users.id`) somewhere below the root.
  - Bands over orders ("orders by value band") are not customer bands, so they get no count requirement and no suppression. Their counts are order counts, not people.
  - The existing QI small-cell rule still covers groups by quasi-identifiers.
- **OD-6. Stricter shapes now refused in aggregate-only mode.** A banded query is refused with `band_count_required` (retryable) when:
  - the count is unaliased;
  - the per-customer subquery also groups by another key, for example customer and month. There `COUNT(*)` counts customer-months, and the model must use `COUNT(DISTINCT user_id)`. Since D-172 the same holds for a key derived from the customer key (`user_id * 100 + month`, `CONCAT(user_id, month)`): only the bare customer key makes a source one row per customer;
  - (since D-172) the outer SELECT list contains a subquery;
  - it joins the per-customer subquery to another table before counting.

  This is fail-closed by design. The worst case is one bounded retry with the hint.
- **OD-7. Window-based bands are refused as `customer_grain`.** `SUM(...) OVER (PARTITION BY user_id)` keeps the id taint, so the analyser treats it as id grain. This was already true under D-159. The hint steers the model to the subquery shape. Accepting windows would need taint changes in the base policy. **No change proposed.**
- **OD-8. Whole-population aggregates in a sticky session.** A query with an IN-filter over a per-customer subquery (for example "revenue from customers with 3+ orders") counts as banded and needs a customer count column. The analyst may need one retry. Acceptable under fail-closed.
- **OD-10. Conditional counts inside a band are not checked against k (D-172 review, pre-existing).** A banded query may return, next to `customers`, a column such as `COUNTIF(s.spend > 5000) AS whales` or `SUM(IF(..., 1, 0))`. It counts customers, but it is not a proven customer-count column, so it is not checked against k and can show a sub-band of 1 to 4 customers. Options: (a) treat any `COUNTIF` / conditional `SUM(IF(...,1,0))` over a one-row-per-customer source as a count column that must reach k; (b) refuse such columns in banded queries; (c) accept. Same family: `MAX` / `MIN` / `ANY_VALUE` of a per-customer value stay visible on bands at or above k, and the top band's `MAX(spend)` is one customer's exact spend. Option (b′): refuse `MAX` / `MIN` / `ANY_VALUE` over per-customer values in banded queries. **Owner to decide.**
- **OD-11. Subqueries that gate a whole banded result (D-172 review, pre-existing).** A scalar subquery inside the per-customer CTE (`WHERE (SELECT COUNT(*) ...) = 3`) or inside an allowed `user_id IN (...)` returns all rows or none, one bit about a small set per query. Options: (a) refuse scalar subqueries anywhere in a banded query; (b) accept as a D-63-class residual. **Owner to decide.**
- **OD-9. D-163 applies only in aggregate-only mode.** Outside a ranking or sticky session, band-shaped queries are not filtered (`test_small_bands_are_not_filtered_outside_aggregate_only_mode`). D-163 is worded for spend bands, which only exist on ranking turns. Making it global would mean classifying every query as banded or not on every turn. **Owner to confirm.**

## Verification

- `uv run ruff check . && uv run pytest -q`
- `OPSFLEET_GOLDEN_STRICT=1 uv run pytest -q`
- `PYTHONHASHSEED=2 uv run pytest -q -p no:randomly`
- `uv run python evals/run.py --offline --yes --cases-dir evals/cases/_fixtures`
