"""D-151a: the agent never shows SQL in chat, even when asked (offline, synthetic data only)."""

from __future__ import annotations

import json
import re
import time

import pytest

from opsfleet_agent.graph.intents import is_sql_request
from opsfleet_agent.guards.plain_language import (
    MAX_STRIP_CHARS,
    PLAIN_LANGUAGE_RULE,
    REPORT_PLAIN_LANGUAGE_RULE,
    SQL_NOT_SHOWN_TEXT,
    SQL_REMOVED_NOTE,
    SQL_STRIPPED,
    describe_data_used,
    sql_request_reply,
    strip_sql,
)
from opsfleet_agent.reports import library
from opsfleet_agent.reports.schema import REQUIRED_SECTIONS, parse_draft, render_markdown
from opsfleet_agent.roles.analyst import ModelTurn
from tests.unit.test_graph import Router, Scripted, sql_call
from tests.unit.test_graph import detector as detector  # noqa: F401  (fixture)
from tests.unit.test_graph import make_env as make_env  # noqa: F401  (fixture)
from tests.unit.test_graph import settings as settings  # noqa: F401  (fixture)
from tests.unit.test_library import ACME, _add
from tests.unit.test_library import store as store  # noqa: F401  (fixture)
from tests.unit.test_reports import DRAFT
from tests.unit.test_run_sql import SIMPLE, SIMPLE2

SQL_WORDS = re.compile(r"\b(SELECT|FROM|GROUP BY|JOIN)\b|order_items|sale_price|`")
JOIN_SQL = (
    "SELECT p.brand, SUM(oi.sale_price) AS revenue "
    "FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi "
    "JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON p.id = oi.product_id "
    "WHERE oi.created_at >= '2026-01-01' GROUP BY p.brand"
)


# --- strip_sql -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"Here it is:\n\n```sql\n{SIMPLE}\n```\n\nThat is all.",
        f"Here it is:\n\n```\n{SIMPLE}\n```\n\nThat is all.",
        f"Here it is:\n\n~~~\n{SIMPLE}\n~~~\n\nThat is all.",
        f"Here it is:\n\n```sql\n{SIMPLE}",  # unclosed fence
        f"Here it is: `{SIMPLE}`. That is all.",
        f"Here it is:\n\n{SIMPLE}\n\nThat is all.",
        "Here it is:\n\nWITH t AS (SELECT 1 AS n) SELECT n FROM t\n\nThat is all.",
        f"Here it is:\n\n{JOIN_SQL.lower()}\n\nThat is all.",
    ],
)
def test_strip_sql_removes_every_form(text: str) -> None:
    out = strip_sql(text)
    assert not SQL_WORDS.search(out), out
    assert "order_items" not in out.lower() and SQL_REMOVED_NOTE in out
    assert out.startswith("Here it is")
    assert strip_sql(out) == out  # idempotent


def test_strip_sql_keeps_plain_prose() -> None:
    prose = (
        "Please select the brands from the list.\n\nRevenue grew 12% from June to July; "
        "orders came from the Search channel.\n\n```\nnot code, just a table\n```"
    )
    assert strip_sql(prose) == prose


@pytest.mark.parametrize(
    "prose",
    [
        "Select a brand from the menu, e.g. Acme, to narrow the view.",
        "You can select the top brands from the summary and compare them by month.",
        "Most orders came from returning customers; select from the options below.",
    ],
)
def test_strip_sql_keeps_select_from_english(prose: str) -> None:
    assert strip_sql(prose) == prose


def test_strip_sql_is_fast_on_hostile_text() -> None:
    hostile = ("select " * 4000 + "\n") * 10 + ("from x " * 2000)
    t0 = time.perf_counter()
    strip_sql(hostile[:MAX_STRIP_CHARS])
    assert time.perf_counter() - t0 < 2.0


def test_strip_sql_collapses_repeated_notes() -> None:
    out = strip_sql(f"```sql\n{SIMPLE}\n```\n\n```sql\n{SIMPLE2}\n```")
    assert out.count(SQL_REMOVED_NOTE) == 1


def test_strip_sql_bounded_and_typed() -> None:
    big = "word " * 30_000 + SIMPLE
    assert strip_sql(big) == SQL_NOT_SHOWN_TEXT
    with pytest.raises(TypeError):
        strip_sql(None)  # type: ignore[arg-type]


# --- describe_data_used / sql_request_reply ------------------------------------------------------


