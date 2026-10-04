"""Brand-scope rewrite and post-rewrite invariant (iteration 7; HLD §5.3 steps 8 and 10).

Semantic tests run the scoped SQL on a tiny synthetic dataset with sqlglot's in-memory
executor (no network): ``@scope_brands`` is bound to literals and the fully qualified
names are stripped *only for local execution*, never in the code under test.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
import sqlglot
from sqlglot import exp
from sqlglot.executor import execute

from opsfleet_agent.guards import scope as scope_mod
from opsfleet_agent.guards.scope import (
    ProductScope,
    ScopedQuery,
    ScopeError,
    ScopeInvariantError,
    ScopeRefusal,
    apply_scope,
    scope_rewrite,
    verify_scoped,
)
from opsfleet_agent.guards.scope_ctes import (
    CODE_CTE_FOR_TABLE,
    CODE_CTE_ORDER,
    SCOPE_PARAM,
    cte_body_sql,
    required_ctes,
)
from opsfleet_agent.guards.sql_policy import ALLOWED_TABLES, PII_COLUMNS

FQ = "`bigquery-public-data.thelook_ecommerce.{t}`"
ACME = ProductScope.for_brands(["Acme"])
ALL = ProductScope.all()

# --------------------------------------------------------------------------- synthetic data
# Brands: Acme (in scope), Other (out of scope). Users: 10 buys Acme, 20 buys Other only,
# 30 never bought. All values are synthetic.


def _user(uid: int) -> dict[str, Any]:
    row: dict[str, Any] = {c: None for c in ALLOWED_TABLES["users"]}
    row.update(id=uid, age=30 + uid, gender="F", city="Town", state="ST", country="XX",
               traffic_source="Search", created_at="2026-01-01")
    return row


TABLES: dict[str, list[dict[str, Any]]] = {
    "products": [
        {"id": 1, "name": "Acme Tee", "brand": "Acme", "category": "Tops", "department": "W",
         "retail_price": 10.0, "cost": 4.0, "sku": "S1", "distribution_center_id": 1},
        {"id": 2, "name": "Acme Cap", "brand": "Acme", "category": "Hats", "department": "W",
         "retail_price": 5.0, "cost": 2.0, "sku": "S2", "distribution_center_id": 1},
        {"id": 3, "name": "Other Tee", "brand": "Other", "category": "Tops", "department": "M",
         "retail_price": 99.0, "cost": 50.0, "sku": "S3", "distribution_center_id": 2},
    ],
    "order_items": [
        {"id": 100, "order_id": 1000, "user_id": 10, "product_id": 1, "inventory_item_id": 1,
         "status": "Complete", "sale_price": 10.0, "created_at": "2026-02-01",
         "shipped_at": None, "delivered_at": None, "returned_at": None},
        {"id": 101, "order_id": 1000, "user_id": 10, "product_id": 3, "inventory_item_id": 2,
         "status": "Complete", "sale_price": 99.0, "created_at": "2026-02-01",
         "shipped_at": None, "delivered_at": None, "returned_at": None},
        {"id": 102, "order_id": 2000, "user_id": 20, "product_id": 3, "inventory_item_id": 3,
         "status": "Complete", "sale_price": 99.0, "created_at": "2026-02-02",
         "shipped_at": None, "delivered_at": None, "returned_at": None},
    ],
    "orders": [
        {"order_id": 1000, "user_id": 10, "status": "Complete", "created_at": "2026-02-01",
         "shipped_at": None, "delivered_at": None, "returned_at": None, "num_of_item": 2,
         "gender": "F"},
        {"order_id": 2000, "user_id": 20, "status": "Complete", "created_at": "2026-02-02",
         "shipped_at": None, "delivered_at": None, "returned_at": None, "num_of_item": 1,
         "gender": "F"},
    ],
    "users": [_user(10), _user(20), _user(30)],
}


def run_local(scoped: ScopedQuery) -> list[tuple[Any, ...]]:
    """Execute scoped SQL in memory: bind the parameter, strip the project/dataset."""
    root = sqlglot.parse_one(scoped.sql, read="bigquery")
    values: tuple[str, ...] = ()
    for p in scoped.parameters:
        assert p.name == SCOPE_PARAM
        values = p.values
    for node in list(root.find_all(exp.In)):
        if node.args.get("unnest") is not None:
            lits = [exp.Literal.string(v) for v in values] or [exp.null()]
            node.replace(exp.In(this=node.this.copy(), expressions=lits))
    for t in root.find_all(exp.Table):
        if t.args.get("db") is not None:
            t.set("db", None)
            t.set("catalog", None)
    return _execute(root)


def _execute(root: exp.Expression) -> list[tuple[Any, ...]]:
    """Run one query; a root set operation is run branch by branch.

    sqlglot's executor confuses same-named sources in different branches of a set operation
    (e.g. ``__p AS products`` in both), and which branch wins depends on ``PYTHONHASHSEED``.
    Each branch is therefore executed on its own, with the root ``WITH``, and the rows are
    combined here (``UNION ALL`` concatenates, ``UNION DISTINCT`` de-duplicates).
    """
    if not isinstance(root, exp.SetOperation):
        return list(execute(root, tables=TABLES).rows)
    assert isinstance(root, exp.Union), "only UNION is supported by the local oracle"
    assert not root.args.get("order") and not root.args.get("limit"), "unsupported here"
    with_ = root.args.get("with_")
    rows: list[tuple[Any, ...]] = []
    for side in (root.left, root.right):
        branch = side.unnest().copy() if isinstance(side, exp.Subquery) else side.copy()
        if isinstance(with_, exp.With):
            own = branch.args.get("with_")
            ctes = [c.copy() for c in with_.expressions] + (list(own.expressions) if own else [])
            branch.set("with_", exp.With(expressions=ctes))
        rows.extend(_execute(branch))
    if root.args.get("distinct"):
        rows = list(dict.fromkeys(rows))
    return rows


def scoped_ok(sql: str, scope: ProductScope = ACME) -> ScopedQuery:
    result = apply_scope(sql, scope)
    assert isinstance(result, ScopedQuery), result
    assert_fully_scoped(result.sql, scope)
    return result


def assert_fully_scoped(sql: str, scope: ProductScope) -> None:
    """Independent structural check of the invariant (in addition to ``verify_scoped``)."""
    verify_scoped(sql, scope)
    root = sqlglot.parse_one(sql, read="bigquery")
    with_ = root.args.get("with_")
    code = [c for c in (with_.expressions if with_ else []) if c.alias in CODE_CTE_ORDER]
    code_ids = {id(c) for c in code}
    for cte in code:
        assert cte.this.sql(dialect="bigquery") == sqlglot.parse_one(
            cte_body_sql(cte.alias, scoped=scope.scoped), read="bigquery"
        ).sql(dialect="bigquery")
    for table in root.find_all(exp.Table):
        node: exp.Expression | None = table
        inside_code = False
        while node is not None:
            if id(node) in code_ids:
                inside_code = True
                break
            node = node.parent
        if inside_code:
            continue
        assert not table.db and not table.catalog, "qualified table outside code CTEs"
        assert table.name.lower() not in ALLOWED_TABLES, "raw base table outside code CTEs"
    if scope.scoped and code:
        assert "__p" in [c.alias for c in code]
        assert f"UNNEST(@{SCOPE_PARAM})" in sql


def brands_of(rows: list[tuple[Any, ...]]) -> set[Any]:
    return {r[0] for r in rows}


# --------------------------------------------------------------------------- named tests


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT brand, name FROM products", id="no_where"),
        pytest.param(
            "SELECT brand, name FROM products WHERE brand = 'Other' OR 1=1", id="or_1_eq_1"
        ),
        pytest.param(
            "SELECT brand, name FROM products WHERE id IN (SELECT id FROM products)",
            id="subquery_on_raw_products",
        ),
        pytest.param(
            "SELECT brand, name FROM products WHERE brand = 'Acme' "
            "UNION ALL SELECT brand, name FROM products WHERE brand = 'Other'",
            id="union_attempt",
        ),
        pytest.param(
            "SELECT a.brand, a.name FROM products a WHERE a.brand = 'Acme' "
            "UNION ALL SELECT b.brand, b.name FROM products b WHERE b.brand = 'Other'",
            id="union_attempt_distinct_aliases",
        ),
        pytest.param(
            "SELECT brand, name FROM `bigquery-public-data.thelook_ecommerce.products`",
            id="fully_qualified",
        ),
        pytest.param(
            "SELECT brand, name FROM `bigquery-public-data`.thelook_ecommerce.products",
            id="fully_qualified_split_quotes",
        ),
        pytest.param("SELECT brand, name FROM thelook_ecommerce.products", id="two_part"),
    ],
)
def test_scope_filter_cannot_be_bypassed(sql: str) -> None:
    """AC-09.1, AC-09.2: whatever the model writes, only in-scope rows come back."""
    scoped = scoped_ok(sql)
    rows = run_local(scoped)
    # Exact rows (order-insensitive): every case returns precisely the two Acme products.
    assert sorted(rows) == [("Acme", "Acme Cap"), ("Acme", "Acme Tee")]
    assert "Other" not in scoped.sql or "'Other'" in sql  # only the model's own literal


def test_scope_filter_cannot_be_bypassed_through_joined_tables() -> None:
    """Revenue through order_items/orders never includes the out-of-scope item."""
    scoped = scoped_ok(
        "SELECT SUM(oi.sale_price) AS revenue FROM order_items oi "
        "JOIN orders o ON o.order_id = oi.order_id"
    )
    assert run_local(scoped) == [(10.0,)]


def test_nested_cte_scope_resolution() -> None:
    """A CTE in an inner WITH does not hide a base table in an outer scope (HLD §5.3)."""
    sql = (
        "WITH outer_c AS ("
        "  SELECT t.id FROM (WITH inner_c AS (SELECT id FROM products) "
        "                    SELECT id FROM inner_c) AS t) "
        "SELECT p.brand FROM products p "
        "WHERE p.id IN (SELECT id FROM outer_c) OR p.id NOT IN (SELECT id FROM outer_c)"
    )
    scoped = scoped_ok(sql)
    root = sqlglot.parse_one(scoped.sql, read="bigquery")
    inner = next(c for c in root.find_all(exp.CTE) if c.alias == "inner_c")
    assert [t.name for t in inner.find_all(exp.Table)] == ["__p"]
    assert brands_of(run_local(scoped)) == {"Acme"}


def test_nested_cte_inner_with_name_does_not_leak_outward() -> None:
    """An inner CTE name used outside its WITH is unresolved and rejected (fail closed)."""
    result = apply_scope(
        "SELECT t.id FROM (WITH inner_c AS (SELECT id FROM products) SELECT id FROM inner_c) t "
        "JOIN inner_c ON inner_c.id = t.id",
        ACME,
    )
    assert isinstance(result, ScopeRefusal)


class _BrokenRewrite:
    """Test double: a rewriter that leaves one raw reference behind."""

    def __init__(self, mode: str) -> None:
        self.mode = mode

    def __call__(self, statement: exp.Expression, scope: ProductScope) -> exp.Expression:
        out = scope_rewrite(statement, scope)
        tables = [t for t in out.find_all(exp.Table) if t.name == "__p"]
        victim = tables[-1]
        if self.mode == "raw_bare":
            victim.set("this", exp.to_identifier("products"))
        elif self.mode == "raw_fq":
            victim.replace(exp.to_table("`bigquery-public-data.thelook_ecommerce.products`",
                                        dialect="bigquery"))
        elif self.mode == "drop_filter":
            body = next(c for c in out.find_all(exp.CTE) if c.alias == "__p").this
            body.set("where", None)
        elif self.mode == "all_template_in_brand_scope":
            cte = next(c for c in out.find_all(exp.CTE) if c.alias == "__p")
            cte.set("this", sqlglot.parse_one(cte_body_sql("__p", scoped=False),
                                              read="bigquery"))
        elif self.mode == "extra_column":
            body = next(c for c in out.find_all(exp.CTE) if c.alias == "__p").this
            body.select("brand AS brand2", copy=False)
        elif self.mode == "drop_code_ctes":
            out.set("with_", None)
        return out


@pytest.mark.parametrize(
    "mode",
    ["raw_bare", "raw_fq", "drop_filter", "all_template_in_brand_scope", "extra_column",
     "drop_code_ctes"],
)
def test_rewrite_invariant_fail_closed(
    mode: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """HLD §5.3 step 10: a broken rewriter is caught; the query is refused, never run."""
    monkeypatch.setattr(scope_mod, "scope_rewrite", _BrokenRewrite(mode))
    sql = "SELECT p.brand FROM products p WHERE p.id IN (SELECT id FROM products)"
    with caplog.at_level(logging.ERROR, logger="opsfleet_agent.guards.scope"):
        result = apply_scope(sql, ACME)
    assert isinstance(result, ScopeRefusal)
    assert result.error_code == "SQL_POLICY"
    assert result.rule == "rewrite_invariant"
    assert caplog.records and all(r.levelno == logging.ERROR for r in caplog.records)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "SELECT" not in logged and "products" not in logged and "Acme" not in logged


@pytest.mark.parametrize(
    ("scope", "rule"),
    [
        pytest.param(None, "empty_scope", id="no_scope"),
        pytest.param("empty_tuple", "empty_scope", id="empty_brand_list_tampered"),
        pytest.param("both", "scope_invalid", id="brands_and_all"),
        pytest.param("not_a_scope", "scope_invalid", id="wrong_type"),
    ],
)
def test_rewrite_invariant_fail_closed_on_bad_scope(scope: Any, rule: str) -> None:
    """An empty brand list fails closed: refused, never 'no filter'."""
    if scope == "empty_tuple":
        scope = ProductScope.for_brands(["Acme"])
        object.__setattr__(scope, "brands", ())  # bypass the constructor's check
    elif scope == "both":
        scope = ProductScope.for_brands(["Acme"])
        object.__setattr__(scope, "all_products", True)
    elif scope == "not_a_scope":
        scope = ("Acme",)
    result = apply_scope("SELECT brand FROM products", scope)
    assert isinstance(result, ScopeRefusal)
    assert result.rule == rule


@pytest.mark.parametrize("brands", [[], ()])
def test_empty_brand_list_cannot_be_constructed(brands: Any) -> None:
    with pytest.raises(ScopeError):
        ProductScope.for_brands(brands)
    with pytest.raises(ScopeError):
        ProductScope(brands=tuple(brands))


def test_all_scope_counts_non_buyers() -> None:
    """AC-08.12 / R3-M12: under ``all`` a never-bought user is counted; under a brand
    scope only users with an in-scope purchase are."""
    sql = "SELECT COUNT(DISTINCT id) AS n FROM users"
    all_q = scoped_ok(sql, ALL)
    assert all_q.parameters == ()
    assert "WHERE" not in all_q.sql
    assert run_local(all_q) == [(3,)]
    acme_q = scoped_ok(sql, ACME)
    assert run_local(acme_q) == [(1,)]


def test_all_scope_keeps_pii_free_projection() -> None:
    scoped = scoped_ok("SELECT COUNT(*) AS n FROM users u JOIN orders o ON o.user_id = u.id", ALL)
    for cte in ("__u", "__o"):
        assert cte_body_sql(cte, scoped=False) in scoped.sql.replace("\n", " ")
    for pii in PII_COLUMNS["users"]:
        assert pii not in scoped.sql
    assert "num_of_item" not in scoped.sql


# --------------------------------------------------------------------------- design-review tests


@pytest.mark.parametrize(
    "ref",
    ["`bigquery-public-data.thelook_ecommerce.order_items`", "order_items",
     "thelook_ecommerce.order_items"],
)
def test_cte_shadowing_rejected(ref: str) -> None:
    result = apply_scope(f"WITH order_items AS (SELECT 1 AS id) SELECT id FROM {ref}", ACME)
    assert isinstance(result, ScopeRefusal)
    assert result.rule == "cte_shadows_table"


def test_orders_num_of_item_not_exposed() -> None:
    result = apply_scope("SELECT num_of_item FROM orders", ACME)
    assert isinstance(result, ScopeRefusal)
    assert result.error_code == "UNKNOWN_COLUMN"
    for scoped in (True, False):
        assert "num_of_item" not in cte_body_sql("__o", scoped=scoped)
        assert "gender" not in cte_body_sql("__o", scoped=scoped)


# --------------------------------------------------------------------------- name forms


@pytest.mark.parametrize("table", sorted(ALLOWED_TABLES))
@pytest.mark.parametrize(
    "form",
    [
        "{t}",
        "`{t}`",
        "thelook_ecommerce.{t}",
        "`thelook_ecommerce.{t}`",
        "`thelook_ecommerce`.`{t}`",
        "`bigquery-public-data.thelook_ecommerce.{t}`",
        "`bigquery-public-data`.thelook_ecommerce.{t}",
        "`bigquery-public-data`.`thelook_ecommerce`.`{t}`",
        "bigquery-public-data.thelook_ecommerce.{t}",
    ],
)
def test_all_table_name_forms_are_normalised(table: str, form: str) -> None:
    col = "order_id" if table == "orders" else "id"
    scoped = scoped_ok(f"SELECT COUNT(DISTINCT x.{col}) AS n FROM {form.format(t=table)} AS x")
    root = sqlglot.parse_one(scoped.sql, read="bigquery")
    assert root.args["from_"].this.name == CODE_CTE_FOR_TABLE[table]


def test_unaliased_reference_keeps_table_qualifier() -> None:
    scoped = scoped_ok(
        "SELECT products.brand FROM `bigquery-public-data.thelook_ecommerce.products`"
    )
    assert "FROM __p AS products" in scoped.sql
    assert brands_of(run_local(scoped)) == {"Acme"}


# --------------------------------------------------------------------------- binding


def test_brand_values_are_bound_never_inlined() -> None:
    nasty = "O'Hare\\x -- '); DROP TABLE t; --"
    scope = ProductScope.for_brands(["Acme", nasty])
    scoped = scoped_ok("SELECT brand FROM products", scope)
    assert nasty not in scoped.sql
    assert "O'Hare" not in scoped.sql and "DROP" not in scoped.sql
    assert scoped.parameters[0].name == SCOPE_PARAM
    assert scoped.parameters[0].values == ("Acme", nasty)
    assert brands_of(run_local(scoped)) == {"Acme"}


def test_bigquery_parameters_are_string_arrays() -> None:
    pytest.importorskip("google.cloud.bigquery")
    scoped = scoped_ok("SELECT brand FROM products", ProductScope.for_brands(["a'b\\c--d"]))
    (param,) = scoped.bigquery_parameters()
    assert param.name == SCOPE_PARAM
    assert param.array_type == "STRING"
    assert param.values == ["a'b\\c--d"]


def test_no_parameters_without_scoped_tables() -> None:
    scoped = scoped_ok("SELECT 1 AS one")
    assert scoped.parameters == ()
    assert "WITH" not in scoped.sql


# --------------------------------------------------------------------------- CTE templates


def test_code_ctes_have_explicit_pii_free_columns() -> None:
    for name in CODE_CTE_ORDER:
        for scoped in (True, False):
            body = sqlglot.parse_one(cte_body_sql(name, scoped=scoped), read="bigquery")
            assert not list(body.find_all(exp.Star))
            cols = {c.alias_or_name for c in body.expressions}
            for pii in PII_COLUMNS.get("users", frozenset()):
                assert pii not in cols


_GOLDEN_COLS: dict[str, str] = {
    "__p": "id, name, brand, category, department, retail_price, cost, sku, "
           "distribution_center_id",
    "__oi": "id, order_id, user_id, product_id, inventory_item_id, status, sale_price, "
            "created_at, shipped_at, delivered_at, returned_at",
    "__o": "order_id, user_id, status, created_at, shipped_at, delivered_at, returned_at",
    "__u": "id, age, gender, city, state, country, traffic_source, created_at",
}
_GOLDEN_TABLE = {"__p": "products", "__oi": "order_items", "__o": "orders", "__u": "users"}
_GOLDEN_FILTER: dict[str, str] = {
    "__p": " WHERE brand IN UNNEST(@scope_brands)",
    "__oi": " WHERE product_id IN (SELECT id FROM __p)",
    "__o": " WHERE order_id IN (SELECT order_id FROM __oi)",
    "__u": " WHERE id IN (SELECT user_id FROM __oi)",
}


def _golden(name: str, *, scoped: bool) -> str:
    """Hand-written literal SQL of each code CTE body (independent of ``scope_ctes``)."""
    body = (f"SELECT {_GOLDEN_COLS[name]} FROM "
            f"`bigquery-public-data.thelook_ecommerce.{_GOLDEN_TABLE[name]}`")
    return body + (_GOLDEN_FILTER[name] if scoped else "")


@pytest.mark.parametrize("scoped", [True, False], ids=["brand_scope", "all_scope"])
@pytest.mark.parametrize("name", ["__p", "__oi", "__o", "__u"])
def test_code_cte_templates_golden(name: str, scoped: bool) -> None:
    """Pins the exact template text: an edit to ``_COLUMNS``/``_FILTERS`` or the template
    must fail here (``verify_scoped`` only compares the template with itself)."""
    golden = _golden(name, scoped=scoped)
    assert cte_body_sql(name, scoped=scoped) == golden
    # ... and what the rewriter actually emits for each body is the same literal SQL.
    scope = ACME if scoped else ALL
    scoped_q = scoped_ok(
        "SELECT COUNT(*) AS n FROM users u JOIN orders o ON o.user_id = u.id "
        "JOIN order_items oi ON oi.order_id = o.order_id JOIN products p ON p.id = oi.product_id",
        scope,
    )
    root = sqlglot.parse_one(scoped_q.sql, read="bigquery")
    cte = next(c for c in root.args["with_"].expressions if c.alias == name)
    assert cte.this.sql(dialect="bigquery") == golden
    assert f"{name} AS ({golden})" in scoped_q.sql


def test_required_ctes_dependency_closure() -> None:
    assert required_ctes(["__u"], scoped=True) == ("__p", "__oi", "__u")
    assert required_ctes(["__o"], scoped=True) == ("__p", "__oi", "__o")
    assert required_ctes(["__u"], scoped=False) == ("__u",)
    assert required_ctes([], scoped=True) == ()


def test_rewritten_sql_is_regenerated_without_comments() -> None:
    scoped = scoped_ok("SELECT brand /* users */ FROM products -- orders\n")
    assert "/*" not in scoped.sql and "--" not in scoped.sql


def test_model_ctes_are_kept_after_code_ctes() -> None:
    scoped = scoped_ok("WITH acme AS (SELECT id, brand FROM products) SELECT brand FROM acme")
    root = sqlglot.parse_one(scoped.sql, read="bigquery")
    assert [c.alias for c in root.args["with_"].expressions] == ["__p", "acme"]


def test_scope_rewrite_does_not_mutate_input() -> None:
    stmt = sqlglot.parse_one("SELECT brand FROM products", read="bigquery")
    before = stmt.sql(dialect="bigquery")
    scope_rewrite(stmt, ACME)
    assert stmt.sql(dialect="bigquery") == before


def test_scope_rewrite_rejects_non_query_root() -> None:
    with pytest.raises(ScopeError):
        scope_rewrite(sqlglot.parse_one("(SELECT 1)", read="bigquery"), ACME)


# --------------------------------------------------------------------------- verify_scoped


def _good(sql: str = "SELECT brand FROM products") -> str:
    result = apply_scope(sql, ACME)
    assert isinstance(result, ScopedQuery)
    return result.sql


_P = f"__p AS ({cte_body_sql('__p', scoped=True)})"
_P_ALL = f"__p AS ({cte_body_sql('__p', scoped=False)})"
_OI = f"__oi AS ({cte_body_sql('__oi', scoped=True)})"


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT brand FROM products", id="raw_bare"),
        pytest.param(f"SELECT brand FROM {FQ.format(t='products')}", id="raw_fq"),
        pytest.param("SELECT brand FROM thelook_ecommerce.products", id="raw_two_part"),
        pytest.param(f"WITH {_P} SELECT brand FROM __p UNION ALL SELECT brand FROM products",
                     id="raw_in_union_branch"),
        pytest.param(f"WITH {_P} SELECT brand FROM __p WHERE id IN (SELECT id FROM Products)",
                     id="raw_case_variant_in_subquery"),
        pytest.param(f"WITH {_P_ALL} SELECT brand FROM __p", id="unfiltered_template"),
        pytest.param(f"WITH {_OI} SELECT id FROM __oi", id="missing_dependency"),
        pytest.param(f"WITH {_OI}, {_P} SELECT id FROM __oi", id="wrong_order"),
        pytest.param(f"WITH {_P}, __x AS (SELECT 1 AS a) SELECT a FROM __x",
                     id="unknown_code_cte"),
        pytest.param(f"WITH {_P} SELECT t.brand FROM (WITH __p AS (SELECT 'x' AS brand) "
                     "SELECT brand FROM __p) t", id="nested_code_cte_name"),
        pytest.param(f"WITH {_P}, m AS (SELECT id FROM __p) SELECT id FROM m "
                     "WHERE id IN UNNEST(@scope_brands)", id="parameter_outside_code_cte"),
        pytest.param(f"WITH {_P} SELECT id FROM nowhere", id="unresolved_reference"),
        pytest.param(f"WITH {_P} SELECT x.brand FROM __p AS __x", id="reserved_alias"),
        pytest.param(f"WITH {_P}, a AS (SELECT id FROM b), b AS (SELECT id FROM __p) "
                     "SELECT id FROM a", id="forward_cte_reference"),
        pytest.param("SELECT 1; SELECT 2", id="two_statements"),
        pytest.param("not sql at all (", id="unparseable"),
    ],
)
def test_verify_scoped_rejects(sql: str) -> None:
    with pytest.raises(ScopeInvariantError):
        verify_scoped(sql, ACME)


def test_verify_scoped_accepts_rewriter_output() -> None:
    verify_scoped(_good(), ACME)


def test_verify_scoped_is_scope_specific() -> None:
    """Brand-scope output does not verify as all-scope output, and vice versa."""
    with pytest.raises(ScopeInvariantError):
        verify_scoped(_good(), ALL)
    all_q = apply_scope("SELECT brand FROM products", ALL)
    assert isinstance(all_q, ScopedQuery)
    with pytest.raises(ScopeInvariantError):
        verify_scoped(all_q.sql, ACME)


# --------------------------------------------------------------------------- scope object


def test_scope_from_profile() -> None:
    class _P:
        def __init__(self, brands: tuple[str, ...], all_products: bool) -> None:
            self.brands, self.all_products = brands, all_products

    assert ProductScope.from_profile(_P(("A", "B"), False)).brands == ("A", "B")
    assert ProductScope.from_profile(_P((), True)) == ALL
    with pytest.raises(ScopeError):
        ProductScope.from_profile(_P((), False))
    with pytest.raises(ScopeError):
        ProductScope.from_profile(None)


@pytest.mark.parametrize("brands", [("A",), ["A", "B"]])
def test_scope_from_profile_all_plus_brands_fails_closed(brands: Any) -> None:
    """``all_products: true`` with a brand list is ambiguous: refused, never widened to all."""

    class _P:
        def __init__(self) -> None:
            self.brands, self.all_products = brands, True

    with pytest.raises(ScopeError):
        ProductScope.from_profile(_P())


def test_scope_from_profile_non_bool_all_flag_rejected() -> None:
    class _P:
        brands = ("A",)
        all_products = "yes"

    with pytest.raises(ScopeError):
        ProductScope.from_profile(_P())


def test_shipped_profiles_build_a_scope() -> None:
    """No profile in config/profiles.yaml sets both ``all_products`` and brands."""
    from opsfleet_agent.session import load_profiles

    for profile in load_profiles().values():
        ProductScope.from_profile(profile)


@pytest.mark.parametrize(
    "brands",
    [["A", "A"], [""], ["   "], ["x" * 101], ["bad\nbrand"], ["tab\tbrand"], [1], "Acme", None],
)
def test_invalid_brand_lists_rejected(brands: Any) -> None:
    with pytest.raises(ScopeError):
        ProductScope.for_brands(brands)


def test_all_scope_takes_no_brands() -> None:
    with pytest.raises(ScopeError):
        ProductScope(brands=("A",), all_products=True)


def test_scope_key() -> None:
    assert ALL.scope_key == "all"
    a = ProductScope.for_brands(["B", "A"]).scope_key
    assert a == ProductScope.for_brands(["A", "B"]).scope_key
    assert a != ProductScope.for_brands(["A"]).scope_key
    assert a.startswith("brands:")
    assert len(a.removeprefix("brands:")) == 32  # a hash, never the brand names
