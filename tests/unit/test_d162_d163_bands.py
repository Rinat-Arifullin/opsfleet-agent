"""D-162 / D-163: aggregate-only mode is sticky for the session; every band holds >= k customers.

D-162: once a turn of a session was a customer ranking, every later turn of the same session runs
run_sql in aggregate-only mode ("show their IDs", "list those customers" are refused at customer
grain). The flag lives in ``RunSqlSession`` and in the checkpointed graph state; it never resets
within the session, and a new session starts clear.

D-163: in aggregate-only mode a banded query must return a per-band customer count (SQL policy),
and run_sql drops every result row whose count is below k (result side, so no SQL can bypass it).
Offline, synthetic data only.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from opsfleet_agent.graph.intents import (
    CUSTOMER_BANDS_NOTICE,
    CUSTOMER_BANDS_RULE,
    mentions_customer_id,
    mentions_customers,
)
from opsfleet_agent.guards.small_cell import DEFAULT_K
from opsfleet_agent.guards.sql_policy import Rule, aggregate_only_plan, check_sql
from opsfleet_agent.roles.analyst import ModelTurn
from opsfleet_agent.session import Session
from opsfleet_agent.tools.run_sql import (
    EMPTY_HINT_FIRST,
    SMALL_BANDS_HINT,
    RunSqlTurn,
    _suppress_small_bands,
)
from tests.unit.test_d159_customer_bands import BANDS, BY_USER, CLEAN, ID_GRAIN, TOP10, WITH_IDS
from tests.unit.test_graph import PROFILE, Router, Scripted, sql_call
from tests.unit.test_graph import detector as detector  # noqa: F401  (fixture)
from tests.unit.test_graph import make_env as make_env  # noqa: F401  (fixture)
from tests.unit.test_graph import settings as settings  # noqa: F401  (fixture)
from tests.unit.test_run_sql import OI, SIMPLE, RoutingClient, call, make_tool, new_session

CLEAN2 = "The bands above are the most detailed view available for customers."
CLEAN3 = "Revenue by status is shown in the table above."
FOLLOW_UPS = ["Show their IDs", "List those customers", "Which customers are in the top band?"]

_S = (
    f"WITH s AS (SELECT oi.user_id, SUM(oi.sale_price) AS spend FROM {OI} AS oi "
    "GROUP BY oi.user_id) "
)
_BAND = "CASE WHEN s.spend >= 1000 THEN 'a' ELSE 'b' END"
BANDS_NO_COUNT = _S + f"SELECT {_BAND} AS band, SUM(s.spend) AS revenue FROM s GROUP BY band"
BANDS_UNALIASED = _S + f"SELECT {_BAND} AS band, COUNT(*) FROM s GROUP BY band"
BANDS_DISTINCT = (
    _S + f"SELECT {_BAND} AS band, COUNT(DISTINCT s.user_id) AS n, SUM(s.spend) AS revenue "
    "FROM s GROUP BY 1"
)
BANDS_UNION = (
    _S + "SELECT 'hi' AS band, COUNT(*) AS customers FROM s WHERE s.spend >= 500 "
    "UNION ALL SELECT 'lo' AS band, COUNT(*) AS customers FROM s WHERE s.spend < 500"
)
# one row per customer and month: COUNT(*) counts customer-months, not customers
BANDS_CUSTOMER_MONTH = (
    f"WITH s AS (SELECT oi.user_id, DATE_TRUNC(oi.created_at, MONTH) AS m, "
    f"SUM(oi.sale_price) AS spend FROM {OI} AS oi GROUP BY oi.user_id, m) "
    f"SELECT {_BAND} AS band, COUNT(*) AS customers FROM s GROUP BY band"
)


def _band(name: str, customers: Any, revenue: float = 100.0) -> dict[str, Any]:
    return {"band": name, "customers": customers, "revenue": revenue}


def _tool_with(tmp_path: Path, rows: list[dict[str, Any]]):
    return make_tool(tmp_path, RoutingClient(lambda sql: [dict(r) for r in rows]))


def _text(calls: list) -> str:
    return json.dumps([c[1] for c in calls], default=str)


def _state(env) -> dict[str, Any]:
    stored = env.saver.get_tuple({"configurable": {"thread_id": env.session.session_id}})
    return dict(stored.checkpoint["channel_values"]) if stored else {}


# --- D-163: SQL policy ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "counts"),
    [(BANDS, ("customers",)), (BANDS_DISTINCT, ("n",)), (BANDS_UNION, ("customers",))],
)
def test_banded_query_names_its_customer_count_columns(sql: str, counts: tuple) -> None:
    plan = aggregate_only_plan(sql)
    assert plan.decision.allowed and plan.band_counts == counts


@pytest.mark.parametrize("sql", [BANDS_NO_COUNT, BANDS_UNALIASED, BANDS_CUSTOMER_MONTH])
def test_banded_query_without_a_customer_count_is_refused(sql: str) -> None:
    assert check_sql(sql).allowed  # outside aggregate-only mode: unchanged
    plan = aggregate_only_plan(sql)
    assert not plan.decision.allowed and plan.decision.reason_code is Rule.BAND_COUNT_REQUIRED
    assert plan.decision.error_code == "SQL_POLICY" and "customers" in plan.decision.hint
    assert plan.band_counts == ()


def test_unbanded_aggregate_needs_no_count_column() -> None:
    plan = aggregate_only_plan(SIMPLE)
    assert plan.decision.allowed and plan.band_counts == ()


def test_id_grain_still_refused_first() -> None:
    assert aggregate_only_plan(BY_USER).decision.reason_code is Rule.CUSTOMER_GRAIN


# --- D-163: result-side suppression -----------------------------------------------------------


def test_band_of_exactly_k_is_kept_and_k_minus_one_is_hidden(tmp_path: Path) -> None:
    rows = [_band("1000+", DEFAULT_K - 1), _band("500-999", DEFAULT_K), _band("under 500", 40)]
    tool, _ = _tool_with(tmp_path, rows)
    turn = RunSqlTurn("t1", aggregate_only=True)
    out = call(tool, BANDS, new_session(), turn)
    assert out["ok"] is True
    data = out["data"]
    assert [r["band"] for r in data["rows"]] == ["500-999", "under 500"]
    assert data["row_count"] == 2 and "1 band(s)" in data["suppressed_groups"]
    assert f"fewer than {DEFAULT_K} customers" in data["suppressed_groups"]
    assert "hint" not in data and turn.ledger[-1]["rows"] == 2


def test_all_bands_small_returns_no_rows_and_a_specific_hint(tmp_path: Path) -> None:
    tool, _ = _tool_with(tmp_path, [_band("a", 1), _band("b", DEFAULT_K - 1), _band("c", 0)])
    turn = RunSqlTurn("t1", aggregate_only=True)
    out = call(tool, BANDS, new_session(), turn)
    assert out["ok"] is True and out["data"]["rows"] == [] and out["data"]["row_count"] == 0
    assert out["data"]["hint"] == SMALL_BANDS_HINT
    assert "3 band(s)" in out["data"]["suppressed_groups"]
    assert turn.empty_results == 1  # still counted: the empty-result budget is bounded


def test_empty_result_keeps_the_normal_empty_hint(tmp_path: Path) -> None:
    tool, _ = _tool_with(tmp_path, [])
    out = call(tool, BANDS, new_session(), RunSqlTurn("t1", aggregate_only=True))
    assert out["ok"] is True and out["data"]["rows"] == []
    assert out["data"]["hint"] == EMPTY_HINT_FIRST and out["data"]["suppressed_groups"] is None


def test_small_bands_are_not_filtered_outside_aggregate_only_mode(tmp_path: Path) -> None:
    tool, _ = _tool_with(tmp_path, [_band("a", 2)])
    out = call(tool, BANDS, new_session(), RunSqlTurn("t1"))
    assert out["ok"] is True and out["data"]["rows"] == [_band("a", 2)]


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        (DEFAULT_K, True),
        (DEFAULT_K - 1, False),
        (Decimal(DEFAULT_K), True),
        (float(DEFAULT_K), True),
        (DEFAULT_K + 0.5, False),
        (None, False),
        (True, False),
        ("5", False),
        (float("nan"), False),
        (-1, False),
    ],
)
def test_count_cells_fail_closed(value: Any, kept: bool) -> None:
    rows, hidden = _suppress_small_bands([_band("a", value)], ("customers",), DEFAULT_K)
    assert (rows != []) is kept and hidden == (0 if kept else 1)


def test_count_column_match_is_case_insensitive_and_missing_column_drops() -> None:
    rows = [{"band": "a", "Customers": 9}, {"band": "b", "n": 9}]
    kept, hidden = _suppress_small_bands(rows, ("customers",), DEFAULT_K)
    assert kept == [{"band": "a", "Customers": 9}] and hidden == 1


def test_every_count_column_must_reach_k() -> None:
    rows = [{"band": "a", "customers": 9, "buyers": 3}, {"band": "b", "customers": 9, "buyers": 9}]
    kept, hidden = _suppress_small_bands(rows, ("buyers", "customers"), DEFAULT_K)
    assert [r["band"] for r in kept] == ["b"] and hidden == 1


def test_no_count_columns_means_no_filtering() -> None:
    rows = [{"status": "Complete", "n": 1}]
    assert _suppress_small_bands(rows, (), DEFAULT_K) == (rows, 0)


def test_bands_rule_tells_the_model_small_bands_are_hidden() -> None:
    assert f"fewer than {DEFAULT_K} customers" in CUSTOMER_BANDS_RULE
    assert "COUNT(*) AS customers" in CUSTOMER_BANDS_RULE and "hidden" in CUSTOMER_BANDS_RULE


# --- D-162: run_sql session flag --------------------------------------------------------------


@pytest.mark.parametrize("sql", ID_GRAIN)
def test_sticky_session_refuses_id_grain_on_a_plain_turn(tmp_path: Path, sql: str) -> None:
    tool, client = make_tool(tmp_path)
    session = new_session()
    session.aggregate_only = True
    out = call(tool, sql, session, RunSqlTurn("turn-2"))  # the turn itself is not a ranking
    assert out["ok"] is False and out["error"]["rule"] == Rule.CUSTOMER_GRAIN.value
    assert not [c for c in client.calls if not c["job_config"].dry_run]


def test_sticky_session_applies_band_suppression_on_a_plain_turn(tmp_path: Path) -> None:
    tool, _ = _tool_with(tmp_path, [_band("a", 3), _band("b", 30)])
    session = new_session()
    session.aggregate_only = True
    out = call(tool, BANDS, session, RunSqlTurn("turn-2"))
    assert [r["band"] for r in out["data"]["rows"]] == ["b"]


def test_session_flag_defaults_off() -> None:
    assert new_session().aggregate_only is False


# --- D-162: graph, follow-up turns ------------------------------------------------------------


def _ranking_then(make_env, follow_up_steps: list[Any], **kw):  # noqa: F811
    analyst = Scripted(sql_call(BANDS, "c1"), ModelTurn(CLEAN), *follow_up_steps)
    env = make_env(Router("simple"), analyst, **kw)
    first = env.ask(TOP10)
    assert first.outcome == "answered" and first.notice == CUSTOMER_BANDS_NOTICE
    return env, analyst


@pytest.mark.parametrize("follow_up", FOLLOW_UPS)
def test_follow_up_after_a_bands_answer_stays_aggregate_only(
    make_env,  # noqa: F811
    follow_up: str,
) -> None:
    env, analyst = _ranking_then(make_env, [sql_call(BY_USER, "c2"), ModelTurn(CLEAN2)])
    assert _state(env).get("aggregate_only") is True
    assert env.graph._sql_sessions[env.session.session_id].aggregate_only is True
    before = len(analyst.calls)
    out = env.ask(follow_up)
    assert out.outcome == "answered" and not mentions_customer_id(out.text)
    assert out.sql_queries == 0  # the per-customer query never ran
    sent = _text(analyst.calls[before:])
    assert Rule.CUSTOMER_GRAIN.value in sent and CUSTOMER_BANDS_RULE in sent
    assert out.notice == CUSTOMER_BANDS_NOTICE
    assert _state(env).get("aggregate_only") is True  # not reset by the follow-up


def test_flag_stays_set_across_several_unrelated_turns(make_env) -> None:  # noqa: F811
    env, analyst = _ranking_then(
        make_env, [sql_call(SIMPLE, "c2"), ModelTurn(CLEAN3), sql_call(BY_USER, "c3"),
                   ModelTurn(CLEAN2)],
    )  # fmt: skip
    plain = env.ask("What is revenue by order status?")
    assert plain.outcome == "answered" and plain.notice is None  # no notice on unrelated turns
    before = len(analyst.calls)
    out = env.ask("Show their IDs")
    assert out.sql_queries == 0 and Rule.CUSTOMER_GRAIN.value in _text(analyst.calls[before:])


def test_follow_up_answer_with_ids_is_retried(make_env) -> None:  # noqa: F811
    env, _ = _ranking_then(make_env, [ModelTurn(WITH_IDS), ModelTurn(CLEAN2)])
    out = env.ask("List those customers")
    assert out.outcome == "answered" and out.text.startswith(CLEAN2)
    verdicts = [f["verdict"] for t, n, f in env.spans if t == "guard" and n == "customer_id"]
    assert verdicts == ["retry"]


def test_flag_survives_a_process_restart(make_env) -> None:  # noqa: F811
    env, _ = _ranking_then(make_env, [])
    analyst = Scripted(sql_call(BY_USER, "c9"), ModelTurn(CLEAN2))
    fresh = make_env(Router("simple"), analyst, saver=env.saver)  # new AgentGraph, same store
    assert env.session.session_id not in fresh.graph._sql_sessions
    out = fresh.ask("Show their IDs")
    assert out.sql_queries == 0 and Rule.CUSTOMER_GRAIN.value in _text(analyst.calls)
    assert fresh.graph._sql_sessions[env.session.session_id].aggregate_only is True


def test_new_session_starts_clear(make_env) -> None:  # noqa: F811
    env, analyst = _ranking_then(make_env, [sql_call(BY_USER, "c2"), ModelTurn(CLEAN2)])
    env.session = Session("sess-2", PROFILE)
    out = env.ask("What does each customer spend?")
    assert out.sql_queries == 1  # a different session: the per-customer query is allowed
    assert _state(env).get("aggregate_only") in (None, False)
    assert env.graph._sql_sessions["sess-2"].aggregate_only is False


def test_session_without_a_ranking_is_not_aggregate_only(make_env) -> None:  # noqa: F811
    analyst = Scripted(sql_call(BY_USER, "c1"), ModelTurn(CLEAN2))
    env = make_env(Router("simple"), analyst)
    out = env.ask("What does each customer spend?")
    assert out.sql_queries == 1 and out.notice is None
    assert not _state(env).get("aggregate_only")


@pytest.mark.parametrize(
    ("text", "hit"),
    [("Show their IDs", True), ("list those customers", True), ("who are they?", True),
     ("revenue by month", False), ("", False), (None, False)],
)  # fmt: skip
def test_mentions_customers(text: Any, hit: bool) -> None:
    assert mentions_customers(text) is hit
