"""Iteration 29: the golden and router case files load and are well formed. No network."""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evals import run as eval_run  # noqa: E402

from opsfleet_agent.roles.router import LABELS  # noqa: E402

CASES_DIR = ROOT / "evals" / "cases"
# report turns end as report_pending (draft shown) or report_saved (graph.py outcomes)
GOLDEN_OUTCOMES = {"answered", "refused", "clarify", "degraded", "report_pending", "report_saved"}

REQUIRED_GOLDEN = {
    "top_customers", "aov_by_traffic_source", "compare_brands_why", "show_sql",
    "monthly_revenue_12m", "ytd_revenue_by_brand", "schema_overview", "inventory_unavailable",
    "state_underspend_compare", "churn_last_month", "churn_user_definition",
    "followup_breakdown", "followup_why_march", "cross_session_memory", "discuss_saved_report",
    "stated_assumption_defaults", "clarify_unresolved_reference",
    "q1_report", "report_save_confirm", "save_this", "report_search", "roadmap_actions_unsupported",
    "my_scope", "smalltalk_light_path", "smalltalk_then_task",
}
OPTIONAL_GOLDEN = {
    "persona_tone_change", "preference_table_vs_bullets", "retry_report_without_ledger",
    "library_agent",
}
# Live-1 set 2: more topics, so live runs do not repeat the same questions
SET2_GOLDEN = {
    "revenue_by_category_last_quarter", "return_rate_by_category", "revenue_by_country",
    "delivery_time_trend", "customer_age_gender_mix", "order_status_breakdown",
    "margin_by_department", "best_sellers_last_month", "new_vs_returning_customers",
    "repeat_purchase_cohorts", "followup_filter_department", "customer_contact_request",
    "off_topic_question", "non_english_question",
}

PII = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s().-]{8,}\d")


@pytest.fixture(scope="module")
def cases() -> dict:
    loaded = eval_run.load_cases(CASES_DIR)
    return {c.id: c for c in loaded}


def _golden(cases):
    return {k.split("/", 1)[1]: v for k, v in cases.items() if k.startswith("golden/")}


def _router(cases):
    return {k: v for k, v in cases.items() if k.startswith("router/")}


def test_all_named_golden_cases_exist(cases):
    names = set(_golden(cases))
    assert REQUIRED_GOLDEN <= names
    assert OPTIONAL_GOLDEN <= names
    assert names == REQUIRED_GOLDEN | OPTIONAL_GOLDEN | SET2_GOLDEN


def test_set2_cases_tagged_and_refusals_run_no_sql(cases):
    g = _golden(cases)
    for name in SET2_GOLDEN:
        c = g[name]
        assert "set2" in c.tags, name
        if c.expect["outcome"] == "refused":
            assert c.expect.get("no_sql") and c.estimate == {"llm": {"router": 1}, "bq": 0}, name


def test_ids_unique(cases):
    loaded = eval_run.load_cases(CASES_DIR)
    dupes = [i for i, n in Counter(c.id for c in loaded).items() if n > 1]
    assert not dupes


def test_golden_cases_shape(cases):
    for name, c in _golden(cases).items():
        assert c.fake, name
        exp = c.expect
        assert exp["outcome"] in GOLDEN_OUTCOMES, name
        # golden cases stay out of the router report and the pii gate
        assert "label" not in exp and "detect" not in exp, name
        assert c.fake.get("outcome") == exp["outcome"], name


def test_optional_cases_marked(cases):
    g = _golden(cases)
    assert g["preference_table_vs_bullets"].skip
    assert "iteration 39" in g["preference_table_vs_bullets"].skip
    assert not g["persona_tone_change"].skip


def test_golden_profiles_are_synthetic(cases):
    profiles = yaml.safe_load((ROOT / "config" / "profiles.yaml").read_text())
    pdict = {p["id"]: p for p in profiles["profiles"]}
    for name, c in _golden(cases).items():
        assert c.session.get("profile") in pdict, name


def test_golden_brands_only_from_profiles(cases):
    profiles = yaml.safe_load((ROOT / "config" / "profiles.yaml").read_text())
    pdict = {p["id"]: p for p in profiles["profiles"]}
    known = {b for p in pdict.values() for b in (p.get("brands") or [])}
    assert {"Calvin Klein", "Carhartt", "Levi's"} <= known
    text = " ".join(
        (CASES_DIR / "golden" / f"{n}.yaml").read_text() for n in _golden(cases)
    )
    assert not PII.search(text)
    for other in ("Nike", "Adidas", "Gucci", "Zara"):
        assert other not in text


def test_router_set(cases):
    router = _router(cases)
    assert 45 <= len(router) <= 80
    labels = Counter(c.expect["label"] for c in router.values())
    assert set(labels) == set(LABELS)
    for cid, c in router.items():
        assert c.expect["label"] in LABELS, cid
        assert c.fake["label"] == c.expect["label"], cid
        assert not PII.search(" ".join(c.turns)), cid
    tags = Counter(t for c in router.values() for t in c.tags)
    assert tags["borderline"] >= 3
    assert tags["keyword_trap"] >= 2
    # D-155: memory and comment have English and at least two non-English cases each
    for label in ("memory", "comment"):
        of_label = [c for c in router.values() if c.expect["label"] == label]
        assert sum("non_english" in c.tags for c in of_label) >= 2, label
        assert any("non_english" not in c.tags for c in of_label), label
    # negatives: a comment with a question, and "remember" about data, are data questions
    # iter-live1: questions about data the dataset lacks are data questions, not meta/off_topic
    for tag in ("comment_question", "memory_trap", "unavailable_data"):
        negatives = [c for c in router.values() if tag in c.tags]
        assert negatives and all(c.expect["label"] in {"simple", "complex"} for c in negatives)
    # keyword traps are normal data questions, never injection
    for c in router.values():
        if "keyword_trap" in c.tags:
            assert c.expect["label"] in {"simple", "complex"}


def test_router_label_injection_case(cases):
    c = cases["adversarial/injection/router_label_injection"]
    assert c.expect["label"] == "injection"
    assert c.expect["outcome"] == "refused"
    assert c.expect.get("no_sql")
    assert "injection" in c.tags
