"""D-159: a customer ranking is answered with spend bands and customer counts only.

Code-enforced: on a customer-ranking turn run_sql refuses a query at customer / order / item
grain or one that returns an id column (a retryable SQL_POLICY failure), and an answer that names
customers by ID gets one bounded retry, then the force-answer fallback. Offline, synthetic only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from opsfleet_agent.graph import graph as gr
from opsfleet_agent.graph.intents import (
    CUSTOMER_BANDS_NOTICE,
    CUSTOMER_BANDS_RULE,
    mentions_customer_id,
)
from opsfleet_agent.guards.small_cell import DEFAULT_K
from opsfleet_agent.guards.sql_policy import Rule, check_aggregate_only, check_sql
from opsfleet_agent.roles.analyst import ModelTurn
from opsfleet_agent.tools.run_sql import RunSqlTurn
from tests.unit.test_d156_echo import Echoing
from tests.unit.test_graph import Router, Scripted, sql_call
from tests.unit.test_graph import detector as detector  # noqa: F401  (fixture)
from tests.unit.test_graph import make_env as make_env  # noqa: F401  (fixture)
from tests.unit.test_graph import settings as settings  # noqa: F401  (fixture)
from tests.unit.test_run_sql import OI, ORD, SIMPLE, call, make_tool, new_session

TOP10 = "Who are our top 10 customers by total spend?"
CLEAN = "Most revenue comes from the highest spend band, shown in the table above."
WITH_IDS = "Customer ID 10234 spent the most, followed by customer ID 2231."

BY_USER = (
    f"SELECT oi.user_id, SUM(oi.sale_price) AS spend FROM {OI} AS oi "
    "GROUP BY oi.user_id ORDER BY spend DESC LIMIT 10"
)
BANDS = (
    f"WITH s AS (SELECT oi.user_id, SUM(oi.sale_price) AS spend FROM {OI} AS oi "
    "GROUP BY oi.user_id) "
    "SELECT CASE WHEN s.spend >= 1000 THEN '1000+' WHEN s.spend >= 500 THEN '500-999' "
    "ELSE 'under 500' END AS band, COUNT(*) AS customers, SUM(s.spend) AS revenue "
    "FROM s GROUP BY band"
)
ID_GRAIN = [
    BY_USER,
    f"SELECT MAX(oi.user_id) AS top_user, SUM(oi.sale_price) AS spend FROM {OI} AS oi",
    f"SELECT o.order_id, o.status FROM {ORD} AS o LIMIT 10",
    f"SELECT ANY_VALUE(o.user_id) AS u, COUNT(*) AS n FROM {ORD} AS o GROUP BY o.status",
    f"SELECT s.user_id FROM (SELECT oi.user_id, SUM(oi.sale_price) AS t FROM {OI} AS oi "
    "GROUP BY oi.user_id) AS s",
]
AGGREGATE = [
    BANDS,
    SIMPLE,
    f"SELECT COUNT(DISTINCT oi.user_id) AS customers, SUM(oi.sale_price) AS revenue FROM {OI} oi",
]


# --- SQL policy -------------------------------------------------------------------------------


@pytest.mark.parametrize("sql", ID_GRAIN)
def test_id_grain_is_refused_on_a_ranking_turn(sql: str) -> None:
    assert check_sql(sql).allowed  # D-159 is narrow: allowed on other turns (ADR-013)
    decision = check_aggregate_only(sql)
    assert not decision.allowed and decision.reason_code is Rule.CUSTOMER_GRAIN
    assert decision.error_code == "SQL_POLICY" and "bands" in decision.hint
    assert "SELECT" not in decision.hint


@pytest.mark.parametrize("sql", AGGREGATE)
def test_banded_aggregates_are_allowed(sql: str) -> None:
    assert check_aggregate_only(sql).allowed


def test_aggregate_only_keeps_the_base_policy() -> None:
    pii = "SELECT u.email FROM `bigquery-public-data.thelook_ecommerce.users` AS u"
    assert check_aggregate_only(pii).reason_code is Rule.PII_PROJECTION
    assert check_aggregate_only("SELECT FROM WHERE (").reason_code is Rule.SQL_SYNTAX
    assert not check_aggregate_only(None).allowed  # type: ignore[arg-type]


# --- run_sql --------------------------------------------------------------------------------


def test_run_sql_refuses_id_grain_only_when_the_turn_is_aggregate_only(tmp_path: Path) -> None:
    tool, client = make_tool(tmp_path)
    session = new_session()
    ranking = RunSqlTurn("turn-1", aggregate_only=True)
    out = call(tool, BY_USER, session, ranking)
    assert out["ok"] is False and out["error"]["rule"] == Rule.CUSTOMER_GRAIN.value
    assert out["error"]["retryable"] is True and not client.calls
    assert ranking.consecutive_failures == 1 and not ranking.ledger
    assert call(tool, BANDS, session, ranking)["ok"] is True
    plain = RunSqlTurn("turn-2")
    assert call(tool, BY_USER, session, plain)["ok"] is True  # other turns: unchanged


def test_run_sql_gives_up_after_bounded_id_grain_retries(tmp_path: Path) -> None:
    tool, _ = make_tool(tmp_path)
    session, turn = new_session(), RunSqlTurn("turn-1", aggregate_only=True)
    outs = [call(tool, sql, session, turn) for sql in ID_GRAIN]
    assert all(o["ok"] is False for o in outs)
    assert turn.gave_up or outs[-1]["error"]["retryable"] is False


# --- answer check -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [WITH_IDS, "user #881 is first", "the user_id column", "| Customer ID | Spend |", "userid 5"],
)
def test_customer_ids_in_an_answer_are_detected(text: str) -> None:
    assert mentions_customer_id(text)


@pytest.mark.parametrize(
    "text",
    [
        CLEAN,
        "I show bands, not individual customer IDs.",
        "3,120 customers spent $100 to $499",
        "",
        None,
    ],
)
def test_band_answers_are_not_flagged(text: Any) -> None:
    assert not mentions_customer_id(text)


def test_bands_rule_names_k_and_revenue_share() -> None:
    assert f"fewer than {DEFAULT_K} customers" in CUSTOMER_BANDS_RULE
    assert "share of total revenue" in CUSTOMER_BANDS_RULE


# --- graph ----------------------------------------------------------------------------------


def _text(calls: list) -> str:
    return json.dumps([c[1] for c in calls], default=str)


def _guard_spans(env) -> list[dict[str, Any]]:
    return [f for t, n, f in env.spans if t == "guard" and n == "customer_id"]


@pytest.mark.parametrize("label", ["simple", "injection"])
def test_ranking_turn_retries_an_id_grain_query_then_answers_with_bands(
    make_env,  # noqa: F811
    label: str,
) -> None:
    analyst = Scripted(sql_call(BY_USER, "c1"), sql_call(BANDS, "c2"), ModelTurn(CLEAN))
    env = make_env(Router(label), analyst)
    out = env.ask(TOP10)
    assert out.outcome == "answered" and out.notice == CUSTOMER_BANDS_NOTICE
    assert out.text.startswith(CLEAN) and out.sql_queries == 1
    sent = _text(analyst.calls)
    assert CUSTOMER_BANDS_RULE in sent and Rule.CUSTOMER_GRAIN.value in sent


def test_non_ranking_turn_gets_no_bands_rule(make_env) -> None:  # noqa: F811
    analyst = Scripted(ModelTurn(CLEAN))
    env = make_env(Router("simple"), analyst)
    out = env.ask("What is revenue by category?")
    assert out.notice is None and CUSTOMER_BANDS_RULE not in _text(analyst.calls)


def test_answer_with_ids_is_retried_once(make_env) -> None:  # noqa: F811
    analyst = Scripted(ModelTurn(WITH_IDS), ModelTurn(CLEAN))
    env = make_env(Router("simple"), analyst)
    out = env.ask(TOP10)
    assert out.outcome == "answered" and out.text.startswith(CLEAN)
    assert not mentions_customer_id(out.text)
    assert [s["verdict"] for s in _guard_spans(env)] == ["retry"]


def test_answer_with_ids_twice_is_never_shown(make_env) -> None:  # noqa: F811
    analyst = Scripted(ModelTurn(WITH_IDS))
    env = make_env(Router("simple"), analyst)
    out = env.ask(TOP10)
    assert not mentions_customer_id(out.text) and "10234" not in out.text
    assert [s["verdict"] for s in _guard_spans(env)] == ["retry", "block"]
    assert len([c for c in analyst.calls if c[2]]) == 2  # two analyst calls: bounded


def test_force_answer_with_ids_falls_back_to_the_template(make_env, monkeypatch) -> None:  # noqa: F811
    env = make_env(Router("simple"), Echoing(sql_call(SIMPLE), ModelTurn(WITH_IDS), force=WITH_IDS))
    out = env.ask(TOP10)
    assert "10234" not in out.text and not mentions_customer_id(out.text)
    assert [s["verdict"] for s in _guard_spans(env)] == ["retry", "block", "block"]
    assert gr.CUSTOMER_ID_REJECTED in _guard_spans(env)[-1]["rule_hits"]
