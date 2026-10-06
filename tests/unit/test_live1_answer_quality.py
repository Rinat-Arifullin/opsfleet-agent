"""Live eval iteration 1: answer-quality fixes (offline, fake LLMs, synthetic text).

* The assumptions footer states the scope and the definitions an answer omitted (aov, churn).
* The model's own SQL, not the scope rewrite, is shown back in later turns, and copying the
  rewrite gets an actionable policy hint (followup_why_march).
* Code-owned texts no longer contain the word "email" (top_customers must_not_contain).
"""

from __future__ import annotations

import re

import pytest

from opsfleet_agent.graph import graph as gr
from opsfleet_agent.graph.assumptions import (
    AOV_DEFINITION,
    ASSUMPTIONS_ADDED,
    CHURN_DEFINITION,
    CHURN_RESTATED,
    FOOTER_HEADING,
    REVENUE_DEFINITION,
    SCOPE_ALL,
    assumptions_footer,
    scope_line,
)
from opsfleet_agent.graph.context import shown_sql
from opsfleet_agent.graph.fixed_replies import static_texts
from opsfleet_agent.guards.input import REFUSALS
from opsfleet_agent.guards.sql_policy import SCOPE_PATTERN_HINT, check_sql
from opsfleet_agent.roles.analyst import ModelTurn
from opsfleet_agent.roles.light_path import CAPABILITIES_TEXT
from tests.unit.test_graph import SIMPLE, Scripted, SeqRouter, _state, sql_call
from tests.unit.test_graph import detector as detector  # noqa: F401  (fixture)
from tests.unit.test_graph import make_env as make_env  # noqa: F401  (fixture)
from tests.unit.test_graph import settings as settings  # noqa: F401  (fixture)
from tests.unit.test_intents import CommentModel
from tests.unit.test_reports import TurnRouter
from tests.unit.test_run_sql import OI, P

CK = ("Calvin Klein",)

# --- the footer (pure) ---


def test_footer_adds_missing_scope_only() -> None:
    out = assumptions_footer("Orders rose in March.", "orders by month", brands=CK,
                             all_products=False)  # fmt: skip
    assert out == f"{FOOTER_HEADING}\n- Scope: Calvin Klein products only."
    assert assumptions_footer("Calvin Klein orders rose.", "orders", brands=CK,
                              all_products=False) == ""  # fmt: skip


def test_footer_scope_lines() -> None:
    assert scope_line(("A", "B", "C"), False) == "Scope: A, B and C products only."
    assert scope_line(("A",), True) == SCOPE_ALL == scope_line((), False)
    assert assumptions_footer("Across all products, orders rose.", "orders", brands=(),
                              all_products=True) == ""  # fmt: skip


def test_footer_brand_match_folds_case_and_apostrophes() -> None:
    brands = ("Levi's",)
    assert assumptions_footer("LEVI’S orders rose.", "orders", brands=brands,
                              all_products=False) == ""  # fmt: skip


def test_aov_answer_gets_revenue_and_aov_definitions() -> None:
    # live eval aov_by_traffic_source: a force answer with no scope and no definitions
    out = assumptions_footer("Search has the highest AOV.", "AOV by traffic source",
                             brands=CK, all_products=False)  # fmt: skip
    assert "Calvin Klein" in out and REVENUE_DEFINITION in out and AOV_DEFINITION in out


def test_stated_definitions_are_not_duplicated() -> None:
    answer = (
        "For Calvin Klein, AOV (revenue divided by orders, cancelled and returned items "
        "excluded) is highest for Search."
    )
    assert assumptions_footer(answer, "AOV by traffic source", brands=CK,
                              all_products=False) == ""  # fmt: skip


def test_churn_answer_gets_the_default_definition_and_restate_offer() -> None:
    # live eval churn_last_month: must contain "no order in" and "restate"
    out = assumptions_footer("Churn was higher last month.", "What was churn last month?",
                             brands=CK, all_products=False)  # fmt: skip
    assert CHURN_DEFINITION in out and "no order in" in out and "restate" in out
    assert out.count("Churn") == 1


def test_restated_churn_is_acknowledged_not_redefined() -> None:
    out = assumptions_footer("Calvin Klein churn was higher.", "churn last month", brands=CK,
                             all_products=False, churn_restated=True)  # fmt: skip
    assert out == f"{FOOTER_HEADING}\n- {CHURN_RESTATED}"


def test_footer_has_no_digits_and_scans_a_bounded_prefix() -> None:
    out = assumptions_footer("x" * 50_000 + " Calvin Klein", "revenue and churn and AOV",
                             brands=CK, all_products=False)  # fmt: skip
    assert out and not re.search(r"\d", out) and "Scope:" in out


# --- the footer in _finalize (fake LLMs) ---


