"""Typed-PII gate (iteration 8b). PROVISIONAL (SEC-20): thresholds and the case set
are owner-reviewable; the fixture is synthetic and small (64 cases).

Runs the real detector (local spaCy model, no network) over
``evals/cases/adversarial/pii_typed`` and enforces in code:
recall >= 95% on positives and 0 false positives on brand cases. Recall is also
measured strictly: a positive counts only if every ``must_not_contain`` value is absent
from the masked text (a partial mask that leaves a surname is a miss). It also checks that
each case's recorded ``fake.detected`` matches the real verdict, so the offline eval
run (``evals/run.py --offline``) reports what the detector really does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from evals.gates import PII_RECALL_MIN, PII_TYPED, CaseResult, evaluate_gates
from evals.run import load_cases

from opsfleet_agent.guards.pii import PiiDetector, build_allowlist

PROVISIONAL = "SEC-20"  # owner sign-off pending on thresholds and case set

CASES_DIR = Path(__file__).resolve().parents[2] / "evals" / "cases" / "adversarial" / "pii_typed"
EXPECTED_CASES = 64


def _allowlist_spec() -> dict:
    data = yaml.safe_load((CASES_DIR / "brand_false_positive.yaml").read_text(encoding="utf-8"))
    return data["allowlist"]


@pytest.fixture(scope="module")
def gate_detector() -> PiiDetector:
    return PiiDetector(build_allowlist(**_allowlist_spec()))


@pytest.fixture(scope="module")
def verdicts(gate_detector: PiiDetector) -> list[tuple]:
    cases = [c for c in load_cases() if c.id.startswith(PII_TYPED + "/")]
    out = []
    for c in cases:
        result = gate_detector.mask(c.turns[0])
        leaked = [
            v for v in c.expect.get("must_not_contain", []) if v.lower() in result.text.lower()
        ]
        out.append((c, bool(result.findings), leaked))
    return out


def test_case_count_and_parse(verdicts: list[tuple]) -> None:
    assert len(verdicts) == EXPECTED_CASES
    for c, *_ in verdicts:
        assert "detect" in c.expect and isinstance(c.expect["detect"], bool), c.id
        assert c.fake is not None and "detected" in c.fake, c.id
        assert len(c.turns) == 1, c.id
    brand = [c for c, *_ in verdicts if "brand" in c.tags]
    assert len(brand) >= 8
    assert all(c.expect["detect"] is False for c in brand)


def test_recorded_fakes_match_detector(verdicts: list[tuple]) -> None:
    stale = [c.id for c, got, _ in verdicts if bool(c.fake["detected"]) != got]
    assert not stale, f"fake.detected differs from the real detector: {stale}"


def test_recall_on_positives(verdicts: list[tuple]) -> None:
    pos = [(c, got) for c, got, _ in verdicts if c.expect["detect"]]
    missed = [c.id for c, got in pos if not got]
    recall = (len(pos) - len(missed)) / len(pos)
    assert recall >= PII_RECALL_MIN, f"recall {recall:.0%}; missed: {missed}"


def test_zero_brand_false_positives(verdicts: list[tuple]) -> None:
    fps = [c.id for c, got, _ in verdicts if "brand" in c.tags and got]
    assert fps == []


def test_no_false_positives_on_negatives(verdicts: list[tuple]) -> None:
    fps = [c.id for c, got, _ in verdicts if not c.expect["detect"] and got]
    assert fps == []


def test_eval_gates_pass(verdicts: list[tuple]) -> None:
    results = [
        CaseResult(
            case_id=c.id,
            suite=c.suite,
            tags=list(c.tags),
            status="pass" if got == c.expect["detect"] else "fail",
            pii={"expected": c.expect["detect"], "detected": got, "brand": "brand" in c.tags},
        )
        for c, got, _ in verdicts
    ]
    gates = {g.name: g for g in evaluate_gates(results, judge_calibrated=True)}
    assert gates["pii_typed recall"].passed, gates["pii_typed recall"].detail
    assert gates["pii_typed brand false positives"].passed


def test_every_positive_lists_must_not_contain(verdicts: list[tuple]) -> None:
    missing = [
        c.id for c, *_ in verdicts if c.expect["detect"] and not c.expect.get("must_not_contain")
    ]
    assert missing == []


def test_strict_recall_no_value_survives(verdicts: list[tuple]) -> None:
    pos = [(c, leaked) for c, _, leaked in verdicts if c.expect["detect"]]
    misses = [(c.id, leaked) for c, leaked in pos if leaked]
    recall = (len(pos) - len(misses)) / len(pos)
    assert recall >= PII_RECALL_MIN, f"strict recall {recall:.0%}; leaked: {misses}"
    assert misses == [], f"values left in masked text: {misses}"
