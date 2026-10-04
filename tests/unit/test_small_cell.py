"""Small-cell rule (iteration 9; HLD §5.2 layer 5, §5.3 step 9, ADR-004 layer 5, FR-69).

All data is synthetic. No network: the oracle runs the rewritten SQL in sqlglot's executor.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlglot
from sqlglot import exp
from sqlglot.executor import execute

from opsfleet_agent.guards.scope import (
    ProductScope,
    ScopedQuery,
    ScopeRefusal,
    apply_scope,
    verify_scoped,
)
from opsfleet_agent.guards.scope_ctes import SCOPE_PARAM
from opsfleet_agent.guards.small_cell import (
    DEFAULT_K,
    PopulationCheck,
    SmallCellRewrite,
    apply_small_cell,
    population_ok,
)
from opsfleet_agent.guards.sql_policy import ALLOWED_TABLES

ACME = ProductScope.for_brands(["Acme"])
ALL = ProductScope.all()
HINT = "use a coarser grouping or aggregate over the whole population"


def t(name: str) -> str:
    return f"`bigquery-public-data.thelook_ecommerce.{name}`"


U, OI, P, ORD = t("users"), t("order_items"), t("products"), t("orders")


def scoped(sql: str, scope: ProductScope = ACME) -> ScopedQuery:
    result = apply_scope(sql, scope)
    assert isinstance(result, ScopedQuery), result
    return result


def small_cell(sql: str, scope: ProductScope = ACME, k: int = DEFAULT_K) -> Any:
    return apply_small_cell(scoped(sql, scope), scope, k)


def outcome(sql: str, scope: ProductScope = ACME) -> str:
    """Refusal reason from either stage, or the result type name."""
    first = apply_scope(sql, scope)
    if isinstance(first, ScopeRefusal):
        return first.reason_code
    result = apply_small_cell(first, scope)
    if isinstance(result, ScopeRefusal):
        assert result.error_code == "SQL_POLICY"
        return result.reason_code
    return type(result).__name__


def rewrite(sql: str, scope: ProductScope = ACME, k: int = DEFAULT_K) -> SmallCellRewrite:
    result = small_cell(sql, scope, k)
    assert isinstance(result, SmallCellRewrite), result
    return result


def havings(sql: str) -> list[str]:
    root = sqlglot.parse_one(sql, read="bigquery")
    return [h.sql(dialect="bigquery") for h in root.find_all(exp.Having)]


def refused(result: Any, reason: str) -> None:
    assert isinstance(result, ScopeRefusal), result
    assert result.error_code == "SQL_POLICY"
    assert result.reason_code == reason
    if reason == "small_cell_unplaceable":
        assert result.hint == HINT


# --------------------------------------------------------------------------- synthetic data
# Product 1 is Acme, product 2 is Other. Users 1-3 (state AA) buy Acme, users 4-6 (state AA)
# buy Other, users 7-11 (state BB) buy Acme, user 12 (state CC) never bought.
# Overall AA has 6 users; inside the Acme scope it has 3 (< k) and BB has 5 (= k).


def _user(uid: int, state: str) -> dict[str, Any]:
    row: dict[str, Any] = {c: None for c in ALLOWED_TABLES["users"]}
    row.update(
        id=uid,
        age=20 + uid,
        gender="F",
        city=f"Town{state}",
        state=state,
        country="XX",
        traffic_source="Search",
        created_at="2026-01-01",
    )
    return row


def _item(iid: int, uid: int, pid: int) -> dict[str, Any]:
    return {
        "id": iid,
        "order_id": iid,
        "user_id": uid,
        "product_id": pid,
        "inventory_item_id": iid,
        "status": "Complete",
        "sale_price": 10.0,
        "created_at": "2026-02-01",
        "shipped_at": None,
        "delivered_at": None,
        "returned_at": None,
    }


ORACLE_TABLES: dict[str, list[dict[str, Any]]] = {
    "products": [
        {
            "id": 1,
            "name": "Acme Tee",
            "brand": "Acme",
            "category": "Tops",
            "department": "W",
            "retail_price": 10.0,
            "cost": 4.0,
            "sku": "S1",
            "distribution_center_id": 1,
        },
        {
            "id": 2,
            "name": "Other Tee",
            "brand": "Other",
            "category": "Tops",
            "department": "M",
            "retail_price": 9.0,
            "cost": 3.0,
            "sku": "S2",
            "distribution_center_id": 2,
        },
    ],
    "order_items": [_item(100 + u, u, 1 if u <= 3 or 7 <= u <= 11 else 2) for u in range(1, 12)],
    "orders": [],
    "users": [_user(u, "AA") for u in range(1, 7)]
    + [_user(u, "BB") for u in range(7, 12)]
    + [_user(12, "CC")],
}


def run_local(query: ScopedQuery) -> list[tuple[Any, ...]]:
    """Bind the scope parameter as literals, strip the dataset, execute in memory."""
    root = sqlglot.parse_one(query.sql, read="bigquery")
    values: tuple[str, ...] = ()
    for p in query.parameters:
        assert p.name == SCOPE_PARAM
        values = p.values
    for node in list(root.find_all(exp.In)):
        if node.args.get("unnest") is not None:
            lits = [exp.Literal.string(v) for v in values] or [exp.null()]
            node.replace(exp.In(this=node.this.copy(), expressions=lits))
    for table in root.find_all(exp.Table):
        if table.args.get("db") is not None:
            table.set("db", None)
            table.set("catalog", None)
    return list(execute(root, tables=ORACLE_TABLES).rows)


BY_STATE = f"SELECT u.state, COUNT(*) AS n FROM {U} u GROUP BY u.state ORDER BY u.state"


# --------------------------------------------------------------------------- named tests


def test_small_cell_group_by_qi() -> None:
    sql = (
        f"SELECT u.state, COUNT(*) AS n FROM {U} u JOIN {OI} oi ON oi.user_id = u.id "
        "GROUP BY u.state ORDER BY n DESC LIMIT 10"
    )
    result = rewrite(sql)
    root = sqlglot.parse_one(result.query.sql, read="bigquery")
    assert isinstance(root, exp.Select)
    having = root.args["having"].sql(dialect="bigquery")
    assert having == "HAVING COUNT(DISTINCT u.id) >= 5"
    # Placed at the aggregation level; ORDER BY and LIMIT stay outside the threshold.
    assert root.args.get("order") is not None and root.args.get("limit") is not None
    # k is a literal, the parameters are unchanged, and the result is still fully scoped.
    assert result.query.parameters == scoped(sql).parameters
    verify_scoped(result.query.sql, ACME)
    assert result.suppressed_query is not None
    verify_scoped(result.suppressed_query.sql, ACME)
    assert havings(result.suppressed_query.sql) == ["HAVING COUNT(DISTINCT u.id) < 5"]
    sup_root = sqlglot.parse_one(result.suppressed_query.sql, read="bigquery")
    assert [p.alias_or_name for p in sup_root.expressions] == ["has_suppressed_groups"]
    assert sup_root.find(exp.Limit) is None and sup_root.find(exp.Order) is None


def test_small_cell_after_brand_scope() -> None:
    result = rewrite(BY_STATE)
    root = sqlglot.parse_one(result.query.sql, read="bigquery")
    # The threshold counts u.id of the scoped __u (buyers of the brand), not all users.
    source = root.args["from_"].this
    assert isinstance(source, exp.Table) and source.name == "__u"
    assert havings(result.query.sql)[-1] == "HAVING COUNT(DISTINCT u.id) >= 5"
    assert result.query.parameters and result.query.parameters[0].values == ("Acme",)
    assert result.query.scope_key == ACME.scope_key


def test_small_cell_counts_in_scope_population() -> None:
    acme = rewrite(BY_STATE, ACME)
    everyone = rewrite(BY_STATE, ALL)
    # Acme: AA has 3 buyers (< k), BB has 5 (= k).
    assert run_local(acme.query) == [("BB", 5)]
    assert acme.suppressed_query is not None
    assert run_local(acme.suppressed_query) == [(True,)]
    # Whole population: AA 6 and BB 5 are kept; CC (1 user) is suppressed.
    assert run_local(everyone.query) == [("AA", 6), ("BB", 5)]
    assert everyone.suppressed_query is not None
    assert run_local(everyone.suppressed_query) == [(True,)]
    # Without the rule the in-scope AA cell would have been released.
    assert ("AA", 3) in run_local(scoped(BY_STATE, ACME))


def test_small_cell_rejects_qi_in_value_aggregate() -> None:
    for agg in (
        "MAX(u.city)",
        "MIN(u.age)",
        "ANY_VALUE(u.gender)",
        "STRING_AGG(u.city)",
        "SUM(u.age)",
        "AVG(u.age)",
        "APPROX_COUNT_DISTINCT(u.city)",
    ):
        assert outcome(f"SELECT u.country, {agg} AS v FROM {U} u GROUP BY u.country") == (
            "qi_position"
        ), agg
    # ARRAY_AGG is refused earlier, as an unresolvable output value.
    assert outcome(f"SELECT u.country, ARRAY_AGG(u.state) AS v FROM {U} u GROUP BY u.country") in {
        "qi_position",
        "unresolved_value",
    }
    # Defence in depth: the same shape smuggled past apply_scope is refused here too.
    good = scoped(f"SELECT u.country, COUNT(DISTINCT u.city) AS v FROM {U} u GROUP BY u.country")
    forged = ScopedQuery(
        good.sql.replace("COUNT(DISTINCT u.city)", "MAX(u.city)"), good.parameters, good.scope_key
    )
    refused(apply_small_cell(forged, ACME), "qi_position")


def test_small_cell_rejects_window_over_qi() -> None:
    assert outcome(
        f"SELECT u.state, RANK() OVER (PARTITION BY u.country ORDER BY u.age) AS r FROM {U} u"
    ) in {"qi_position", "qi_at_id_grain"}
    assert (
        outcome(
            f"SELECT u.state, COUNT(*) AS n, SUM(COUNT(*)) OVER () AS total FROM {U} u "
            "GROUP BY u.state"
        )
        == "small_cell_unplaceable"
    )
    good = scoped(f"SELECT u.state, COUNT(*) AS n FROM {U} u GROUP BY u.state")
    forged = ScopedQuery(
        good.sql.replace("COUNT(*) AS n", "COUNT(*) OVER (PARTITION BY u.state) AS n"),
        good.parameters,
        good.scope_key,
    )
    assert isinstance(apply_small_cell(forged, ACME), ScopeRefusal)


def test_signup_timestamp_is_qi() -> None:
    month = rewrite(
        f"SELECT DATE_TRUNC(DATE(u.created_at), MONTH) AS m, COUNT(*) AS n FROM {U} u GROUP BY m"
    )
    assert havings(month.query.sql) == ["HAVING COUNT(DISTINCT u.id) >= 5"]
    for expr in (
        "u.created_at",
        "DATE(u.created_at)",
        "DATE_TRUNC(DATE(u.created_at), WEEK)",
        "EXTRACT(YEAR FROM u.created_at)",
    ):
        assert outcome(f"SELECT {expr} AS s, COUNT(*) AS n FROM {U} u GROUP BY 1") == (
            "qi_position"
        ), expr
    # order_items.created_at is not a quasi-identifier.
    product_only = rewrite(
        f"SELECT DATE(oi.created_at) AS d, COUNT(*) AS n FROM {OI} oi GROUP BY d"
    )
    assert product_only.suppressed_query is None


def test_qi_lineage_through_cte() -> None:
    passthrough = rewrite(
        f"WITH t AS (SELECT u.id AS uid, u.country AS c FROM {U} u) "
        "SELECT t.c, COUNT(*) AS n FROM t GROUP BY t.c"
    )
    assert havings(passthrough.query.sql) == ["HAVING COUNT(DISTINCT t.uid) >= 5"]
    band = rewrite(
        f"WITH t AS (SELECT u.id, CASE WHEN u.age < 30 THEN 'young' ELSE 'older' END AS band "
        f"FROM {U} u) SELECT t.band, COUNT(*) AS n FROM t GROUP BY t.band"
    )
    assert havings(band.query.sql) == ["HAVING COUNT(DISTINCT t.id) >= 5"]
    # The cell inside the CTE gets the threshold; the plain reader is untouched.
    inner = rewrite(
        f"WITH s AS (SELECT u.state, COUNT(*) AS n FROM {U} u GROUP BY u.state) "
        "SELECT s.state, s.n FROM s ORDER BY s.n DESC LIMIT 3"
    )
    root = sqlglot.parse_one(inner.query.sql, read="bigquery")
    assert root.args.get("having") is None
    assert havings(inner.query.sql) == ["HAVING COUNT(DISTINCT u.id) >= 5"]
    # A CTE that drops the user key leaves nothing to count: refused, fail closed.
    refused(
        small_cell(
            f"WITH t AS (SELECT u.country AS c FROM {U} u) "
            "SELECT t.c, COUNT(*) AS n FROM t GROUP BY t.c"
        ),
        "small_cell_unplaceable",
    )


def test_qi_filter_aggregate_population_check() -> None:
    sql = (
        f"SELECT SUM(oi.sale_price) AS revenue FROM {OI} oi JOIN {U} u ON u.id = oi.user_id "
        "WHERE u.state = 'AA'"
    )
    check = small_cell(sql)
    assert isinstance(check, PopulationCheck)
    assert check.then_run == scoped(sql)
    assert check.query.parameters == check.then_run.parameters
    verify_scoped(check.query.sql, ACME)
    root = sqlglot.parse_one(check.query.sql, read="bigquery")
    assert [p.sql(dialect="bigquery") for p in root.expressions] == [
        "COUNT(DISTINCT u.id) AS population"
    ]
    assert root.args["where"].sql(dialect="bigquery") == "WHERE u.state = 'AA'"
    # Oracle: 3 Acme buyers in AA (< k) -> refused; the whole population has 6 -> allowed.
    (acme_count,) = run_local(check.query)[0]
    refused(check.evaluate(acme_count), "small_cell_unplaceable")
    everyone = small_cell(sql, ALL)
    assert isinstance(everyone, PopulationCheck)
    (all_count,) = run_local(everyone.query)[0]
    assert everyone.evaluate(all_count) == everyone.then_run
    for bad in (None, True, 4, -1, 4.9, "5"):
        refused(check.evaluate(bad), "small_cell_unplaceable")
    assert check.evaluate(5) == check.then_run
    assert population_ok(5) and not population_ok(4) and not population_ok(True)


def test_user_grain_rejects_qi_projection() -> None:
    assert outcome(f"SELECT u.id, u.city FROM {U} u") == "qi_at_id_grain"
    assert outcome(
        f"SELECT oi.user_id, SUM(oi.sale_price) AS s, ANY_VALUE(u.state) AS st FROM {OI} oi "
        f"JOIN {U} u ON u.id = oi.user_id GROUP BY oi.user_id"
    ) in {"qi_position", "qi_at_id_grain"}
    # ADR-013 option A: no QI predicate at id grain either.
    assert (
        outcome(
            f"SELECT oi.user_id, SUM(oi.sale_price) AS s FROM {OI} oi JOIN {U} u "
            "ON u.id = oi.user_id WHERE u.state = 'AA' GROUP BY oi.user_id"
        )
        == "qi_at_id_grain"
    )
    assert outcome(f"SELECT u.id FROM {U} u ORDER BY u.age") == "qi_at_id_grain"


def test_product_only_group_not_suppressed() -> None:
    sql = (
        f"SELECT p.brand, p.category, COUNT(DISTINCT oi.user_id) AS buyers FROM {OI} oi "
        f"JOIN {P} p ON p.id = oi.product_id GROUP BY p.brand, p.category"
    )
    for scope in (ACME, ALL):
        result = rewrite(sql, scope)
        assert result.query == scoped(sql, scope)
        assert result.suppressed_query is None
        assert havings(result.query.sql) == []


def test_top_customers_by_spend_allowed() -> None:
    sql = (
        f"SELECT oi.user_id, SUM(oi.sale_price) AS spend FROM {OI} oi GROUP BY oi.user_id "
        "ORDER BY spend DESC LIMIT 10"
    )
    result = rewrite(sql)
    assert result.query == scoped(sql)
    assert result.suppressed_query is None


# --------------------------------------------------------------------------- ADR-004 layer 5


def test_adr004_case_b_id_grain_without_qi_allowed() -> None:
    sql = (
        f"SELECT o.order_id, o.status FROM {ORD} o WHERE o.created_at >= '2026-01-01' "
        "ORDER BY o.order_id LIMIT 10"
    )
    result = rewrite(sql)
    assert result.suppressed_query is None


def test_adr004_case_c_unplaceable() -> None:
    nested = (
        f"SELECT MAX(s.n) AS m FROM (SELECT u.state, COUNT(*) AS n FROM {U} u GROUP BY u.state) s"
    )
    refused(small_cell(nested), "small_cell_unplaceable")
    id_literal = f"SELECT u.state, COUNT(*) AS n FROM {U} u WHERE u.id = 7 GROUP BY u.state"
    assert outcome(id_literal) == "small_cell_unplaceable"


# --------------------------------------------------------------------------- API guards


def test_k_is_a_literal_parameter() -> None:
    result = rewrite(BY_STATE, k=10)
    assert havings(result.query.sql) == ["HAVING COUNT(DISTINCT u.id) >= 10"]
    assert result.k == 10
    for bad in (1, 0, -5, True, 5.0, "5", 10_001):
        refused(apply_small_cell(scoped(BY_STATE), ACME, bad), "rewrite_invariant")


def test_scope_mismatch_and_tampering_fail_closed() -> None:
    query = scoped(BY_STATE, ACME)
    assert isinstance(apply_small_cell(query, ALL), ScopeRefusal)
    assert isinstance(apply_small_cell(query, ProductScope.for_brands(["Other"])), ScopeRefusal)
    tampered = ScopedQuery(
        query.sql.replace("FROM __u AS u", f"FROM {U} AS u"), query.parameters, query.scope_key
    )
    refused(apply_small_cell(tampered, ACME), "rewrite_invariant")
    refused(apply_small_cell("SELECT 1", ACME), "scope_invalid")  # type: ignore[arg-type]


@pytest.mark.parametrize("scope", [ACME, ALL], ids=["acme", "all"])
def test_rewrite_is_deterministic(scope: ProductScope) -> None:
    sql = (
        f"WITH t AS (SELECT u.id AS uid, u.id AS uid2, u.country AS c FROM {U} u) "
        "SELECT t.c, COUNT(*) AS n FROM t GROUP BY t.c"
    )
    first = rewrite(sql, scope)
    assert all(rewrite(sql, scope) == first for _ in range(3))
    assert havings(first.query.sql) == ["HAVING COUNT(DISTINCT t.uid) >= 5"]
