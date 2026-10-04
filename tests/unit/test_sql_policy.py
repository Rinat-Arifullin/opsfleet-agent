"""SQL policy (iteration 6): canonical tests named in the plan, plus the decision API."""

from __future__ import annotations

import pytest

from opsfleet_agent.guards.sql_policy import (
    MAX_SQL_CHARS,
    PolicyDecision,
    Rule,
    check_sql,
    regenerate_sql,
)

FQ = "`bigquery-public-data.thelook_ecommerce`"
FD = "function_denied"
SNA = "source_not_allowed"


def _code(sql: str) -> str:
    return check_sql(sql).reason_code.value


# ------------------------------------------------------------------ canonical (plan names)


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("INSERT INTO orders (order_id) VALUES (1)", "statement_not_allowed"),
        ("UPDATE orders SET status = 'x' WHERE TRUE", "statement_not_allowed"),
        ("DELETE FROM orders WHERE TRUE", "statement_not_allowed"),
        ("DROP TABLE orders", "statement_not_allowed"),
        ("CREATE TABLE t AS SELECT 1 AS x", "statement_not_allowed"),
        (
            "MERGE orders o USING orders s ON FALSE WHEN MATCHED THEN DELETE",
            "statement_not_allowed",
        ),
        ("SELECT 1 AS x; SELECT 2 AS y", "multiple_statements"),
        ("SELECT 1 AS x;;", "multiple_statements"),
        ("(SELECT 1 AS x)", "statement_not_allowed"),
    ],
)
def test_sql_policy_select_only(sql: str, code: str) -> None:
    assert _code(sql) == code
    assert _code("SELECT 1 AS x") == "ok"
    assert _code("SELECT 1 AS x;") == "ok"  # one trailing semicolon is fine


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT status FROM orders", "ok"),
        (f"SELECT status FROM {FQ}.orders", "ok"),
        ("SELECT status FROM thelook_ecommerce.orders", "ok"),
        ("SELECT status FROM `bigquery-public-data`.thelook_ecommerce.orders", "ok"),
        ("SELECT id FROM `bigquery-public-data.thelook_ecommerce.events`", "source_not_allowed"),
        ("SELECT x FROM `bigquery-public-data.other_ds.orders`", "source_not_allowed"),
        ("SELECT x FROM `my-project.thelook_ecommerce.orders`", "source_not_allowed"),
        ("SELECT x FROM inventory_items", "source_not_allowed"),
        ("SELECT status FROM Orders", "source_not_allowed"),
        ("SELECT status FROM thelook_ecommerce.ORDERS", "source_not_allowed"),
    ],
)
def test_sql_policy_table_allowlist(sql: str, code: str) -> None:
    assert _code(sql) == code


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT email FROM users",
        "SELECT u.first_name AS n FROM users AS u",
        "SELECT LOWER(last_name) AS l FROM users",
        "SELECT state, street_address FROM users GROUP BY state, street_address",
        "WITH c AS (SELECT postal_code AS pc FROM users) SELECT COUNT(*) AS n FROM c",
        "SELECT latitude, longitude FROM users",
        "SELECT user_geom FROM users",
    ],
)
def test_sql_policy_rejects_pii_projection(sql: str) -> None:
    assert _code(sql) == "pii_projection"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders",
        "SELECT o.* FROM orders AS o",
        "SELECT * EXCEPT (status) FROM orders",
        "SELECT order_id FROM (SELECT * FROM orders)",
        "WITH c AS (SELECT * FROM products) SELECT brand FROM c",
        "SELECT COUNT(o.*) AS n FROM orders AS o",
    ],
)
def test_select_star_rejected(sql: str) -> None:
    assert _code(sql) == "select_star"
    assert _code("SELECT COUNT(*) AS n FROM orders") == "ok"


