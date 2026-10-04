# Owner queue: decisions and reviews waiting for you

Started 2026-10-04 evening, when the owner handed Step 5 to the orchestrator for autonomous overnight work. Newest items at the bottom of each section. Every item says what the orchestrator did in the meantime (the 🟡 default) so nothing is blocked silently.

## How the night run works (🟡 defaults taken by the orchestrator)

- **Branch:** all work is on the local branch `step5-implementation`; `main` has no commits yet, nothing is pushed. You merge after review.
- **🔴 iteration reviews (T-4):** the plan wants your review *before* each 🔴 commit. Waiting would stop the critical path for the whole night, so each 🔴 iteration is implemented by T1, gets the second T1 review (security-reviewer, a separate agent), and is committed on the branch with `[owner review pending]` in the message. It is listed below under "🔴 iterations awaiting your review". Nothing is merged into `main` without you. If you reject one, the later iterations that depend on it are reworked.
- **No live calls:** no Gemini or BigQuery calls are made overnight. L0 (the iteration-2 spike) is yours to run (T-3), so live checks are prepared as `@pytest.mark.live` tests and scripts but not run.
- **Scope creep:** anything outside the plan is not implemented; it is listed under "Scope questions".

## 🔴 iterations awaiting your review

| Iteration | Commit | Second T1 review | What to look at |
|---|---|---|---|
| 8a | fdfbf3d | APPROVE WITH FIXES (0 blockers, 4 MAJOR); all 4 MAJOR and minors m1, m3–m6 fixed, 188 tests | `guards/pii_regex.py`: the Unicode fold (`_build_fold`), separator rules, and the over-masking trade-offs pinned by `*_by_design` tests (see D-16). Open: m7 (callers must catch scrubber exceptions and refuse) is tracked in 12/14a |

## Decisions needed from you

| # | Question | Default taken overnight | Blocks |
|---|---|---|---|
| D-1 | Q-5 / the embedding row: is there a `gemini-embedding-*` row in AI Studio, and is its RPD near the assumed 1,000? | the plan keeps 300/day as an assumed ceiling | iteration 38 (forecast drop) and the L0 spike |
| D-2 | Q-6: is the deadline end of day Thu 2026-10-08, or a specific hour? | end of day; Step 6 ring-fenced from Thu 12:00 | Thursday schedule |
| D-3 | Approve the night-run workflow above (🔴 commits on a branch, your review afterwards) | taken as the default | merge to `main` |
| D-4 | `requirements.txt` is exported with `--no-emit-project` (the plain CLAUDE.md command adds `-e .`). pip users then run `pip install -r requirements.txt && pip install -e .`. Update the CLAUDE.md command to match? | flag used; CLAUDE.md not edited (yours) | README install section (iteration 43) |
| D-5 | Embedding limits in `config/models.yaml` (100 RPM / 1,000 RPD / 30K TPM) are placeholders marked unconfirmed | kept as placeholders | iteration 38 only |
| D-6 | Retry count: the plan (iteration 3) says 3 retries with 1/2/4 s backoff, HLD ADR-003 says 1 retry. Which wins? | the plan (3 retries, then one fallback attempt, then force_answer); all bounded by the turn deadline | ADR-003 text |
| D-7 | Drop `pandas` and `db-dtypes` (the spike shows `to_arrow()` and row iteration are enough; pyarrow must stay)? | kept; a lock change waits for iteration 31/40 (hot file) | nothing |
| D-8 | Deadlines the plan does not state: light path 120 s, retry-report 180 s (8 calls, 0 SQL) | taken as set by the implementer | nothing |
| D-9 | PII mask format: AC-08.2/AC-10.7 say `[REDACTED]`, HLD §5.4 says typed tokens (`<EMAIL>`, `<PHONE>`, `<CARD>`, `<ID>`) | HLD typed tokens; ACs to be aligned by you | AC text |
| D-10 | Street-address regex: HLD §5.4 lists it for the regex scrubber, the plan gives addresses to 8b (NER) | 8b (NER) covers addresses | 8b |
| D-11 | Top-level parenthesised `(SELECT ...)` in the SQL policy: reject or unwrap? | the iteration-6 implementer decides and documents it; see the 🔴 row for 6 | nothing |
| D-12 | Input-guard mode (8a review): should *user messages* also mask unseparated 10–12 digit runs and national phone formats (`07700900123`, `8 (495) 010-44-77`)? That would need a stricter `mode` for input than for output | not done; the same rules apply to input and output | nothing (a later addition to 8a/11) |
| D-13 | AC-08.2 and AC-10.7 say `[REDACTED]`; code uses the HLD §5.4 typed tokens (`<EMAIL>`...). Amend the ACs (the same issue as D-9) | code keeps the typed tokens | AC text |
| D-14 | Street address for HLD layer 7 (the post-tool result scrubber, `architecture.md` around line 933), which can run without NER: add a simple address regex to 8a, or have layer 7 call the full detector (regex + NER, 8b)? | layer 7 will call the full 8b detector; no address regex in 8a | 8b, 13 |
| D-15 | AC-08.2 also asks to mask "exact matches of PII values that were present in tool results"; the 8a reviewer found no iteration in `04-plan.md` that owns it | proposed: give it to 12 (output guard) with a named test; to be confirmed by you | 12 |
| D-16 | 8a over-masking trade-offs, each pinned by a `*_by_design` test: a one-column list of 3-3-4 digits and `100 - 200 - 3000` become `<PHONE>`; "Cell phones 2023 2024" becomes "Cell phones <PHONE>"; any 3-2-4 group with spaces or dots ("100 20 2024") becomes `<ID>`; the optional `jane.doe at example.com` rule also masks `orders.status at thelook.com`. Keep them? | kept (safer to over-mask); the optional dotted "at" rule (`_EMAIL_AT_DOTTED`) can be removed cleanly if you prefer | nothing |

