"""Red-team cases for the small-cell rule (iteration 9). Synthetic SQL only, no network.

Each case is refused (by the policy inside ``apply_scope`` or by ``apply_small_cell``) or is
rewritten with a threshold at the right level. A refusal must never be weaker than expected.
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from opsfleet_agent.guards.scope import verify_scoped
from tests.unit.test_small_cell import (
    ACME,
    ALL,
    OI,
    ORD,
    P,
    U,
    havings,
    outcome,
    rewrite,
    run_local,
)

POSITION = {"qi_position"}
GRAIN = {"qi_at_id_grain"}
UNPLACEABLE = {"small_cell_unplaceable"}
DIFFERENCING = {"qi_differencing"}
# ARRAY_AGG / APPROX_TOP_COUNT / ROLLUP / SELECT * fail earlier, on other policy rules.
EARLY = {"unresolved_value", "function_denied", "unsupported_syntax", "select_star"}

JOIN_UOI = f"FROM {OI} oi JOIN {U} u ON u.id = oi.user_id JOIN {P} p ON p.id = oi.product_id"

REFUSED: list[tuple[str, str, set[str]]] = [
    # Review B1: a QI expression inside a counting aggregate counts a hidden sub-cell.
    (
        "b1_a11_count_or_null",
        f"SELECT u.country, COUNT(*) AS n, COUNT(u.state = 'CC' OR NULL) AS m FROM {U} u "
        "GROUP BY u.country",
        POSITION,
    ),
    (
        "b1_a12_count_and_or_null",
        f"SELECT u.country, COUNT(u.age = 32 AND u.city = 'TownCC' OR NULL) AS m FROM {U} u "
        "GROUP BY u.country",
        POSITION,
    ),
    ("b1_a13_whole", f"SELECT COUNT(u.state = 'AA' OR NULL) AS m FROM {U} u", POSITION),
    (
        "b1_a14_count_distinct_expr",
        f"SELECT u.country, COUNT(DISTINCT u.age = 32 OR NULL) AS m FROM {U} u GROUP BY u.country",
        POSITION,
    ),
    (
        "b1_a15_per_category",
        f"SELECT p.category, COUNT(u.state = 'AA' OR NULL) AS m {JOIN_UOI} GROUP BY 1",
        POSITION,
    ),
    (
        "b1_h4_having",
        f"SELECT u.country, COUNT(*) AS n FROM {U} u GROUP BY u.country "
        "HAVING COUNT(u.age = 32 OR NULL) > 0",
        POSITION,
    ),
    (
        "b1_h7_like",
        f"SELECT u.country, COUNT(u.city LIKE 'Town%' OR NULL) AS m FROM {U} u GROUP BY u.country",
        POSITION,
    ),
    (
        "b1_h8_in",
        f"SELECT u.country, COUNT(u.age IN (32) OR NULL) AS m FROM {U} u GROUP BY u.country",
        POSITION,
    ),
    (
        "b1_nullif",
        f"SELECT u.country, COUNT(NULLIF(u.state, 'CC')) AS m FROM {U} u GROUP BY u.country",
        POSITION,
    ),
    (
        "b1_safe_divide",
        f"SELECT u.country, COUNT(SAFE_DIVIDE(1, u.age - 32)) AS m FROM {U} u GROUP BY u.country",
        POSITION | EARLY,
    ),
    (
        "b1_coalesce_nullif",
        f"SELECT u.country, COUNT(COALESCE(NULLIF(u.state, 'CC'), NULL)) AS m FROM {U} u "
        "GROUP BY u.country",
        POSITION,
    ),
    (
        "b1_greatest",
        f"SELECT u.country, COUNT(DISTINCT GREATEST(u.age, 32)) AS m FROM {U} u GROUP BY u.country",
        POSITION | EARLY,
    ),
    (
        "b1_countif_having",
        f"SELECT u.country, COUNT(*) AS n FROM {U} u GROUP BY u.country "
        "HAVING COUNTIF(u.state = 'CC') > 0",
        POSITION,
    ),
    (
        "b1_sum_over_case",
        f"SELECT u.country, SUM(CASE WHEN u.state = 'CC' THEN 1 ELSE 0 END) AS m FROM {U} u "
        "GROUP BY u.country",
        POSITION,
    ),
    (
        "b1_count_non_distinct_qi",
        f"SELECT u.country, COUNT(u.city) AS m FROM {U} u GROUP BY u.country",
        POSITION,
    ),
    (
        "b1_count_distinct_derived_cte_column",
        f"WITH t AS (SELECT u.id, u.country, u.state = 'CC' OR NULL AS f FROM {U} u) "
        "SELECT t.country, COUNT(DISTINCT t.f) AS m FROM t GROUP BY t.country",
        POSITION,
    ),
    # Review M1: more than one aggregating level in a QI statement (differencing).
    (
        "m1_d17_joined_ctes_subtracted",
        f"WITH a AS (SELECT u.country, COUNT(*) AS n FROM {U} u GROUP BY u.country), "
        f"b AS (SELECT u.country, COUNT(*) AS n FROM {U} u WHERE u.state <> 'CC' "
        "GROUP BY u.country) "
        "SELECT a.country, a.n - b.n AS d FROM a JOIN b ON a.country = b.country",
        DIFFERENCING,
    ),
    (
        "m1_g3_except_distinct",
        f"SELECT u.country, COUNT(*) AS n FROM {U} u GROUP BY u.country EXCEPT DISTINCT "
        f"SELECT u.country, COUNT(*) AS n FROM {U} u WHERE u.age <> 21 GROUP BY u.country",
        DIFFERENCING,
    ),
    (
        "m1_g3_intersect_distinct",
        f"SELECT u.country, COUNT(*) AS n FROM {U} u GROUP BY u.country INTERSECT DISTINCT "
        f"SELECT u.country, COUNT(*) AS n FROM {U} u WHERE u.age <> 21 GROUP BY u.country",
        DIFFERENCING,
    ),
    (
        "m1_g3_union_all",
        f"SELECT u.country, COUNT(*) AS n FROM {U} u GROUP BY u.country UNION ALL "
        f"SELECT u.country, COUNT(*) AS n FROM {U} u WHERE u.age <> 21 GROUP BY u.country",
        DIFFERENCING,
    ),
    (
        "m1_d3_groups_plus_total",
        f"SELECT u.state, COUNT(*) AS n FROM {U} u GROUP BY u.state UNION ALL "
        f"SELECT 'ALL' AS state, COUNT(*) AS n FROM {U} u",
        DIFFERENCING,
    ),
    (
        "m1_d4_total_union_filtered_total",
        f"SELECT COUNT(*) AS n FROM {U} u UNION ALL "
        f"SELECT COUNT(*) AS n FROM {U} u WHERE u.state <> 'CC'",
        DIFFERENCING,
    ),
    (
        "m1_g6_total_minus_filtered_total",
        f"WITH a AS (SELECT COUNT(*) AS t FROM {U} u), "
        f"b AS (SELECT COUNT(*) AS t FROM {U} u WHERE u.state <> 'CC') "
        "SELECT a.t - b.t AS d FROM a CROSS JOIN b",
        DIFFERENCING,
    ),
    (
        "m1_cell_next_to_orders_total",
        f"WITH a AS (SELECT u.state, COUNT(*) AS n FROM {U} u JOIN {ORD} o "
        "ON o.user_id = u.id GROUP BY u.state), "
        f"b AS (SELECT COUNT(*) AS t FROM {ORD} o) "
        "SELECT a.state, a.n, b.t FROM a CROSS JOIN b",
        DIFFERENCING,
    ),
    (
        "m1_union_of_two_groupings",
        f"SELECT u.state AS v, COUNT(*) AS n FROM {U} u GROUP BY u.state UNION ALL "
        f"SELECT u.country AS v, COUNT(*) AS n FROM {U} u GROUP BY u.country",
        DIFFERENCING,
    ),
    # QI inside value aggregates.
    ("string_agg", f"SELECT p.category, STRING_AGG(u.city) AS v {JOIN_UOI} GROUP BY 1", POSITION),
    (
        "array_agg",
        f"SELECT p.category, ARRAY_AGG(u.city) AS v {JOIN_UOI} GROUP BY 1",
        POSITION | EARLY,
    ),
    ("min", f"SELECT p.category, MIN(u.age) AS v {JOIN_UOI} GROUP BY 1", POSITION),
    ("max", f"SELECT p.category, MAX(u.age) AS v {JOIN_UOI} GROUP BY 1", POSITION),
    ("any_value", f"SELECT p.category, ANY_VALUE(u.state) AS v {JOIN_UOI} GROUP BY 1", POSITION),
    ("approx_count_distinct", f"SELECT APPROX_COUNT_DISTINCT(u.city) AS v FROM {U} u", POSITION),
    (
        "approx_top_count",
        f"SELECT u.state, APPROX_TOP_COUNT(u.city, 3) AS v FROM {U} u GROUP BY u.state",
        POSITION | EARLY,
    ),
    # IF / CASE / COUNTIF over a QI inside an aggregate.
    (
        "sum_if",
        f"SELECT p.category, SUM(IF(u.city = 'X', 1, 0)) AS v {JOIN_UOI} GROUP BY 1",
        POSITION,
    ),
    (
        "count_case",
        f"SELECT p.category, COUNT(CASE WHEN u.age > 60 THEN 1 END) AS v {JOIN_UOI} GROUP BY 1",
        POSITION,
    ),
    (
        "countif_qi",
        f"SELECT p.category, COUNTIF(u.city = 'X') AS v {JOIN_UOI} GROUP BY 1",
        POSITION | UNPLACEABLE,
    ),
    ("countif_qi_whole", f"SELECT COUNTIF(u.city = 'X') AS v FROM {U} u", POSITION | UNPLACEABLE),
    # Windows.
    (
        "window_partition_qi",
        f"SELECT p.category, COUNT(*) OVER (PARTITION BY u.state) AS v {JOIN_UOI}",
        POSITION | GRAIN,
    ),
    (
        "window_order_qi",
        f"SELECT u.state, COUNT(*) AS n, RANK() OVER (ORDER BY u.state) AS r FROM {U} u "
        "GROUP BY u.state",
        POSITION | UNPLACEABLE,
    ),
    (
        "window_total_over_cells",
        f"SELECT u.state, COUNT(*) AS n, SUM(COUNT(*)) OVER () AS t FROM {U} u GROUP BY u.state",
        UNPLACEABLE,
    ),
    # Nested aggregates over QI cells.
    (
        "nested_max",
        f"SELECT MAX(s.n) AS m FROM (SELECT u.state, COUNT(*) AS n FROM {U} u GROUP BY u.state) s",
        UNPLACEABLE,
    ),
    (
        "nested_cte_rejoin",
        f"WITH s AS (SELECT u.state, COUNT(*) AS n FROM {U} u GROUP BY u.state) "
        f"SELECT s.state, SUM(oi.sale_price) AS r FROM s JOIN {OI} oi ON TRUE GROUP BY s.state",
        UNPLACEABLE,
    ),
    (
        "cell_rejoined_with_users",
        f"WITH s AS (SELECT u.state, COUNT(*) AS n FROM {U} u GROUP BY u.state) "
        f"SELECT s.state, s.n, v.city FROM s JOIN {U} v ON v.state = s.state",
        GRAIN | UNPLACEABLE,
    ),
    # Correlated and filtering subqueries.
    (
        "correlated_scalar",
        f"SELECT p.category, (SELECT COUNT(*) FROM {U} u WHERE u.id = oi.user_id "
        f"AND u.city = 'X') AS c FROM {OI} oi JOIN {P} p ON p.id = oi.product_id",
        GRAIN | UNPLACEABLE,
    ),
    (
        "in_subquery_qi",
        f"SELECT p.category, COUNT(*) AS n FROM {OI} oi JOIN {P} p ON p.id = oi.product_id "
        f"WHERE oi.user_id IN (SELECT u.id FROM {U} u WHERE u.city = 'X') GROUP BY p.category",
        GRAIN | UNPLACEABLE,
    ),
    (
        "exists_qi",
        f"SELECT p.category, COUNT(*) AS n FROM {OI} oi JOIN {P} p ON p.id = oi.product_id "
        f"WHERE EXISTS (SELECT 1 FROM {U} u WHERE u.id = oi.user_id AND u.state = 'AA') "
        "GROUP BY p.category",
        GRAIN | UNPLACEABLE,
    ),
    # An id literal next to a QI.
    (
        "id_literal",
        f"SELECT u.state, COUNT(*) AS n FROM {U} u WHERE u.id = 7 GROUP BY u.state",
        UNPLACEABLE,
    ),
    (
        "id_in_list",
        f"SELECT u.state, COUNT(*) AS n FROM {U} u WHERE u.id IN (7, 8) GROUP BY u.state",
        UNPLACEABLE,
    ),
    (
        "countif_id_literal",
        f"SELECT u.state, COUNTIF(u.id = 7) AS n FROM {U} u GROUP BY u.state",
        POSITION | UNPLACEABLE,
    ),
    # ROLLUP / GROUPING SETS / CUBE.
    (
        "rollup",
        f"SELECT u.state, u.gender, COUNT(*) AS n FROM {U} u GROUP BY ROLLUP(1, 2)",
        UNPLACEABLE | EARLY,
    ),
    (
        "grouping_sets",
        f"SELECT u.state, u.gender, COUNT(*) AS n FROM {U} u "
        "GROUP BY GROUPING SETS ((u.state), (u.gender))",
        UNPLACEABLE | EARLY,
    ),
    (
        "cube",
        f"SELECT u.state, u.gender, COUNT(*) AS n FROM {U} u GROUP BY CUBE(1, 2)",
        UNPLACEABLE | EARLY,
    ),
    # DISTINCT.
    ("distinct_rows", f"SELECT DISTINCT u.state FROM {U} u", GRAIN | UNPLACEABLE),
    (
        "distinct_cell",
        f"SELECT DISTINCT u.state, COUNT(*) AS n FROM {U} u GROUP BY u.state",
        UNPLACEABLE,
    ),
    # Self-joins: two users instances feed one cell.
    (
        "self_join_cross",
        f"SELECT a.state, b.city, COUNT(*) AS n FROM {U} a CROSS JOIN {U} b GROUP BY 1, 2",
        UNPLACEABLE,
    ),
    (
        "cte_self_join",
        f"WITH t AS (SELECT u.id AS uid, u.country AS c FROM {U} u) "
        "SELECT a.c, b.c AS c2, COUNT(*) AS n FROM t a JOIN t b ON a.uid < b.uid GROUP BY 1, 2",
        UNPLACEABLE,
    ),
    # Key dropped by a CTE; HAVING on an ungrouped QI aggregate.
    (
        "cte_without_key",
        f"WITH t AS (SELECT u.country AS c FROM {U} u) SELECT t.c, COUNT(*) AS n FROM t GROUP BY 1",
        UNPLACEABLE,
    ),
    (
        "ungrouped_having",
        f"SELECT COUNT(*) AS n FROM {U} u WHERE u.age > 30 HAVING COUNT(*) > 0",
        UNPLACEABLE,
    ),
    # Id grain with QI anywhere (ADR-013 option A).
    (
        "id_grain_qi_filter",
        f"SELECT oi.order_id FROM {OI} oi JOIN {U} u ON u.id = oi.user_id WHERE u.city = 'X'",
        GRAIN,
    ),
    (
        "id_grain_qi_group",
        f"SELECT oi.user_id, COUNT(*) AS n FROM {OI} oi JOIN {U} u ON u.id = oi.user_id "
        "GROUP BY oi.user_id, u.state",
        GRAIN,
    ),
    # Union branch that is not at the root, carrying QI rows.
    (
        "union_in_derived_table",
        f"SELECT s.v, COUNT(*) AS n FROM (SELECT u.state AS v FROM {U} u UNION ALL "
        f"SELECT u.city AS v FROM {U} u) s GROUP BY s.v",
        UNPLACEABLE | GRAIN,
    ),
    # Sign-up timestamp finer than a month.
    (
        "created_at_week",
        f"SELECT DATE_TRUNC(DATE(u.created_at), WEEK) AS w, COUNT(*) AS n FROM {U} u GROUP BY w",
        POSITION,
    ),
    (
        "created_at_cte_raw",
        f"WITH t AS (SELECT u.id, u.created_at AS ts FROM {U} u) "
        "SELECT DATE_TRUNC(DATE(t.ts), MONTH) AS m, COUNT(*) AS n FROM t GROUP BY m",
        POSITION,
    ),
]


@pytest.mark.parametrize("scope", [ACME, ALL], ids=["acme", "all"])
@pytest.mark.parametrize(("name", "sql", "expected"), REFUSED, ids=[c[0] for c in REFUSED])
def test_redteam_refused(name: str, sql: str, expected: set[str], scope: object) -> None:
    assert outcome(sql, scope) in expected, name  # type: ignore[arg-type]


def test_existing_having_is_kept_in_main_and_dropped_from_companion() -> None:
    result = rewrite(
        f"SELECT u.state, COUNT(*) AS n FROM {U} u GROUP BY u.state "
        "HAVING COUNT(*) > 1 OR COUNT(*) < 100"
    )
    assert havings(result.query.sql) == [
        "HAVING (COUNT(*) > 1 OR COUNT(*) < 100) AND COUNT(DISTINCT u.id) >= 5"
    ]
    assert result.suppressed_query is not None
    # Review B2: the model's HAVING never reaches the companion.
    assert havings(result.suppressed_query.sql) == ["HAVING COUNT(DISTINCT u.id) < 5"]


def _companion(sql: str, scope: object = ALL) -> str:
    result = rewrite(sql, scope)  # type: ignore[arg-type]
    assert result.suppressed_query is not None
    verify_scoped(result.suppressed_query.sql, scope)  # type: ignore[arg-type]
    root = sqlglot.parse_one(result.suppressed_query.sql, read="bigquery")
    assert [p.alias_or_name for p in root.expressions] == ["has_suppressed_groups"]
    for node in (exp.Order, exp.Limit, exp.Offset, exp.Qualify):
        assert root.find(node) is None, node
    return result.suppressed_query.sql


@pytest.mark.parametrize("scope", [ACME, ALL], ids=["acme", "all"])
def test_b2_f1_having_count_equals_n_probe_is_blind(scope: object) -> None:
    """Review F1: ``HAVING COUNT(*) = n`` must not make the companion answer "is CC n?"."""
    base = f"SELECT u.state, COUNT(*) AS n FROM {U} u WHERE u.state = 'CC' GROUP BY u.state "
    probes = {_companion(base + f"HAVING COUNT(*) = {n}", scope) for n in (1, 2, 3)}
    assert len(probes) == 1  # identical SQL whatever n the model guesses
    (sql,) = probes
    assert "COUNT(*) = " not in sql


@pytest.mark.parametrize("scope", [ACME, ALL], ids=["acme", "all"])
def test_b2_f2_having_sum_threshold_probe_is_blind(scope: object) -> None:
    """Review F2: ``HAVING SUM(x) > t`` must not binary-search a suppressed cell's sum."""
    base = (
        f"SELECT u.state, SUM(oi.sale_price) AS s FROM {OI} oi JOIN {U} u ON u.id = oi.user_id "
        "WHERE u.age = 21 GROUP BY u.state "
    )
    probes = {_companion(base + f"HAVING SUM(oi.sale_price) > {t}", scope) for t in (5, 9, 11)}
    assert len(probes) == 1
    (sql,) = probes
    assert havings(sql)[-1] == "HAVING COUNT(DISTINCT u.id) < 5"