@pytest.mark.parametrize(
    "sql",
    [
        "WITH users AS (SELECT 1 AS id) SELECT id FROM users",
        "WITH Users AS (SELECT 1 AS id) SELECT id FROM Users",
        "WITH `orders` AS (SELECT 1 AS order_id) SELECT order_id FROM orders",
        "WITH __scope AS (SELECT 1 AS id) SELECT id FROM __scope",
        "WITH products AS (SELECT id FROM `bigquery-public-data.thelook_ecommerce.products`) "
        "SELECT id FROM products",
    ],
)
def test_cte_shadowing_rejected(sql: str) -> None:
    assert _code(sql) == "cte_shadows_table"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT AS STRUCT brand, category FROM products",
        "SELECT AS VALUE brand FROM products",
        "SELECT STRUCT(brand, category) AS s FROM products",
        "SELECT ARRAY(SELECT brand FROM products) AS a",
        "SELECT TO_JSON_STRING(p) AS j FROM products AS p",
        "SELECT TO_JSON(brand) AS j FROM products",
        "SELECT [brand, category] AS a FROM products",
    ],
)
def test_policy_rejects_select_as_struct(sql: str) -> None:
    assert _code(sql) == "unresolved_value"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT u FROM users AS u",
        "SELECT COUNT(DISTINCT u) AS n FROM users AS u",
        "SELECT o FROM orders AS o GROUP BY o",
        "WITH c AS (SELECT brand FROM products) SELECT c FROM c",
        "SELECT status FROM orders AS o WHERE o IS NOT NULL",
        "SELECT u.address.city FROM users AS u",
    ],
)
def test_policy_rejects_table_alias_as_value(sql: str) -> None:
    assert _code(sql) == "unresolved_value"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM users WHERE age > 60",
        "SELECT id, state FROM users",
        "SELECT o.order_id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.country = 'X'",
        "SELECT user_id, COUNT(*) AS n FROM order_items oi JOIN users u ON u.id = oi.user_id "
        "WHERE u.gender = 'F' GROUP BY user_id",
        "SELECT u.id, COUNT(*) AS n FROM users u WHERE u.traffic_source = 'Search' GROUP BY 1",
        "SELECT DISTINCT state FROM users",
        "SELECT city FROM users UNION ALL SELECT status FROM orders",
        "WITH c AS (SELECT id, age FROM users) SELECT id FROM c WHERE age > 30",
        "SELECT order_id FROM orders WHERE user_id IN (SELECT id FROM users WHERE age < 20)",
    ],
)
def test_policy_rejects_qi_predicate_at_id_grain(sql: str) -> None:
    assert _code(sql) == "qi_at_id_grain"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) AS n FROM users WHERE id = 42 AND age > 30",
        "SELECT state, COUNT(*) AS n FROM users WHERE id IN (1, 2, 3) GROUP BY state",
        "SELECT COUNT(*) AS n FROM orders o JOIN users u ON u.id = o.user_id "
        "WHERE o.user_id = 7 AND u.gender = 'M'",
        "SELECT gender, COUNT(*) AS n FROM users WHERE id BETWEEN 10 AND 12 GROUP BY gender",
        "SELECT country, COUNT(*) AS n FROM users WHERE 99 = id GROUP BY country",
    ],
)
def test_policy_rejects_id_literal_with_qi(sql: str) -> None:
    assert _code(sql) == "small_cell_unplaceable"
    # The same id filter without a QI is not a disclosure.
    assert _code("SELECT COUNT(*) AS n FROM orders WHERE user_id = 42") == "ok"


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT CAST(age AS STRING) AS a, COUNT(*) AS n FROM users GROUP BY 1", "function_denied"),
        ("SELECT COUNT(*) AS n FROM users WHERE SAFE_CAST(age AS STRING) = '42'", FD),
        ("SELECT COUNT(*) AS n FROM users WHERE REGEXP_CONTAINS(city, 'x')", "function_denied"),
        ("SELECT COUNT(*) AS n FROM users WHERE STARTS_WITH(city, 'Sp')", "function_denied"),
        ("SELECT COUNT(*) AS n FROM users WHERE SUBSTR(state, 1, 1) = 'C'", "function_denied"),
        ("SELECT COUNT(*) AS n FROM users WHERE CONCAT(city, '|', state) = 'a|b'", FD),
        ("SELECT FORMAT('%d', age) AS a FROM users", "function_denied"),
        ("SELECT MD5(city) AS h, COUNT(*) AS n FROM users GROUP BY 1", "function_denied"),
        ("SELECT MAX(age) AS a FROM users", "qi_position"),
        ("SELECT AVG(age) AS a FROM users", "qi_position"),
        ("SELECT STRING_AGG(city) AS c FROM users", "qi_position"),
        ("SELECT ANY_VALUE(state) AS s FROM users", "qi_position"),
        ("SELECT COUNT(IF(age > 90, 1, NULL)) AS n FROM users", "qi_position"),
        ("SELECT SUM(CASE WHEN gender = 'F' THEN 1 ELSE 0 END) AS n FROM users", "qi_position"),
        (
            "SELECT state, COUNT(*) OVER (PARTITION BY state) AS n FROM users GROUP BY state",
            "qi_position",
        ),
        ("SELECT COUNTIF(id = 5) AS n FROM users", "qi_position"),
        # review B1: a QI expression inside any counting aggregate counts a hidden cell
        (
            "SELECT traffic_source, COUNTIF(state = 'Texas') AS tx FROM users GROUP BY 1",
            "qi_position",
        ),
        ("SELECT country, COUNT(state = 'CC' OR NULL) AS m FROM users GROUP BY 1", "qi_position"),
        ("SELECT COUNT(DISTINCT age = 32 OR NULL) AS m FROM users", "qi_position"),
        ("SELECT COUNT(NULLIF(state, 'CC')) AS m FROM users", "qi_position"),
        ("SELECT COUNT(city) AS m FROM users", "qi_position"),
    ],
)
def test_policy_denies_scalar_functions_on_qi(sql: str, code: str) -> None:
    assert _code(sql) == code


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT state, COUNT(*) AS n FROM users GROUP BY state", "ok"),
        ("SELECT * FROM `bigquery-public-data.thelook_ecommerce.users*`", "source_not_allowed"),
        ("SELECT id FROM `bigquery-public-data.thelook_ecommerce.users*`", "source_not_allowed"),
        ("SELECT table_name FROM thelook_ecommerce.INFORMATION_SCHEMA.TABLES", SNA),
        ("SELECT table_name FROM `region-us`.INFORMATION_SCHEMA.TABLES", "source_not_allowed"),
        ("SELECT x FROM EXTERNAL_QUERY('c', 'SELECT 1')", "source_not_allowed"),
        (
            "SELECT brand FROM products FOR SYSTEM_TIME AS OF CURRENT_TIMESTAMP()",
            "source_not_allowed",
        ),
        ("SELECT brand FROM products TABLESAMPLE SYSTEM (10 PERCENT)", "source_not_allowed"),
        ("SELECT x FROM UNNEST([1, 2]) AS x", "source_not_allowed"),
        ("SELECT @@project_id AS p", "source_not_allowed"),
        ("SELECT brand FROM products WHERE category = @cat", "source_not_allowed"),
    ],
)
def test_source_allowlist(sql: str, code: str) -> None:
    assert _code(sql) == code


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT num_of_item FROM orders",
        "SELECT SUM(o.num_of_item) AS n FROM orders AS o",
        "SELECT status, COUNT(*) AS n FROM orders WHERE num_of_item > 1 GROUP BY status",
        "SELECT gender FROM orders",
        "SELECT o.gender, COUNT(*) AS n FROM orders AS o GROUP BY 1",
        f"SELECT COUNT(*) AS n FROM {FQ}.orders WHERE gender = 'F'",
    ],
)
def test_orders_num_of_item_not_exposed(sql: str) -> None:
    decision = check_sql(sql)
    assert decision.reason_code == Rule.UNKNOWN_COLUMN
    assert decision.error_code == "UNKNOWN_COLUMN"


