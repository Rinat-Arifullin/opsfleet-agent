"""D-151: plain-language chat answers (no table, column, SQL or schema terms).

Offline only: the graph tests use the scripted fakes from ``test_graph`` and ``test_reports``.
All data is synthetic.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import date
from typing import Any

import pytest

from opsfleet_agent.guards.plain_language import (
    PLAIN_LANGUAGE_RULE,
    PLAIN_LANGUAGE_SECTION,
    REPORT_PLAIN_LANGUAGE_RULE,
    SCHEMA_TERMS_REWRITTEN,
    SQL_REMOVED_NOTE,
    humanize_identifiers,
    strip_sql,
)
from opsfleet_agent.guards.sql_policy import ALLOWED_TABLES
from opsfleet_agent.persona import builtin_persona
from opsfleet_agent.roles.analyst import DEEP, QUICK, ModelTurn, build_system_prompt
from opsfleet_agent.store.db import open_store
from opsfleet_agent.store.reports import ReportStore
from tests.unit.test_graph import Env, Router, Scripted, sql_call
from tests.unit.test_graph import detector as _detector_fixture  # noqa: F401 (pytest fixture)
from tests.unit.test_graph import settings as _settings_fixture  # noqa: F401 (pytest fixture)
from tests.unit.test_reports import ReportModel, TurnRouter
from tests.unit.test_run_sql import SIMPLE

detector = _detector_fixture
settings = _settings_fixture

EXAMPLE = (
    "Total revenue for Calvin Klein products in Q1 2024 was $7,715.29 across 312 items. "
    "I summed `sale_price` from the `order_items` table, filtered on `orders.created_at` "
    "and grouped by products.brand. order_items.status was 'Complete' for 87.5% of rows."
)
SCHEMA_TERMS = re.compile(
    r"`|\b(?:" + "|".join(n for n in ALLOWED_TABLES if "_" in n) + r")\b|\w_\w|\b\w+\.\w+_\w+"
)


def _digits(text: str) -> list[str]:
    return re.findall(r"\d+", text)


@pytest.fixture
def make_env(tmp_path, settings, detector):
    def make(router: Any = None, analyst: Any = None) -> Env:
        return Env(tmp_path, settings, detector, router or Router(), analyst or Scripted())

    return make


# --- humanize_identifiers -----------------------------------------------------------------


def test_example_answer_is_rewritten_in_business_words() -> None:
    out = humanize_identifiers(EXAMPLE)
    assert not SCHEMA_TERMS.search(out), out
    assert "item sale price" not in out  # a bare `sale_price` has no table to qualify it
    assert "sale price from the order item records" in out
    assert "filtered on order date" in out and "grouped by brand." in out
    assert "Item status was 'Complete'" in out  # capitalised at the start of a sentence
    assert "$7,715.29" in out and "87.5%" in out and "Q1 2024" in out
    assert "for Calvin Klein products" in out


def test_ordinary_english_is_left_alone() -> None:
    text = (
        "There were 3 complete orders last week. Users who placed orders came from 12 states; "
        "the status of most orders was Complete and the brand with the best sales was Acme."
    )
    assert humanize_identifiers(text) == text
    assert humanize_identifiers("") == ""


def test_identifier_forms_are_rewritten() -> None:
    cases = {
        "the users table": "the customer records",
        "from the orders table": "from the order records",
        "the `sale_price` column": "the sale price",
        "by users.created_at": "by customer sign-up date",
        "using `oi.sale_price`": "using sale price",
        "the bigquery-public-data.thelook_ecommerce dataset": "the store data",
        "see `thelook_ecommerce.orders`.": "see order records.",
        "grouped by traffic_source": "grouped by traffic source",
    }
    for text, want in cases.items():  # mid-sentence: no capitalisation
        assert humanize_identifiers(f"so {text}") == f"so {want}", text
    assert humanize_identifiers("the users table") == "The customer records"  # sentence start


def test_rewrite_is_idempotent() -> None:
    samples = [EXAMPLE, "the users table has a traffic_source column", "orders.status, `users`"]
    for text in samples:
        once = humanize_identifiers(text)
        assert humanize_identifiers(once) == once


def test_numbers_are_never_changed() -> None:
    samples = [
        EXAMPLE,
        "order_items.sale_price rose 12.4% from $1,204.00 to $1,353.30 (2024-01-01 to 2024-03-31)",
        "user_id 42 and product_id 7 appear in 3 orders.created_at buckets",
    ]
    for text in samples:
        assert _digits(humanize_identifiers(text)) == _digits(text)


def test_sql_is_removed_not_kept() -> None:
    """D-151a (overrides AC-02.2): SQL is stripped from answers, never shown."""
    fenced = "Here is the query:\n\n```sql\nSELECT SUM(sale_price) FROM order_items\n```\n"
    assert humanize_identifiers(fenced) == fenced  # code blocks are left to strip_sql
    assert "SELECT" not in strip_sql(fenced) and strip_sql(fenced).startswith("Here is the query")
    bare = "I used sale_price.\n\nSELECT order_id, sale_price\nFROM order_items\nLIMIT 10"
    out = humanize_identifiers(strip_sql(bare))
    assert out.startswith("I used sale price.") and "SELECT" not in out and "FROM" not in out
    unclosed = "```\nSELECT created_at FROM orders"
    assert strip_sql(unclosed) == SQL_REMOVED_NOTE


def test_rejects_non_str() -> None:
    with pytest.raises(TypeError):
        humanize_identifiers(None)  # type: ignore[arg-type]


# --- the rule is in every prompt that writes chat text -----------------------------------


def _assert_rule(system: str) -> None:
    assert f"## {PLAIN_LANGUAGE_SECTION}" in system
    assert PLAIN_LANGUAGE_RULE in system


@pytest.mark.parametrize("role", [QUICK, DEEP])
def test_analyst_prompts_contain_rule(role: str) -> None:
    window = (date(2019, 1, 1).isoformat(), date(2026, 9, 30).isoformat())
    system = build_system_prompt(role, scope_label="Acme", persona=builtin_persona(), window=window)
    _assert_rule(system)
    # code-owned and placed after the analyst rules, before any per-turn context
    assert system.index("## Analyst rules") < system.index(f"## {PLAIN_LANGUAGE_SECTION}")


def test_light_prompt_contains_rule(make_env) -> None:
    env = make_env(Router("smalltalk", "Hi! Ask me about the store data."))
    env.ask("hello")
    system = env.router.calls[-1][1][0].content
    _assert_rule(system)


def test_force_answer_prompt_contains_rule(make_env) -> None:
    env = make_env(Router("complex"), Scripted(ModelTurn("")))  # empty answer -> force answer
    env.ask("Why did revenue fall?")
    force = [c for c in env.analyst.calls if c[2] == 0]
    assert force, "force answer did not run"
    _assert_rule(force[-1][1][0]["content"])


def test_report_writer_prompt_contains_prose_rule(tmp_path, settings, detector) -> None:
    model = ReportModel()
    env = Env(tmp_path, settings, detector, TurnRouter("report"), model)
    store = ReportStore(open_store(tmp_path / "reports.db"))
    env.graph.services = dataclasses.replace(env.graph.services, reports=store)
    out = env.ask("Write a report on complete orders")
    assert out.outcome == "report_pending"
    writer = [m for k, m in model.calls if k == "writer"]
    system = writer[0][0]["content"] if isinstance(writer[0][0], dict) else writer[0][0].content
    assert f"## {PLAIN_LANGUAGE_SECTION}" in system and REPORT_PLAIN_LANGUAGE_RULE in system
    assert "## Action items" in out.text  # report requirements are unchanged


# --- integration: the graph rewrites the allowed answer and traces it --------------------


def _guard_output_spans(env: Env) -> list[dict]:
    return [f for t, n, f in env.spans if t == "guard" and n == "output"]


def test_graph_rewrites_answer_after_guard(make_env) -> None:
    answer = "There were 3 complete orders; I counted `order_id` in the orders table."
    env = make_env(Router("simple"), Scripted(sql_call(SIMPLE), ModelTurn(answer)))
    out = env.ask("How many complete orders are there?")
    assert out.outcome == "answered"
    assert "There were 3 complete orders; I counted order ID in the order records." in out.text
    assert "`" not in out.text and "order_id" not in out.text
    spans = _guard_output_spans(env)
    assert spans and SCHEMA_TERMS_REWRITTEN in spans[-1]["rule_hits"]
    assert spans[-1]["verdict"] == "allow"


def test_graph_plain_answer_has_no_rewrite_code(make_env) -> None:
    env = make_env(
        Router("simple"),
        Scripted(sql_call(SIMPLE), ModelTurn("There were 3 complete orders.")),
    )
    out = env.ask("How many complete orders are there?")
    assert "There were 3 complete orders." in out.text
    assert SCHEMA_TERMS_REWRITTEN not in _guard_output_spans(env)[-1]["rule_hits"]


def test_light_model_reply_is_rewritten(make_env) -> None:
    env = make_env(Router("smalltalk", "Hi! Ask me anything about the order_items data."))
    out = env.ask("hello")
    assert out.route == "light" and "order items data" in out.text
    assert "order_items" not in out.text
    spans = _guard_output_spans(env)
    assert spans and SCHEMA_TERMS_REWRITTEN in spans[-1]["rule_hits"]
