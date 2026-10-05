# iter-live1: router fixes for the live golden failures (ODs)

Files:
- `roles/light_path.py`: new `CAPABILITIES_TEXT`; new `static_fallback` argument on `run_light_path`.
- `graph/intents.py`: `UNAVAILABLE_DATA_TEXTS` and `unavailable_data_topic()`.
- `graph/graph.py`: an unavailable-data override in `input_guard`, and a static reply in the `light` node.
- `graph/fixed_replies.py`: the new texts are registered as `unavailable_data` (D-156).
- `prompts/router.md` and `roles/router.py`: now router-v3.
- `evals/cases/router/labelled.yaml`: cases 67–73.
- `evals/cases/golden/inventory_unavailable.yaml`: tags, estimate and fake text.
- Tests:
  - `tests/unit/test_eval_cases_golden.py`: the set-size bound is now 45–80, and the new `unavailable_data` tag must be `simple` or `complex`.
  - `tests/unit/test_iter_live1_router.py`: new file.

No hot files, no new dependencies, no config fields.

## What changed and why

- **schema_overview.** The meta static text said "emails" and did not say "users". It also listed distribution centers and web events, which are not in `ALLOWED_TABLES`.
  - The new text names exactly the four allowed tables (orders, order items, products, users) and contains no PII column words.
  - A unit test ties the text to `ALLOWED_TABLES` and to the golden's `must_contain` and `must_not_contain` lists.
  - The router prompt intro had the same wrong table list. It is fixed too.
- **inventory_unavailable.** The router labelled the question `meta`, so the turn got the capabilities text. Code now decides instead of the router.
  - `unavailable_data_topic()` is a bounded English regex on the scan fold. It detects four topics: inventory/stock, warehouse, marketing spend, and web visits.
  - When it matches, `input_guard` sends the turn to the light path. The light path answers with a code-owned text: "X is not available: ... outside the tables I can use (...)", followed by proxies such as units sold.
  - There is no analyst call, no SQL and no model call after the router. The result is the same for Gemini and for a small LM Studio model, whatever label either one gives.
  - The override applies to every allowed route, and also to an English `off_topic` refusal. `injection` and non-English turns stay refused.
  - The output guard still runs on the static text. If it blocks, the fallback is `CAPABILITIES_TEXT`, not the D-151a "SQL is not shown" text.
- **cross_session_memory.** router-v3 still has the `memory` label. "what did we discuss yesterday?" is now a prompt example and labelled case 72. `MEMORY_TEXT` already contains the golden's "between sessions" and "saved reports".
- **Router set.** Seven new cases (67–73):
  - two `meta` schema paraphrases;
  - three `simple` unavailable-data questions (tag `unavailable_data`);
  - two `memory` questions.
  The prompt now says that a question about store data that may not exist is `simple`, not `meta` or `off_topic`.

## Verification

- `uv run ruff check . && uv run pytest -q`: 3709 passed, 6 deselected.
- `OPSFLEET_GOLDEN_STRICT=1 uv run pytest -q`: 3709 passed.
- `PYTHONHASHSEED=2 uv run pytest -q -p no:randomly`: 3709 passed.
- `uv run python evals/run.py --offline --yes --cases-dir evals/cases/_fixtures`: PASS.
- The full offline run (`evals/run.py --offline --yes` with no `--cases-dir`): every case passes (golden 68/68, router 73/73). The run still reports FAIL because the judge is uncalibrated (no calibration record). That gate was already in place and is unrelated to this change.
- No live run has been done. See OD-6.

## Open decisions

- **OD-1. A regex detector, against the D-155 direction.** Resolved by D-165: router only. The regex, `UNAVAILABLE_DATA_TEXTS` and the light-path override are removed; the router labels these questions `simple` and the analyst prompt (analyst-v2) says the data is not available and offers proxies. OD-2, OD-3 and OD-5 fall away with it.
  - D-155 moved memory and comment detection from regex to router labels.
  - This change adds an English regex again, for a narrow factual topic: data that is not in the allowlist. The reasons are:
    - a small local model mislabels this case;
    - the answer must hold no matter what the router says.
  - The regex is English only. That fits FR-17, since non-English turns are refused anyway.
  - The alternative is router-only: a new `unavailable_data` label, or relying on `simple` plus the analyst prompt. That costs an analyst call and is not deterministic.
  - Owner: keep the code override, or go router-only?
- **OD-2. Mixed questions are pre-empted.**
  - A message such as "units sold vs inventory by brand" gets only the not-available text plus proxies. The units-sold part is not answered.
  - The text offers units sold as the next step, so the cost is one extra turn.
  - The narrower option is to override only when the router label is `meta`, `off_topic` or `smalltalk`, and to let `simple` and `complex` reach the analyst with the text as a hint. That depends on the analyst prompt and is less robust on small models.
  - Owner: accept the current behaviour, or narrow it?
- **OD-3. An English `off_topic` refusal is overridden.** If the router says `off_topic` but the message asks about inventory, ad spend and so on, the turn is answered with the not-available text instead of the refusal. `injection` and non-English turns are never overridden. Owner: confirm.
- **OD-4. Topic coverage and false positives.**
  - The patterns exclude "inventory item id(s)", a real `order_items` column.
  - "Stock" on its own, with no qualifier such as level, on hand, left, out of or "how much", does not match. Neither does "traffic source", which the data can answer.
  - The marketing topic matches "campaign cost", "CPC" and "ROAS". If the owner later adds a marketing-spend table, that topic must be removed.
  - Owner: is the topic list (inventory, warehouse, marketing spend, web visits) right?
- **OD-5. Golden `inventory_unavailable` changes.**
  - The case moved from `quick` and `quick_analyst: 1` to `light`, `router: 1` and `no_sql: true`.
  - The fake text now matches the code-owned reply.
  - Owner: accept the new route as the expected behaviour for AC-04.2 (FR-23)?
- **OD-6. A live rerun is needed.**
  - Offline checks cannot show what a live model does. A live golden run on Gemini and on the LM Studio profile is needed to confirm three cases: `schema_overview`, `inventory_unavailable` and `cross_session_memory`.
  - For `schema_overview`, a live run on `analyst_a` should also check that the judge scores the static text at 4 or above, because the text lists unavailable data types.
- **OD-7. The capabilities text now lists data the agent does not have.** The last line says the agent has no inventory, warehouse, marketing spend or website visit data. This sets expectations early. Owner: keep it, or shorten it?