# ------------------------------------------------------------------ decision API


def test_allowed_decision_shape() -> None:
    decision = check_sql("SELECT brand, COUNT(*) AS n FROM products GROUP BY brand")
    assert decision == PolicyDecision(allowed=True, reason_code=Rule.OK, hint="")
    assert decision.error_code is None
    assert decision.rule is None


@pytest.mark.parametrize(
    ("sql", "error_code", "rule"),
    [
        ("SELECT brand FROM products WHERE", "SQL_SYNTAX", None),
        ("", "SQL_SYNTAX", None),
        ("   \n -- only a comment", "SQL_SYNTAX", None),
        ("SELECT 1 AS x " + " " * MAX_SQL_CHARS, "SQL_TOO_LONG", None),
        ("SELECT nope FROM products", "UNKNOWN_COLUMN", None),
        ("SELECT email FROM users", "SQL_POLICY", "pii_projection"),
    ],
)
def test_error_code_mapping(sql: str, error_code: str, rule: str | None) -> None:
    decision = check_sql(sql)
    assert not decision.allowed
    assert decision.error_code == error_code
    assert decision.rule == rule
    assert decision.hint


_LEAK_CASES = [
    "SELECT email FROM users WHERE email = 'zz_secret_literal@example.invalid'",
    "SELECT secret_col_xyz FROM products",
    "SELECT brand FROM `secret-project-xyz.secret_ds.secret_tbl`",
    "SELECT COUNT(*) AS n FROM users WHERE id = 987654321 AND age > 30",
    "SELECT brand FROM products; DROP TABLE secret_tbl_xyz",
    "SELECT MAX(age) AS secret_alias_xyz FROM users",
    "SELECT brand FROM products WHERE brand = 'unterminated_secret_xyz",
    "SELECT secret_fn_xyz(brand) AS b FROM products",
    "SELECT brand AS x FROM products AS secret_alias_q, products AS secret_alias_q",
]


