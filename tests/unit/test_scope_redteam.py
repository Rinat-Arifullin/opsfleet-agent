"""Red-team cases for the brand-scope rewriter (iteration 7, AC-09.x).

Each case is either refused, or rewritten so that executing it on synthetic data
returns only in-scope rows. Execution is local (sqlglot executor), with no network calls.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlglot
from sqlglot import exp

from opsfleet_agent.guards.scope import (
    ProductScope,
    ScopedQuery,
    ScopeInvariantError,
    ScopeRefusal,
    apply_scope,
    verify_scoped,
)
from opsfleet_agent.guards.scope_ctes import CODE_CTE_ORDER
from tests.unit.test_scope_rewrite import ACME, assert_fully_scoped, run_local

FQP = "`bigquery-public-data.thelook_ecommerce.products`"


def _scoped(sql: str, scope: ProductScope = ACME) -> ScopedQuery:
    result = apply_scope(sql, scope)
    assert isinstance(result, ScopedQuery), result
    assert_fully_scoped(result.sql, scope)
    return result


def _brands(sql: str) -> set[Any]:
    return {r[0] for r in run_local(_scoped(sql))}


# --------------------------------------------------------------------------- rewritten safely


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT brand FROM products WHERE brand = 'Other'", id="other_brand_request"),
        pytest.param("SELECT brand FROM products WHERE brand IN ('Acme', 'Other')",
                     id="other_brand_in_list"),
        pytest.param("SELECT brand FROM products UNION ALL "
                     "SELECT brand FROM products WHERE brand = 'Other'", id="union_all"),
        pytest.param("SELECT 'x' AS brand FROM products WHERE FALSE UNION DISTINCT "
                     f"SELECT brand FROM {FQP} WHERE brand = 'Other'", id="brand_named_in_union"),
        pytest.param("SELECT a.brand FROM products a JOIN products b ON a.id <> b.id "
                     "OR b.brand = 'Other'", id="self_join"),
        pytest.param("SELECT p.brand FROM products p WHERE EXISTS "
                     "(SELECT 1 FROM products q WHERE q.id = p.id AND q.brand = 'Other') "
                     "OR TRUE", id="correlated_subquery"),
        pytest.param("SELECT p.brand FROM products p WHERE p.id IN "
                     "(SELECT p.id FROM products p WHERE p.brand = 'Other') OR p.id > 0",
                     id="alias_reused_across_scopes"),
        pytest.param("WITH c AS (SELECT brand FROM products) SELECT brand FROM c",
                     id="table_only_through_cte"),
        pytest.param("WITH c AS (SELECT id FROM products), d AS (SELECT id FROM c) "
                     "SELECT p.brand FROM d JOIN products p ON p.id = d.id", id="cte_chain"),
        pytest.param("SELECT brand FROM products WHERE id IN (SELECT id FROM "
                     f"{FQP} WHERE brand = 'Other') OR TRUE", id="in_subquery_raw_fq"),
        pytest.param("SELECT t.brand FROM (SELECT brand FROM thelook_ecommerce.products) AS t",
                     id="derived_table_two_part"),
        pytest.param("SELECT brand FROM /* users */ products -- WHERE brand='Acme'\n",
                     id="comment_injected_names"),
        pytest.param("SELECT brand FROM products /* ) UNION ALL SELECT brand FROM "
                     f"{FQP} -- */", id="comment_hides_union"),
        pytest.param("SELECT x.brand FROM products AS x", id="table_behind_alias"),
        pytest.param("SELECT products.brand FROM products AS products", id="alias_equals_name"),
        pytest.param("SELECT brand FROM `products`", id="backtick_bare"),
        pytest.param("SELECT o.brand FROM (SELECT brand FROM products) o "
                     "CROSS JOIN (SELECT 1 AS k) k", id="cross_join_derived"),
    ],
)
def test_redteam_rewritten_returns_only_scope(sql: str) -> None:
    brands = _brands(sql)
    assert brands <= {"Acme", "x"}
    assert "Other" not in brands


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        pytest.param("SELECT brand FROM products UNION ALL "
                     "SELECT brand FROM products WHERE brand = 'Other'",
                     [("Acme",), ("Acme",)], id="union_all_same_source_name"),
        pytest.param("SELECT brand FROM products UNION DISTINCT "
                     "SELECT brand FROM products WHERE brand = 'Other'",
                     [("Acme",)], id="union_distinct_same_source_name"),
        pytest.param("SELECT 'x' AS brand FROM products WHERE FALSE UNION DISTINCT "
                     f"SELECT brand FROM {FQP} WHERE brand = 'Other'", [],
                     id="brand_named_in_union"),
    ],
)
def test_redteam_union_exact_rows(sql: str, expected: list[tuple[Any, ...]]) -> None:
    """Set operations pin the exact rows, so a wrong (e.g. empty) result cannot pass."""
    assert sorted(run_local(_scoped(sql))) == expected


def test_redteam_revenue_cannot_reach_other_brand_items() -> None:
    sql = ("SELECT SUM(sale_price) AS r FROM order_items WHERE product_id IN "
           "(SELECT id FROM products WHERE brand = 'Other') OR TRUE")
    assert run_local(_scoped(sql)) == [(10.0,)]


def test_redteam_users_without_in_scope_purchase_hidden() -> None:
    sql = "SELECT id FROM users WHERE id NOT IN (SELECT user_id FROM order_items)"
    assert run_local(_scoped(sql)) == []


def test_redteam_orders_reached_only_through_scoped_items() -> None:
    sql = "SELECT order_id FROM orders o WHERE TRUE OR o.order_id = 2000"
    assert run_local(_scoped(sql)) == [(1000,)]


# --------------------------------------------------------------------------- refused


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("WITH products AS (SELECT 'Other' AS brand) SELECT brand FROM products",
                     id="cte_shadowing_bare"),
        pytest.param("WITH PRODUCTS AS (SELECT 1 AS id) SELECT id FROM PRODUCTS",
                     id="cte_shadowing_case"),
        pytest.param("WITH __p AS (SELECT 'Other' AS brand) SELECT brand FROM __p",
                     id="user_cte_named_like_code_cte"),
        pytest.param("WITH __oi AS (SELECT 1 AS id) SELECT id FROM __oi",
                     id="user_cte_named_like_code_cte_oi"),
        pytest.param("WITH __x AS (SELECT 1 AS id) SELECT id FROM __x",
                     id="user_cte_reserved_prefix"),
        pytest.param("SELECT brand FROM __p", id="direct_code_cte_reference"),
        pytest.param("SELECT brand FROM products WHERE brand IN UNNEST(@scope_brands)",
                     id="model_supplied_param"),
        pytest.param("SELECT brand FROM products WHERE brand IN UNNEST(['Other'])",
                     id="model_supplied_unnest"),
        pytest.param("SELECT brand FROM other_dataset.products", id="other_dataset"),
        pytest.param("SELECT brand FROM `other-project.thelook_ecommerce.products`",
                     id="other_project"),
        pytest.param("SELECT brand FROM Products", id="case_variant_table"),
        pytest.param("SELECT email FROM users", id="pii_column"),
        pytest.param("SELECT num_of_item FROM orders", id="orders_num_of_item"),
        pytest.param("DELETE FROM products WHERE TRUE", id="dml"),
        pytest.param("SELECT 1; SELECT brand FROM products", id="multi_statement"),
        pytest.param("WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r) "
                     "SELECT n FROM r", id="recursive_cte"),
    ],
)
def test_redteam_refused(sql: str) -> None:
    result = apply_scope(sql, ACME)
    assert isinstance(result, ScopeRefusal), getattr(result, "sql", result)
    assert result.allowed is False


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT brand FROM products AS __p", id="alias_named_like_code_cte"),
        pytest.param("SELECT __p.brand FROM products __p", id="alias_without_as"),
        pytest.param("SELECT __x.brand FROM products AS __x", id="alias_reserved_prefix"),
        pytest.param("SELECT o.order_id FROM orders o JOIN order_items AS __oi "
                     "ON __oi.order_id = o.order_id", id="join_alias_named_like_code_cte"),
    ],
)
def test_redteam_reserved_alias_refused(sql: str) -> None:
    """Model-written aliases starting with ``__`` are refused, like ``__`` CTE names."""
    result = apply_scope(sql, ACME)
    assert isinstance(result, ScopeRefusal), getattr(result, "sql", result)
    assert result.error_code == "SQL_POLICY"
    assert result.rule == "identifier_not_allowed"


def test_redteam_brand_injection_via_scope_is_inert() -> None:
    evil = ProductScope.for_brands(["Acme') OR TRUE --", "x\\' UNION SELECT 1"])
    result = apply_scope("SELECT brand FROM products", evil)
    assert isinstance(result, ScopedQuery)
    assert "OR TRUE" not in result.sql and "UNION" not in result.sql
    assert run_local(result) == []


def test_redteam_every_reference_rewritten_in_large_query() -> None:
    """A query touching all four tables in CTEs, joins, subqueries and a set operation."""
    sql = (
        "WITH buyers AS (SELECT DISTINCT user_id FROM order_items) "
        "SELECT 'u' AS c FROM users u JOIN buyers b ON b.user_id = u.id "
        "WHERE u.id IN (SELECT user_id FROM orders) "
        "UNION ALL SELECT p.brand AS c FROM products p "
        "JOIN `bigquery-public-data.thelook_ecommerce.order_items` oi ON oi.product_id = p.id"
    )
    scoped = _scoped(sql)
    root = sqlglot.parse_one(scoped.sql, read="bigquery")
    names = [c.alias for c in root.args["with_"].expressions]
    assert names == [*CODE_CTE_ORDER, "buyers"]
    assert sorted(r[0] for r in run_local(scoped)) == ["Acme", "u"]


def test_redteam_tampered_output_not_accepted_for_other_scope() -> None:
    a = apply_scope("SELECT brand FROM products", ACME)
    assert isinstance(a, ScopedQuery)
    root = sqlglot.parse_one(a.sql, read="bigquery")
    for node in list(root.find_all(exp.Where)):
        node.pop()
    with pytest.raises(ScopeInvariantError):
        verify_scoped(root.sql(dialect="bigquery"), ACME)
