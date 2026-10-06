# D-152: memory question and loose follow-up statements

Scope: two issues the owner found while testing the CLI on the local LM Studio provider (a 27B
model with reasoning off). Not committed; owner review pending. Budgets are unchanged (see D-149).

## Issue A: "do you see our previous messages?" got the capabilities text

**Root cause.** The trace shows the router labelled the turn `meta`, with route `light` and one
LLM call. The light path answers every `meta` turn with the static `CAPABILITIES_TEXT`, which
says nothing about conversation memory. No label or code path covered the question.

**Fix.** `graph/intents.py` (new) adds `is_memory_question`, a bounded regex on the input
guard's scan fold, limited to 300 characters. A message that asks for figures ("do you remember
the revenue...") is excluded. When it matches and the router did not refuse, `input_guard`
routes the turn `meta`/`light`, whatever label the router gave. It records a `router`/`intent`
span. The light path then returns the code-owned `MEMORY_TEXT`. That text makes no model call,
gets no scope suffix, and takes its turn count from `HISTORY_TURNS` (12).

**History coverage.**

| Path | Gets history? |
| --- | --- |
| Quick and Deep | Yes, the scope-filtered `HISTORY_TURNS`. |
| Light path | No, by design (FR-71: current message only). Kept, and listed as an open item. |
| Router | Only the previous user message. |
| Force answer | None before this change. It now gets the latest previous answer (see below). |

## Issue B: a loose follow-up statement returned UNAVAILABLE_TEXT

**Evidence from the trace (codes and counts only).** The failing turn was labelled `complex`,
with `history_turns=3`.

1. Deep made 3 LLM calls in about 105 s, roughly 50 s per local call.
2. Deep made 2 `run_sql` calls. Both were refused with `source_not_allowed`, so 0 queries
   succeeded.
3. Deep ended `partial` with `error_class=budget`.
4. The force answer had about 10 s left. Its call timed out, and the wrapper blocked the retry
   (usable time < backoff + 10 s reserve).
5. The result was `template_only`, which gave `UNAVAILABLE_TEXT` with no queries list.
   `llm_calls_total=5`.

**Root causes.**

1. A statement or opinion about the previous answer went into the full analyst loop, which spent
   the turn budget looking for data.
2. The 120 s deadline and 10 s force reserve are not scaled for about 50 s local calls (D-149,
   owner decision).
3. The force answer and its template had no previous-answer context.

**Fix.**

- **The comment intent.** The new `is_comment_followup` matches a message with no "?", no
  question or request opener, and an opinion marker (for example worth, should, I think, looks
  like, promote). In `load_context` the turn becomes a comment only when all of these hold:
  - the route is `full`;
  - the label is `simple` or `complex`;
  - it is not a clarification answer;
  - there is a previous answer in the scope-filtered history.

  The turn then goes straight to `force_answer`, which makes one call on the `comment` reason
  with no tools.
- **The comment reply.** The prompt section `Comment reply` asks for 2 to 4 sentences: agree or
  add a caveat, using only the previous answer and the user's message, and suggest one check.
  It may state no new numbers. The previous answer is passed as an `assistant` message, trimmed
  to 1500 characters, so the user's text stays the last message. Grounding and the output guard
  still run. If the call fails, the template is `COMMENT_FALLBACK_TEXT`.
- **The partial force answer.** The force answer now also sees the previous answer. When the
  force call cannot run, the template is chosen in this order:
  1. `UNAVAILABLE_TEXT` plus the "Queries run" list, when queries ran;
  2. otherwise `PARTIAL_WITH_CONTEXT_TEXT` ("...my previous answer above still stands..."), when
     there is a previous answer;
  3. otherwise `UNAVAILABLE_TEXT`.
- **Quota text.** `degraded._GENERIC_TEXTS` now includes the two new templates, so the quota
  text still replaces them when the quota is hit.

## Tests

`tests/unit/test_intents.py` has 37 offline tests that use fake LLMs and synthetic text.

- Intent checks, both positive and negative, including data questions and oversize input.
- `MEMORY_TEXT` takes its count from `HISTORY_TURNS`.
- A memory question returns `MEMORY_TEXT` on the light path with no analyst calls, for router
  labels `meta`, `smalltalk`, `simple` and `complex`. The capabilities answer is unchanged.
- A comment after an answer makes exactly one tool-less call. That call sees the previous
  answer, runs 0 SQL, and records the intent span.
- If the comment reply fails, the result is `COMMENT_FALLBACK_TEXT`.
- A statement with no previous answer, and a question that follows an answer, both take the
  normal path.
- A follow-up that hits the deadline (injected clock) gets `PARTIAL_WITH_CONTEXT_TEXT`. A first
  question keeps `UNAVAILABLE_TEXT`.

## Open items for the owner

- D-149: scale the deadline and force reserve for local models. Without that change, a real
  question on the local model can still run out of time.
- Deep stops after `source_not_allowed` refusals instead of rewriting the query (seen in the
  same session).
- A brand name was redacted as PII in one turn (the NER allowlist does not cover it).
- The intents use regexes (cheap and deterministic, but English-only and heuristic). The
  alternative is new router labels, which cost no extra call but need a prompt and golden
  change.
- Whether the light path should get history.
- Wording of the three new user-facing texts.
