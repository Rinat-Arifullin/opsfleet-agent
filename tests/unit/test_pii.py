"""Typed-PII detector (iteration 8b). Local spaCy model only; no network.

All people, streets and brands here are synthetic.
"""

from __future__ import annotations

import dataclasses
import random
import threading
import uuid
from pathlib import Path

import pytest
import yaml

from opsfleet_agent.guards import pii, pii_regex
from opsfleet_agent.guards.pii import (
    ADDRESS,
    PERSON,
    Finding,
    MaskResult,
    PiiDetector,
    PiiDetectorError,
    PiiModelMissing,
    build_allowlist,
    ensure_model_available,
    scrub_output,
    set_default_detector,
)

CALIBRATION = Path(__file__).resolve().parents[2] / "evals" / "calibration" / "cases.yaml"

BRANDS = (
    "Quillon Barrowmere",
    "Pemberton Lacey",
    "Fenwick & Bramble",
    "Mortlake",
    "Ellery Vane",
    "Dr. Pimbleton",
    "Corvina Ashby",
)
CATEGORIES = ("Jeans", "Outerwear & Coats", "Sleep & Lounge", "Accessories")
DEPARTMENTS = ("Men", "Women")


@pytest.fixture(scope="session")
def detector() -> PiiDetector:
    return PiiDetector(build_allowlist(BRANDS, CATEGORIES, DEPARTMENTS))


@pytest.fixture(scope="session")
def bare_detector() -> PiiDetector:
    return PiiDetector()


# --- named tests ---------------------------------------------------------------------


def test_pii_detector_missing_model_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise OSError("[E050] Can't find model")

    monkeypatch.setattr(pii, "_nlp", None)
    monkeypatch.setattr(pii, "_load_spacy_model", boom)
    with pytest.raises(PiiModelMissing) as exc:
        ensure_model_available()
    assert pii.SPACY_MODEL in str(exc.value)
    assert "pip install" in str(exc.value)
    # No regex-only fallback: constructing a detector also fails.
    with pytest.raises(PiiModelMissing):
        PiiDetector()


def test_pii_detector_other_load_error_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise ValueError("corrupt")

    monkeypatch.setattr(pii, "_nlp", None)
    monkeypatch.setattr(pii, "_load_spacy_model", boom)
    with pytest.raises(PiiDetectorError):
        ensure_model_available()


def test_brand_allowlist_not_masked(detector: PiiDetector, bare_detector: PiiDetector) -> None:
    text = "Pemberton Lacey sold 1,840 units; Corvina Ashby's jackets led in March."
    assert bare_detector.detect(text), "precondition: these brands look like people"
    r = detector.mask(text)
    assert r.text == text and not r.findings
    for t in (
        "QUILLON BARROWMERE revenue",
        "pemberton lacey vs Ellery Vane",
        "Ellery Vane revenue was flat.",
        "Fenwick & Bramble grew 8%.",
        "| brand | revenue |\n|---|---|\n| Corvina Ashby | 39,004.10 |",
        "Dr. Pimbleton boots grew 12%.",
    ):
        assert detector.detect(t) == [], t


def test_typed_pii_ner_masks_person_and_address(detector: PiiDetector) -> None:
    r = detector.mask("Zorbina Quandleworth lives at 742 Evergreen Terrace, Springfield.")
    assert "Zorbina" not in r.text and "Quandleworth" not in r.text
    assert "742" not in r.text and "Evergreen" not in r.text
    assert r.types() == {PERSON, ADDRESS}
    assert "<PERSON>" in r.text and "<ADDRESS>" in r.text


def test_output_guard_uses_ner_detector(detector: PiiDetector) -> None:
    text = "The top customer was Bartholomew Fizzlewick (zed@example.com)."
    explicit = scrub_output(text, detector)
    assert explicit.text == "The top customer was <PERSON> (<EMAIL>)."
    assert PERSON in explicit.types()  # regex alone would only find the e-mail
    assert pii_regex.scrub(text).text.count("Fizzlewick") == 1
    set_default_detector(detector)
    try:
        assert scrub_output(text).text == explicit.text
    finally:
        set_default_detector(None)


