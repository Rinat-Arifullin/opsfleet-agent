# Iteration 38: Semantic report search fused with FTS by RRF (ODs)

AC-21.13 (semantic half), AC-21.14 (search half), HLD §6.3.2. `/search` and the library
agent's `search_reports` now run a hybrid search: the FTS5 bm25 ranking from iteration 37 is
fused, by Reciprocal Rank Fusion, with an embedding cosine ranking over the same owner's
in-scope reports. A synonym ("loquat" for a "Medlar" report) therefore finds a report that has
no word in common with the query. The search degrades to bm25 when embeddings are unavailable,
and to the word match when FTS5 is unavailable too.

Files:
- new `src/opsfleet_agent/store/vector_schema.py` (stdlib leaf, which avoids an import cycle
  with `store.db`): the `report_vector` DDL and the owner index
- new `src/opsfleet_agent/reports/semantic.py`: `document_text`, `pack`/`unpack` (float32,
  stdlib `struct`, no numpy), `rrf_fuse`, `SemanticIndex` (`index`, `search`, lazy backfill,
  query LRU) and `build_semantic_index(settings)`. It reuses the golden embedding client
  (`build_embedder`, `embedding_prefixes`) and the golden helpers (`_valid_vector`, `_cosine`,
  `DEFAULT_MIN_SCORE`, `MAX_QUERY_CHARS`, `QUERY_LRU_SIZE`)
- `store/db.py`: migration 6 (DDL only)
- `store/reports.py`: `ReportStore(conn, semantic=None)`; `save` and `rename` embed after the
  commit; `semantic_search`; `ensure_schema` creates the vector table
- `delete/flow.py` 🔴: `setup_delete` registers `report_vector` as a plain dependent whenever
  the table exists
- `reports/library.py`: `mode="semantic"` (hybrid), `ListResult.semantic_unavailable`,
  `RANKED_PATHS`, the `hybrid` and `hybrid_substring` paths
- `commands/__init__.py`: `/search` uses hybrid mode, the order header covers the hybrid paths,
  `/help` reads "Search saved reports by words and meaning, best match first.", and the trace
  carries `semantic_unavailable`
- `roles/library_agent.py`: the tool's `mode` gains `semantic`, which is now the default;
  the tool traces `semantic_unavailable`
- `obs/tracer.py`: the `semantic_unavailable` tool-span field
- `cli.py`: builds the store with `build_semantic_index(settings)`
- tests: new `tests/unit/test_semantic.py`; `tests/unit/test_fts.py` (the migration version is
  now the latest, not a fixed 5; the library-tool mode test treats `fuzzy` as the bad mode, and
  the default path without an index is `ranked` with `semantic_unavailable`)
- docs: README (/search row, the "Not built" row), `technical.md` (stores table, Search row),
  plan ticked

No new dependencies.

## Decisions
- **D-209 Embedding runs after the commit, outside the transaction, and never fails a save.**
  `save` (new rows only) and `rename` call `SemanticIndex.index` after `write_tx` has
  committed, so a slow provider never holds the SQLite write lock. One embed call, one
  attempt, with the golden client's 10 s timeout. Any exception or an invalid vector (NaN,
  wrong dims) is logged by class name only, counted in `failures`, and the save still returns
  its record. The report then has no vector until the lazy backfill (D-210) picks it up.
  An unchanged content hash skips the embed call.
- **D-210 Migration 6 is DDL only; the backfill is lazy and bounded.** The migration never
  calls the network. On a hybrid search, up to `MAX_BACKFILL` (16) of the candidates with a
  missing or stale vector (the content hash covers the text, the model, the dims and the
  prefixes) are embedded in the same single call as the query. So a search makes at most one
  embed call of at most 17 texts, and none at all when the query vector is in the LRU and
  nothing is missing. A larger backlog drains over later searches.
- **D-211 `mode="semantic"` means hybrid: RRF with k=60.** score = Σ 1/(60 + rank) over the
  lexical list (bm25 or word-match order) and the semantic list (cosine ≥ 0.6, best first,
  at most 50). Ties go to the better best rank, then to the id, so the order is deterministic.
  The result is capped at `MAX_RESULTS` (20). Paths: `hybrid` (bm25 + cosine) and
  `hybrid_substring` (word match + cosine, no FTS5). They degrade to `ranked` or
  `substring_fallback` with `semantic_unavailable=True` when there is no index, no embedding
  model, or the embed call fails. `/search` and the library tool trace the path and the flag,
  never the query text. The library agent's default moves from `substring` to `semantic`. The
  `min_score` of 0.6 is inherited from the golden cache and is tunable; the RRF adds a
  semantic-only hit only when it clears that bar.
- **D-212 PII: the stored, guarded text is embedded, scrubbed again.** The document is the
  title, the Summary section (or the first 2,000 characters of the body), and the tags. It is
  whitespace-collapsed, capped at 2,000 characters and passed through `guards.pii_regex.scrub`
  before it is sent. Saved reports are already guarded and scrubbed at save time, so this is
  defence in depth. The query is scrubbed and capped at `MAX_QUERY_CHARS` before embedding.
  The vector itself is treated as report content: it is owner-scoped and deleted with the
  report (D-214).
- **D-213 Owner and scope are filtered before scoring.** The candidates are the owner's rows
  after the scope (drift), tag and date filters, capped at 200. The vector read joins
  `saved_report` and filters the owner on both tables in SQL. Another user's or an
  out-of-scope report is never scored, never embedded by the backfill, and never returned.
  The upsert is `INSERT ... SELECT ... WHERE EXISTS (the owner's report)`, so an embedding
  that finishes after a racing delete writes nothing and cannot leave an orphan vector.
