# iter-live1: report fixes for the live golden failures (ODs)

These are the failures from the live run on a small LM Studio model with profile `analyst_a`: `save_this`, `q1_report`, and a logic check of `report_save_confirm`.

Files:
- `reports/schema.py`: `parse_draft` now accepts the JSON shapes that small models produce. `title` and `summary` are still required.
- `roles/report_writer.py`:
  - a corrective retry message that lists the required keys;
  - `fallback_draft()`;
  - `FALLBACK_NOTE`;
  - `produce_report(extra_notes=...)`;
  - a new status `fallback`.
- `graph/graph.py`:
  - the writer also runs on a `partial` analysis, with `PARTIAL_REPORT_NOTE`;
  - a structure-preserving `_report_guard`;
  - the reply options are capitalised (`REPORT_PROMPT`, `DELETE_WHILE_PENDING_TEXT`);
  - the saved and "Already saved" texts show the `R-` display id.
- `reports/library.py`: `DISPLAY_PREFIX`, `display_id()` and `strip_display_prefix()`. `view_report` and `open_report` accept `R-<id>`.
- Tests:
  - `tests/unit/test_live1_reports.py` is new, with 15 tests. 11 of them fail on HEAD 7b8fa1c.
  - `tests/unit/test_reports.py`: one expectation changed (OD-2).

No hot files were touched, and there are no new dependencies or config fields. The harness, the eval cases, `store/`, and the delete flow are unchanged.

## Root causes (trace evidence)

- **save_this (trace ev-0f7138a0).** The guard's `rule_hits` was `pii_redacted`, and the turn ended with `SAVE_FAILED_TEXT`.
  - spaCy tags "Data" in the rendered line `Data window: ...` as PERSON, so the guarded body reads `<PERSON> window: ...`.
  - `missing_sections()` then reports `Data window` as missing. `_build_report` blocks the draft, and nothing is saved.
  - With the real detector this happened in 158 of 158 fuzzed drafts (scratch fuzz over titles, categories and limitations). It is not tied to one wording.
  - A second, independent risk: a small model that never returns parseable JSON also ends in `SAVE_FAILED_TEXT`, because `produce_report` returns `failed`.
- **q1_report (trace ev-bc71a4ff).** The deep analyst ran 4 successful queries, then hit `UNKNOWN_COLUMN` twice, then `GIVE_UP`, so the status was `partial`.
  - `report_writer` ran only when the status was `ok`. The turn therefore showed the force-answer text, with no headings and no Save / Revise / Cancel.
  - The prompt also spelled the options in lowercase ("Reply save ..."), while the case expects `Save`, `Revise` and `Cancel`.
- **`R-` (all three cases).** Report ids are bare uuid4 hex, so the string "R-" never appeared in the output.
- **report_save_confirm.** The turn logic is correct. A Q1 report request followed by "Save" saves the report, and no delete runs; this is covered by `test_q1_report_then_save` and `test_partial_analysis_gets_a_report_draft`. The `R-` expectation is now met by the display id.
  - The case's `outcome: answered` cannot match, because the graph returns `report_pending` or `report_saved`. This is OD-7, and per the brief the harness was not changed.

## ODs

- **OD-1: a partial analysis gets a report draft.** `report_writer` now runs when the status is `ok` or `partial`, as long as the SQL ledger is non-empty, grounding did not block, and the draft is not empty.
  - It never runs for the customer-ID or echo error classes, for a comment, or for a blocked turn.
  - The draft carries this code-owned note: "Partial analysis: some queries for this report could not be completed, so it covers only the results that were retrieved."
  - The analysis input is the force-answer text, which summarises what was retrieved. The verifier and the code checks run as usual.
  - Alternative: refuse to draft a report on a partial analysis. That is safer, but `q1_report` would then always fail whenever the analyst gives up on the last query.
- **OD-2: the deterministic fallback draft.**
  - **When it runs.** The writer must have replied at least once, and none of its up to `MAX_WRITER_CALLS`=3 replies parsed. Each retry carries the corrective message, which lists the required keys.
  - **When it does not run.** A writer that never answered keeps the old outcome (no draft, show the analysis answer), so `test_degraded::...mn3` is unchanged. This covers quota, budget, deadline and provider failure.
  - **What it contains.** It is composed in code from the analysis answer:
    - title: "Report: <question without the request words>";
    - summary: the first sentences, at most 120 words;
    - key metrics: "name: value" lines and table rows, at most 8;
    - insights: sentences that contain figures, at most 3;
    - action items: 3 generic, verb-first follow-ups tied to insight 1;
    - one definition and one limitation.
  - **Labelling.** It is labelled: "Composed automatically from the analysis answer: the report writer did not return a usable draft. Review the wording before relying on it." The verifier is skipped (it would only judge the analysis against itself), and the code precheck issues are listed in Verification.
  - **Confirmation.** A normal report turn still waits for Save / Revise / Cancel. "save this as a report" saves at once, because the request is the confirmation (AC-21.2, as before).
  - **Changed test.** `test_reports::test_writer_and_verifier_loops_are_bounded` asserted that "not json" every time means not pending. It now asserts pending, the fallback note, store count 0, and exactly 3 writer calls.
  - Alternative: no fallback, which brings back the `SAVE_FAILED_TEXT` seen live.