def test_b2_companion_is_a_boolean_not_a_count() -> None:
    # Whole population: CC (1 user) is suppressed; filtering to BB (5 users) suppresses none.
    by_state = f"SELECT u.state, COUNT(*) AS n FROM {U} u GROUP BY u.state"
    yes = rewrite(by_state, ALL).suppressed_query
    no = rewrite(
        f"SELECT u.state, COUNT(*) AS n FROM {U} u WHERE u.state = 'BB' GROUP BY u.state", ALL
    ).suppressed_query
    assert yes is not None and no is not None
    assert run_local(yes) == [(True,)] and run_local(yes)[0][0] is True
    assert run_local(no) == [(False,)] and run_local(no)[0][0] is False


def test_b1_count_distinct_bare_qi_column_is_allowed() -> None:
    result = rewrite(
        f"SELECT u.country, COUNT(DISTINCT u.city) AS cities FROM {U} u GROUP BY u.country"
    )
    assert havings(result.query.sql) == ["HAVING COUNT(DISTINCT u.id) >= 5"]


def test_m1_id_grain_inner_aggregate_feeding_one_cell_is_allowed() -> None:
    result = rewrite(
        f"WITH per_user AS (SELECT oi.user_id, COUNT(*) AS items FROM {OI} oi "
        "GROUP BY oi.user_id) "
        f"SELECT u.state, AVG(pu.items) AS avg_items FROM per_user pu JOIN {U} u "
        "ON u.id = pu.user_id GROUP BY u.state"
    )
    assert havings(result.query.sql)[-1] == "HAVING COUNT(DISTINCT u.id) >= 5"


