# Iteration 31 owner decisions

OD-1 Embedding model unverified (L0 spike pending). gemini-embedding-001 @ 768 is taken from
     config/models.yaml; nothing in code depends on the name. The 0.6 minimum score is
     untested on the real model and may need tuning after the spike (golden.min_score).
OD-2 Degrade behaviour: default is NO examples with unavailable=True (AC-26.4). A deterministic
     lexical top-k (Jaccard on question+tags, min 0.2) exists behind degrade="lexical". Keep
     "none" or switch?
OD-3 Invalid trio: default skip with a logged warning (AC-26.1); strict=True raises at startup
     (HLD wording). Which should the CLI use? Suggest strict in CI/tests, skip in production.
OD-4 Agnostic trios get the caller's current scope snapshot at retrieval time (context.covers
     has no "agnostic" snapshot). Brand-tied trios need ALL their brands covered by the scope.
OD-5 Cache: JSON file <OPSFLEET_DATA_DIR or ./data>/golden_embeddings.json, 0600, atomic write,
     capped at 1000 entries, keyed by sha256(normalised trio, model, dim). Unreadable or corrupt
     cache is silently treated as empty.
OD-6 The injection scan uses the private guards.input._scan (same helper context.py imports),
     plus pii_regex.scrub; the heavy NER detector is not used. Consider a public alias.
OD-7 Seed lives in config/golden_seed.yaml (Q-7 deviation) with a new golden/ package; 10 trios
     (7 agnostic, 1 Calvin Klein, 2 Carhartt+Levi's). Summaries must contain no digits (enforced).
OD-8 One batched embed call per retrieval (query plus any uncached trio vectors), single attempt,
     10 s timeout, no retry. No task_type is set (query and document share one call).

OD-1 addendum: when the L0 spike picks the embedder, call it with task_type=SEMANTIC_SIMILARITY
(symmetric question-to-question matching) for both queries and trio texts.
OD-9 HLD fix needed: HLD 6.1 says trios are retrieved when the scope "intersects"; the code (and
AC-26.x intent) uses full coverage (covers(scope, snapshot_of(trio.scope))), which is stricter and safe.
Reword the HLD to "covers".
OD-10 Fix round decisions: guards.input.scan_injection is a public alias of _scan (additive). Seed SQL
is normalised via regenerate_sql (comments dropped) and the normalised form is stored; raw SQL and string
literals are injection-scanned. brand_mismatch is checked only against brands named in the seed plus
config/profiles.yaml (best effort, offline); brands not in either are not detectable.
OD-11 Number words ("three", "one hundred") still pass the no-figures rule (only isnumeric characters
are rejected). Owner may want an NL check.
OD-12 brand_case: brands=["calvin klein"] loads, but matching is case-sensitive against products.brand,
so it never matches any scope. Owner may want validation against the known brand list.
OD-13 Vector cache: load bounded to 32 MB, any Exception caught (type logged only), keys not belonging
to current trios pruned on write, dir mode 0o700, file 0o600.

Round 2 updates:
OD-12 FIXED: a declared brand that casefold-matches a known brand (profiles.yaml plus seed) but is not
identical is rejected with brand_case. If the seed itself names two casings of one brand, both are rejected.
OD-10 (extended): brand predicates must be direct = / IN over plain string literals; function-wrapped, LIKE,
<>, subquery- and CTE-fed uses are brand_mismatch; every trio is checked for foreign known brands in prose and
literals. REMAINING GAP: a brand column reached through an alias in a subquery or derived table
(SELECT brand AS bb FROM products) WHERE bb = 'X' is not seen by the column check, because only columns named
brand are inspected. Foreign known brands are still caught in literals, unknown brands are not.
OD-14 sql_escape: any backslash in the seed SQL other than \' and \\ is rejected (hex/octal/unicode/\n
escapes could hide text from the scans). _known_brands always reads the default profiles.yaml.

## Integration proposal (not wired yet)


Startup (once, in the graph builder or CLI wiring):
    loaded = load_seed(strict=<cfg>)                     # golden.seed
    embedder = GenaiEmbedder(settings.gemini_api_key, settings.embedding_model,
                             settings.embedding_dimensionality)
    index = GoldenIndex(loaded.trios, embedder, settings.embedding_model,
                        settings.embedding_dimensionality, cache_dir=default_data_dir())
Pass `index` into the graph deps (None = golden disabled, treated as unavailable).

In load_context (replacing the `_retrieve_golden(state)` seam in graph.py):
    r = index.retrieve(state.message, scope)             # never raises, <= 1 embed call
    store_items = [*saved_reports, *to_store_items(r.hits, scope)]
    trace.golden = {"trio_refs": r.trio_refs, "unavailable": r.unavailable}   # AC-26.3/26.4
    ctx = assemble_context(..., store_items=store_items, known_brands=...)

Notes:
- The retrieval call is outside the LLM-call cap (HLD 6.1); it runs only for Deep/Quick analyst
  turns, after the input guard (use the scrubbed message, never the raw one).
- assemble_context already fences items as EXAMPLE blocks, scrubs them and drops items naming
  an out-of-scope brand; to_store_items only supplies the scope snapshot.
- Record `golden.unavailable` in the trace when r.unavailable is True.
