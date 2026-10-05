"""D-162 / D-163: aggregate-only mode is sticky for the session; every band holds >= k customers.

D-162: once a turn of a session was a customer ranking, every later turn of the same session runs
run_sql in aggregate-only mode ("show their IDs", "list those customers" are refused at customer
grain). The flag lives in ``RunSqlSession`` and in the checkpointed graph state; it never resets
within the session, and a new session starts clear.

D-163: in aggregate-only mode a banded query must return a per-band customer count (SQL policy),
and run_sql merges every band whose count is below k into a neighbour (OD-3 / D-172), or hides it
when the bands may overlap (result side, so no SQL can bypass it). Offline, synthetic data only.
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
from opsfleet_agent.guards.sql_policy import (
    AggregateOnlyPlan,
    Rule,
    aggregate_only_plan,
    check_sql,
)
from opsfleet_agent.roles.analyst import ModelTurn
from opsfleet_agent.session import Session
from opsfleet_agent.tools.run_sql import (
    EMPTY_HINT_FIRST,
    MERGED_BANDS_HINT,
    SMALL_BANDS_HINT,
    RunSqlTurn,
    _merge_small_bands,
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


# --- D-163 / D-172: result side: merge small bands, or hide them --------------------------------

_LOCAL = ("avg_spend", "band", "buyers", "customers", "revenue")
MERGE = AggregateOnlyPlan(
    check_sql(SIMPLE), ("customers",), True, ("customers", "revenue"), ("band",), _LOCAL
)
HIDE = AggregateOnlyPlan(check_sql(SIMPLE), ("customers",), row_local=_LOCAL)


def _merge(rows: list[dict[str, Any]], plan: AggregateOnlyPlan = MERGE):
    return _merge_small_bands(rows, plan, DEFAULT_K)


def test_banded_query_is_mergeable_with_its_label_and_additive_columns() -> None:
    plan = aggregate_only_plan(BANDS)
    assert plan.mergeable and plan.labels and "customers" in plan.additive
    assert set(plan.labels).isdisjoint(plan.additive)


@pytest.mark.parametrize(
    "sql",
    [
        BANDS_DISTINCT.replace("GROUP BY oi.user_id", "GROUP BY oi.user_id, oi.order_id"),
        BANDS_UNION,
        _S + "SELECT COUNT(*) AS customers FROM s",  # one row: no label, nothing to merge into
    ],
)
def test_overlapping_or_unlabelled_bands_are_not_mergeable(sql: str) -> None:
    plan = aggregate_only_plan(sql)
    assert plan.decision.allowed and plan.band_counts and not plan.mergeable, sql


def test_rollup_bands_are_refused_before_any_merge() -> None:
    sql = _S + f"SELECT {_BAND} AS band, COUNT(*) AS customers FROM s GROUP BY ROLLUP(band)"
    assert not aggregate_only_plan(sql).decision.allowed  # overlapping groups never merge


def test_merge_columns_skip_averages_shares_and_distinct_sums() -> None:
    sql = _S + (
        f"SELECT {_BAND} AS band, COUNT(*) AS customers, SUM(s.spend) AS revenue, "
        "AVG(s.spend) AS avg_spend, ROUND(SUM(s.spend) / 10, 2) AS share, "
        "SUM(DISTINCT s.spend) AS d, COUNTIF(s.spend > 10) AS big FROM s GROUP BY band"
    )
    plan = aggregate_only_plan(sql)
    assert plan.mergeable and plan.labels == ("band",)
    assert plan.additive == ("big", "customers", "revenue")


def test_band_of_exactly_k_is_kept_and_k_minus_one_is_merged(tmp_path: Path) -> None:
    rows = [_band("1000+", DEFAULT_K - 1), _band("500-999", DEFAULT_K), _band("under 500", 40)]
    tool, _ = _tool_with(tmp_path, rows)
    turn = RunSqlTurn("t1", aggregate_only=True)
    out = call(tool, BANDS, new_session(), turn)
    assert out["ok"] is True
    data = out["data"]
    assert [r["band"] for r in data["rows"]] == ["other bands", "under 500"]
    assert data["rows"][0]["customers"] == 2 * DEFAULT_K - 1 and data["rows"][0]["revenue"] == 200
    assert data["row_count"] == 2 and "band(s)" not in data["suppressed_groups"]
    assert "merged with other bands" in data["suppressed_groups"]
    assert data["hint"] == MERGED_BANDS_HINT and turn.ledger[-1]["rows"] == 2


def test_all_bands_small_returns_no_rows_and_a_specific_hint(tmp_path: Path) -> None:
    tool, _ = _tool_with(tmp_path, [_band("a", 1), _band("b", 1), _band("c", 0)])
    turn = RunSqlTurn("t1", aggregate_only=True)
    out = call(tool, BANDS, new_session(), turn)
    assert out["ok"] is True and out["data"]["rows"] == [] and out["data"]["row_count"] == 0
    assert out["data"]["hint"] == SMALL_BANDS_HINT
    assert "bands hidden" in out["data"]["suppressed_groups"]
    assert turn.empty_results == 1  # still counted: the empty-result budget is bounded


def test_small_bands_that_add_up_to_k_become_one_row(tmp_path: Path) -> None:
    tool, _ = _tool_with(tmp_path, [_band("a", 2), _band("b", 2), _band("c", 1)])
    out = call(tool, BANDS, new_session(), RunSqlTurn("t1", aggregate_only=True))
    assert out["data"]["rows"] == [{"band": "other bands", "customers": 5, "revenue": 300.0}]


def test_empty_result_keeps_the_normal_empty_hint(tmp_path: Path) -> None:
    tool, _ = _tool_with(tmp_path, [])
    out = call(tool, BANDS, new_session(), RunSqlTurn("t1", aggregate_only=True))
    assert out["ok"] is True and out["data"]["rows"] == []
    assert out["data"]["hint"] == EMPTY_HINT_FIRST and out["data"]["suppressed_groups"] is None


def test_small_bands_are_not_filtered_outside_aggregate_only_mode(tmp_path: Path) -> None:
    tool, _ = _tool_with(tmp_path, [_band("a", 2)])
    out = call(tool, BANDS, new_session(), RunSqlTurn("t1"))
    assert out["ok"] is True and out["data"]["rows"] == [_band("a", 2)]


def test_union_bands_are_hidden_not_merged(tmp_path: Path) -> None:
    tool, _ = _tool_with(tmp_path, [_band("hi", 2), _band("lo", 40)])
    out = call(tool, BANDS_UNION, new_session(), RunSqlTurn("t1", aggregate_only=True))
    rows = out["data"]["rows"]
    assert [(r["band"], r["customers"]) for r in rows] == [("lo", 40)] and "hint" not in out["data"]
    assert "bands hidden" in out["data"]["suppressed_groups"]


def test_small_band_merges_into_the_smallest_big_band() -> None:
    rows, merged, hidden = _merge([_band("a", 12), _band("b", 1), _band("c", 9), _band("d", 9)])
    assert [r["band"] for r in rows] == ["a", "other bands", "d"] and (merged, hidden) == (1, 0)
    assert rows[1]["customers"] == 10


def test_merge_partner_ties_break_on_the_label() -> None:
    rows, merged, _ = _merge([_band("b", 9), _band("a", 9), _band("c", 1)])
    assert [r["band"] for r in rows] == ["b", "other bands"] and merged == 1
    assert rows[1]["customers"] == 10  # the partner is a, the first label


def test_all_small_bands_merge_into_one_row_first() -> None:
    rows, merged, _ = _merge([_band(n, 1) for n in "abcdefg"] + [_band("h", 9)])
    assert [(r["band"], r["customers"]) for r in rows] == [("other bands", 7), ("h", 9)]
    assert merged == 6


@pytest.mark.parametrize("seed", range(6))
def test_merge_does_not_depend_on_the_result_order(seed: int) -> None:
    """B2: re-sorting the same query must not show a different split of the small bands."""
    import random

    base = [_band("a", 3), _band("b", 10), _band("c", 10), _band("d", 2), _band("e", 30)]
    shuffled = list(base)
    random.Random(seed).shuffle(shuffled)

    def canon(rows: list[dict[str, Any]]) -> set:
        return {(r["band"], r["customers"], r["revenue"]) for r in rows}

    assert canon(_merge(shuffled)[0]) == canon(_merge(base)[0])
    assert canon(_merge(base)[0]) == {
        ("other bands", 5, 200.0), ("b", 10, 100.0), ("c", 10, 100.0), ("e", 30, 100.0),
    }


def test_small_bands_with_no_big_band_and_too_few_in_all_are_hidden() -> None:
    rows, merged, hidden = _merge([_band("a", 1), _band("b", 2)])
    assert rows == [] and (merged, hidden) == (0, 2)


def test_merged_row_empties_columns_that_cannot_be_added() -> None:
    rows = [
        {"band": "a", "customers": 2, "revenue": 10.0, "avg_spend": 5.0, "share": 0.1},
        {"band": "b", "customers": 9, "revenue": 90.0, "avg_spend": 10.0, "share": 0.9},
    ]
    merged, n, _ = _merge(rows)
    assert merged == [
        {"band": "other bands", "customers": 11, "revenue": 100.0, "avg_spend": None, "share": None}
    ] and n == 1


def test_merged_label_is_fixed_and_bad_sums_fail_closed() -> None:
    rows = [
        {"band": None, "customers": 2, "revenue": "x"},
        {"band": "b", "customers": Decimal(9), "revenue": 1.0},
    ]
    merged, _, _ = _merge(rows)
    assert merged == [{"band": "other bands", "customers": 11, "revenue": None}]


@pytest.mark.parametrize(
    ("values", "total"),
    [
        ([1.0, 2.0], 3.0),
        ([Decimal("1.5"), Decimal("2")], Decimal("3.5")),
        ([Decimal(1), 2.0], None),
        ([1.0, None], None),
        ([1, True], None),
        ([float("inf"), 1.0], None),
    ],
)
def test_additive_cells_fail_closed(values: list, total: Any) -> None:
    rows = [{"band": str(i), "customers": 3, "revenue": v} for i, v in enumerate(values)]
    merged, _, _ = _merge(rows)
    assert merged[0]["revenue"] == total


def test_multiple_count_columns_each_must_reach_k() -> None:
    plan = AggregateOnlyPlan(check_sql(SIMPLE), ("buyers", "customers"), True, (), ("band",))
    rows = [
        {"band": "a", "customers": 9, "buyers": 3},
        {"band": "b", "customers": 9, "buyers": 9},
    ]
    merged, n, _ = _merge(rows, plan)
    assert merged == [{"band": "other bands", "customers": 18, "buyers": 12}] and n == 1


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
@pytest.mark.parametrize("plan", [MERGE, HIDE], ids=["merge", "hide"])
def test_count_cells_fail_closed(value: Any, kept: bool, plan: AggregateOnlyPlan) -> None:
    rows, merged, hidden = _merge([_band("a", value)], plan)
    assert (rows != []) is kept and hidden == (0 if kept else 1) and merged == 0


def test_invalid_count_rows_are_dropped_before_merging() -> None:
    rows, merged, hidden = _merge([_band("a", 9), _band("b", None), _band("c", 2)])
    assert [r["band"] for r in rows] == ["other bands"] and (merged, hidden) == (1, 1)


def test_count_column_match_is_case_insensitive_and_missing_column_drops() -> None:
    rows = [{"band": "a", "Customers": 9}, {"band": "b", "n": 9}]
    for plan in (MERGE, HIDE):
        kept, _, hidden = _merge(rows, plan)
        assert kept == [{"band": "a", "Customers": 9}] and hidden == 1


def test_hide_mode_drops_every_small_band() -> None:
    rows = [{"band": "a", "customers": 9, "buyers": 3}, {"band": "b", "customers": 9, "buyers": 9}]
    plan = AggregateOnlyPlan(check_sql(SIMPLE), ("buyers", "customers"), row_local=_LOCAL)
    kept, merged, hidden = _merge(rows, plan)
    assert [r["band"] for r in kept] == ["b"] and (merged, hidden) == (0, 1)


def test_no_count_columns_means_no_filtering() -> None:
    rows = [{"status": "Complete", "n": 1}]
    assert _merge(rows, AggregateOnlyPlan(check_sql(SIMPLE))) == (rows, 0, 0)
    assert _merge_small_bands(rows, None, DEFAULT_K) == (rows, 0, 0)


def test_window_columns_are_emptied_once_a_band_is_merged() -> None:
    """S1: a window reads other rows, so it could still show a merged band."""
    plan = AggregateOnlyPlan(check_sql(SIMPLE), ("customers",), True, ("customers",), ("band",),
                             ("band", "customers"), ("share",))  # fmt: skip
    rows = [
        {"band": "a", "customers": 2, "share": 0.1, "next": 9},
        {"band": "b", "customers": 9, "share": 0.4, "next": 20},
        {"band": "c", "customers": 20, "share": 0.5, "next": None},
    ]
    out, merged, _ = _merge(rows, plan)
    assert merged == 1 and out == [
        {"band": "other bands", "customers": 11, "share": None, "next": None},
        {"band": "c", "customers": 20, "share": 0.5, "next": None},
    ]


def test_every_window_column_is_emptied_once_a_band_is_hidden() -> None:
    plan = AggregateOnlyPlan(check_sql(SIMPLE), ("customers",), row_local=("band", "customers"),
                             totals=("share",))  # fmt: skip
    rows = [
        {"band": "a", "customers": 2, "share": 0.1},
        {"band": "b", "customers": 9, "share": 0.9},
    ]
    out, _, hidden = _merge(rows, plan)
    assert out == [{"band": "b", "customers": 9, "share": None}] and hidden == 1


def test_banded_plan_names_row_local_and_grand_total_columns() -> None:
    sql = BANDS_DISTINCT.replace(
        "SUM(s.spend) AS revenue",
        "SUM(s.spend) AS revenue, SUM(s.spend) / SUM(SUM(s.spend)) OVER () AS share, "
        "LEAD(COUNT(*)) OVER (ORDER BY 1) AS nxt",
    )
    plan = aggregate_only_plan(sql)
    assert plan.decision.allowed and plan.mergeable
    assert plan.row_local == ("band", "n", "revenue") and plan.totals == ("share",)


_GROUP = "FROM s GROUP BY band"


@pytest.mark.parametrize(
    "label",
    [
        "s.spend",
        "ROUND(s.spend)",
        "CAST(s.spend AS STRING)",
        "CASE WHEN s.spend >= 1000 THEN CAST(s.spend AS STRING) ELSE 'b' END",
        "IF(s.spend >= 1000, s.spend, 0)",
    ],
)
def test_raw_value_labels_are_hidden_not_merged(label: str) -> None:
    """B1: a merged label would list each small band's own value."""
    plan = aggregate_only_plan(_S + f"SELECT {label} AS band, COUNT(*) AS customers {_GROUP}")
    assert plan.decision.allowed and plan.band_counts == ("customers",) and not plan.mergeable