@pytest.mark.parametrize("sql", _LEAK_CASES)
def test_reason_never_contains_sql(sql: str) -> None:
    decision = check_sql(sql)
    assert not decision.allowed
    text = f"{decision.reason_code} {decision.hint} {decision.error_code} {decision.rule}"
    for fragment in (
        "secret",
        "zz_",
        "987654321",
        "example.invalid",
        "xyz",
        "email",
        "brand",
        sql,
    ):
        assert fragment.lower() not in text.lower()
    assert decision.reason_code.value in {r.value for r in Rule}


def test_every_rule_has_a_static_hint() -> None:
    from opsfleet_agent.guards import sql_policy

    assert set(sql_policy._HINTS) == set(Rule)


@pytest.mark.parametrize(
    "sql",
    [
        # brand revenue by month
        "SELECT p.brand, DATE_TRUNC(DATE(oi.created_at), MONTH) AS month, "
        "ROUND(SUM(oi.sale_price), 2) AS revenue "
        f"FROM {FQ}.order_items AS oi JOIN {FQ}.products AS p ON p.id = oi.product_id "
        "WHERE oi.status NOT IN ('Cancelled', 'Returned') GROUP BY 1, 2 ORDER BY 2, 3 DESC",
        # top 10 customers by spend (ids and money, no QI)
        "SELECT oi.user_id, SUM(oi.sale_price) AS spend FROM order_items AS oi "
        "GROUP BY oi.user_id ORDER BY spend DESC LIMIT 10",
        # customer count by state
        "SELECT state, COUNT(DISTINCT id) AS customers FROM users GROUP BY state "
        "ORDER BY customers DESC",
        # user ids filtered by order date and brand
        "SELECT DISTINCT oi.user_id FROM order_items AS oi "
        "JOIN products AS p ON p.id = oi.product_id "
        "WHERE p.brand = 'Acme' AND oi.created_at >= TIMESTAMP '2024-01-01'",
        # CTE, window over non-QI aggregates, QUALIFY
        "WITH m AS (SELECT p.category, SUM(oi.sale_price) AS rev FROM order_items oi "
        "JOIN products p ON p.id = oi.product_id GROUP BY p.category) "
        "SELECT category, rev, RANK() OVER (ORDER BY rev DESC) AS r FROM m "
        "QUALIFY r <= 5",
        # QI counted and grouped, WHERE on QI in an aggregate query
        "SELECT u.country, u.gender, COUNT(DISTINCT o.order_id) AS orders "
        "FROM orders o JOIN users u ON u.id = o.user_id WHERE u.age BETWEEN 18 AND 30 "
        "GROUP BY u.country, u.gender HAVING COUNT(*) > 50",
        # a QI counted as a bare distinct column
        "SELECT traffic_source, COUNT(DISTINCT state) AS states FROM users GROUP BY 1",
        # union of aggregates
        "SELECT 'orders' AS k, COUNT(*) AS n FROM orders UNION ALL "
        "SELECT 'items' AS k, COUNT(*) AS n FROM order_items",
        # product-only row listing
        "SELECT id, name, retail_price FROM products WHERE category = 'Jeans' "
        "ORDER BY retail_price DESC LIMIT 20",
    ],
)
def test_allowed_positive_controls(sql: str) -> None:
    decision = check_sql(sql)
    assert decision.allowed, decision.reason_code