- **D-214 Delete (🔴): the vector table is a plain, exactly counted dependent.**
  `report_vector` has a TEXT key, no trigger and no foreign key, so `audited_delete` deletes
  it with `WHERE report_id IN (targets)` inside the same transaction, and adds its row count
  to the exact `total_changes` check. Audit-first ordering is unchanged: `delete.executed` is
  still the first write, and a failed audit write aborts the delete. The store runs with
  `secure_delete=ON`, so after `checkpoint_truncate` the packed vector bytes are not in the
  raw DB or WAL (the test first proves the probe finds them before the delete). Like
  iteration 37's (D-205), this residue test lives in `tests/unit/test_semantic.py` until
  plan iteration 23's `test_residue.py` exists.
- No new tool or guardrail was added (a new mode value on an existing tool), so there is no
  new eval case; `golden/report_search.yaml` still passes offline.

## Tests (`tests/unit/test_semantic.py`, 17 functions, 18 cases)
`test_save_embeds_outside_the_transaction` (`conn.in_transaction` is False inside the embed
call; a repeat save embeds nothing), `test_embedding_failure_does_not_fail_save[fail|bad]`,
`test_rename_reembeds` (another user's rename embeds nothing), `test_document_text_is_scrubbed_and_capped`,
`test_search_semantic_hybrid_finds_synonyms` (also via `/search`), `test_rrf_ordering`,
`test_search_semantic_rrf_puts_both_lists_first`, `test_search_semantic_degrades_to_fts`
(failing embedder, no index, no FTS5, and `hybrid_substring`),
`test_search_semantic_owner_and_scope` (another owner, scope drift and the date filter are
neither returned nor embedded), `test_search_semantic_backfill_bounded_one_call`,
`test_search_semantic_query_scrubbed_and_not_traced`, `test_library_agent_defaults_to_hybrid`,
`test_migration_is_ddl_only`, `test_delete_leaves_no_vector_residue`,
`test_delete_of_report_without_vector`, `test_backfill_after_delete_leaves_no_orphan`,
`test_pack_roundtrip`. All offline: a local concept embedder, no provider call.

Full suite: 4001 passed, 6 deselected. The strict golden run is green, and so is the offline
eval (`--cases-dir evals/cases/_fixtures`).

Seen in passing: `tests/unit/test_library_agent.py::test_list_and_view_via_tool_calls` is
flaky (about 1 run in 26). A random report id is sometimes redacted as `<PERSON>` by the
output PII guard. It predates this iteration and the list/view path is unchanged.

## Out of scope
- A vector index or ANN service: the cosine scan is brute force over at most 200 of the
  owner's reports, which fits the prototype. Production uses pgvector or a managed index with
  the same owner filter.
- Embedding on save is synchronous in the REPL (after the commit, at most one 10 s call). A
  background queue is the production shape.
- Tuning `min_score` and the RRF weights against a labelled set of report queries.

## Follow-up: flaky report-id masking (D-215) [owner review pending]
**Root cause.** This was a product bug, not a test problem. Real output could mask a report
display id, and two layers did it:
1. spaCy `en_core_web_sm` tags some `R-<32 hex>` tokens as PERSON. In "Your report R-{id} is
   Quarterly Widgets." this happened for about 35 of 400 random ids.
2. The regex `_LONG_RUN` rule (13+ contiguous digits) masked a digit-heavy run inside the hex
   as `<ID>`.
spaCy also tags "Renamed R-<id>" as one PERSON span, so the leftover word got masked.

**D-215.** A report display id (`R-` + exactly 32 lowercase hex, the `reports.library.display_id`
form, with no word character or `-` glued on) is exempt from every PII rule:
- `pii_regex.scrub` swaps each id for an internal `\x00<letters>\x00` sentinel before the
  patterns run, then restores it. Any NUL in the input is stripped first, so the sentinel
  cannot be forged.
- `pii.PiiDetector` subtracts id spans from spaCy spans. A residual that was cut and is a
  single common English word ("Renamed") is dropped. Any other residual, such as a name next
  to an id, is still judged and masked.
- The pattern is kept in `pii_regex` (`REPORT_DISPLAY_ID`) because `guards` cannot import
  `reports.library` (that would be a cycle).
- Trade-off: a 32-hex token with the `R-` prefix can carry about 128 bits that the guard does
  not inspect. Bare hex, uppercase, other lengths and glued suffixes are still scanned. A card
  or phone number next to an id is still masked. The id is generated by the app and never
  comes from user data.
- Known gap: "R-<id> Marlowe Finch" with no cue is sometimes unmasked because spaCy tags
  nothing. "Report 7 Marlowe Finch" behaves the same, so this recall gap existed before the
  change and is not a regression.

**Tests.**
- `test_pii.py`:
  - `test_known_report_ids_survive_the_output_guard` covers the 3 ids seen failing.
  - `test_report_display_ids_never_masked_over_many_ids` checks 300 seeded ids × 5 templates.
  - `test_name_next_to_a_report_id_is_still_masked` checks that names next to an id are still masked.
- `test_pii_regex.py`:
  - `test_report_display_id_with_long_digit_run_is_kept`
  - `test_report_id_exemption_is_exact`
  - `test_report_id_sentinel_cannot_be_forged`
- `test_list_and_view_via_tool_calls` passed 60 runs in a row. Before the fix it failed about
  1 run in 26.
- Full suite: 4009 passed, 6 deselected. The strict golden run and the offline eval are green.