def test_a_second_raw_label_makes_bands_unmergeable() -> None:
    sql = _S + (f"SELECT {_BAND} AS band, s.user_id + 0 AS u, COUNT(*) AS customers "
                "FROM s GROUP BY band, u")  # fmt: skip
    assert not aggregate_only_plan(sql).mergeable


def test_if_band_names_are_mergeable() -> None:
    band = "IF(s.spend >= 1000, 'big', 'small')"
    sql = _S + f"SELECT {band} AS band, COUNT(*) AS customers {_GROUP}"
    plan = aggregate_only_plan(sql)
    assert plan.mergeable and plan.labels == ("band",)


@pytest.mark.parametrize(
    "key",
    [
        "oi.user_id * 100 + EXTRACT(MONTH FROM oi.created_at)",
        "CONCAT(CAST(oi.user_id AS STRING), '-', "
        "CAST(EXTRACT(MONTH FROM oi.created_at) AS STRING))",
    ],
)
def test_derived_customer_keys_are_not_one_row_per_customer(key: str) -> None:
    """B3: a key built from user_id (customer-month) has many rows per customer."""
    cte = (f"WITH s AS (SELECT {key} AS um, SUM(oi.sale_price) AS spend FROM {OI} AS oi "
           "GROUP BY um) ")  # fmt: skip
    for count in ("COUNT(*)", "COUNT(DISTINCT s.um)"):
        plan = aggregate_only_plan(cte + f"SELECT {_BAND} AS band, {count} AS customers {_GROUP}")
        assert not plan.decision.allowed and plan.band_counts == ()


