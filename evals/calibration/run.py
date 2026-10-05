"""Judge calibration gate (iteration 30, HLD 6.6, AC-29.6).

The judge grades the 30 synthetic cases in `cases.yaml`; its pass/fail (score >= 4) is compared
with the owner's (score >= 4). Agreement >= 80% on all 30 cases writes a calibrated status record
to `evals/calibration/status.json`, which `evals/run.py` and `evals/gates.py` read. Anything else
writes an uncalibrated record with a reason, so judged golden scores do not count.

The status record is bound to its inputs: `inputs_hash` covers the case texts, the owner labels,
the rubric and the judge model, and the record stores the per-case judge scores. `evals/run.py`
recomputes the hash from the current `cases.yaml` and the agreement from the recorded scores, so
a stale, edited or forged record does not count.

Owner labels are never invented. With a missing, partial or invalid label set the gate makes no
judge call and records "uncalibrated". `cases.yaml` is the only label source; its provenance is
the reviewed commit that fills `owner_score` (there is no separate labels file).

All `kind` values in the set are drafter hints (good, partial, wrong); there are no report-kind
cases, so the judge always uses the "golden" rubric kind.

Live use needs `--llm module:factory` (a factory returning `llm_call(model, prompt) -> str`);
without it nothing is called. One judge call per case: at most 30, no retries. A judge error or
unparsable reply counts as a fail verdict for that case.

    uv run python -m evals.calibration.run --llm my_module:make_llm
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

if __package__ in (None, ""):  # executed as a script
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evals import judge as judge_mod  # noqa: E402

CASES_PATH = Path(__file__).resolve().parent / "cases.yaml"
REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_CASES = judge_mod.CALIBRATION_MIN_CASES


class LabelError(Exception):
    """The label set cannot back a calibration; the message becomes the status reason."""


def _unique_key_loader() -> type:
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    def construct(loader: Any, node: Any, deep: bool = False) -> dict:
        seen: set = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=True)
            if key in seen:
                raise LabelError(f"duplicate YAML key: {key!r}")
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    Loader.construct_mapping = construct  # type: ignore[method-assign]
    return Loader


def _load_yaml(path: Path) -> Any:
    import yaml

    return yaml.load(path.read_text(encoding="utf-8"), Loader=_unique_key_loader())  # noqa: S506


def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    data = _load_yaml(path)
    cases = data["cases"] if isinstance(data, dict) else None
    if not isinstance(cases, list):
        raise LabelError("calibration cases file is malformed")
    return cases


def _valid_score(v: Any) -> int | None:
    if type(v) is not int or not 1 <= v <= 5:  # no bool, float or string
        return None
    return v


def owner_labels(cases: list[dict[str, Any]]) -> dict[str, int]:
    """Return {case_id: owner_score} or raise LabelError when the set is not usable."""
    ids = [c.get("id") if isinstance(c, dict) else None for c in cases]
    if len(cases) != EXPECTED_CASES or len(set(ids)) != EXPECTED_CASES:
        raise LabelError(
            f"need exactly {EXPECTED_CASES} unique cases, found {len(cases)} "
            f"({len(set(ids))} unique ids)"
        )
    for c in cases:
        if not isinstance(c, dict) or not isinstance(c.get("id"), str):
            raise LabelError("a case has no string id")
        for f in ("question", "candidate_answer"):
            if not isinstance(c.get(f), str) or not c[f].strip():
                raise LabelError(f"case {c['id']} has no {f}")
        if not isinstance(c.get("reference_note", ""), str):
            raise LabelError(f"case {c['id']} has a non-text reference_note")
    out: dict[str, int] = {}
    missing: list[str] = []
    for c in cases:
        score = _valid_score(c.get("owner_score"))
        if score is None:
            missing.append(c["id"])
        else:
            out[c["id"]] = score
    if missing:
        shown = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
        raise LabelError(
            f"owner labels missing or invalid for {len(missing)} of {EXPECTED_CASES} cases "
            f"({shown})"
        )
    return out


def judge_case(judge: judge_mod.Judge, case: dict[str, Any]) -> judge_mod.JudgeVerdict:
    return judge.grade(
        case["question"], case["candidate_answer"], context=case.get("reference_note", "")
    )


def measure_agreement(
    cases: list[dict[str, Any]],
    labels: dict[str, int],
    judge: judge_mod.Judge,
    min_score: int = judge_mod.PASS_SCORE,
) -> tuple[float, list[dict[str, Any]]]:
    """One judge call per case (bounded by len(cases)); pass/fail agreement with the owner."""
    rows: list[dict[str, Any]] = []
    agree = 0
    for case in cases[:EXPECTED_CASES]:
        verdict = judge_case(judge, case)
        owner_pass = labels[case["id"]] >= min_score
        judge_pass = verdict.passed(min_score)
        agree += owner_pass == judge_pass
        rows.append(
            {
                "id": case["id"], "owner_score": labels[case["id"]],
                "judge_score": verdict.score, "agree": owner_pass == judge_pass,
                "error": verdict.error,
            }
        )  # fmt: skip
    return (agree / len(rows) if rows else 0.0), rows


def calibrate(
    cases: list[dict[str, Any]],
    labels: dict[str, int],
    llm_call: Callable[[str, str], str],
    judge_model: str,
) -> tuple[judge_mod.CalibrationStatus, list[dict[str, Any]]]:
    agreement, rows = measure_agreement(cases, labels, judge_mod.Judge(llm_call, judge_model))
    status = judge_mod.evaluate_agreement(
        agreement,
        len(rows),
        judge_model,
        inputs_hash=judge_mod.inputs_hash(cases, labels, judge_model),
        judge_scores={r["id"]: r["judge_score"] for r in rows},
    )
    return status, rows


def uncalibrated(reason: str, judge_model: str | None = None) -> judge_mod.CalibrationStatus:
    return judge_mod.CalibrationStatus(
        calibrated=False,
        judge_model=judge_model,
        rubric_version=judge_mod.RUBRIC_VERSION,
        reason=reason,
    )


def main(
    argv: list[str] | None = None,
    *,
    llm_call: Callable[[str, str], str] | None = None,
    out: TextIO | None = None,
) -> int:
    """Exit 0 calibrated, 1 uncalibrated (gate not met or labels missing), 2 refused."""
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="evals.calibration.run", description=__doc__.split("\n\n")[0])
    ap.add_argument("--cases", type=Path, default=CASES_PATH)
    ap.add_argument("--status-file", type=Path, default=judge_mod.DEFAULT_STATUS_PATH)
    ap.add_argument("--models-yaml", type=Path, default=None)
    ap.add_argument("--llm", help="module:factory returning llm_call(model, prompt) -> str")
    args = ap.parse_args(argv)

    try:
        judge_model = judge_mod.judge_model_from_models_yaml(args.models_yaml)
        cases = load_cases(args.cases)
    except Exception as exc:
        print(f"error: {exc}", file=out)
        return 2

    try:
        labels = owner_labels(cases)
    except Exception as exc:  # LabelError or a malformed case
        reason = (
            str(exc) if isinstance(exc, LabelError) else f"cases malformed: {type(exc).__name__}"
        )
        judge_mod.write_status(uncalibrated(reason, judge_model), args.status_file)
        print(f"Judge UNCALIBRATED: {reason}. No judge call made.", file=out)
        return 1

    if llm_call is None and not args.llm:
        print("error: labels are complete but no judge LLM was given (--llm)", file=out)
        return 2
    # A record from an earlier run must not survive a run that dies half way.
    judge_mod.write_status(uncalibrated("calibration run started", judge_model), args.status_file)
    if llm_call is None:
        try:
            mod, _, fn = args.llm.partition(":")
            llm_call = getattr(importlib.import_module(mod), fn)()
        except Exception as exc:
            reason = f"judge LLM factory failed: {type(exc).__name__}"
            judge_mod.write_status(uncalibrated(reason, judge_model), args.status_file)
            print(f"error: {reason}", file=out)
            return 2

    try:
        status, rows = calibrate(cases, labels, llm_call, judge_model)
    except Exception as exc:
        reason = f"calibration run failed: {type(exc).__name__}"
        judge_mod.write_status(uncalibrated(reason, judge_model), args.status_file)
        print(f"error: {reason}", file=out)
        return 2
    judge_mod.write_status(status, args.status_file)
    disagreeing = [r["id"] for r in rows if not r["agree"]]
    print(
        f"Judge {judge_model}, rubric {judge_mod.RUBRIC_VERSION}: agreement "
        f"{status.agreement:.0%} on {status.n_cases} cases "
        f"(need >= {judge_mod.CALIBRATION_MIN_AGREEMENT:.0%}): "
        + ("CALIBRATED" if status.calibrated else "UNCALIBRATED"),
        file=out,
    )
    if disagreeing:
        print("Disagreements: " + ", ".join(disagreeing), file=out)
    detail_dir = args.status_file.resolve().parent
    if not detail_dir.is_relative_to(REPO_ROOT):  # never write run detail into the repo
        (detail_dir / "last_run.json").write_text(
            json.dumps({"cases": rows}, indent=2) + "\n", encoding="utf-8"
        )
    return 0 if status.calibrated else 1


if __name__ == "__main__":
    raise SystemExit(main())