- **OD-3: a structure-preserving report guard instead of NER context tuning.**
  - **What it keeps.** `_report_guard` keeps the code-owned structure lines whole:
    - `## <known heading>` lines;
    - the `# `, `Scope: ` and `Data window: ` prefixes.
  - **What it guards.** It guards only their content, joined into one text so NER still sees context. If the guarded text has a different number of lines, it guards line by line.
  - **What it does not change.** PII inside headings' bodies, titles and labels is still masked (`test_real_pii_in_a_report_is_still_masked`, and the B1 tests are unchanged). The store's re-guard on save goes through the same function. A text that is exactly a known heading passes unchanged, which covers the section-name re-guard.
  - **Alternative.** Allowlist "Data window" in the PII detector. That is a narrower fix in `guards/pii.py`, but it does not protect the other headings ("Key metrics", "Action items") from the same kind of miss.
- **OD-4: the scope of tolerant parsing.**
  - **What is accepted.** `parse_draft` coerces these shapes:
    - a `{"report": {...}}` wrapper;
    - a list summary, which is joined;
    - definitions, limitations or tags given as a string or dict;
    - `key_metrics` as a dict or as "name: value" strings;
    - insights as strings, with `n` and `figures` filled in by code;
    - action items as strings, with "(not stated)" fields and `insight_ref` 1.
  - **What is still rejected.** `title` and `summary` stay required, so `{"title": "x"}` is still None. `validate_draft` still enforces its rules, such as at least 3 action items. A model that returns 2 action items gets the repair message, and then the fallback.
- **OD-5: the reply options are capitalised.** `REPORT_PROMPT` now reads "Reply Save to store this report, Revise <what to change>, or Cancel.", and `DELETE_WHILE_PENDING_TEXT` uses the same casing. Replies are still matched case-insensitively.
- **OD-6: the `R-` display id.**
  - **Where it is shown.** Saved and "Already saved" messages show `R-<32 hex>`. Storage and `/reports` lists keep the bare id; changing lists would touch many existing tests and is left to the owner.
  - **Where it is accepted.** `view_report` and `open_report` accept `R-<id>`, `r-<id>`, and the bare id. The prefix is stripped only in front of a full 32-hex id.
  - **Delete (not changed, risk area).** A delete of "R-<id>" is safely refused as `SELECTOR_EMPTY`. Suggested diff for the owner, in `delete/flow.py` `parse_delete_request`, before the selector is parsed: `raw = re.sub(r"\bR-(?=[0-9a-f]{32}\b)", "", raw)`, plus a test that "delete R-<id>" previews that id.
- **OD-7: outcome mismatch in the cases (not fixed, harness/cases).**
  - `report_save_confirm`, `q1_report` and `save_this` expect `outcome: answered`.
  - The graph returns `report_pending` or `report_saved`, and `evals/live_sut.py` passes these through.
  - Either the cases should expect `report_saved` (or `report_pending` for a draft-only case), or `live_sut` should map the `report_*` outcomes to `answered`. This is the owner's decision.
- **OD-8: catalogue categories in the PII allowlist.** The live run also masked "Swim" (a category) as a person inside a report body. That did not block the save, but it damaged the text. Suggestion: build the allowlist from the catalogue's category and department values, not only from brands. Not done here, because it is in `guards/pii.py` and outside this brief.

## Verification

- `uv run ruff check . && uv run pytest -q`: all checks passed; 3692 passed, 6 deselected.
- `OPSFLEET_GOLDEN_STRICT=1 uv run pytest -q`: 3692 passed, 6 deselected.
- `PYTHONHASHSEED=2 uv run pytest -q -p no:randomly`: 3692 passed, 6 deselected.
- `uv run python evals/run.py --offline --yes --cases-dir evals/cases/_fixtures`: RESULT: PASS.
- The new tests were run against HEAD's `src/`: 11 of 15 fail there. The 4 that pass are guard tests that must hold both before and after: the NER reproduction, PII still masked, a partial turn with no SQL gets no draft, and the display-id round trip, which uses a local stub on HEAD.
