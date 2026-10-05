# D-149, D-151a, D-153, D-154: owner decisions (iteration notes and open decisions)

Scope: OWNER-QUEUE D-149, D-151a, D-153 and D-154. Not committed; owner review pending.
All test data is synthetic: the person names in tests ("Marlowe Finch", "Thistlewood Bramble")
are invented, and "Calvin Klein" appears only as a catalogue brand.

## D-149: no time limits for the lmstudio provider

**Decision.** A local model (lmstudio) is slow but free, so a turn on it is not cut by a
deadline.

**Changes.**
- `graph/budget.py`: `TurnBudget(time_bounded=False)` turns off the turn deadline and the
  per-call timeouts derived from it. The call, query and token caps are unchanged, so every loop
  stays bounded by count.
- `graph/providers.py`, `graph/llm.py`: the local chat client is built with
  `LOCAL_CHAT_TIMEOUT_S = None`.
- `graph/graph.py`, `cli.py`, `roles/analyst.py`, `roles/router.py`: these build the budget as
  `time_bounded=False` when the provider is local, and pass `None` timeouts through.
- Tests: `tests/unit/test_d149_local_no_deadline.py` (13).

## D-151a: never show SQL, even when asked

**Decision.** This removes the AC-02.2 exception from D-151. The agent says it doesn't show
queries and describes the data used in business terms.

**Changes.**
- `graph/intents.py`: `is_sql_request`, a bounded English intent check for "show me the SQL".
- `guards/plain_language.py`:
  - `strip_sql` removes SQL from any text (fenced, inline or bare `SELECT/WITH ... FROM`); it is
    bounded and idempotent;
  - `describe_data_used` describes the data from ledger SQL in business words;
  - `sql_request_reply` builds the reply to a SQL request.
- `graph/graph.py`: a SQL request goes to the light path with a code-built reply, with no
  analyst and no query. `_finalize` strips SQL on every route (guard code `sql_stripped`).
- `roles/light_path.py`, `commands/__init__.py`: the same rule applies on those paths.
- Reports: `reports/schema.py`, `roles/report_writer.py` and `reports/library.py` render
  "## Data used" instead of "## SQL used". Viewing an older report strips its SQL.
- Evals `golden/show_sql.yaml` and `golden/save_this.yaml`, plus requirements AC-02.2, AC-21.1
  and FR-22, and HLD §6.1 and risk 15, are updated. See also
  [iter-d151-ods.md](iter-d151-ods.md).
- Tests: `tests/unit/test_d151a_no_sql.py` (41), plus updates in `test_plain_language.py`.

## D-153: scope brands are not PII

**Problem.** A Langfuse trace showed "Calvin Klein" masked as a person. spaCy tags the brand as
PERSON, and the brand was not on the detector's allowlist: an all-products profile has no
brands, and the catalogue-wide allowlist is still deferred (iter19 OD-12).

**Changes.**
- `guards/pii.py`:
  - `extend_allowlist(base, brands)` adds brands with the same folding and limits as
    `build_allowlist`.
  - `PiiDetector.with_brands(brands)` returns a detector whose allowlist also has these brands.
    - It shares the loaded spaCy analyzer and its lock, so there is no second model load.
    - It returns `self` when nothing is new.
    - Derived detectors are cached per base and bounded (`MAX_DERIVED_DETECTORS = 32`).
    - It raises `TypeError` when given a bare string.
  - Matching is unchanged: exact, case-insensitive and whole-phrase, and terms never combine.
    So "Marlowe Klein" or "Calvin Bramblewood" is still masked.
- `graph/graph.py` `_turn_detector(ctx)`: each turn uses the base detector plus the scope
  brands. An all-products scope uses `GraphServices.known_brands` (profiles plus the golden
  seed). This applies to the input guard, the light path, the report guard and the output guard.
- `cli.py` `build_runtime`:
  - the default detector, the Langfuse trace cleaner (`build_sink(detector=...)`) and the graph
    services all use `default_detector().with_brands(profile brands + known brands)`;
  - `set_default_detector` makes the callers that fall back to `default_detector()`
    (`scrub_output`, and the input and output guards when they get no detector) use the same
    one.
- Docs: HLD §5.4 has a new "Scope brands" bullet.
- Tests: `tests/unit/test_d153_scope_brands.py` (19). They cover:
  - the precondition that the base detector masks the brand;
  - the brand kept in prose, a table, lowercase and the possessive;
  - synthetic persons still masked;
  - a person sharing a brand word still masked;
  - caching, bounds and type checks;
  - graph answers for a scoped profile and for an all-products profile;
  - the Langfuse `_Cleaner`.

