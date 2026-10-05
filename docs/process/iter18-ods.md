# Iteration 18 - Report list, view, substring search: decisions, deviations, risks

## Decisions
- OD-1 One matcher module, `reports/matcher.py`, serves `/reports <words>`, `/open <title>` and the future delete preview (22a). `delete_candidates(store, owner, phrase, scope)` takes a phrase `str` only (TypeError otherwise) and has no listing parameter, so a search result or listing can never be a delete target (AC-21.11).
- OD-2 Query rules (literal, no SQL LIKE or regex): the needle is canonicalised (NFKC, Cc/Cf dropped, whitespace collapsed, casefold) and must hold at least 3 letters or digits and at most 100 chars. Every character, including `* % ? _ [ ]`, is a literal, so `snake_case` and `what?` work. Empty, whitespace-only, control-only and too-short queries are refused with static messages that never echo the query.
- OD-3 Owner-only: all reads go through the owner-scoped `ReportStore`. A foreign, missing, malformed or no-hit lookup gives the same `NOT_FOUND_TEXT` (no existence oracle).
- OD-4 Scope drift: `in_scope` fails closed on a None scope. Drifted reports show as id, date and "created under a different product scope" in lists, are withheld on `/open`, and are never matched by content.
- OD-5 Untrusted text: `/open` fences the body (`fence_untrusted(KIND_REPORT, ...)`); titles and tags go through `_one_line`; the CLI applies `terminal_safe` to every output.
- OD-6 `/search` syntax is `words [tag:x] [from:YYYY-MM-DD] [to:YYYY-MM-DD]`, filters ANDed, max 5 tags, 20 results plus a total. Dates are ASCII-only, checked with `date.fromisoformat`; an invalid date, from after to, or a repeated `from:`/`to:` is refused with a clear message.
- OD-7 `CommandContext` gained `scope` (built from the profile, None on `ScopeError`, failing closed) and `listing`.
- OD-8 `/open <ref>`: a bare 1-2 digit number opens that row of the last `/reports`, `/search` or ambiguous `/open` listing of this REPL session (the list lives on `_Repl` and is cleared on a new session). Otherwise an id that belongs to the owner opens; otherwise the text goes through the matcher over the owner's in-scope reports: one hit opens, several list up to 20 and ask for an id or row number, none gives `NOT_FOUND_TEXT`. Owner and scope are re-checked at open time on every path; a listing only supplies ids and `/open` is the only reader.
- OD-9 List/delete parity: list, search and delete share one row source, `owner_rows` (the owner's newest 200 reports plus a `truncated` flag), and `owner_matches` is the single match function. Chosen over letting delete scan everything: delete never acts on a row the list could not have shown. The cost is that an owner with more than 200 reports cannot delete an older one by phrase until 22a offers another path (see risks). Truncated counts are worded "at least N", and an empty truncated result says only the newest 200 were searched.
- OD-10 Matching is on rendered-equivalent text: haystack (title, body) and needle use the same `canon`.
- OD-11 `session_candidates(reports, session_id)` returns reports CREATED in the session (the row's `session_id`). Viewed or opened reports are not included.

## Deviations
- `cli.py` touched minimally (scope and listing plumbing). `graph.py`, `store/db.py` and config files untouched.
- AC-22.3 follow-up context is NOT done (see coverage).
- `/export` remains a stub.

## AC coverage
- AC-06.3: covered (owner-only, fenced untrusted text).
- AC-21.3: covered by id, by title words and by row number ("the second one" as `/open 2`, not free text). Natural-language "open the second one" typed as a chat message is not handled; only the command is.
- AC-21.4: covered (`/reports <words>` uses the delete matcher, parity test beyond 200 reports).
- AC-21.5: covered (drift masked in lists, withheld on open, never matched by content).
- AC-21.6: matcher half covered (`session_candidates`, `test_session_delete_excludes_viewed_reports`). The delete flow itself is 22a.
- AC-21.10: covered (`/search` filters, 20 plus total).
- AC-21.11: covered at command and matcher level; search never feeds a delete (real test, no skip).
- AC-22.3: NOT covered. Opened report as follow-up context needs a `graph.py` change; deferred, see proposed diff in the report. Only the `/open` half exists.

## Residual risks
- Owners with over 200 reports: list, search and delete see only the newest 200; no FTS index (needs a `store/db.py` migration).
- `library` imports the private `_one_line` from `graph.context`; consider making it public.
- Token syntax for `/search` and the row-number rule (`/open 7` is a row, never an id) are unreviewed UX choices.
- Matching is NFKC plus casefold only; it does not fold accents.
