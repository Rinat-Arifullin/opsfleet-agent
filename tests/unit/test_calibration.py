"""Judge calibration gate (iteration 30, AC-29.6). Offline: fake judge, synthetic labels."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml
from evals import gates as G
from evals import judge as J
from evals import run as R
from evals.calibration import run as C

CASES = C.load_cases()
MODEL = J.judge_model_from_models_yaml()
OWNER = {c["id"]: (5 if i % 2 == 0 else 1) for i, c in enumerate(CASES)}  # synthetic, 15 / 15


def _cases_file(tmp_path: Path, scores=None, name="cases.yaml", cases=None) -> Path:
    scores = OWNER if scores is None else scores
    rows = [dict(c, owner_score=scores.get(c["id"])) for c in (cases or CASES)]
    p = tmp_path / name
    p.write_text(yaml.safe_dump({"cases": rows}), encoding="utf-8")
    return p


def _fake_llm(agree_on: int, errors: int = 0, owner=None):
    """Owner verdict on the first `agree_on` cases, the opposite after; last `errors` raise."""
    owner = owner or OWNER
    order = {c["candidate_answer"]: i for i, c in enumerate(CASES)}
    calls: list[str] = []

    def call(model: str, prompt: str) -> str:
        calls.append(model)
        idx = next(i for a, i in order.items() if f"<answer>\n{a}\n</answer>" in prompt)
        if idx >= len(CASES) - errors:
            raise RuntimeError("quota")
        owner_pass = owner[CASES[idx]["id"]] >= 4
        judge_pass = owner_pass if idx < agree_on else not owner_pass
        return json.dumps({"score": 5 if judge_pass else 1, "rationale": "synthetic"})

    call.calls = calls  # type: ignore[attr-defined]
    return call


def _run(tmp_path, llm, cases: Path | None = None, extra=()):
    out = io.StringIO()
    argv = ["--status-file", str(tmp_path / "status.json")]
    if cases:
        argv += ["--cases", str(cases)]
    rc = C.main([*argv, *extra], llm_call=llm, out=out)
    return rc, out.getvalue(), J.load_status(tmp_path / "status.json")


def _counts(status, cases_path: Path | None = None) -> bool:
    """What evals/run.py asks: does the record match the current cases file and labels?"""
    h, labels = R._calibration_binding(cases_path, MODEL)
    return status.counts_for(MODEL, inputs_hash=h, labels=labels)


def test_judge_calibration_gate_blocks_on_low_agreement(tmp_path) -> None:
    cases = _cases_file(tmp_path)
    rc, text, status = _run(tmp_path, _fake_llm(agree_on=23), cases)  # 23/30 = 77% < 80%
    assert rc == 1
    assert status.calibrated is False and status.agreement == pytest.approx(23 / 30, abs=1e-3)
    assert not _counts(status, cases)
    assert "UNCALIBRATED" in text
    # the written status is what gates.py acts on: judged golden case, uncalibrated -> gate fails
    golden = [G.CaseResult(f"golden/g{i}", "golden", judged=True) for i in range(10)]
    gate = next(
        g for g in G.evaluate_gates(golden, judge_calibrated=_counts(status, cases))
        if g.name == "golden"
    )  # fmt: skip
    assert not gate.passed and "uncalibrated" in gate.detail


def test_calibration_passes_at_threshold_and_counts(tmp_path) -> None:
    cases = _cases_file(tmp_path)
    llm = _fake_llm(agree_on=24)  # 24/30 = 80%
    rc, _, status = _run(tmp_path, llm, cases)
    assert rc == 0 and status.calibrated and status.n_cases == 30
    assert _counts(status, cases)
    assert not status.counts_for("some-other-model", inputs_hash=status.inputs_hash, labels=OWNER)
    assert not status.counts_for(MODEL)  # unbound callers never count
    assert len(llm.calls) == 30  # one call per case, no retries
    # evals/run.py reports the judge as counted only through this binding
    assert status.judge_scores and len(status.judge_scores) == 30


def test_missing_owner_labels_report_uncalibrated_without_calls(tmp_path) -> None:
    llm = _fake_llm(30)
    rc, text, status = _run(tmp_path, llm)  # shipped cases.yaml: owner_score still empty
    assert rc == 1 and not status.calibrated
    assert "owner labels missing" in status.reason and "No judge call made" in text
    assert llm.calls == []


@pytest.mark.parametrize("bad", [9, 0, True, 5.0, "5", None], ids=repr)
def test_invalid_label_values_are_uncalibrated(tmp_path, bad) -> None:
    # True and 5.0 equal 1 and 5 in Python: they must still be refused
    scores = dict(OWNER)
    scores[CASES[3]["id"]] = bad
    llm = _fake_llm(30)
    rc, _, status = _run(tmp_path, llm, _cases_file(tmp_path, scores))
    assert rc == 1 and not status.calibrated and "1 of 30" in status.reason
    assert llm.calls == []


def test_duplicate_case_id_is_refused(tmp_path) -> None:
    cases = [dict(c) for c in CASES]
    cases[1]["id"] = cases[0]["id"]
    rc, _, status = _run(tmp_path, _fake_llm(30), _cases_file(tmp_path, cases=cases))
    assert rc == 1 and not status.calibrated and "30 unique" in status.reason


def test_twenty_nine_cases_are_refused(tmp_path) -> None:
    rc, _, status = _run(tmp_path, _fake_llm(30), _cases_file(tmp_path, cases=CASES[:29]))
    assert rc == 1 and not status.calibrated and "found 29" in status.reason


def test_judge_errors_count_as_fail_verdicts(tmp_path) -> None:
    owner = {c["id"]: 5 for c in CASES}  # owner passes everything
    cases = _cases_file(tmp_path, owner)
    rc, _, status = _run(tmp_path, _fake_llm(30, errors=7, owner=owner), cases)
    assert rc == 1 and not status.calibrated
    assert status.agreement == pytest.approx(23 / 30, abs=1e-3)


def test_stale_record_does_not_count_after_labels_change(tmp_path) -> None:
    cases = _cases_file(tmp_path)
    rc, _, status = _run(tmp_path, _fake_llm(30), cases)
    assert rc == 0 and _counts(status, cases)
    changed = dict(OWNER)
    changed[CASES[0]["id"]] = 1
    _cases_file(tmp_path, changed)  # same path, one label flipped
    rc, _, status = _run(tmp_path, None, cases)  # no --llm: refused, record untouched
    assert rc == 2
    assert not _counts(J.load_status(tmp_path / "status.json"), cases)


@pytest.mark.parametrize("field", ["candidate_answer", "question", "reference_note"])
def test_editing_any_case_text_makes_record_stale(tmp_path, field) -> None:
    cases = _cases_file(tmp_path)
    rc, _, status = _run(tmp_path, _fake_llm(30), cases)
    assert rc == 0 and _counts(status, cases)
    edited = [dict(c) for c in CASES]
    edited[3][field] = edited[3].get(field, "") + " Edited."
    _cases_file(tmp_path, OWNER, cases=edited)  # same path, one text changed
    assert not _counts(status, cases)


def test_hash_covers_rubric_kinds_and_prompt_template(monkeypatch) -> None:
    base = J.inputs_hash(CASES, OWNER, MODEL)
    other = next(k for k in J.RUBRIC_KINDS if k != "golden")  # not in the golden prompt
    monkeypatch.setattr(J, "RUBRIC_KINDS", {**J.RUBRIC_KINDS, other: "changed"})
    assert J.inputs_hash(CASES, OWNER, MODEL) != base
    monkeypatch.undo()
    monkeypatch.setattr(J, "build_prompt", lambda *a, **k: "another template")
    assert J.inputs_hash(CASES, OWNER, MODEL) != base


def test_forged_record_does_not_count(tmp_path) -> None:
    cases = _cases_file(tmp_path)
    h, labels = R._calibration_binding(cases, MODEL)
    base = dict(
        calibrated=True, agreement=0.9, n_cases=30, judge_model=MODEL,
        rubric_version=J.RUBRIC_VERSION, inputs_hash=h,
    )  # fmt: skip
    ok_scores = {k: (5 if v >= 4 else 1) for k, v in labels.items()}
    assert J.CalibrationStatus(**base, judge_scores=ok_scores).counts_for(
        MODEL, inputs_hash=h, labels=labels
    )
    bad_scores = {k: (1 if v >= 4 else 5) for k, v in labels.items()}  # claims 0.9, scores say 0
    one = next(iter(ok_scores))
    missing_id = {k: v for k, v in ok_scores.items() if k != one}
    extra_id = {**ok_scores, "cal-99": 5}
    swapped_id = {**missing_id, "cal-99": 5}  # right size, one id wrong
    for forged in (
        J.CalibrationStatus(**base, judge_scores=bad_scores),
        J.CalibrationStatus(**base, judge_scores=None),
        J.CalibrationStatus(**base, judge_scores=missing_id),
        J.CalibrationStatus(**base, judge_scores=extra_id),
        J.CalibrationStatus(**base, judge_scores=swapped_id),
        J.CalibrationStatus(**{**base, "agreement": "0.9"}, judge_scores=ok_scores),
        J.CalibrationStatus(**{**base, "calibrated": 1}, judge_scores=ok_scores),
        J.CalibrationStatus(**{**base, "n_cases": 0}, judge_scores=ok_scores),
        J.CalibrationStatus(**{**base, "agreement": 0.1}, judge_scores=ok_scores),
        J.CalibrationStatus(**{**base, "inputs_hash": "x"}, judge_scores=ok_scores),
        J.CalibrationStatus(**{**base, "rubric_version": "rubric-0.9"}, judge_scores=ok_scores),
    ):
        assert forged.counts_for(MODEL, inputs_hash=h, labels=labels) is False
    # malformed labels or scores never raise out of counts_for
    rec = J.CalibrationStatus(**base, judge_scores=ok_scores)
    assert rec.counts_for(MODEL, inputs_hash=h, labels={**labels, one: None}) is False
    assert rec.counts_for(MODEL, inputs_hash=h, labels={k: "x" for k in labels}) is False
    assert J.recorded_agreement(missing_id, labels) is None
    assert J.recorded_agreement(extra_id, labels) is None


def test_load_status_is_type_strict(tmp_path) -> None:
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"calibrated": "false", "n_cases": 30, "agreement": 0.9}))
    assert J.load_status(p).calibrated is False
    p.write_text(json.dumps({"calibrated": True, "n_cases": True, "agreement": True}))
    st = J.load_status(p)
    assert st.n_cases == 0 and st.agreement is None
    h = "x"
    for bad in ({"a": 5.0}, {"a": "5"}, {"a": 5.0, "b": 5}, [5]):
        p.write_text(json.dumps({"calibrated": True, "inputs_hash": h, "judge_scores": bad}))
        assert J.load_status(p).judge_scores is None
    p.write_text(json.dumps({"calibrated": True, "judge_scores": {"a": 5, "b": None}}))
    assert J.load_status(p).judge_scores == {"a": 5, "b": None}


def test_recorded_agreement_requires_real_int_scores() -> None:
    labels = {"a": 5, "b": 1}
    assert J.recorded_agreement({"a": 5, "b": 1}, labels) == 1.0
    assert J.recorded_agreement({"a": 5.0, "b": 1}, labels) == 0.5  # 5.0 is no verdict
    assert J.recorded_agreement({"a": True, "b": 1}, labels) == 0.5
    assert J.recorded_agreement({"a": None, "b": 1}, labels) == 0.5


def test_started_marker_and_factory_failure_leave_uncalibrated(tmp_path) -> None:
    cases = _cases_file(tmp_path)
    _run(tmp_path, _fake_llm(30), cases)  # a valid record exists
    out = io.StringIO()
    rc = C.main(
        ["--status-file", str(tmp_path / "status.json"), "--cases", str(cases),
         "--llm", "no_such_module_xyz:make"],
        out=out,
    )  # fmt: skip
    st = J.load_status(tmp_path / "status.json")
    assert rc == 2 and not st.calibrated and "factory failed" in st.reason


def test_midrun_crash_does_not_leave_old_record(tmp_path) -> None:
    cases = _cases_file(tmp_path)
    _run(tmp_path, _fake_llm(30), cases)

    class Boom(BaseException):
        pass

    def crash(model: str, prompt: str) -> str:
        raise Boom

    with pytest.raises(Boom):
        _run(tmp_path, crash, cases)
    st = J.load_status(tmp_path / "status.json")
    assert not st.calibrated and st.reason == "calibration run started"


@pytest.mark.parametrize(
    "raw", ['{"score": 3.9}', '{"score": true}', '{"score": "4"}', '{"score": 4.0}', "[1]"]
)
def test_judge_score_must_be_a_real_int(raw) -> None:
    assert J.parse_verdict(raw, "m").score is None
    assert J.parse_verdict('{"score": 4}', "m").score == 4


def test_no_llm_and_complete_labels_is_refused(tmp_path) -> None:
    rc, text, _ = _run(tmp_path, None, _cases_file(tmp_path))
    assert rc == 2 and "no judge LLM" in text


def test_last_run_detail_not_written_into_repo_by_default() -> None:
    assert C.REPO_ROOT == Path(__file__).resolve().parents[2]
    assert J.DEFAULT_STATUS_PATH.resolve().parent.is_relative_to(C.REPO_ROOT)


def test_thirty_one_cases_with_one_duplicate_are_refused(tmp_path) -> None:
    cases = [dict(c) for c in CASES] + [dict(CASES[0])]  # 31 rows, 30 unique ids
    rc, _, status = _run(tmp_path, _fake_llm(30), _cases_file(tmp_path, cases=cases))
    assert rc == 1 and not status.calibrated and "found 31" in status.reason


def test_last_run_detail_only_outside_repo(tmp_path, monkeypatch) -> None:
    repo, outside = tmp_path / "repo", tmp_path / "out"
    (repo / "evals").mkdir(parents=True)
    outside.mkdir()
    monkeypatch.setattr(C, "REPO_ROOT", repo)
    cases = _cases_file(tmp_path)
    for d, expect in ((repo / "evals", False), (outside, True)):
        out = io.StringIO()
        rc = C.main(
            ["--cases", str(cases), "--status-file", str(d / "status.json")],
            llm_call=_fake_llm(30), out=out,
        )  # fmt: skip
        assert rc == 0
        assert (d / "status.json").exists()
        assert (d / "last_run.json").exists() is expect


@pytest.mark.parametrize("field", ["question", "candidate_answer"])
@pytest.mark.parametrize("blank", ["", "   \n", None, 5])
def test_blank_question_or_answer_is_refused(tmp_path, field, blank) -> None:
    cases = [dict(c) for c in CASES]
    cases[4][field] = blank
    llm = _fake_llm(30)
    rc, _, status = _run(tmp_path, llm, _cases_file(tmp_path, cases=cases))
    assert rc == 1 and not status.calibrated and f"has no {field}" in status.reason
    assert llm.calls == []


def test_duplicate_id_message_reports_unique_count(tmp_path) -> None:
    cases = [dict(c) for c in CASES]
    cases[1]["id"] = cases[0]["id"]
    _, _, status = _run(tmp_path, _fake_llm(30), _cases_file(tmp_path, cases=cases))
    assert "29 unique ids" in status.reason


def test_duplicate_yaml_key_is_refused(tmp_path) -> None:
    p = tmp_path / "dup.yaml"
    p.write_text("cases:\n  - id: a\n    id: b\n", encoding="utf-8")
    with pytest.raises(C.LabelError, match="duplicate YAML key"):
        C.load_cases(p)
