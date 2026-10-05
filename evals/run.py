"""Eval runner (HLD section 6.6, FR-65).

    uv run python evals/run.py [--suite S ...] [--case ID ...] [--yes] [--offline]

Loads YAML cases, prints a request estimate BEFORE any call, refuses an over-budget live run,
writes `evals/results/<timestamp>/` (one JSON per case plus summary.json, each linked to its
trace) and exits 0 (gates pass), 1 (a gate failed) or 2 (refused or usage error).

The agent graph is injected as a `Harness` (`--sut module:factory`); `--offline` uses the
`fake:` block recorded in each case, so CI needs no network and no agent.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

if __package__ in (None, ""):  # executed as `python evals/run.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import gates as gates_mod  # noqa: E402
from evals import judge as judge_mod  # noqa: E402
from opsfleet_agent.config import default_models_path, parse_models_yaml  # noqa: E402
from opsfleet_agent.obs.tracer import Tracer, scrub_text  # noqa: E402

EVALS_DIR = Path(__file__).resolve().parent
CASES_DIR = EVALS_DIR / "cases"
RESULTS_DIR = EVALS_DIR / "results"
DAILY_CEILING_FRACTION = 0.75  # plan section 5: leave 25% of the daily quota for development
EXIT_OK, EXIT_GATE, EXIT_REFUSED = 0, 1, 2

EXPECT_KEYS = {
    "outcome", "label", "no_sql", "must_contain", "must_not_contain", "must_not_call",
    "max_llm_calls", "numbers", "judge", "detect",
}  # fmt: skip
CASE_KEYS = {
    "id",
    "suite",
    "tags",
    "input",
    "turns",
    "session",
    "expect",
    "estimate",
    "skip",
    "fake",
}

# Default request estimate per case when it declares none: {role: calls}, bq queries.
DEFAULT_ESTIMATES: dict[str, tuple[dict[str, int], int]] = {
    "deep": ({"deep_analyst": 3, "report_verifier": 1}, 1),
    "report": ({"report_writer": 3, "report_verifier": 1}, 1),
    "quick": ({"quick_analyst": 2, "report_verifier": 1}, 1),
}


class CaseError(Exception):
    pass


@dataclass
class Case:
    id: str
    suite: str
    tags: list[str] = field(default_factory=list)
    turns: list[str] = field(default_factory=list)
    session: dict[str, Any] = field(default_factory=dict)
    expect: dict[str, Any] = field(default_factory=dict)
    estimate: dict[str, Any] | None = None
    skip: str | None = None
    fake: dict[str, Any] | None = None


@dataclass
class SutResult:
    """What the system under test returns for one case."""

    outcome: str | None = None  # e.g. answered, refused, clarify, degraded
    text: str = ""
    label: str | None = None  # router label
    detected: bool | None = None  # typed-PII detector verdict
    sql: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    facts: dict[str, float] = field(default_factory=dict)  # named numbers for grounding
    llm_calls: dict[str, int] = field(default_factory=dict)  # per model id
    bq_queries: int = 0
    bq_bytes: int = 0
    trace_id: str | None = None
    trace_path: str | None = None
    judge_score: int | None = None  # recorded score, offline only

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SutResult:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class RunContext:
    session_id: str  # use as the Tracer session id so the case links to its trace
    trace_dir: Path
    offline: bool


Sut = Callable[[Case, RunContext], SutResult]


@dataclass
class Harness:
    sut: Sut
    judge_call: Callable[[str, str], str] | None = None  # (model, prompt) -> text
    reference: Callable[[str], float] | None = None  # reference SQL -> number


def recorded_harness() -> Harness:
    def sut(case: Case, ctx: RunContext) -> SutResult:
        if case.fake is None:
            raise CaseError("no recorded `fake` block for an offline run")
        return SutResult.from_dict(case.fake)

    return Harness(sut=sut)


# --------------------------------------------------------------------------- loading


def _as_turns(raw: dict[str, Any], where: str) -> list[str]:
    if "turns" in raw:
        out = [t["user"] if isinstance(t, dict) else t for t in raw["turns"]]
    elif "input" in raw:
        out = [raw["input"]]
    else:
        raise CaseError(f"{where}: needs `input` or `turns`")
    if not out or not all(isinstance(t, str) and t for t in out):
        raise CaseError(f"{where}: turns must be non-empty strings")
    return out


def _build_case(raw: dict[str, Any], case_id: str, suite: str, base_tags: list[str]) -> Case:
    unknown = set(raw) - CASE_KEYS
    if unknown:
        raise CaseError(f"{case_id}: unknown keys {sorted(unknown)}")
    expect = raw.get("expect") or {}
    bad = set(expect) - EXPECT_KEYS
    if bad:
        raise CaseError(f"{case_id}: unknown expect keys {sorted(bad)}")
    return Case(
        id=case_id,
        suite=suite,
        tags=sorted({*base_tags, *raw.get("tags", [])}),
        turns=_as_turns(raw, case_id),
        session=raw.get("session") or {},
        expect=expect,
        estimate=raw.get("estimate"),
        skip=raw.get("skip"),
        fake=raw.get("fake"),
    )


def load_cases(root: Path = CASES_DIR) -> list[Case]:
    """One case per file, or many under a top-level `cases:` list (router/labelled.yaml).

    Ids come from the path under `root`; directories starting with `_` are skipped.
    """
    import yaml

    cases: list[Case] = []
    for path in sorted(root.rglob("*.yaml")):
        rel = path.relative_to(root)
        if any(p.startswith("_") for p in rel.parts):
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise CaseError(f"{rel}: unreadable YAML ({type(exc).__name__})") from None
        if not isinstance(raw, dict):
            raise CaseError(f"{rel}: expected a mapping")
        stem = rel.with_suffix("").as_posix()
        suite = rel.parent.as_posix()
        if "cases" in raw:
            tags = list(raw.get("tags", []))
            for i, item in enumerate(raw["cases"]):
                cid = f"{stem}/{item.get('id', i)}"
                cases.append(_build_case(item, cid, stem, tags))
        else:
            cases.append(_build_case(raw, raw.get("id", stem), raw.get("suite", suite), []))
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise CaseError("duplicate case ids")
    return cases


def select_cases(cases: list[Case], suites: list[str], ids: list[str]) -> list[Case]:
    def match(c: Case) -> bool:
        if ids and c.id in ids:
            return True
        if suites and any(c.id == s or c.id.startswith(s.rstrip("/") + "/") for s in suites):
            return True
        return not ids and not suites

    return [c for c in cases if match(c)]


# --------------------------------------------------------------------------- estimator


@dataclass
class Estimate:
    llm: dict[str, int] = field(default_factory=dict)  # per model id
    bq_queries: int = 0


def case_estimate(case: Case, roles: dict[str, str]) -> tuple[dict[str, int], int]:
    """Per-model LLM calls and BQ queries for one case. `roles` maps role -> model id."""
    if case.estimate is not None:
        llm_decl = dict(case.estimate.get("llm", {}))
        bq = int(case.estimate.get("bq", 0))
    elif case.suite.startswith(("resilience", "adversarial/pii_typed")):
        llm_decl, bq = {}, 0
    elif case.suite.startswith("router"):
        llm_decl, bq = {"router": 1}, 0
    else:
        tag = next((t for t in ("report", "deep", "quick") if t in case.tags), None)
        if tag is None and case.suite.startswith("golden"):
            tag = "quick"
        llm_decl, bq = DEFAULT_ESTIMATES[tag] if tag else ({"quick_analyst": 1}, 0)
        llm_decl = dict(llm_decl)
    if case.expect.get("judge") is not None:
        llm_decl["judge"] = llm_decl.get("judge", 0) + 1
    per_model: dict[str, int] = {}
    for key, n in llm_decl.items():
        model = roles.get(key, key)  # a role name, else a literal model id
        per_model[model] = per_model.get(model, 0) + int(n)
    return per_model, bq


def estimate_requests(cases: list[Case], roles: dict[str, str]) -> Estimate:
    est = Estimate()
    for c in cases:
        if c.skip:
            continue
        per_model, bq = case_estimate(c, roles)
        for m, n in per_model.items():
            est.llm[m] = est.llm.get(m, 0) + n
        est.bq_queries += bq
    return est


def usage_path(results_root: Path) -> Path:
    return results_root / "usage.json"


def _quota_day(now: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        return now.astimezone(UTC).date().isoformat()


def load_usage(results_root: Path, now: datetime) -> dict[str, int]:
    """Eval-run requests already spent today (development calls are not tracked)."""
    try:
        data = json.loads(usage_path(results_root).read_text(encoding="utf-8"))
        if data.get("day") == _quota_day(now):
            return {k: int(v) for k, v in data["models"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return {}


def save_usage(results_root: Path, now: datetime, used: dict[str, int]) -> None:
    usage_path(results_root).write_text(
        json.dumps({"day": _quota_day(now), "models": used}, indent=2), encoding="utf-8"
    )


def remaining_budget(model: str, rpd: int, used: dict[str, int]) -> int:
    return max(0, int(rpd * DAILY_CEILING_FRACTION) - used.get(model, 0))


def over_budget(est: Estimate, rpd: dict[str, int], used: dict[str, int]) -> list[str]:
    out = []
    for model, n in sorted(est.llm.items()):
        if model not in rpd:
            out.append(f"{model}: no limit in models.yaml")
        elif n > remaining_budget(model, rpd[model], used):
            out.append(f"{model}: needs {n}, {remaining_budget(model, rpd[model], used)} left")
    return out


def print_estimate(
    est: Estimate, rpd: dict[str, int], used: dict[str, int], out: TextIO, n_cases: int
) -> None:
    print(f"Request estimate for {n_cases} case(s):", file=out)
    for model, n in sorted(est.llm.items()):
        left = remaining_budget(model, rpd[model], used) if model in rpd else "?"
        print(f"  LLM  {model}: {n} request(s); {left} left of today's ceiling", file=out)
    if not est.llm:
        print("  LLM  none", file=out)
    print(f"  BQ   {est.bq_queries} query(ies) (each dry-run and byte-capped)", file=out)


# --------------------------------------------------------------------------- checks


def check_numbers(
    case: Case, res: SutResult, reference: Callable[[str], float] | None
) -> list[tuple[str, bool, str]]:
    """Hook: numbers are compared by code, never by the judge.

    Each item: {name, value | reference_sql, tolerance_pct (default 0.5)}. `res.facts[name]` is the
    figure the agent reported. A `reference_sql` needs `Harness.reference`.
    """
    out = []
    for item in case.expect.get("numbers", []):
        name = item["name"]
        tol = float(item.get("tolerance_pct", 0.5)) / 100
        if "value" in item:
            expected = float(item["value"])
        elif "reference_sql" in item and reference is not None:
            expected = float(reference(item["reference_sql"]))
        else:
            out.append((f"number:{name}", False, "no reference value or reference hook"))
            continue
        got = res.facts.get(name)
        ok = got is not None and abs(got - expected) <= abs(expected) * tol
        out.append((f"number:{name}", ok, "" if ok else f"expected {expected}, got {got}"))
    return out


def check_expect(
    case: Case, res: SutResult, reference: Callable[[str], float] | None
) -> list[tuple[str, bool, str]]:
    e = case.expect
    out: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, why: str) -> None:
        out.append((name, ok, "" if ok else why))

    if "outcome" in e:
        add("outcome", res.outcome == e["outcome"], f"expected {e['outcome']}, got {res.outcome}")
    if "label" in e:
        add("label", res.label == e["label"], f"expected {e['label']}, got {res.label}")
    if e.get("no_sql"):
        add("no_sql", not res.sql, f"{len(res.sql)} SQL statement(s) were run")
    text = res.text.lower()
    for s in e.get("must_contain", []):
        add(f"must_contain:{s}", s.lower() in text, "text missing")
    for s in e.get("must_not_contain", []):
        add(f"must_not_contain:{s}", s.lower() not in text, "forbidden text present")
    for t in e.get("must_not_call", []):
        add(f"must_not_call:{t}", t not in res.tools, "forbidden tool was called")
    if "max_llm_calls" in e:
        total = sum(res.llm_calls.values())
        add("max_llm_calls", total <= int(e["max_llm_calls"]), f"{total} calls > cap")
    if "detect" in e:
        add(
            "detect",
            res.detected is not None and res.detected == bool(e["detect"]),
            f"expected detected={e['detect']}, got {res.detected}",
        )
    out.extend(check_numbers(case, res, reference))
    return out


# --------------------------------------------------------------------------- running


def session_id_for(case_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", case_id)[:40]
    return f"ev-{hashlib.sha1(case_id.encode()).hexdigest()[:8]}-{safe}"


def _file_name(case_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "__", case_id) + ".json"


def run_case(
    case: Case,
    harness: Harness,
    *,
    trace_dir: Path,
    offline: bool,
    judge_model: str,
    judge_counts: bool,
) -> tuple[gates_mod.CaseResult, dict[str, Any], SutResult | None]:
    sid = session_id_for(case.id)
    started = time.monotonic()
    record: dict[str, Any] = {
        "case_id": case.id,
        "suite": case.suite,
        "tags": case.tags,
        "trace_id": sid,
        "trace_path": str(Tracer(trace_dir, sid).path),
    }
    cr = gates_mod.CaseResult(
        case.id, case.suite, case.tags, judged=case.expect.get("judge") is not None
    )
    if case.skip:
        cr.status, cr.reasons = "na", [f"not applicable: {case.skip}"]
        record.update(status="na", reasons=cr.reasons)
        return cr, record, None
    checks: list[tuple[str, bool, str]] = []
    res: SutResult | None = None
    try:
        res = harness.sut(case, RunContext(sid, trace_dir, offline))
    except CaseError as exc:
        checks.append(("run", False, str(exc)))
    except Exception as exc:
        checks.append(("run", False, f"system under test raised {type(exc).__name__}"))
    if res is not None:
        if res.trace_id:
            record["trace_id"] = res.trace_id
        if res.trace_path:
            record["trace_path"] = res.trace_path
        record["trace_exists"] = Path(record["trace_path"]).exists()
        checks.extend(check_expect(case, res, harness.reference))
        if "detect" in case.expect:
            cr.pii = {
                "expected": bool(case.expect["detect"]),
                "detected": bool(res.detected),
                "brand": "brand" in case.tags,
            }
        if "label" in case.expect:
            cr.router = {"expected": case.expect["label"], "got": res.label}
        if cr.judged:
            _judge(case, res, harness, offline, judge_model, judge_counts, checks, record)
        record.update(
            outcome=res.outcome,
            answer=scrub_text(res.text, 2000),
            llm_calls=res.llm_calls,
            bq_queries=res.bq_queries,
            bq_bytes=res.bq_bytes,
        )
    failed = [(n, why) for n, ok, why in checks if not ok]
    cr.status = "fail" if failed else "pass"
    cr.reasons = [f"{n}: {why}" for n, why in failed]
    record.update(
        status=cr.status,
        reasons=cr.reasons,
        checks=[{"name": n, "ok": ok, "reason": why} for n, ok, why in checks],
        duration_s=round(time.monotonic() - started, 3),
    )
    return cr, record, res


def _judge(case, res, harness, offline, judge_model, judge_counts, checks, record) -> None:
    spec = case.expect["judge"] if isinstance(case.expect["judge"], dict) else {}
    min_score = int(spec.get("min_score", judge_mod.PASS_SCORE))
    if offline:
        verdict = judge_mod.JudgeVerdict(
            res.judge_score, "recorded", judge_model, judge_mod.RUBRIC_VERSION,
            None if res.judge_score is not None else "no recorded judge_score",
        )  # fmt: skip
    elif harness.judge_call is None:
        verdict = judge_mod.JudgeVerdict(
            None, "", judge_model, judge_mod.RUBRIC_VERSION, "no judge available"
        )
    else:
        verdict = judge_mod.Judge(harness.judge_call, judge_model).grade(
            case.turns[-1], res.text, kind=spec.get("kind", "golden")
        )
    record["judge"] = {
        **asdict(verdict),
        "counted": judge_counts,
        "calibration": "calibrated" if judge_counts else "uncalibrated",
    }
    if judge_counts:  # an uncalibrated judge contributes no pass or fail
        checks.append(
            ("judge", verdict.passed(min_score), verdict.error or f"score {verdict.score}")
        )


def pace_seconds(
    calls: dict[str, int], rpm: dict[str, int], fraction: float, elapsed: float
) -> float:
    """Sleep needed so this case's calls stay under fraction x RPM per model."""
    need = max((n * 60 / (rpm[m] * fraction) for m, n in calls.items() if rpm.get(m)), default=0.0)
    return max(0.0, need - elapsed)


