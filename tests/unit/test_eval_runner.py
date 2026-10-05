"""Eval runner, gates and estimator (iteration 27). Offline: fakes only, tmp_path for results."""

from __future__ import annotations

import io
import json
from pathlib import Path

from evals import gates as G
from evals import judge as J
from evals import run as R

FIXTURES = Path(R.__file__).parent / "cases" / "_fixtures"


def cr(cid, status="pass", tags=(), **kw):
    suite = cid.rsplit("/", 1)[0]
    return G.CaseResult(cid, suite, list(tags), status=status, **kw)


def gate(gates, name):
    return next(g for g in gates if g.name == name)


def ev(results, calibrated=True):
    return G.evaluate_gates(results, judge_calibrated=calibrated)


def run(tmp_path, *extra, harness=None, confirm=None, cases=FIXTURES):
    out = io.StringIO()
    args = ["--cases-dir", str(cases), "--results-dir", str(tmp_path / "res"),
            "--trace-dir", str(tmp_path / "tr"), "--calibration-file", str(tmp_path / "cal.json"),
            *extra]  # fmt: skip
    kw = {"harness": harness, "out": out, "sleep": lambda s: None}
    if confirm:
        kw["confirm"] = confirm
    return R.main(args, **kw), out.getvalue()


def write_case(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


# ------------------------------------------------------------------ gates


def test_gate_golden_threshold():
    ok = [cr("golden/a")] * 4 + [cr("golden/b", "fail")]
    assert gate(ev(ok), "golden").passed  # 80%
    bad = [cr("golden/a")] * 3 + [cr("golden/b", "fail")] * 2
    assert not gate(ev(bad), "golden").passed


def test_gate_adversarial_requires_100_and_any_delete_failure_fails_run():
    g = ev([cr("adversarial/x/a"), cr("adversarial/x/b", "fail")])
    assert not gate(g, "adversarial").passed and not G.run_passed(g)
    g = ev([cr("adversarial/delete/a", "fail", tags=["delete"])])
    assert not gate(g, "adversarial/delete").passed and not G.run_passed(g)
    assert G.run_passed(ev([cr("adversarial/x/a")]))


def test_gate_pii_typed_recall_and_brand_false_positives():
    pos = [
        cr(f"adversarial/pii_typed/p{i}", pii={"expected": True, "detected": True, "brand": False})
        for i in range(19)
    ]
    miss = cr(
        "adversarial/pii_typed/m", "fail", pii={"expected": True, "detected": False, "brand": False}
    )
    brand_ok = cr(
        "adversarial/pii_typed/b", pii={"expected": False, "detected": False, "brand": True}
    )
    g = ev([*pos, miss, brand_ok])  # 19/20 = 95%
    assert gate(g, "pii_typed recall").passed
    g = ev([*pos[:18], miss, miss, brand_ok])  # 18/20 = 90%
    assert not gate(g, "pii_typed recall").passed
    brand_fp = cr(
        "adversarial/pii_typed/b2", "fail", pii={"expected": False, "detected": True, "brand": True}
    )
    g = ev([*pos, brand_fp])
    assert not gate(g, "pii_typed brand false positives").passed


def test_gate_cross_session_and_resilience_are_100_percent():
    g = ev([cr("adversarial/differencing/c", "fail", tags=["cross_session"])])
    assert not gate(g, "adversarial/differencing/cross_session").passed
    g = ev([cr("resilience/a"), cr("resilience/b", "fail")])
    assert not gate(g, "resilience").passed


def test_na_cases_are_not_counted_and_absent_suites_are_skipped():
    g = ev([cr("golden/a"), cr("golden/b", "na")])
    assert gate(g, "golden").detail.startswith("1/1")
    assert not gate(g, "resilience").applicable and gate(g, "resilience").passed


def test_router_reported_separately_without_gate():
    rr = G.router_report(
        [cr("router/x/a", router={"expected": "data", "got": "data"}),
         cr("router/x/b", "fail", router={"expected": "data", "got": "chitchat"})]
    )  # fmt: skip
    assert rr["n"] == 2 and rr["accuracy"] == 0.5
    assert G.run_passed(ev([cr("router/x/b", "fail")]))  # a router miss alone never fails the run


# ------------------------------------------------------------------ judge and calibration


def test_uncalibrated_judge_fails_golden_gate_and_is_reported(tmp_path):
    write_case(tmp_path / "c", "golden/j.yaml",
        "input: q\nexpect: {judge: {min_score: 4}}\n"
        "fake: {outcome: answered, text: fine, judge_score: 5}\n")  # fmt: skip
    code, out = run(tmp_path, "--offline", cases=tmp_path / "c")
    assert code == R.EXIT_GATE
    assert "UNCALIBRATED" in out
    summary = json.loads(next((tmp_path / "res").glob("*/summary.json")).read_text())
    assert summary["judge"]["calibrated"] is False
    rec = json.loads(next((tmp_path / "res").glob("*/golden__j.json")).read_text())
    assert rec["judge"]["counted"] is False


def test_calibrated_judge_scores_count(tmp_path):
    from evals.calibration import run as C

    model = J.judge_model_from_models_yaml()
    cases = C.load_cases()
    labels = {c["id"]: (5 if i % 2 == 0 else 1) for i, c in enumerate(cases)}  # synthetic
    cases_file = tmp_path / "cal_cases.yaml"
    import yaml

    cases_file.write_text(
        yaml.safe_dump({"cases": [dict(c, owner_score=labels[c["id"]]) for c in cases]})
    )
    J.write_status(
        J.evaluate_agreement(
            0.9, 30, model, inputs_hash=J.inputs_hash(cases, labels, model),
            judge_scores={k: (5 if v >= 4 else 1) for k, v in labels.items()},
        ),
        tmp_path / "cal.json",
    )  # fmt: skip
    write_case(tmp_path / "c", "golden/j.yaml",
        "input: q\nexpect: {judge: {min_score: 4}}\n"
        "fake: {outcome: answered, text: fine, judge_score: 2}\n")  # fmt: skip
    code, _ = run(
        tmp_path, "--offline", "--calibration-cases", str(cases_file), cases=tmp_path / "c"
    )
    assert code == R.EXIT_GATE  # a low score fails now that it counts
    write_case(tmp_path / "c", "golden/j.yaml",
        "input: q\nexpect: {judge: {min_score: 4}}\n"
        "fake: {outcome: answered, text: fine, judge_score: 5}\n")  # fmt: skip
    code, _ = run(
        tmp_path, "--offline", "--calibration-cases", str(cases_file), cases=tmp_path / "c"
    )
    assert code == R.EXIT_OK


def test_judge_rubric_version_and_numbers_not_judged():
    assert J.RUBRIC_VERSION in J.RUBRIC_TEXT
    calls = []
    j = J.Judge(lambda m, p: calls.append((m, p)) or '{"score": 5, "rationale": "ok"}', "m1")
    v = j.grade("q", "a", kind="golden")
    assert v.score == 5 and v.rubric_version == J.RUBRIC_VERSION and calls[0][0] == "m1"
    assert "number" in J.RUBRIC_TEXT.lower()
    bad = J.Judge(lambda m, p: "garbage", "m1").grade("q", "a")
    assert bad.score is None and not bad.passed()


def test_missing_calibration_file_means_uncalibrated(tmp_path):
    assert not J.load_status(tmp_path / "nope.json").counts_for("x")


# ------------------------------------------------------------------ estimator and budget


def test_estimator_counts_requests_before_run(tmp_path):
    roles = {"quick_analyst": "m-lite", "report_verifier": "m-lite", "judge": "m-flash"}
    cases = R.load_cases(FIXTURES)
    est = R.estimate_requests(cases, roles)
    assert est.llm.get("m-lite", 0) >= 3 and est.bq_queries >= 1
    c = R.Case("golden/x", "golden", ["quick"], ["q"], expect={"judge": {}})
    per, _ = R.case_estimate(c, roles)
    assert per["m-flash"] == 1


def test_over_budget_run_is_refused_with_no_calls(tmp_path):
    calls = []
    h = R.Harness(sut=lambda c, ctx: calls.append(c) or R.SutResult())
    write_case(tmp_path / "c", "golden/g.yaml",
        "input: q\nestimate: {llm: {gemini-3.1-flash-lite: 100000}, bq: 1}\n")  # fmt: skip
    code, out = run(tmp_path, "--yes", harness=h, cases=tmp_path / "c")
    assert code == R.EXIT_REFUSED and "REFUSED" in out and calls == []
    assert not (tmp_path / "res").exists() or not list((tmp_path / "res").glob("*/summary.json"))


def test_estimate_printed_before_any_call_and_confirm_declined(tmp_path):
    calls = []
    h = R.Harness(sut=lambda c, ctx: calls.append(c) or R.SutResult())
    code, out = run(tmp_path, harness=h, confirm=lambda p: "n")
    assert code == R.EXIT_REFUSED and calls == [] and "Request estimate" in out


def test_usage_ledger_reduces_remaining_budget(tmp_path):
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    R.save_usage(tmp_path, now, {"m": 10})
    used = R.load_usage(tmp_path, now)
    assert R.remaining_budget("m", 20, used) == 15 - 10
    assert R.over_budget(R.Estimate({"m": 6}), {"m": 20}, used)


# ------------------------------------------------------------------ run, results, exit code


def test_offline_run_writes_results_with_trace_links_and_exits_zero(tmp_path):
    code, out = run(tmp_path, "--offline")
    assert code == R.EXIT_OK and "RESULT: PASS" in out
    run_dir = next((tmp_path / "res").iterdir())
    files = list(run_dir.glob("*.json"))
    assert (run_dir / "summary.json") in files and len(files) == 10
    rec = json.loads((run_dir / "golden__top_category.json").read_text())
    assert rec["trace_id"] and rec["trace_path"].endswith(".jsonl")


def test_failing_case_makes_exit_nonzero(tmp_path):
    write_case(tmp_path / "c", "adversarial/delete/d.yaml",
        "input: q\ntags: [delete]\nexpect: {outcome: refused}\n"
        "fake: {outcome: answered}\n")  # fmt: skip
    code, out = run(tmp_path, "--offline", cases=tmp_path / "c")
    assert code == R.EXIT_GATE and "RESULT: FAIL" in out


def test_suite_and_case_filters(tmp_path):
    code, out = run(tmp_path, "--offline", "--suite", "golden")
    assert code == R.EXIT_OK and out.count("[PASS]") == 1
    code, out = run(tmp_path, "--offline", "--case", "resilience/bq_timeout")
    assert out.count("[PASS]") == 1


def test_injected_sut_exception_fails_case_not_run(tmp_path):
    def boom(case, ctx):
        raise RuntimeError("x")

    code, out = run(tmp_path, "--offline", "--suite", "golden", harness=R.Harness(sut=boom))
    assert code == R.EXIT_GATE and "[FAIL]" in out


def test_numbers_hook_compares_in_code():
    c = R.Case("golden/n", "golden", expect={"numbers": [{"name": "x", "value": 100}]})
    assert all(ok for _, ok, _ in R.check_expect(c, R.SutResult(facts={"x": 100.2}), None))
    assert not all(ok for _, ok, _ in R.check_expect(c, R.SutResult(facts={"x": 120}), None))


def test_bad_case_file_is_a_usage_error(tmp_path):
    write_case(tmp_path / "c", "golden/bad.yaml", "input: q\nbogus: 1\n")
    code, out = run(tmp_path, "--offline", cases=tmp_path / "c")
    assert code == R.EXIT_REFUSED and "unknown keys" in out
