# Iteration 30 open decisions

- OD-1: Owner labels (T-1) are not supplied. `cases.yaml` has `owner_score: null`, so the gate writes "uncalibrated" (no judge call) until you fill all 30 scores 1-5. No placeholder labels were invented. When you label, drop `test_owner_fields_empty` in tests/unit/test_calibration_cases.py.
- OD-2: Labels may go in `cases.yaml` (default) or a separate file via `--labels` (needs `source: owner`; `synthetic_placeholder` is always refused). Confirm which you prefer.
- OD-3: Agreement is measured on pass/fail (score >= 4), per HLD 6.6, not exact-score match. A judge error or unparsable reply counts as a fail verdict (disagreement when the owner passed). Confirm.
- OD-4: Live run is `python -m evals.calibration.run --llm module:factory` (the factory returns `llm_call(model, prompt) -> str`); no factory exists in the repo yet. One call per case, 30 flash-lite calls, no retries, counted in the L5 budget. Who writes the live factory (shared with the golden `--sut`)?
- OD-5: `status.json` is not committed and not ignored. Decide whether to commit the calibrated record (it ties to judge model and rubric version) or gitignore it (.gitignore is a hot file).
- OD-6: A second miss after the one allowed rubric fix (R6) is yours to decide; the gate never weakens (80% and 30 cases are constants in judge.py).

## Round 2 updates (review REQUEST CHANGES)
- OD-7: the `--labels` option is dropped. cases.yaml `owner_score` in a reviewed commit is the only label source and the provenance.
- OD-8: status.json is bound to a sha256 inputs_hash (case ids, questions, answers, reference notes, owner labels, rubric text hash, judge model) and stores per-case judge scores; evals/run.py recomputes the hash against the shipped cases.yaml and requires n_cases >= 30 and agreement >= 0.80 recomputed offline. Any label or case edit makes the record stale until the owner reruns calibration.
- OD-9: Report-kind cases: the 30 calibration cases carry kind good/partial/wrong, not report; no `kind` is passed to the judge. Revisit if report cases are added.
- OD-10: last_run.json is written only when the status file is outside the repo; owner to approve the .gitignore request for status.json and last_run.json (hot-file-requests.md).
- OD-11: tests/unit/test_calibration_cases.py::test_owner_fields_empty must be dropped when the owner labels the cases; test_eval_runner.py::test_calibrated_judge_scores_count was updated for the binding.
- OD-12: the inputs hash also covers RUBRIC_KINDS and the golden prompt template; changing any rubric or prompt wording makes the record stale.
