"""Loader test for the judge calibration draft (iteration 20)."""

import re
from collections import Counter
from pathlib import Path

import yaml

CASES_PATH = Path(__file__).resolve().parents[2] / "evals" / "calibration" / "cases.yaml"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
DIGIT_RUN = re.compile(r"\d{7,}")


def _cases() -> list[dict]:
    return yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))["cases"]


def test_thirty_cases_unique_ids() -> None:
    cases = _cases()
    assert len(cases) == 30
    assert len({c["id"] for c in cases}) == 30
    for c in cases:
        assert c["question"] and c["candidate_answer"]


def test_kinds_balanced() -> None:
    counts = Counter(c["kind"] for c in _cases())
    assert set(counts) == {"good", "partial", "wrong"}
    assert all(8 <= n <= 12 for n in counts.values()), counts


def test_owner_fields_empty() -> None:
    for c in _cases():
        assert c["owner_score"] in (None, "")
        assert c["owner_reason"] in (None, "")


def test_no_pii_patterns() -> None:
    for c in _cases():
        text = " ".join(str(v) for v in c.values() if v is not None)
        assert not EMAIL.search(text), c["id"]
        assert not PHONE.search(text), c["id"]
        assert not DIGIT_RUN.search(text), c["id"]


def test_no_entity_detected_by_typed_pii_guard() -> None:
    from opsfleet_agent.guards.pii import PiiDetector, build_allowlist

    detector = PiiDetector(build_allowlist())
    for c in _cases():
        for field in ("question", "candidate_answer", "reference_note"):
            assert detector.detect(c[field]) == [], (c["id"], field)
