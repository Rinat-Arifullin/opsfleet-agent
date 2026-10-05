# Iteration 37: Ranked full-text report search (ODs)

AC-21.13 (FTS half), HLD §6.3.2 (Ranked search). `/search` now ranks the user's reports by
SQLite FTS5 `bm25` over title, body and tags. The user's text never reaches FTS5 as syntax.

Files:
- new `src/opsfleet_agent/reports/fts.py` (stdlib leaf): the DDL, the `secure-delete` option,
  the backfill, `fts5_supported`, `has_index`, `index_report`, `reindex_title`, `build_match`,
  `ranked_sql`
- `store/db.py`: migration 5 (`report_fts` plus a backfill; empty when FTS5 is unavailable)
- `store/reports.py`: `ensure_schema` creates the index when it is missing, `save` and `rename`
  write it in the same transaction, and there is a new `ranked_search`
- `store/audit.py` 🔴: `DeletableKind.fts_dependents`, which is checked statically, checked
  live (it must be a regular FTS5 table with the named column, and its shadow tables are added
  to the trigger check), deleted inside the audited transaction, verified gone and `optimize`d
  before COMMIT
- `delete/flow.py`: `setup_delete` registers the FTS dependent whenever the index exists
- `reports/library.py`: `search_reports(..., mode="ranked")`, `ListResult.path`, `SEARCH_MODES`
- `commands/__init__.py`: `/search` uses ranked mode, its header says "best match first",
  `/help` is updated and the search path is traced
- `roles/library_agent.py`, `graph/graph.py`: the `search_reports` tool takes an optional
  `mode` (default `substring`) and traces the path; the tracer is passed in
- `obs/tracer.py`: the `search_path` tool-span field
- tests: new `tests/unit/test_fts.py`
- docs: README (/search row, the "Not built" row), `technical.md` (the stores table, §9 Search
  row, §10 deviation 7), plan ticked

No new dependencies (SQLite FTS5 comes with the stdlib `sqlite3`; the local build is 3.53.1).

## Decisions
- **D-199 A regular FTS5 table, synced by code, no triggers.** `report_fts(report_id
  UNINDEXED, title, body, tags)` uses the tokenizer `porter unicode61 remove_diacritics 2`. It
  is written in the same transaction as the report insert (`save`), the title update
  (`rename`) and the audited delete. The HLD says "maintained by triggers", but `audited_delete`
  refuses any DELETE trigger on a table it touches (exact change counting). So the sync is
  done in code, and the drift is recorded in `technical.md` §10.7. The table is neither
  external-content nor contentless: the audit check rejects those, because their delete
  semantics differ and could leave tokens.
- **D-200 No residue: `secure-delete` plus `optimize` inside the delete transaction.** The
  table is created with FTS5's `secure-delete` option, and `audited_delete` runs
  `INSERT INTO report_fts(report_fts) VALUES('optimize')` before COMMIT. Then
  `checkpoint_truncate` leaves no token in `*_data`, `*_idx`, `*_content` or `*_docsize`, nor
  in the raw DB or WAL bytes. A SQLite without FTS5 or without `secure-delete` (older than
  3.44) gets no index at all: `fts5_supported` is probed once per process.
- **D-201 FTS rows are deleted after the exact change-counter check (🔴).** FTS5 shadow-table
  writes do not count row for row, so the kind's own and plain-dependent deletes keep their
  exact `total_changes` check first. Then each FTS dependent is deleted by
  `WHERE col IN (targets)`, re-counted (any remaining row raises `DeleteMismatchError` and
  rolls back), and optimized. All of this happens before the audit re-read and COMMIT, so
  audit-first ordering is unchanged: the `delete.executed` row is still the first write.
- **D-202 `setup_delete` fails closed.** It registers `fts_dependents` whenever the index
  exists, and the live verification runs at registration and inside every delete. A broken
  index therefore disables `/delete`; it never deletes a report while leaving its tokens.