## D-154: the deep analyst rewrites after `source_not_allowed`

**Problem.** After a refused table (for example `events` or `inventory_items`), the analyst got
"The query was refused by the data policy." and a one-line table hint, and often gave up.

**Changes.**
- `guards/sql_policy.py`:
  - `allowed_sources_text()` lists every allowed table with its columns, minus the PII columns,
    sorted and with no SQL;
  - `SOURCE_NOT_ALLOWED_HINT` tells the model to rewrite with only those tables and columns, and
    is the hint for `Rule.SOURCE_NOT_ALLOWED`.
- `tools/run_sql.py`: a `source_not_allowed` refusal now carries `SOURCE_NOT_ALLOWED_MESSAGE`
  ("...Rewrite it with the allowed tables and columns in the hint and run it again."). It was
  already `retryable: true`.
- `roles/analyst.py` loop: unchanged. A refused call is a failed SQL attempt, and the loop goes
  on to the next model call within its existing bounds: the role-call budget, the SQL budget,
  `GIVE_UP` after repeated failures, and the recursion guard.
- Docs: HLD §5.3 step 4 has a new note.
- Tests: `tests/unit/test_d154_source_rewrite.py` (7). They cover:
  - the hint lists every table and column and no PII column;
  - the policy hint and the run_sql envelope (message, hint, retryable, no BigQuery call);
  - a fake-LLM scenario for both the deep and the quick role: refused, then a rewrite, then
    "answered";
  - a model that never rewrites stays bounded.

## Open decisions (owner)

| # | Decision | Question | Default taken |
|---|---|---|---|
| OD-1 | D-149 | Should the embedding call (semantic memory) also have no timeout on a local provider? | Kept at 10 s: embeddings are short, and a hang would block every turn |
| OD-2 | D-151a | Reports used to have "SQL used" and now have "Data used". Should old saved reports be rewritten on disk? | No. They are stripped at view time; the stored `sql_used` stays for audit and follow-ups |
| OD-3 | D-151a | `/trace` still shows the sanitized, scoped SQL. Is that acceptable as a developer command? | Yes. It is a developer command and the scope is enforced, not secret |
| OD-4 | D-151a | The SQL-request intent regexes are English-only | Accepted for the prototype. Another language goes to the analyst, and `strip_sql` still removes any SQL from the answer |
| OD-5 | D-151a | The reply describes only the last ≤ 6 in-scope queries | Accepted (bounded). Older queries are not described |
| OD-6 | D-151a | A data question that mentions SQL ("write a SQL query for revenue by brand") goes to the analyst as a data question | Yes. The answer is given in prose and any SQL is stripped |
| OD-7 | D-151a | Prose SQL in any case is stripped only when there is a code signal (a backtick, the dataset name, an aggregate call, `GROUP/ORDER BY`, a comparison, or a dotted snake_case column) | Yes. This avoids stripping English like "select a brand from the list" |
| OD-8 | D-151a | The `show_sql` golden eval no longer checks for " FROM " | It checks the refusal wording and that no SQL keyword or table name appears |
| OD-9 | D-153 | The catalogue-wide brand allowlist (all distinct `products.brand` values) is still deferred (iter19 OD-12). An all-products scope uses only the offline known brands (profiles plus the golden seed) | Deferred: loading it needs a warehouse query at startup |
| OD-10 | D-153 | Brands that appear only in a query result (`brand` column values) are not added to the allowlist | Possible follow-up: allowlist the brand values of the turn's own result rows |
| OD-11 | D-153 | A span where a person name runs into a brand ("Marlowe Finch Calvin Klein bought...") is masked whole | Accepted: over-redaction is fail-safe |
| OD-12 | D-153 | Existing spaCy miss, not caused by this change: "Customer Thistlewood Bramble bought Calvin Klein." is not masked by either detector | Logged. Candidate for the `adversarial/pii_typed` set and a cue rule for "Customer <Name>" |
| OD-13 | D-153 | `guards/pii_regex.py` (the regex layer) has no name detection, so it needs no allowlist | No change |
| OD-14 | D-154 | The hint lists QI columns of `users` (age, gender, city, ...), which the QI rules still restrict | Listed, because the QI rules give their own hints when they refuse. PII columns are never listed |
| OD-15 | D-154 | The quick analyst gets the same message. A refusal counts towards the quick-to-deep escalation threshold | Unchanged. Escalation stays bounded by `try_escalate` |
