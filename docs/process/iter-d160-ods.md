# D-160: profile matrix for golden evals (ODs)

Owner request 2026-10-05: run the golden eval checks under several profiles (`analyst_a`,
`analyst_b`, `ceo_demo`), not only the one each case was written for.

Files:
- new `evals/profile_matrix.py`: the case fields, expansion into (case, profile) runs, the
  scope invariants and the case x profile summary table
- `evals/run.py`: new `Case` fields (`profiles`, `profiles_reason`, `per_profile`,
  `known_brands`, plus the derived `profile` and `base_id`), `expand_cases`, the `--profile`
  filter, `scope:*` checks on every run, the matrix in the console output and in
  `summary.json` (`profiles`, `matrix`)
- `evals/langfuse_dataset.py`: one item per run (`<case>@<profile>`), `--profile` on `upload`
  and `run`, stale item reporting and `--archive-stale`, the matrix in the run summary
- golden cases: `per_profile` overrides where the expected text depends on the scope, and
  opt-outs with a reason (below); `top_customers.yaml` gained only the opt-out lines
- new fixture `evals/cases/_fixtures/golden/matrix_scope.yaml`
- new tests: `tests/unit/test_eval_profile_matrix.py`; `tests/unit/test_eval_runner.py`
  updated for run ids and the matrix line
- docs: `infra/langfuse/README.md`, the root README, one bullet in `docs/architecture.md` §6.6

No hot files, no new dependencies, no config fields.

## Case schema

```yaml
profiles: all                  # default for golden cases; or a list of profile ids
profiles_reason: "..."         # required when the list is a strict subset of the profiles
per_profile:                   # optional; only expect, fake and skip may be overridden
  ceo_demo:
    expect: {must_contain: [all products]}
    fake: {text: "... across all products ..."}
known_brands: [Acme]           # optional extra brands for the answer check
```

Overrides are merged one level deep over the base case (`expect.must_contain` replaces the
base list, other keys are kept). A `per_profile` entry for a profile the case does not run
under, an unknown profile id, or a subset without `profiles_reason` is a load error.
Non-golden suites are not expanded: their ids and behaviour are unchanged.

## Matrix size

- Default offline set: 27 golden base cases produce 69 golden runs (3 profiles each, minus
  the opt-outs below).
- Fixture set (`evals/cases/_fixtures`): 6 matrix runs.

## Opt-outs

| Case | Runs under | Reason |
|---|---|---|
| `discuss_saved_report`, `report_search`, `roadmap_actions_unsupported` | `analyst_a` | the saved-report seed belongs to `analyst_a`; another owner would see no reports by design |
| `clarify_unresolved_reference`, `compare_brands_why` | `analyst_b`, `ceo_demo` | the user types `analyst_b`'s brands; under `analyst_a` that is an out-of-scope request, which the adversarial suite already covers |
| `preference_table_vs_bullets` | `analyst_a` | the case is skipped until iteration 39 |
| `top_customers` | `analyst_a` | D-159 is reworking the case in parallel; the per-profile variants follow it |

## Decisions
- **OD-1 Golden cases default to every profile.** A golden case with no `profiles` field runs
  under all shipped profiles, so a new case is matrix-tested unless its author says why not.
  The `--profile` filter (repeatable) keeps one or more profiles on `run.py`,
  `langfuse_dataset.py upload` and `langfuse_dataset.py run`. An unknown id is refused.
- **OD-2 Scope invariants are checked in code on every run.** `scope:answer_brands`: for a
  brand-scoped profile, the answer names no known brand outside the profile (the same matcher
  as the context guard). `scope:sql`: every executed statement passes `verify_scoped` for the
  profile's scope. `scope:all_products_path`: an all-products profile resolves to all products
  and no statement carries the `@scope_brands` filter. A missing or unresolvable profile is a
  failed `scope:profile` check. Every loop is capped: at most 50 statements checked per run,
  20 profiles and 200 `known_brands` entries per case.
- **OD-3 Placeholder SQL makes the offline SQL check vacuous.** Most golden fakes record
  `SELECT 1 /* recorded placeholder */`, which touches no base table, so `scope:sql` passes
  trivially offline. Offline, the check has real teeth only in `show_sql` (recorded
  `apply_scope` output per profile), the matrix fixture and the unit tests. Live runs check
  the real SQL of every run.
- **OD-4 The known-brand list is small.** The answer check knows the brands in the profiles
  and the Golden seed (3 brands today: Calvin Klein, Carhartt, Levi's), plus a case's
  `known_brands`. An answer naming any other real brand is not caught. A full brand list
  would need a live query or a checked-in snapshot of the dataset; both are out of scope.
- **OD-5 CEO wording.** The `ceo_demo` overrides expect the scope to be named as "all
  products", the label the scope code emits. If that label changes, the overrides change
  with it.
- **OD-6 Brands the user typed are echoed.** Where the user names brands outside the
  profile, the answer may repeat them in a refusal, which would fail the answer check. These
  cases run only under profiles that own the brands (opt-outs above); the refusal itself is
  tested by the adversarial suite.
- **OD-7 Trace SQL is restored before the check.** The tracer replaces every literal with `?`.
  The check turns each placeholder back into a literal `0` before `verify_scoped`. This is
  sound only while the code CTE bodies contain no literals, and a unit test asserts that for
  every CTE, scoped and unscoped. A hash-only trace entry (no SQL text) fails the check
  instead of passing silently.
- **OD-8 Langfuse item ids are `<case>@<profile>`.** `@` is now allowed in item ids. The
  metadata keeps `case_id` (the run id), `base_case_id`, `profile` and, if set,
  `known_brands`. An item is one already-expanded run and is never expanded again on `run`.
  `run --case` accepts either a case id (all its profiles) or one run id.
- **OD-9 Stale items are kept by default.** After the switch, the pre-matrix items (id without
  `@<profile>`) and items of removed or narrowed cases are listed as stale on a full upload
  and left in place. `upload --archive-stale` sets them to ARCHIVED, an upsert by id that
  keeps the rest of the item, and `run` skips archived items. A `--profile` upload is partial
  and reports nothing as stale. Nothing is deleted, which extends iter40b OD-2.
- **OD-10 Golden detection for the Langfuse upload.** The upload reads `evals/cases/golden`
  directly, so the cases' suite is `.`. When any path part of the cases dir is `golden`, all
  its cases are treated as golden cases.
- **OD-11 The live seeds are unchanged.** Cases that seed saved reports, a persona or
  preferences still fail under the live SUT (iter40b OD-4). The matrix multiplies those
  failures only where the case was not opted out.
- **OD-12 The golden gate still fails offline without a judge calibration.** This is not new.
  With no recorded judge calibration, judged golden runs are reported UNCALIBRATED and the
  offline golden gate exits non-zero; every deterministic check, `scope:*` included, passes.