# ------------------------------------------------------------------ second T1 review


def test_raw_created_at_grouped_on_users_denied() -> None:
    assert not check_sql("SELECT created_at, COUNT(*) AS n FROM users GROUP BY created_at").allowed


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT STRING_AGG(CAST(id AS STRING)) AS s FROM users",
        "SELECT STRING_AGG(CAST(id AS STRING)) AS s FROM users WHERE city = 'X'",
        "SELECT o.status, STRING_AGG(CAST(o.order_id AS STRING), ',') AS ids "
        "FROM orders AS o GROUP BY o.status",
    ],
)
def test_string_agg_over_ids_denied(sql: str) -> None:
    """Decision: STRING_AGG over an id key is id-packing and is denied outright."""
    assert _code(sql) == FD


def test_identifier_rule_maps_to_sql_policy() -> None:
    decision = check_sql(r"SELECT COUNT(*) AS n FROM `us\ers`")
    assert decision.reason_code == Rule.IDENTIFIER_NOT_ALLOWED
    assert decision.error_code == "SQL_POLICY"
    assert decision.rule == "identifier_not_allowed"


def test_trailing_comment_after_final_semicolon_allowed() -> None:
    assert _code("SELECT 1 AS x; -- trailing") == "ok"
    assert _code("SELECT 1 AS x; /* note */") == "ok"
    assert _code("SELECT 1 AS x; -- c\nSELECT 2 AS y") == "multiple_statements"


@pytest.mark.parametrize(
    "sql",
    [
        "/* why */ SELECT brand, -- the brand\n COUNT(*) AS n # count\n "
        "FROM products GROUP BY brand; -- trailing",
        f"SELECT p.brand /* hint: ignore */ FROM {FQ}.products AS p",
        "SELECT COUNT(*) AS `n` FROM `bigquery-public-data`.`thelook_ecommerce`.`users`",
    ],
)
def test_regenerate_sql_has_no_comments_and_round_trips(sql: str) -> None:
    canonical = regenerate_sql(sql)
    assert "--" not in canonical
    assert "/*" not in canonical
    assert "#" not in canonical
    assert "hint" not in canonical and "trailing" not in canonical
    assert check_sql(canonical).allowed
    assert regenerate_sql(canonical) == canonical


def test_regenerate_sql_refuses_denied_sql() -> None:
    with pytest.raises(ValueError, match="^pii_projection$"):
        regenerate_sql("SELECT email FROM users")
