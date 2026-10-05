# D-155: memory and comment detection moved from regex to router labels (ODs)

Files:
- `prompts/router.md` (router-v2: `memory` and `comment` labels, English, Russian, Spanish and German examples, a disambiguation section);
- `roles/router.py` (`LABELS`, `LIGHT_LABELS`, `ROUTER_PROMPT_VERSION = "router-v2"`);
- `graph/intents.py` (regex detectors removed; `COMMENT_FALLBACK_TEXT` now lives here next to `MEMORY_TEXT`);
- `graph/graph.py` (`input_guard`, `_after_guard`, `load_context`, `_is_comment`, new `_has_answer_history`);
- `roles/light_path.py` (`LABEL_TEXTS`, static replies for `memory` and `comment`);
- `guards/output.py` (`LABEL_ROUTES` for the two labels);
- `evals/cases/router/labelled.yaml` (cases 55–66);
- `evals/cases/golden/cross_session_memory.yaml` (`must_contain`);
- tests: `tests/unit/test_intents.py` (rewritten), `tests/unit/test_input_guard_router.py`, `tests/unit/test_eval_cases_golden.py`;
- `docs/architecture.md` (label lists, §4.0.3 routing table, light path, eval set, ADR-010 summary).

No hot files, no new dependencies, no config fields. The wording of `MEMORY_TEXT` and `COMMENT_FALLBACK_TEXT` is unchanged.

## What changed

The D-152 English-only regexes (`is_memory_question`, `is_comment_followup`) are gone. The router is now the only detector:
- **`memory`**: a question about the agent's own conversation memory. The light path answers with `MEMORY_TEXT`. There is no model call after the router, and history is tagged `fixed = "memory"` (D-156).
- **`comment`**: a statement or opinion about the previous answer, with no question or request.
  - With an assistant answer in the history, `_after_guard` sends it to `load_context`. `_is_comment` then sets `status = comment`, and `force_answer` gives one brief reply with the D-152 comment rules: no tools, no SQL, then grounding. If that reply fails, the turn gets `COMMENT_FALLBACK_TEXT`.
  - With no previous answer, the light path answers with static `COMMENT_FALLBACK_TEXT`, tagged `fixed = "comment_fallback"`.
- `output_guard` allows `memory` on `light_path` and `comment` on `light_path` or `force_answer`. An unknown label still fails closed.

## Open decisions

- **OD-1. Non-English memory and comment turns are still refused.**
  - FR-17 runs before the label: `decide()` refuses any message where `is_english` is false. The Russian, Spanish and German examples in the prompt help the router label correctly, but at runtime such a turn gets the English-only refusal, not `MEMORY_TEXT`.
  - A carve-out that answers `memory` and `comment` in any language with the English text is a one-line change in `decide()`. Not done, because it would weaken FR-17.
  - Owner to decide.
- **OD-2. No offline fallback.**
  - The regex is removed entirely. If the router is unavailable or returns bad output, the turn fails open to `complex`, which is bounded by the router's retry cap and the turn budget. A memory question then gets an analyst answer, not `MEMORY_TEXT`.
  - Keeping the regex as a fallback would mean two sources of truth, and the task asked for its removal.
- **OD-3. A comment with no previous answer gets the static text.**
  - Before D-155, a regex-matched statement with no previous answer went to the normal analyst path. Now the router's `comment` label is trusted, and the light path answers with `COMMENT_FALLBACK_TEXT`: no model call, no SQL.
  - With a previous answer, the D-152 brief LLM reply is kept.
- **OD-4. Golden `cross_session_memory` `must_contain` changed from "do not carry" to "between sessions".**
  - The router may now label "what did we discuss yesterday?" as `memory`, and `MEMORY_TEXT` says "I don't keep chat history between sessions".
  - The new phrase matches both the analyst wording and the memory text. Confirm on the next live golden run.
- **OD-5. Router prompt bumped to `router-v2`.**
  - The live router eval (`evals/run.py` on `evals/cases/router`) has not been run in this iteration, because live calls are out of scope for unit work. It needs a rerun to confirm accuracy on the new labels and on the negatives: "Interesting, and what about 2023?" and "Do you remember the revenue for 2023?" must be labelled `simple`.
- **OD-6. Router set bound raised to 45..70 cases.**
  - The labelled set now holds 66 cases. `test_eval_cases_golden.py` also checks that each of `memory` and `comment` has at least two non-English cases and at least one English case, and that the `comment_question` and `memory_trap` negatives are labelled `simple` or `complex`.