def _rates(results: list[gates_mod.CaseResult]) -> dict[str, dict[str, Any]]:
    by: dict[str, list[gates_mod.CaseResult]] = {}
    for r in results:
        by.setdefault(r.suite, []).append(r)
    out = {}
    for suite, rs in sorted(by.items()):
        ok, n = gates_mod._rate(rs)
        out[suite] = {
            "passed": ok, "counted": n, "rate": (ok / n if n else None),
            "na": sum(r.status == "na" for r in rs),
        }  # fmt: skip
    return out


def _calibration_binding(
    cases_path: Path | None, judge_model: str
) -> tuple[str | None, dict[str, int] | None]:
    """Current calibration inputs hash and owner labels; (None, None) if labels are unusable."""
    from evals.calibration import run as cal_run

    try:
        cases = cal_run.load_cases(cases_path or cal_run.CASES_PATH)
        labels = cal_run.owner_labels(cases)
        return judge_mod.inputs_hash(cases, labels, judge_model), labels
    except Exception:
        return None, None


def main(
    argv: list[str] | None = None,
    *,
    harness: Harness | None = None,
    out: TextIO | None = None,
    confirm: Callable[[str], str] = input,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="evals/run.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--suite", action="append", default=[], help="suite or id prefix; repeatable")
    ap.add_argument("--case", action="append", default=[], help="case id; repeatable")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--allow-over-budget", action="store_true", help="override a refused estimate")
    ap.add_argument("--offline", action="store_true", help="use recorded `fake` blocks, no calls")
    ap.add_argument("--sut", help="module:factory returning a Harness (live runs)")
    ap.add_argument("--cases-dir", type=Path, default=CASES_DIR)
    ap.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    ap.add_argument("--trace-dir", type=Path, default=Path("traces"))
    ap.add_argument("--models-yaml", type=Path, default=None)
    ap.add_argument("--calibration-file", type=Path, default=judge_mod.DEFAULT_STATUS_PATH)
    ap.add_argument("--calibration-cases", type=Path, default=None)
    args = ap.parse_args(argv)

    try:
        cases = select_cases(load_cases(args.cases_dir), args.suite, args.case)
        roles_cfg, _, _, limits, fraction = parse_models_yaml(
            args.models_yaml or default_models_path()
        )
    except Exception as exc:
        print(f"error: {exc}", file=out)
        return EXIT_REFUSED
    if not cases:
        print("error: no cases matched", file=out)
        return EXIT_REFUSED

    roles = {name: r.model for name, r in roles_cfg.items()}
    rpd = {m: lim.rpd for m, lim in limits.items()}
    rpm = {m: lim.rpm for m, lim in limits.items()}
    t0 = now()
    used = load_usage(args.results_dir, t0)
    est = Estimate() if args.offline else estimate_requests(cases, roles)
    if args.offline:
        print(f"Offline run of {len(cases)} case(s): 0 LLM requests, 0 BigQuery queries.", file=out)
    else:
        print_estimate(est, rpd, used, out, len(cases))
        problems = over_budget(est, rpd, used)
        if problems and not args.allow_over_budget:
            print("REFUSED: over budget (" + "; ".join(problems) + ").", file=out)
            print("Run a smaller subset, or pass --allow-over-budget.", file=out)
            return EXIT_REFUSED
        if harness is None and args.sut:
            mod, _, fn = args.sut.partition(":")
            harness = getattr(importlib.import_module(mod), fn)()
        if harness is None:
            print("error: a live run needs --sut module:factory (use --offline in CI).", file=out)
            return EXIT_REFUSED
        if not args.yes and confirm("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.", file=out)
            return EXIT_REFUSED
    if args.offline:
        harness = harness or recorded_harness()

    judge_model = judge_mod.judge_model_from_models_yaml(args.models_yaml)
    cal = judge_mod.load_status(args.calibration_file)
    bound_hash, bound_labels = _calibration_binding(args.calibration_cases, judge_model)
    judge_counts = cal.counts_for(judge_model, inputs_hash=bound_hash, labels=bound_labels)
    if args.calibration_cases is not None:
        print(
            f"WARNING: calibration cases from {args.calibration_cases}, not the shipped set",
            file=out,
        )
    cal_reason = cal.reason or (
        "record does not match the current labels, cases, rubric or judge model"
    )

    run_dir = args.results_dir / t0.strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    results: list[gates_mod.CaseResult] = []
    totals: dict[str, Any] = {"llm_requests": {}, "bq_queries": 0, "bq_bytes": 0}
    for case in cases:
        t_case = time.monotonic()
        cr, record, res = run_case(
            case, harness, trace_dir=args.trace_dir, offline=args.offline,
            judge_model=judge_model, judge_counts=judge_counts,
        )  # fmt: skip
        results.append(cr)
        calls = dict(res.llm_calls) if res else {}
        if res is not None:
            for m, n in calls.items():
                totals["llm_requests"][m] = totals["llm_requests"].get(m, 0) + n
            totals["bq_queries"] += res.bq_queries
            totals["bq_bytes"] += res.bq_bytes
        (run_dir / _file_name(case.id)).write_text(
            json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8"
        )
        tag = {"pass": "PASS", "fail": "FAIL", "na": "N/A "}[cr.status]
        print(
            f"[{tag}] {case.id}" + (f"  ({'; '.join(cr.reasons)})" if cr.reasons else ""), file=out
        )
        if not args.offline and res is not None:
            wait = pace_seconds(calls, rpm, fraction, time.monotonic() - t_case)
            if wait:
                sleep(wait)

    gate_results = gates_mod.evaluate_gates(
        results, judge_calibrated=judge_counts, judge_reason=cal_reason
    )
    router = gates_mod.router_report(results)
    passed = gates_mod.run_passed(gate_results)

    print("\nPass rate per suite:", file=out)
    for suite, r in _rates(results).items():
        pct = f"{r['rate']:.0%}" if r["rate"] is not None else "n/a"
        print(f"  {suite}: {r['passed']}/{r['counted']} ({pct}), {r['na']} n/a", file=out)
    if router:
        extra = (
            f", injection recall {router['injection_recall']:.0%}" if router["injection_n"] else ""
        )
        print(
            f"Router set (no gate): accuracy {router['accuracy']:.0%} on {router['n']}{extra}",
            file=out,
        )
    print(f"Totals: LLM requests {totals['llm_requests']}, BQ queries {totals['bq_queries']}, "
          f"BQ bytes {totals['bq_bytes']}", file=out)  # fmt: skip
    if any(r.judged for r in results):
        state = "calibrated" if judge_counts else f"UNCALIBRATED ({cal_reason})"
        print(f"Judge {judge_model}, rubric {judge_mod.RUBRIC_VERSION}: {state}", file=out)
    print("Gates:", file=out)
    for g in gate_results:
        mark = "skip" if not g.applicable else ("ok  " if g.passed else "FAIL")
        print(f"  [{mark}] {g.name}: {g.detail}", file=out)
    print(f"Results: {run_dir}", file=out)
    print("RESULT: " + ("PASS" if passed else "FAIL"), file=out)

    summary = {
        "run_id": run_dir.name,
        "offline": args.offline,
        "cases": len(results),
        "totals": totals,
        "suites": _rates(results),
        "router": router,
        "judge": {
            "model": judge_model,
            "rubric_version": judge_mod.RUBRIC_VERSION,
            "calibrated": judge_counts,
            "reason": "" if judge_counts else cal_reason,
            "calibration_cases": str(args.calibration_cases or "shipped"),
            "inputs_hash": bound_hash,
            "record_timestamp": cal.timestamp,
        },  # fmt: skip
        "gates": [asdict(g) for g in gate_results],
        "passed": passed,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if not args.offline:
        for m, n in totals["llm_requests"].items():
            used[m] = used.get(m, 0) + n
        save_usage(args.results_dir, t0, used)
    return EXIT_OK if passed else EXIT_GATE


if __name__ == "__main__":
    sys.exit(main())