# --- red team ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,secret",
    [
        ("Please refund Bartholomew Fizzlewick today.", "Fizzlewick"),
        ("Please contact Zorbina\nQuandleworth about it.", "Quandleworth"),
        ("Name: Gertrude\nPlumbottom", "Plumbottom"),
        (
            "| first_name | last_name | orders |\n|---|---|---|\n| Ophelia | Wobblesworth | 3 |",
            "Wobblesworth",
        ),
        ("customer,orders\nMortimer Plinkett,4", "Plinkett"),
        ("my name is jebediah crumplehorn", "crumplehorn"),
        ("Dr. Wilhelmina Pfefferkorn asked about returns.", "Pfefferkorn"),
        ("Hey Thaddeus, the report is ready.", "Thaddeus"),
        ("Customer Barnaby Wigglesworth-Toot has 3 returns.", "Wigglesworth"),
        ("Address: 12 Nowhere Rd, Fakeville", "Nowhere"),
        ("1234 Imaginary Lane, Apt 5B, Faketown, ZZ 99999", "Imaginary"),
        ("Returns go to PO Box 4471.", "4471"),
    ],
)
def test_red_team_masked(detector: PiiDetector, text: str, secret: str) -> None:
    r = detector.mask(text)
    assert secret not in r.text
    assert r.redacted


def test_surname_like_brand_next_to_real_name(detector: PiiDetector) -> None:
    r = detector.mask("Thaddeus Grimblewick, a Mortlake loyalty member, complained.")
    assert "Grimblewick" not in r.text
    assert "Mortlake" in r.text


@pytest.mark.parametrize(
    "text",
    [
        "Revenue in France, Germany and Brazil grew in March 2024.",
        "Top cities: Tokyo, Paris, Sao Paulo, Chicago.",
        "Top categories: Jeans, Outerwear & Coats; Women outsold Men.",
        "Orders by status: Complete 41%, Shipped 30%, Processing 19%, Cancelled 6%, Returned 4%.",
        "Classic Denim Jacket sold 1,234 units at $59.99.",
        "Monthly revenue: January 120,450; February 131,020; June 140,200.",
        "Traffic source Facebook and Organic drove 62% of sessions.",
        "Average order value was 86.40 across 12,345 orders in Q3.",
    ],
)
def test_analytics_negatives_unmasked(bare_detector: PiiDetector, text: str) -> None:
    r = bare_detector.mask(text)
    assert r.findings == () and r.text == text


def test_calibration_set_has_no_detections(bare_detector: PiiDetector) -> None:
    data = yaml.safe_load(CALIBRATION.read_text(encoding="utf-8"))
    cases = data["cases"] if isinstance(data, dict) else data
    hits = []
    for c in cases:
        for field in ("question", "candidate_answer", "reference_note"):
            value = c.get(field)
            if isinstance(value, str):
                found = bare_detector.detect(value)
                if found:
                    hits.append((c["id"], field, sorted({f.type for f in found})))
    assert hits == []


# --- result shape, bounds, fail closed -------------------------------------------------


def test_findings_hold_types_and_spans_only(detector: PiiDetector) -> None:
    r = detector.mask("Email zed@example.com or call 555-123-4567, ask for Zorbina Quandleworth.")
    assert {f.type for f in r.findings} == {"EMAIL", "PHONE", PERSON}
    for f in r.findings:
        assert isinstance(f, Finding)
        assert {x.name for x in dataclasses.fields(f)} == {"type", "start", "end"}
        assert r.text[f.start : f.end] == f"<{f.type}>"
    assert detector.detect("nothing here") == []
    assert all(isinstance(x, Finding) for x in detector.detect("Hi Zorbina."))


