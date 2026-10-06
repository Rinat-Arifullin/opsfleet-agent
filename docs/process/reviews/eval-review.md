# Eval report: iteration 39b (HEAD 812b29d)

Date: 2026-10-06 · Tester: T1 (main session, offline only; no live Gemini/BigQuery calls, `.env` never read) · **Verdict: APPROVED**

## Layer 1 — unit tests and lint

| Check | Command | Result |
|---|---|---|
| Lint | `uv run ruff check .` | All checks passed |
| Unit tests | `OPSFLEET_GOLDEN_STRICT=1 uv run pytest -q` | 4224 passed, 6 deselected (live), 84.9 s |
| Dependency sync | `uv export --no-hashes --format requirements-txt \| diff - requirements.txt` | no diff |

## Layer 2 — agent evals (offline fixtures, stub provider)

| Suite | Pass / total | Gate | Verdict |
|---|---|---|---|
| golden (profile matrix analyst_a / analyst_b / ceo_demo) | 6/6 | ≥ 80% | ok |
| adversarial | 2/2 | 100% | ok |
| adversarial/delete | none failing | any failure fails the run | ok |
| pii_typed recall | 2/2 | ≥ 95% | ok |
| pii_typed brand false positives | 0 over 1 brand case | 0 and ≥ 1 case | ok |
| adversarial/differencing/cross_session | 1/1 | 100% | ok |
| resilience (`bq_timeout`) | 1/1 | 100% | ok |
| router/labelled | 2/2, accuracy 100% | no gate | — |

Totals: 15 stub LLM requests, 6 BQ queries, 0 bytes billed. `RESULT: PASS`, exit 0. Results folder: `evals/results/20261006T035046Z`.

## AC → test/eval traceability

| AC | Unit tests | Evals |
|---|---|---|
| R2 PII / scope / small cell | `tests/unit/test_sql_policy*`, `test_scope*`, `test_small_cell*`, `test_run_sql_order.py` | adversarial, pii_typed, differencing |
| R3 strict-confirmation delete, audit-first | `tests/unit/test_delete_*`, `test_audit*` | adversarial/delete |
| R5 self-correction on errors / empty results | `tests/unit/test_run_sql*` (empty-result retry text) | resilience |
| R7 observability | `test_tracer.py`, `test_trace_viewer.py`, `test_triage.py` — **synthetic `llm` spans only** (AF-1) | — |

## Failing cases
None.

## Known gaps
- No live eval run in this review; the live harness runs no LLM judge (`evals/live_sut.py:396-400`, OD-5), so README L748-749 overstates judge usage (RC-26).
- Live cases for `adversarial/delete`, `adversarial/differencing`, `resilience` do not exist; only offline fixtures under `evals/cases/_fixtures/` (RC-21). Golden set is 44 cases, README says 41 (RC-20). Injection cases cover the user turn only (RC-23).
- Missing unit tests named in `code-review.md`: failing-provider → tracer `llm` span (AF-1), forced label vs router refuse (AF-2), idle timeout (AF-3), light-path cap (AF-4); `SELECT COUNT(*) FROM users` false positive (SEC-A2).

## Verdict
**APPROVED** for the test and eval layers as they stand; the gaps above are tracked through the code and final reviews.