def test_describe_data_used_is_business_words_only() -> None:
    text = describe_data_used([JOIN_SQL])
    assert text.startswith("Based on ") and text.endswith(".")
    assert "order item records" in text and "product records" in text and "brand" in text
    assert not SQL_WORDS.search(text) and "_" not in text and not re.search(r"\d", text)
    assert "id" not in text.split()  # id columns are skipped


def test_describe_data_used_handles_junk() -> None:
    assert describe_data_used([]) == ""
    assert describe_data_used(["not sql at all"]) == ""
    assert describe_data_used(None) == ""


def test_sql_request_reply_with_and_without_queries() -> None:
    empty = sql_request_reply([])
    assert empty.startswith(SQL_NOT_SHOWN_TEXT) and not SQL_WORDS.search(empty)
    full = sql_request_reply([SIMPLE, JOIN_SQL])
    assert full.startswith(SQL_NOT_SHOWN_TEXT) and "based on" in full
    assert "order item records" in full and not SQL_WORDS.search(full)


# --- intent --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "show me the SQL you used",
        "Show me the query",
        "can you give me the sql?",
        "what SQL did you run?",
        "the sql you used for that, please",
        "how did you query that?",
        "paste the query",
        "SQL please",
        "I'd like to see the SQL",
    ],
)
def test_sql_request_positive(text: str) -> None:
    assert is_sql_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "How many complete orders are there?",
        "write a sql query for revenue by brand",  # a data question: the analyst answers it
        "show me the query results",
        "I have a query about returns",
        "Please select the top brands",
        "",
        "x" * 100_000,
    ],
)
def test_sql_request_negative(text: str) -> None:
    assert not is_sql_request(text)


# --- graph ---------------------------------------------------------------------------------------


def test_show_sql_follow_up_describes_data_without_sql(make_env) -> None:  # noqa: F811
    analyst = Scripted(sql_call(SIMPLE), ModelTurn("There were 3 complete orders."))
    env = make_env(Router("simple"), analyst)
    first = env.ask("How many complete orders are there?")
    assert first.outcome == "answered"
    calls = len(analyst.calls)
    out = env.ask("show me the SQL you used")
    assert out.outcome == "answered" and out.route == "light"
    assert "don't show" in out.text and "order item records" in out.text
    assert not SQL_WORDS.search(out.text), out.text
    assert len(analyst.calls) == calls and out.sql_queries == 0  # no analyst, no query


def test_show_sql_without_history_says_so(make_env) -> None:  # noqa: F811
    analyst = Scripted(ModelTurn("unused"))
    env = make_env(Router("simple"), analyst)
    out = env.ask("show me the SQL you used")
    assert out.text.startswith(SQL_NOT_SHOWN_TEXT) and not analyst.calls


def test_analyst_answer_with_sql_is_stripped(make_env) -> None:  # noqa: F811
    leaky = f"There were 3 complete orders.\n\n```sql\n{SIMPLE}\n```"
    env = make_env(Router("simple"), Scripted(sql_call(SIMPLE), ModelTurn(leaky)))
    out = env.ask("How many complete orders are there?")
    assert out.outcome == "answered" and "3 complete orders" in out.text
    assert not SQL_WORDS.search(out.text) and SQL_STRIPPED in out.guard_codes


# --- reports -------------------------------------------------------------------------------------


def test_report_has_data_used_not_sql() -> None:
    draft = parse_draft(json.dumps(DRAFT))
    assert draft is not None
    md = render_markdown(draft, [JOIN_SQL])
    assert REQUIRED_SECTIONS[-1] == "Data used" and "## Data used" in md
    assert "## SQL used" not in md and "order item records" in md
    assert not SQL_WORDS.search(md.split("## Data used")[1])


def test_view_report_strips_sql_from_old_reports(store) -> None:  # noqa: F811
    rid = _add(store, "k-old", extra=f"## SQL used\n\n```sql\n{SIMPLE}\n```")
    res = library.view_report(store, "analyst_a", ACME, rid)
    assert res.status == "ok" and "COUNT(*)" not in res.text and "order_items" not in res.text


# --- prompts -------------------------------------------------------------------------------------


@pytest.mark.parametrize("rule", [PLAIN_LANGUAGE_RULE, REPORT_PLAIN_LANGUAGE_RULE])
def test_rules_forbid_sql(rule: str) -> None:
    assert "SQL" in rule and re.search(r"[Nn]ever (show|write)", rule)