def test_subquery_in_the_banded_select_list_is_refused() -> None:
    """S2: a scalar subquery could count a small set of customers outside every band."""
    whales = "(SELECT COUNT(*) FROM s WHERE s.spend > 5000) AS whales"
    sql = _S + f"SELECT {_BAND} AS band, COUNT(*) AS customers, {whales} {_GROUP}"
    plan = aggregate_only_plan(sql)
    assert plan.decision.reason_code is Rule.BAND_COUNT_REQUIRED
    assert "subquery" in plan.decision.hint


@pytest.mark.parametrize(
    ("total", "kept"),
    [
        ("SUM(COUNT(*)) OVER ()", True),
        ("SUM(SUM(s.spend)) OVER ()", True),
        ("SUM(MIN(s.spend)) OVER ()", False),
        ("SUM(IF(COUNT(*) = 1, MIN(s.spend), 0)) OVER ()", False),
        ("SUM(COUNT(DISTINCT s.user_id)) OVER ()", False),
    ],
)
def test_only_totals_of_additive_aggregates_survive_a_merge(total: str, kept: bool) -> None:
    """A total of a non-additive value could single out a small band (D-172 review)."""
    plan = aggregate_only_plan(_S + f"SELECT {_BAND} AS band, COUNT(*) AS customers, "
                                    f"{total} AS t {_GROUP}")  # fmt: skip
    assert plan.decision.allowed and ("t" in plan.totals) is kept