def test_age_band_group_gets_threshold() -> None:
    result = rewrite(
        f"SELECT CASE WHEN u.age < 30 THEN 'under 30' ELSE '30+' END AS band, COUNT(*) AS n "
        f"FROM {U} u GROUP BY band"
    )
    assert havings(result.query.sql) == ["HAVING COUNT(DISTINCT u.id) >= 5"]


def test_qi_filter_on_grouped_product_cell_gets_threshold() -> None:
    result = rewrite(
        f"SELECT p.category, SUM(oi.sale_price) AS r {JOIN_UOI} WHERE u.city = 'X' "
        "GROUP BY p.category"
    )
    assert havings(result.query.sql) == ["HAVING COUNT(DISTINCT u.id) >= 5"]


def test_limit_in_row_subquery_counts_through_it() -> None:
    result = rewrite(
        f"SELECT s.st, COUNT(*) AS n FROM (SELECT u.id, u.state AS st FROM {U} u LIMIT 100) s "
        "GROUP BY s.st"
    )
    assert havings(result.query.sql) == ["HAVING COUNT(DISTINCT s.id) >= 5"]


def test_limit_in_cell_subquery_threshold_before_limit() -> None:
    result = rewrite(
        f"SELECT s.state, s.n FROM (SELECT u.state, COUNT(*) AS n FROM {U} u GROUP BY u.state "
        "ORDER BY n DESC LIMIT 3) s"
    )
    root = sqlglot.parse_one(result.query.sql, read="bigquery")
    inner = root.args["from_"].this.this
    assert isinstance(inner, exp.Select)
    assert inner.args["having"].sql(dialect="bigquery") == "HAVING COUNT(DISTINCT u.id) >= 5"
    assert inner.args.get("limit") is not None
    assert root.args.get("having") is None


def test_self_join_on_id_uses_the_qi_instance() -> None:
    result = rewrite(
        f"SELECT b.state, COUNT(*) AS n FROM {U} a JOIN {U} b ON a.id = b.id GROUP BY b.state"
    )
    assert havings(result.query.sql) == ["HAVING COUNT(DISTINCT b.id) >= 5"]


def test_generated_sql_leaks_no_new_values() -> None:
    """The rewrite adds only COUNT(DISTINCT <key>) and the literal k: no QI value, no id."""
    sql = f"SELECT u.state, COUNT(*) AS n FROM {U} u WHERE u.age > 30 GROUP BY u.state"
    result = rewrite(sql)
    before = sqlglot.parse_one(outcome_sql(sql), read="bigquery")
    after = sqlglot.parse_one(result.query.sql, read="bigquery")
    lits_before = sorted(lit.sql() for lit in before.find_all(exp.Literal))
    lits_after = sorted(lit.sql() for lit in after.find_all(exp.Literal))
    assert sorted([*lits_before, "5"]) == lits_after
    projections = [p.alias_or_name for p in after.expressions]
    assert projections == ["state", "n"]


def outcome_sql(sql: str) -> str:
    from tests.unit.test_small_cell import scoped

    return scoped(sql).sql