- **D-203 The query is a bounded, quoted AND.** `build_match` keeps the letter and digit runs
  of the canonical query, dedupes them, and caps them at 16 (`MAX_TERMS`, a `LibraryError`
  beyond that). It double-quotes each run. So `"`, `*`, `^`, `-`, `:`, parentheses, column
  filters and `NEAR`/`AND`/`OR`/`NOT` are literal words or separators, never operators.
  Every word must match. The error messages are static and never echo the query.
- **D-204 Owner filter in SQL, over the same row source.** The FTS table has no owner column.
  `ranked_sql` joins `saved_report`, filters by owner and restricts to the owner's newest
  `MAX_LIST` (200) reports, the same rows as the substring search (OD-9). Scope (drift), tag
  and date filters apply in code before the 20-result limit. `total` and `truncated_scan`
  count only the owner's rows. bm25's IDF is computed over the whole table, so another user's
  corpus slightly shifts scores; it never changes which rows are returned. That is accepted
  for the prototype; production gets one index per tenant or Postgres FTS.
- **D-205 Residue test in `test_fts.py`.** Plan iteration 23's `tests/unit/test_residue.py`
  does not exist yet (the hard-delete residue checks so far are in `test_audit.py` and
  `test_store_db.py`). The parametrized residue test (`_data`, `_idx`, `_content`,
  `_docsize`, raw bytes) and `test_index_rows_deleted_with_report` live in
  `tests/unit/test_fts.py`. When 23 lands, it should fold this parameter list into its own.
- **D-206 Fallback and tracing.** With no index (or an `OperationalError`), `ranked_search`
  returns None and `search_reports` runs the substring search with
  `path="substring_fallback"`. `/search` and the library tool record a `tool` span with
  `search_path`, `rows` and `truncated`, never the query text. The header says "best match
  first" only on the ranked path.
- **D-207 Library tool mode defaults to substring.** The library agent's `search_reports`
  gains an optional `mode` enum (`substring` or `ranked`); any other value is `INVALID_ARGS`.
  The default stays `substring`, so existing agent behaviour and golden cases are unchanged.
  `/search` uses `ranked`. No new tool or guardrail was added, so there is no new eval case:
  `golden/report_search.yaml` still passes offline.
- **D-208 Ranked search also matches tags.** The substring search only matches title and body
  (OD from iteration 18); the ranked path indexes tags too, with weights (title 4, tags 2,
  body 1). A tag-only hit can therefore appear in ranked results.

## Tests (`tests/unit/test_fts.py`, 30)
`test_search_ranked_fts_bm25` (a title hit outranks a tag hit, which outranks a body hit;
stemming; AND; the "best match first" header), `test_search_fts_query_syntax_quoted` (12
operator inputs, parametrized), `test_build_match_bounds`, `test_search_ranked_owner_isolation`,
`test_search_ranked_filters_and_limit`, `test_rename_reindexes_title`,
`test_index_rows_deleted_with_report`, `test_delete_registers_fts_dependent`,
`test_delete_leaves_no_fts_residue[_data|_idx|_content|_docsize|raw_bytes]`,
`test_audit_rejects_non_fts_dependent` (a plain table, external-content FTS, a missing table,
the own table, a missing column), `test_migration_backfills_index`,
`test_ensure_schema_creates_index_later`, `test_search_falls_back_to_substring` (path, trace,
no FTS dependent), `test_ranked_path_traced`, `test_library_tool_search_mode`.

A manual check showed that the residue test is meaningful: without `secure-delete` and
`optimize`, the deleted token is still in the raw DB bytes after a checkpoint.

Full suite: 3983 passed, 6 deselected. The strict golden run is green, and so is the offline
eval (`--cases-dir evals/cases/_fixtures`).

## Out of scope
- Semantic search and RRF (iteration 38).
- Ranked mode as the library agent's default, and prompt guidance on when to use it.
- A per-tenant index to remove the bm25 IDF side channel (D-204).
- Prefix or phrase queries: every term is an exact, stemmed word.
