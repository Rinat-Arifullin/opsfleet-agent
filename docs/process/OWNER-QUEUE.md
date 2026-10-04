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

## Decisions needed from you

| # | Question | Default taken overnight | Blocks |
|---|---|---|---|
| D-1 | Q-5 / the embedding row: is there a `gemini-embedding-*` row in AI Studio, and is its RPD near the assumed 1,000? | the plan keeps 300/day as an assumed ceiling | iteration 38 (forecast drop) and the L0 spike |
| D-2 | Q-6: is the deadline end of day Thu 2026-10-08, or a specific hour? | end of day; Step 6 ring-fenced from Thu 12:00 | Thursday schedule |
| D-3 | Approve the night-run workflow above (🔴 commits on a branch, your review afterwards) | taken as the default | merge to `main` |
| D-4 | `requirements.txt` is exported with `--no-emit-project` (the plain CLAUDE.md command adds `-e .`). pip users then run `pip install -r requirements.txt && pip install -e .`. Update the CLAUDE.md command to match? | flag used; CLAUDE.md not edited (yours) | README install section (iteration 43) |
| D-5 | Embedding limits in `config/models.yaml` (100 RPM / 1,000 RPD / 30K TPM) are placeholders marked unconfirmed | kept as placeholders | iteration 38 only |

## Scope questions (🟡: add now / defer to README "future work" / skip)

| # | Found in | Item | Default |
|---|---|---|---|

## Run log

| Iteration | Status | Note |
|---|---|---|
| 1 | done | Skeleton, all deps (spaCy model pinned as wheel URL), `config/models.yaml`, startup check, socket block; 11 tests. Note: the orchestrator's CLI smoke run loaded `.env` and made **one `models.list` call** (a listing, not a generation; no RPD used). It passed, so every configured id, including `gemini-embedding-001`, is listed for your key. That is part of L0; the 4 generation calls stay yours. |