## Scope questions (🟡: add now / defer to README "future work" / skip)

| # | Found in | Item | Default |
|---|---|---|---|

## Run log

| Iteration | Status | Note |
|---|---|---|
| 1 | done | Skeleton, all deps (spaCy model pinned as wheel URL), `config/models.yaml`, startup check, socket block; 11 tests. Note: the orchestrator's CLI smoke run loaded `.env` and made **one `models.list` call** (a listing, not a generation; no RPD used). It passed, so every configured id, including `gemini-embedding-001`, is listed for your key. That is part of L0; the 4 generation calls stay yours. |
| 2 | done | Spikes and CI (fb2b4f9). 21 sqlglot quirks documented; the scope rewrite works. Live model/embedding spike prepared as `tests/live/test_spike_models.py`, **not run** (yours, L0). |
| 3 | done | T2 review APPROVE WITH FIXES; all 7 findings fixed (limiter timeout goes straight to fallback, deadline re-checked, `ForceAnswer(template_only=True)` when even force_answer is out of budget); 30 tests |
| 4 | done | T2 review (secrets) REQUEST CHANGES; all 11 findings fixed (af864fa). Log redaction is installed via the log-record factory at startup in `cli.py`; pattern scrubs for API keys and bearer tokens; SQL literals in traces replaced by `?` (fails closed to a hash); trace and DB files are owner-only; secure-delete byte-residue test; 47 tests |
| 8a | done, owner review pending | second T1 review: APPROVE WITH FIXES, 0 blockers, 4 MAJOR (international phones with long groups, an incomplete invisible-character fold, line breaks inside values, defanged `[.]`/`[@]` emails). All 4 plus minors m1, m3–m6 fixed (fdfbf3d, 188 tests). Owner items D-12..D-16 |
| 5, 6 | in progress (T1 opus) | started in parallel; disjoint files |
| 25 | done | 25a50f4. `render_trace` and `metrics_summary` are plain functions, wired into the command table in 19. Allowlisted fields only, printed text re-scrubbed. AC gaps: `user_id` and `finish_reason` are not in the tracer allowlist, the AC-16.2 "message exchange" is not shown (traces hold no message text, by design) and feedback counts wait for 32. Low risk, so no separate T2 review; the orchestrator checked it |
| 16 | done | 8b90866. `--user` selects a profile from `config/profiles.yaml` (synthetic users `analyst_a`, `analyst_b`, `ceo_demo`; the brands are placeholders until the A-38 dry-run count). An unknown user fails before any network call. TR-18 writability checks are in `session.local_startup_check`. Data dir is `./data`, override `OPSFLEET_DATA_DIR`. The check against `products.brand` (AC-20.3) is a pure function, wired once the BQ client lands. A missing `--user` is still a plain argparse error; listing the valid ids is left to 19. Scope is a risk area, so please glance at `session.py` (no separate review run overnight) |
| 27 | in progress (T2 sonnet) | started after 4; 16 gets a minimal `cli.py` edit (`--user`, banner); 25 exposes functions only (wired into the command table in 19) |
| 26 | done | 52048bf. `PersonaStore` reloads `prompts/persona.md` on (mtime, size) change, keeps the last valid persona on a bad file, version = file version + sha256[:8], in the `turn` span. Persona is tone-only: required `## Tone`/`## Style`, any other heading, any `<` and a widened HLD §6.8 denylist (checked on NFKC casefolded text) are rejected. The denylist is strict ("remove jargon" is rejected); relax `_FORBIDDEN` in `persona.py` if it bothers the persona owner. `persona.changed` audit and rollback (FR-52, AC-27.4) are not done (outside the plan). 19 tests |
