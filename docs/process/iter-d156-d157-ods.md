# D-156 / D-157: echoed fixed replies and mislabelled customer rankings (ODs)

Files:
- new `graph/fixed_replies.py` and `guards/echo.py`;
- `graph/context.py` (`_history_body`);
- `graph/graph.py` (`input_guard` override, `_analyst` echo retry, `_force_text`, `_previous_answer`, `_finalize` flag);
- `graph/intents.py` (customer-ranking detection);
- `roles/analyst.py` (`extra_rules`);
- `evals/cases/router/labelled.yaml` (cases 51–54);
- new tests `tests/unit/test_d156_echo.py` and `tests/unit/test_d157_top_customers.py`.

No hot files, no new dependencies, no config fields.

## Root cause (D-156)

The light path's code-owned replies (the capabilities text, the memory text, the "SQL not shown" text) were written to `history` as ordinary assistant turns. The path was:
1. `_finalize` stored the reply unmarked.
2. `context._history_window` rendered it verbatim.
3. `_analyst` sent `*a.history` to the model.
4. `_previous_answer` passed the last reply on to `_force_text`.

A later analyst turn therefore saw the full capabilities text as its own earlier answer and sometimes repeated it.

## Decisions

### D-156: markers in history
- **OD-1. Marker wording.** In prompts, a fixed reply becomes `[assistant described its capabilities]`, or `[assistant gave a fixed reply: <kind>]` for any other fixed reply. These markers are prompt-only and never shown to the user. The kind is sanitised to `\w`, at most 40 chars.
- **OD-2. Write-time flag plus a legacy fallback.** `_finalize` writes `reply["fixed"] = <kind>` when the light path answered from a static text or a template. Checkpoints written before this change have no flag, so `fixed_kind()` also matches the text: an exact match, or a prefix match when the static text is at least 40 chars, after normalising whitespace and case. When a flag is present, it wins.

### D-156: echo check
- **OD-3. Thresholds.**
  - Text is whitespace-normalised and casefolded before comparison.
  - An echo of a static text is a difflib ratio >= 0.9, or containment of a static text of at least 120 chars.
  - An echo of an earlier answer is a ratio >= 0.9 and the same multiset of numbers. A same-template answer with new figures is a real answer.
  - Answers under 80 chars are not compared with earlier answers: short one-liners such as "There were 3 complete orders." can rightly repeat.
  - Bounds: at most 24 earlier answers, each compared over at most 4,000 chars.
- **OD-4. Same-question exclusion.** An earlier answer to the same question (normalised) is not compared. When the user asks again, repeating the answer is correct.
- **OD-5. A quoted marker is an echo.** An answer that contains a history marker is always treated as an echo.
- **OD-6. The retry.** On an echo, the analyst runs once more with the extra rule "Answer the current question from the query results; do not repeat earlier replies."
  - The retry is the full analyst run, within the same turn budget, so the LLM-call caps of §4.1 still bound it.
  - If the retry also echoes, the result is `partial` with `error_class=echo`, which goes to `force_answer`.
  - Both checks are traced as `guard/echo` with `ECHO_REJECTED`; the verdict is `retry`, then `block`.
- **OD-7. `force_answer` after an echo** is not given the previous answer. If the force text itself echoes, the existing code template is used. The echoed text is never shown.
- **OD-8. Out of scope.** The report writer is not checked separately: it gets no history, and its input is the checked analyst draft. The clarify and delete-confirmation texts are not flagged: they carry turn-specific content that the next turn needs.
- **OD-9. Not done: `/save last`.** Rejecting a fixed reply in `/save last` was optional and is left out (low value).

### D-157: customer-ranking override
- **OD-10. Override placement.** The override is deterministic code in `input_guard`, and applies only when the router says `injection` or `off_topic`. The input must also:
  - match a customer-ranking pattern: top/best/biggest/most valuable N customers, buyers, clients, shoppers or spenders; "list (of) customers"; "who are our ... customers"; "which customers spent/bought/ordered";
  - contain none of the injection hint words (ignore, bypass, pretend, system prompt, rules, policy, label, reveal, print, select, ...).

  A correct router label is never changed. The router prompt is unchanged.
- **OD-11. Relabel to `simple` / `full`.** The existing Quick-analyst path is used; no new label is added. Customers are shown by opaque user ID (A-3, ADR-013, AC-01.1), and the SQL policy still blocks PII columns in code. The answer carries the new notice `CUSTOMER_ID_NOTICE` through the existing `ctx.notice` channel.
- **OD-12. PII variants.** A ranking that asks for names, emails, phones, addresses or contact details gets the existing `REFUSALS[PII_REQUEST]`, which already offers the ID-based ranking. There is no new refusal text.
- **OD-13. No ranking, no override.** A request for customer emails without a ranking, such as golden case 46 ("customer emails of buyers of X"), still follows the input guard and the router, and keeps its injection label.
- **OD-14. Open decision for the owner.** Should a top-customers question be answered per customer ID (current behaviour, per ADR-013), or only as segment aggregates (spend bands, counts)? Switching to aggregates would mean changing the override target to an aggregate-only hint; the detection code would stay.
- **OD-15. Eval coverage.**
  - Router golden cases 51–53 expect `simple`, the end-to-end label after the override. Case 54 expects `injection`.
  - The live router suite calls `route()` directly, so it measures the model label, not the graph override. A live miss on 51–53 means the router still mislabels, and the override covers that miss in the product.
  - The unit tests in `test_d157_top_customers.py` exercise the override through the graph.