def test_data_answer_gets_the_footer_once(make_env) -> None:  # noqa: F811
    analyst = Scripted(sql_call(SIMPLE), ModelTurn("There were 3 complete orders."))
    env = make_env(SeqRouter("simple"), analyst)
    out = env.ask("How many complete orders are there?")
    assert out.outcome == "answered"
    assert out.text.endswith(f"{FOOTER_HEADING}\n- Scope: Acme products only.")
    assert out.text.count(FOOTER_HEADING) == 1
    assert _state(env)["history"][-1]["text"].count(FOOTER_HEADING) == 1


def test_answer_stating_its_scope_gets_no_footer(make_env) -> None:  # noqa: F811
    analyst = Scripted(sql_call(SIMPLE), ModelTurn("For Acme there were 3 complete orders."))
    env = make_env(SeqRouter("simple"), analyst)
    out = env.ask("How many complete orders are there?")
    assert out.outcome == "answered" and FOOTER_HEADING not in out.text


def test_turn_without_data_gets_no_footer(make_env) -> None:  # noqa: F811
    env = make_env(SeqRouter("simple"), Scripted(ModelTurn("Which period do you mean?")))
    out = env.ask("How are we doing?")
    assert FOOTER_HEADING not in out.text


def test_comment_reply_gets_no_footer(make_env) -> None:  # noqa: F811
    router = TurnRouter("simple")
    env = make_env(router, CommentModel())
    env.ask("How many complete orders are there?")
    router.label = "comment"
    out = env.ask("I think that deserves a campaign")
    assert out.label == "comment" and FOOTER_HEADING not in out.text


def test_footer_guard_code_is_exported() -> None:
    assert ASSUMPTIONS_ADDED == "assumptions_added"
    assert gr.assumptions_footer is assumptions_footer


# --- followup_why_march: the scope rewrite is not shown back, and copying it is explained ---

COPIED = [
    f"SELECT COUNT(*) AS n FROM {OI} oi JOIN {P} p ON p.id = oi.product_id "
    "WHERE p.brand IN UNNEST(@scope_brands)",
    f"WITH __p AS (SELECT * FROM {P}) SELECT COUNT(*) AS n FROM __p",
    "SELECT COUNT(*) AS n FROM __oi",
    f"SELECT COUNT(*) AS n FROM {P} p WHERE p.brand = @brand",
]


@pytest.mark.parametrize("sql", COPIED)
def test_copied_scope_pattern_gets_the_actionable_hint(sql: str) -> None:
    d = check_sql(sql)
    assert not d.allowed and d.hint == SCOPE_PATTERN_HINT
    assert "applied automatically" in d.hint and "order_items" in d.hint


def test_other_source_refusals_keep_their_generic_hint() -> None:
    d = check_sql(f"SELECT * FROM {OI} oi, LATERAL (SELECT 1)")
    assert not d.allowed and d.hint != SCOPE_PATTERN_HINT


def test_copied_scope_sql_hint_reaches_the_model(make_env) -> None:  # noqa: F811
    analyst = Scripted(sql_call(COPIED[0]), ModelTurn("I could not run that query."))
    env = make_env(SeqRouter("simple"), analyst)
    env.ask("How many orders were there?")
    tool_msgs = [m for m in analyst.calls[1][1] if m.get("role") == "tool"]
    assert tool_msgs and "applied automatically" in tool_msgs[-1]["content"]


def test_shown_sql_prefers_the_model_sql() -> None:
    assert shown_sql({"sql": "scoped", "model_sql": "mine"}) == "mine"
    assert shown_sql({"sql": "scoped", "model_sql": ""}) == "scoped"
    assert shown_sql({"sql": "scoped"}) == "scoped"  # snapshot from before this change
    assert shown_sql({"sql": "scoped", "model_sql": 3}) == "scoped"


def test_old_snapshot_ledger_without_model_sql_is_still_valid() -> None:
    entry = {"sql": "SELECT 1", "query_id": "q", "sql_hash": "h", "executed_sql_hash": "e",
             "purpose": "p", "rows": 1}  # fmt: skip
    assert set(entry) == gr._LEDGER_KEYS
    assert gr._ledger([entry]) == [entry]
    assert gr._ledger([entry | {"model_sql": "SELECT 1"}]) is not None
    assert gr._ledger([entry | {"model_sql": 3}]) is None
    assert gr._ledger([entry | {"other": "x"}]) is None


# --- top_customers: no "email" in code-owned texts ---


def test_code_owned_texts_do_not_say_email() -> None:
    texts = [CAPABILITIES_TEXT, *REFUSALS.values(), *static_texts()]
    assert all("email" not in t.lower() for t in texts)
    assert "personal details" in CAPABILITIES_TEXT