def test_input_bounded_by_regex_limit(detector: PiiDetector) -> None:
    text = "x " * (pii_regex.MAX_SCRUB_CHARS // 2 + 100) + "Zorbina Quandleworth"
    r = detector.mask(text)
    assert r.truncated
    assert r.text.endswith(pii_regex.TRUNCATION_MARKER)
    assert "Quandleworth" not in r.text


@pytest.mark.parametrize("value", [None, b"Zorbina Quandleworth", 42, ["x"]])
def test_non_str_rejected(detector: PiiDetector, value: object) -> None:
    # Fail closed with the module's typed error on every entry point.
    with pytest.raises(PiiDetectorError):
        detector.mask(value)  # type: ignore[arg-type]
    with pytest.raises(PiiDetectorError):
        detector.detect(value)  # type: ignore[arg-type]
    with pytest.raises(PiiDetectorError):
        scrub_output(value, detector)  # type: ignore[arg-type]


def test_fails_closed_on_analyzer_error(monkeypatch: pytest.MonkeyPatch) -> None:
    d = PiiDetector()

    def broken(*_a, **_k):
        raise RuntimeError("Zorbina Quandleworth leaked in an exception message")

    monkeypatch.setattr(d._analyzer, "analyze", broken)
    with pytest.raises(PiiDetectorError) as exc:
        d.mask("Zorbina Quandleworth bought a coat.")
    assert "Quandleworth" not in str(exc.value)
    with pytest.raises(PiiDetectorError):
        scrub_output("Zorbina Quandleworth", d)


def test_concurrent_mask_is_consistent(detector: PiiDetector) -> None:
    text = "Refund Bartholomew Fizzlewick at 88 Gumdrop Avenue."
    expected = detector.mask(text)
    results: list[MaskResult] = []

    def run() -> None:
        results.append(detector.mask(text))

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [expected] * 8


# --- build_allowlist -----------------------------------------------------------------


def test_build_allowlist_is_pure_and_dedupes() -> None:
    brands = ["Pemberton Lacey", "PEMBERTON LACEY", "pemberton  lacey"]
    a = build_allowlist(brands)
    b = build_allowlist(brands)
    assert brands == ["Pemberton Lacey", "PEMBERTON LACEY", "pemberton  lacey"]
    assert len(a) == len(b) == len(build_allowlist()) + 1


def test_build_allowlist_whole_span_and_folding() -> None:
    al = build_allowlist(["Ellery Vane"])
    text = "ELLERY VANE grew"
    assert al.covers(text, 0, 11)
    assert not al.covers("Ellery Vanderhoof grew", 0, 17)
    assert al.covers("Ellery Vane's coats", 0, 13)


def test_build_allowlist_skips_junk_and_bounds() -> None:
    al = build_allowlist([None, 42, "", "  ", "x", "Mortlake"])  # type: ignore[list-item]
    assert len(al) == len(build_allowlist()) + 1
    with pytest.raises(ValueError):
        build_allowlist(["a" * (pii.MAX_TERM_CHARS + 1)])
    with pytest.raises(ValueError):
        build_allowlist([f"brand{i}" for i in range(pii.MAX_ALLOWLIST_TERMS + 1)])


def test_allowlist_exempts_person_only(detector: PiiDetector) -> None:
    # A brand allowlist never exempts structured PII or addresses.
    r = detector.mask("Mortlake support: mortlake@example.com, 12 Mortlake Road.")
    assert "<EMAIL>" in r.text
    assert "<ADDRESS>" in r.text


# --- iter 8b second review: regression probes ----------------------------------------

REVIEW_ALLOWLIST = ("Marlowe", "Finch", "Bramble", "Thistlewood")


@pytest.fixture(scope="session")
def review_detector() -> PiiDetector:
    return PiiDetector(build_allowlist(REVIEW_ALLOWLIST, pii.SCHEMA_TERMS))


@pytest.mark.parametrize(
    ("text", "values"),
    [
        # finding 3: allowlisted words combined into a name, or after a cue
        ("Name: Bramble Thistlewood", ("Bramble", "Thistlewood")),
        ("Hey Marlowe, your refund is ready", ("Marlowe",)),
        ("customer,orders\nMarlowe Finch,4", ("Marlowe", "Finch")),
        ("Thistlewood Marlowe Finch Bramble", ("Thistlewood", "Marlowe", "Finch", "Bramble")),
        ("my name is April May", ("April May",)),
        ("Hey June, your order shipped", ("June",)),
    ],
)
def test_review_allowlist_does_not_override_cues(
    review_detector: PiiDetector, text: str, values: tuple[str, ...]
) -> None:
    out = review_detector.mask(text).text
    for v in values:
        assert v not in out


@pytest.mark.parametrize(
    ("text", "value"),
    [
        # finding 2: a surname glued to a digit
        ("customer,orders\nMarlowe Finch,4", "Finch"),
        ("Mortimer Plinkett2 ordered", "Plinkett"),
        # finding 4: JSON, ALL-CAPS, lower-case, backticks
        ('{"name": "Zorbina Quandleworth", "orders": 3}', "Quandleworth"),
        ('{"customer":"Zorbina Quandleworth"}', "Quandleworth"),
        ("ZORBINA QUANDLEWORTH bought coats", "QUANDLEWORTH"),
        ("the buyer was zorbina quandleworth", "quandleworth"),
        ("`Zorbina Quandleworth`", "Quandleworth"),
        # finding 5: addresses
        ("lives at 742 evergreen terrace", "evergreen"),
        ("742 EVERGREEN TERRACE", "EVERGREEN"),
        ("742 Evergreen Terr.", "Evergreen"),
        ("10 Downing Street, London SW1A 2AA", "SW1A"),
        ("742 Evergreen Terrace, Springfield, IL 62704", "62704"),
    ],
)
def test_review_probes_masked(bare_detector: PiiDetector, text: str, value: str) -> None:
    assert value not in bare_detector.mask(text).text


@pytest.mark.parametrize(
    "text",
    [
        "We shipped 742 orders on the road to Q4.",
        '{"name": "Classic Denim Jacket"}',
        "User: what were the top brands?",
        "RETURNED SHARE IS ABOUT 10 PERCENT.",
        "weekend revenue is double, so run weekend promotions.",
        "order volume rose steadily from about 1,200 to 1,900.",
    ],
)
def test_review_negatives_unmasked(bare_detector: PiiDetector, text: str) -> None:
    assert bare_detector.mask(text).text == text


def test_calibration_set_has_no_detections_in_any_case(bare_detector: PiiDetector) -> None:
    # The lower-case / ALL-CAPS pass must not turn analytics prose into names.
    data = yaml.safe_load(CALIBRATION.read_text(encoding="utf-8"))
    cases = data["cases"] if isinstance(data, dict) else data
    hits = []
    for c in cases:
        for field in ("question", "candidate_answer", "reference_note"):
            value = c.get(field)
            if isinstance(value, str):
                for variant in (value.lower(), value.upper()):
                    if bare_detector.detect(variant):
                        hits.append((c["id"], field))
    assert hits == []


def test_review_brand_terms_still_exempt(review_detector: PiiDetector) -> None:
    for text in ("Bramble sold 120 coats.", "Top brand: Thistlewood.", "Finch grew 4%."):
        assert review_detector.mask(text).text == text


def test_large_table_masks_every_row_without_cap(bare_detector: PiiDetector) -> None:
    # Finding 1: no silent line cap; 5100 rows stay under the input bound.
    rows = [f"Zed Qu{chr(97 + i % 26)}{chr(97 + i // 26 % 26)}x,{i}" for i in range(5100)]
    text = "customer,orders\n" + "\n".join(rows)
    result = bare_detector.mask(text)
    assert not result.truncated
    assert "Zed" not in result.text
    assert result.text.count("<PERSON>") == 5100


def test_truncated_table_drops_bisected_last_row(bare_detector: PiiDetector) -> None:
    row = "Zorbina Quandleworth,1\n"
    text = "customer,orders\n" + row * (pii_regex.MAX_SCRUB_CHARS // len(row) + 50)
    result = bare_detector.mask(text)
    assert result.truncated
    assert "Zorbina" not in result.text and "Quandle" not in result.text


# --- D-215: saved-report display ids are never masked ------------------------------------------

#: Ids that were masked before D-215: as <PERSON> by spaCy, as <ID> by the long-digit-run
#: rule, and as "<PERSON> R-<id>" (spaCy tagged "Renamed" together with the id).
_KNOWN_BAD_IDS = (
    "3099fdf5ab99454aa901e35cd47d380d",
    "97f2a70223664676947f81435add92d1",
    "78255d6807924986bb968a437d5c8dfc",
)
_ID_TEMPLATES = (
    "Your report R-{h} is Quarterly Widgets.",
    'Saved report "Quarterly Widgets" (id R-{h}).',
    "  1. R-{h}  2026-01-02  Quarterly Widgets",
    "Renamed R-{h} to: Weekly Widgets",
    "Report R-{h}, created 2026-01-02, Quarterly Widgets",
)


def _report_ids(n: int) -> list[str]:
    rng = random.Random(215)
    return [uuid.UUID(int=rng.getrandbits(128), version=4).hex for _ in range(n)]


@pytest.mark.parametrize("h", _KNOWN_BAD_IDS)
def test_known_report_ids_survive_the_output_guard(detector: PiiDetector, h: str) -> None:
    for tpl in _ID_TEMPLATES:
        text = tpl.format(h=h)
        assert detector.mask(text).text == text


def test_report_display_ids_never_masked_over_many_ids(detector: PiiDetector) -> None:
    bad = [
        text
        for h in _report_ids(300)
        for text in (tpl.format(h=h) for tpl in _ID_TEMPLATES)
        if detector.mask(text).text != text
    ]
    assert bad == []


def test_name_next_to_a_report_id_is_still_masked(detector: PiiDetector) -> None:
    for h in _KNOWN_BAD_IDS:
        for text in (f"Name: Marlowe Finch, report R-{h}", f"Dear Marlowe Finch, see R-{h}."):
            out = detector.mask(text).text
            assert "Marlowe" not in out and "Finch" not in out and f"R-{h}" in out