@pytest.mark.parametrize(
    "clause",
    [
        "QUALIFY LAG(COUNT(*)) OVER (ORDER BY band) = 3",
        "HAVING (SELECT COUNT(*) FROM s WHERE s.spend > 20000) = 3",
        "HAVING 3 IN (SELECT COUNT(*) FROM s WHERE s.spend > 20000)",
        "ORDER BY (SELECT MAX(s2.spend) FROM s AS s2)",
    ],
)
def test_filters_that_gate_banded_rows_on_a_subquery_are_refused(clause: str) -> None:
    """Row presence must not depend on a small set of customers (D-172 review)."""
    plan = aggregate_only_plan(_S + f"SELECT {_BAND} AS band, COUNT(*) AS customers {_GROUP} "
                                    f"{clause}")  # fmt: skip
    assert plan.decision.reason_code is Rule.BAND_COUNT_REQUIRED


def test_where_scalar_subquery_is_refused_and_an_in_filter_is_allowed() -> None:
    def plan(where: str) -> AggregateOnlyPlan:
        return aggregate_only_plan(_S + f"SELECT {_BAND} AS band, COUNT(*) AS customers "
                                        f"FROM s WHERE {where} GROUP BY band")  # fmt: skip

    gate = plan("(SELECT MAX(s2.spend) FROM s AS s2) > 25000")
    assert gate.decision.reason_code is Rule.BAND_COUNT_REQUIRED
    population = plan(f"s.user_id IN (SELECT oi.user_id FROM {OI} AS oi WHERE oi.sale_price > 5)")
    assert population.decision.allowed


