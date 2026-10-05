"""live1: report failures seen on a small local model, reproduced with offline fakes.

- ``save_this``: NER masked "Data" in "Data window:" as a person, the guarded body lost a
  required line and the save was refused; a writer that never returns parseable JSON left
  nothing to save.
- ``q1_report``: an analysis that ended partial (the analyst gave up after some queries
  succeeded) got no report draft, so no headings and no Save / Revise / Cancel options.
- saved reports are shown with an ``R-`` display id, which the library accepts back.

No network: the fakes of ``test_reports`` / ``test_graph``; all data is synthetic.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re

import pytest

from opsfleet_agent.graph import graph as gr
from opsfleet_agent.guards.pii import PERSON, PiiDetector
from opsfleet_agent.reports import library
from opsfleet_agent.reports.library import display_id, strip_display_prefix
from opsfleet_agent.reports.schema import REQUIRED_SECTIONS, missing_sections, parse_draft
from opsfleet_agent.roles.report_writer import FALLBACK_NOTE, MAX_WRITER_CALLS, fallback_draft
from opsfleet_agent.store.db import open_store
from opsfleet_agent.store.reports import ReportStore
from tests.unit.test_graph import PROFILE, Env
from tests.unit.test_graph import detector as _detector_fixture  # noqa: F401 (pytest fixture)
from tests.unit.test_graph import settings as _settings_fixture  # noqa: F401 (pytest fixture)
from tests.unit.test_reports import DRAFT, ReportModel, TurnRouter, _pending, _save_args

detector = _detector_fixture
settings = _settings_fixture

_RID = re.compile(r"\bR-[0-9a-f]{32}\b")


class DataWindowNer(PiiDetector):
    """The real detector, plus the live NER miss: "Data" in "Data window:" is a PERSON."""

    def _ner_spans(self, text: str) -> list[tuple[str, int, int]]:
        spans = super()._ner_spans(text)
        extra = [(PERSON, m.start(), m.start() + 4) for m in re.finditer(r"Data window:", text)]
        return sorted(spans + extra, key=lambda s: s[1])


@pytest.fixture
def store(tmp_path) -> ReportStore:
    return ReportStore(open_store(tmp_path / "reports.db"))


@pytest.fixture
def make_env(tmp_path, settings, detector, store):
    def make(model=None, label: str = "report", *, det: PiiDetector | None = None) -> Env:
        env = Env(tmp_path, settings, det or detector, TurnRouter(label), model or ReportModel())
        env.graph.services = dataclasses.replace(env.graph.services, reports=store)
        return env

    return make


def _ner_detector(base: PiiDetector) -> PiiDetector:
    det = copy.copy(base)  # shares the loaded model; only the class changes
    det.__class__ = DataWindowNer
    return det


def _has_sections(text: str) -> None:
    assert missing_sections(text) == []
    for name in REQUIRED_SECTIONS:
        assert f"## {name}" in text


# --- save_this: NER over "Data window:" -----------------------------------------------------------


def test_ner_over_data_window_is_reproduced(detector) -> None:
    det = _ner_detector(detector)
    assert det.mask("Data window: 2019-01-01 to 2026-09-30").text.startswith("<PERSON>")


def test_data_window_ner_keeps_the_report_structure(make_env, store, detector) -> None:
    env = make_env(det=_ner_detector(detector))
    out = env.ask("Write a report on complete orders")
    assert out.outcome == "report_pending", out.text
    _has_sections(out.text)
    assert "Data window: 2019" in out.text and "<PERSON>" not in out.text
    saved = env.ask("save")
    assert saved.outcome == "report_saved" and store.count() == 1
    assert "saved" in saved.text.lower() and _RID.search(saved.text)
    (rec,) = store.list(PROFILE.user_id)
    assert missing_sections(rec.body_markdown) == []
    assert display_id(rec.report_id) in saved.text


def test_save_this_survives_data_window_ner(make_env, store, detector) -> None:
    env = make_env(label="simple", det=_ner_detector(detector))
    env.ask("How many complete orders are there?")
    saved = env.ask("save this as a report")
    assert saved.outcome == "report_saved" and store.count() == 1, saved.text
    assert "saved" in saved.text.lower() and _RID.search(saved.text)


def test_real_pii_in_a_report_is_still_masked(make_env, store) -> None:
    leaky = json.dumps(dict(DRAFT, summary="There were 3 complete orders, see user@example.com."))
    env = make_env(ReportModel(leaky))
    out = env.ask("Write a report on complete orders")
    assert "user@example.com" not in out.text
    if out.outcome == "report_pending":
        _has_sections(out.text)


# --- save_this: the writer never returns parseable JSON ------------------------------------------


def test_save_this_with_unparseable_writer_uses_the_fallback(make_env, store) -> None:
    model = ReportModel("Sure! Here is your report: Complete orders were 3.")
    env = make_env(model, label="simple")
    env.ask("How many complete orders are there?")
    saved = env.ask("save this as a report")
    assert model.kinds().count("writer") == MAX_WRITER_CALLS  # bounded retry, then fallback
    assert saved.outcome == "report_saved" and store.count() == 1, saved.text
    assert "saved" in saved.text.lower() and _RID.search(saved.text)
    (rec,) = store.list(PROFILE.user_id)
    assert FALLBACK_NOTE in rec.body_markdown and missing_sections(rec.body_markdown) == []
    assert "3 complete orders" in rec.body_markdown


def test_writer_retry_carries_a_corrective_message(make_env, store) -> None:
    model = ReportModel("not json", json.dumps(DRAFT))
    env = make_env(model)
    out = env.ask("Write a report on complete orders")
    assert out.outcome == "report_pending" and FALLBACK_NOTE not in out.text
    second = [m for k, m in model.calls if k == "writer"][1]
    last = second[-1]
    content = last.get("content") if isinstance(last, dict) else getattr(last, "content", "")
    assert "title" in str(content) and "action_items" in str(content)


SMALL_MODEL = {
    "report": {
        "title": "Complete orders overview",
        "summary": ["There were 3 complete orders.", "The sample is small."],
        "definitions": "Complete orders: order items with the status Complete.",
        "key_metrics": {"Complete orders": "3"},
        "insights": ["Complete orders total 3."],
        "action_items": [
            "Review the checkout funnel",
            "Test a reminder for open carts",
            "Track completions weekly",
        ],
        "limitations": "Synthetic sample.",
    }
}


def test_small_model_shaped_json_is_accepted(make_env, store) -> None:
    model = ReportModel(json.dumps(SMALL_MODEL))
    env = make_env(model, label="simple")
    env.ask("How many complete orders are there?")
    saved = env.ask("save this as a report")
    assert model.kinds().count("writer") == 1
    assert saved.outcome == "report_saved" and _RID.search(saved.text), saved.text
    (rec,) = store.list(PROFILE.user_id)
    assert FALLBACK_NOTE not in rec.body_markdown and "Complete orders overview" in saved.text


def test_tolerant_parse_draft_shapes() -> None:
    d = parse_draft("```json\n" + json.dumps(SMALL_MODEL) + "\n```")
    assert d is not None
    assert d.summary.startswith("There were 3") and d.key_metrics[0].name == "Complete orders"
    assert d.insights[0].n == 1 and "3" in d.insights[0].figures
    assert d.action_items[0].insight_ref == 1 and d.definitions and d.limitations
    assert parse_draft(json.dumps(DRAFT)) is not None
    # title and summary stay required; junk stays None
    assert parse_draft('{"title": "x"}') is None
    assert parse_draft("not json") is None and parse_draft(None) is None


def test_fallback_draft_restates_the_analysis() -> None:
    analysis = (
        "Revenue for Q1 was $12,345.67 across 3 categories.\n\n"
        "| Category | Revenue |\n|---|---|\n| Jeans | $8,000.00 |\n| Socks | $4,345.67 |\n\n"
        "Orders: 41\nJeans led with 65% of revenue."
    )
    d = fallback_draft("Please write a Q1 report on revenue", analysis)
    assert d is not None
    assert d.title == "Report: Q1 revenue"
    assert "$12,345.67" in d.summary
    names = {m.name for m in d.key_metrics}
    assert {"Jeans", "Socks", "Orders"} <= names
    assert d.insights and all(i.figures for i in d.insights)
    assert len(d.action_items) >= 3 and all(a.insight_ref == 1 for a in d.action_items)
    assert fallback_draft("q", "") is None and fallback_draft("q", "   ") is None


# --- q1_report: a partial analysis still gets a draft --------------------------------------------


class PartialModel(ReportModel):
    """The analyst runs one query, then answers nothing (status partial, ``empty``)."""

    def __init__(self, *writer: str) -> None:
        super().__init__(*writer, answer="")


def test_partial_analysis_gets_a_report_draft(make_env, store) -> None:
    model = PartialModel()
    env = make_env(model)
    out = env.ask("Write a Q1 report on complete orders")
    assert out.outcome == "report_pending", out.text
    _has_sections(out.text)
    assert gr.PARTIAL_REPORT_NOTE in out.text
    for word in ("Save", "Revise", "Cancel"):
        assert word in out.text
    assert out.text.rstrip().endswith(gr.REPORT_PROMPT) and _pending(env)
    saved = env.ask("Save")
    assert saved.outcome == "report_saved" and _RID.search(saved.text)
    assert store.count() == 1


def test_report_prompt_options_are_code_owned() -> None:
    for word in ("Save", "Revise", "Cancel"):
        assert re.search(rf"\b{word}\b", gr.REPORT_PROMPT)
    assert "Save" in gr.DELETE_WHILE_PENDING_TEXT


def test_partial_with_no_sql_still_gets_no_draft(make_env, store) -> None:
    class NoSql(ReportModel):
        def __call__(self, model, messages, specs, timeout):
            if specs and "## Report" not in str(messages[0]):
                self.calls.append(("analyst", list(messages)))
                from opsfleet_agent.graph.llm import LLMResponse
                from opsfleet_agent.roles.analyst import ModelTurn

                return LLMResponse(ModelTurn(""), 5, 5)
            return super().__call__(model, messages, specs, timeout)

    env = make_env(NoSql())
    out = env.ask("Write a report on complete orders")
    assert out.outcome != "report_pending" and store.count() == 0


# --- report_save_confirm: a Q1 report, then "Save" -----------------------------------------------


def test_q1_report_then_save(make_env, store) -> None:
    env = make_env()
    out = env.ask("Write a Q1 report on complete orders")
    assert out.outcome == "report_pending" and gr.REPORT_PROMPT in out.text
    saved = env.ask("Save")
    assert saved.outcome == "report_saved" and store.count() == 1
    assert "saved" in saved.text.lower() and _RID.search(saved.text)
    # no delete path ran: no delete span in the trace, and the report is still there
    assert not [s for s in env.spans if "delete" in str(s[1] or "")]
    assert store.count() == 1


# --- R- display ids ------------------------------------------------------------------------------


def test_display_id_round_trip() -> None:
    rid = "a" * 32
    assert display_id(rid) == f"R-{rid}" and strip_display_prefix(f"R-{rid}") == rid
    assert strip_display_prefix(f"r-{rid}") == rid
    assert strip_display_prefix("R-short") == "R-short"  # only a full id is unwrapped
    assert strip_display_prefix(rid) == rid


def test_library_accepts_the_display_id(tmp_path) -> None:
    from opsfleet_agent.guards.scope import ProductScope

    st = ReportStore(open_store(tmp_path / "lib.db"))
    rec, _ = st.save(**_save_args("k-live1"))
    acme = ProductScope.for_brands(["Acme"])
    assert library.view_report(st, "analyst_a", acme, display_id(rec.report_id)).status == "ok"
    assert library.open_report(st, "analyst_a", acme, display_id(rec.report_id)).status == "ok"
    assert library.view_report(st, "analyst_b", acme, display_id(rec.report_id)).status != "ok"
