# Wiring round: D-96, D-114, D-117 (owner decisions)

Scope: the iter19 OD-10/OD-11 and iter31 "integration proposal" items (OWNER-QUEUE D-96, D-114,
D-117, D-120), applied on top of iteration 17 (report route). Not committed; owner review pending.

## What is wired

- `graph/graph.py`
  - `GraphServices.known_brands: Collection[str] = ()` (D-96). `_assemble` now passes it to
    `assemble_context`.
  - `GraphServices.golden_index: Any = None` (D-117; `None` disables Golden examples).
  - `load_context` calls `_retrieve_golden` and passes the hits as `store_items`. The
    `golden` trace field is `{trio_refs, unavailable, mode}`. The chosen refs are checkpointed in
    the new turn field `golden_refs`, which is reset every turn.
  - On resume, `_assembled` rebuilds the same examples by ref through `_golden_from_refs`, with no
    embedding call.
- `golden/runtime.py` (new; offline, unit-tested)
  - `seed_strict()` reads the `OPSFLEET_GOLDEN_STRICT` env var.
  - `build_golden_index()` applies the strict/lenient rules (D-114) and builds the lazy embedder.
  - `offline_known_brands()` returns the profile brands plus the seed brands (D-96).
- `cli.build_runtime` (still `# pragma: no cover`) builds the index first, so a strict failure
  refuses startup through the existing `ConfigError` path. It then derives `known_brands` and
  passes both into `GraphServices`. These are 3 lines plus 2 keyword args.

## Decisions

- **OD-1: when to retrieve.** Only for a turn that goes on to an analyst: no clarification is
  being asked, and either the route is not light or a clarification was just resolved. Light,
  refused and clarification turns make no embedding call. Retrieval is outside the LLM-call cap
  (HLD 6.1). It makes at most one embed call, with a single attempt and no retry (iter31 OD-8).
- **OD-2: which text is embedded.** The embedded text is `AssembledContext.message`, the
  scrubbed message with any resolved clarification merged in. Raw `state.message` is never
  embedded. This deviates from the iter31 proposal, which used `state.message`: with the merge,
  "1" after a clarification retrieves on the full question.
- **OD-3: two `assemble_context` passes.** Retrieval needs the merged message, which only the
  first pass produces. The function is pure, so the second pass (made only when there are hits)
  just adds the EXAMPLE blocks. The pass costs CPU only, no I/O.
- **OD-4: examples stay untrusted data.** `assemble_context` already fences them
  (`<<<EXAMPLE (untrusted data)`), scrubs them, caps them per kind and by characters, and drops
  any item that names a known brand outside the scope. `GoldenIndex.eligible` additionally keeps
  brand-tied trios out of a scope that does not cover them. The examples go into the system
  prompt context section only; the user message is never changed.
- **OD-5: failure handling.** If `retrieve` raises (it should never), or the resume rebuild
  fails, the turn gets no examples. The log records the exception type only, the trace says
  `unavailable: True`, and the turn still answers.
- **OD-6: report turns.** A report turn also gets examples, in its Deep analyst prompt. The writer
  and verifier prompts are built from the draft and the ledger, not from store items, so they are
  unchanged (tested).
- **OD-7: D-114 switch.**
  - **Default (production): lenient.** A bad trio is skipped and the count is logged. An
    unreadable or empty seed disables examples, and startup is never blocked.
  - **Strict:** `OPSFLEET_GOLDEN_STRICT=1|true|yes|on`. Any bad trio, or an unreadable seed,
    raises `ConfigError` and the CLI refuses to start. The message carries the trio id and reason
    code only.
  - **Tests:** the shipped seed must load strictly with 10 trios (`test_golden.py` and
    `test_golden_wiring.py`).
- **OD-8: known-brands source (D-96).** The source is the union of `profiles.yaml` (with
  overrides) and the brands named in the seed. A BigQuery `products.brand` catalogue stays
  deferred (iter19 OD-12). **Limit:** a brand in neither list is not detected by the context name
  check, and history naming it is kept. A test pins this, using an unknown brand that the filter
  does not catch.
- **OD-9: existing test retargeted.** `test_graph.py::test_context_error_goes_to_finalize`
  used to monkeypatch the old no-op seam `_retrieve_golden(state)`, which ran on every turn.
  Retrieval now runs only when an index is configured, and its errors are contained. The test
  now patches `assemble_context`, which keeps the same intent: a failure in `load_context` goes
  to finalize, with no model call and no SQL.

## Residual risks

- The default `min_score` of 0.6 is untested on the real embedding model (iter31 OD-1); the L0
  spike may need to tune it.
- Without the catalogue, most real brands are unknown to the name filter (OD-8). Profile-scoped
  users are still protected by scope snapshots and the SQL scope rewrite, which are enforced in
  code.
- Resume rebuilds examples by ref. If the seed changed between the interrupt and the resume, any
  ref that is no longer in the index is silently dropped (fail safe).
- The first live turn does one batched embedding call for all trio vectors, about 10 s at worst.
  After that they come from the cache file.

## CI change (applied by the orchestrator)

`.github/workflows/ci.yml` now sets the job-level env `OPSFLEET_GOLDEN_STRICT: "1"` (D-114: a bad
seed fails the build), and nothing else. The separate "Golden seed (strict)" step was dropped as
redundant: `test_shipped_seed_loads_strictly_and_lazily` already asserts a strict load of the
shipped seed. `test_golden_wiring.py` has an autouse fixture that clears the variable, so its
lenient-path tests behave the same with or without it.

## Review fixes

- Crash-resume: `test_crash_resume_replays_the_same_examples_without_embedding` kills the run on
  entry to `quick` (after `load_context` is checkpointed), resumes it, and asserts that the same
  examples reach the analyst prompt with zero embedding calls. It fails if `_assembled` stops
  calling `_golden_from_refs`.
- `_golden_from_refs` dedupes refs (`dict.fromkeys`, order kept) and ignores non-strings, so a
  tampered checkpoint cannot repeat one example.
- `GoldenIndex.k` is clamped to `1..MAX_STORE_ITEMS` at construction; a non-int k falls back to
  `DEFAULT_K`.
- `test_graph.py::test_context_error_goes_to_finalize` is renamed to
  `test_assemble_context_error_goes_to_finalize`. A raise inside retrieval degrades instead:
  see `test_broken_index_never_breaks_a_turn`.