def test_bands_rule_tells_the_model_small_bands_are_merged() -> None:
    assert f"fewer than {DEFAULT_K} customers" in CUSTOMER_BANDS_RULE
    assert "COUNT(*) AS customers" in CUSTOMER_BANDS_RULE and "merged" in CUSTOMER_BANDS_RULE


# --- D-162: run_sql session flag --------------------------------------------------------------


@pytest.mark.parametrize("sql", ID_GRAIN)
def test_sticky_session_refuses_id_grain_on_a_plain_turn(tmp_path: Path, sql: str) -> None:
    tool, client = make_tool(tmp_path)
    session = new_session()
    session.aggregate_only = True
    out = call(tool, sql, session, RunSqlTurn("turn-2"))  # the turn itself is not a ranking
    assert out["ok"] is False and out["error"]["rule"] == Rule.CUSTOMER_GRAIN.value
    assert not [c for c in client.calls if not c["job_config"].dry_run]


def test_sticky_session_applies_band_merging_on_a_plain_turn(tmp_path: Path) -> None:
    tool, _ = _tool_with(tmp_path, [_band("a", 3), _band("b", 30)])
    session = new_session()
    session.aggregate_only = True
    out = call(tool, BANDS, session, RunSqlTurn("turn-2"))
    assert [r["band"] for r in out["data"]["rows"]] == ["other bands"]


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
