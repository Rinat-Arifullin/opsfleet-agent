"""SQL policy red team (M-1 classes). Every value is synthetic.

Each case names the attack class and the set of reason codes that count as a correct
rejection (the first failing check decides the code, so several are acceptable for some
attacks). The positive controls at the end must stay allowed, so the policy is not
"secure" by rejecting everything.
"""

from __future__ import annotations

import pytest

from opsfleet_agent.guards.sql_policy import check_sql

PII = frozenset({"pii_projection", "pii_reference"})
SRC = frozenset({"source_not_allowed"})
STMT = frozenset({"statement_not_allowed", "multiple_statements"})
FUNC = frozenset({"function_denied", "unresolved_value"})
IDENT = frozenset({"identifier_not_allowed"})

_CASES: list[tuple[str, str, frozenset[str]]] = [
    # --- PII in every clause
    ("pii_where", "SELECT COUNT(*) AS n FROM users WHERE email LIKE '%@example.test'", PII),
    ("pii_where_qualified", "SELECT COUNT(*) AS n FROM users u WHERE u.last_name = 'Doe'", PII),
    ("pii_join", "SELECT COUNT(*) AS n FROM users a JOIN users b ON a.email = b.email", PII),
    ("pii_group_by", "SELECT COUNT(*) AS n FROM users GROUP BY postal_code", PII),
    ("pii_order_by", "SELECT id FROM users ORDER BY last_name", PII),
    ("pii_having", "SELECT state, COUNT(*) AS n FROM users GROUP BY state "
     "HAVING MAX(LENGTH(email)) > 10", PII),
    ("pii_like", "SELECT COUNT(*) AS n FROM users WHERE first_name LIKE 'A%'", PII),
    ("pii_in_subquery", "SELECT COUNT(*) AS n FROM orders WHERE user_id IN "
     "(SELECT id FROM users WHERE email = 'a@example.test')", PII),
    ("pii_exists", "SELECT COUNT(*) AS n FROM orders o WHERE EXISTS "
     "(SELECT 1 AS x FROM users u WHERE u.id = o.user_id AND u.street_address IS NOT NULL)",
     PII),
    ("pii_cte_rename", "WITH c AS (SELECT email AS e FROM users) SELECT e FROM c", PII),
    ("pii_derived_rename", "SELECT x FROM (SELECT postal_code AS x FROM users)", PII),
    ("pii_scalar_subquery", "SELECT (SELECT MAX(email) FROM users) AS m", PII),
    ("pii_union", "SELECT brand FROM products UNION ALL SELECT email FROM users", PII),
    ("pii_window", "SELECT id, ROW_NUMBER() OVER (ORDER BY last_name) AS r FROM users", PII),
    ("pii_case", "SELECT CASE WHEN latitude > 0 THEN 1 ELSE 0 END AS n FROM users", PII),
    ("pii_count_distinct", "SELECT COUNT(DISTINCT email) AS n FROM users", PII),
    ("pii_geom", "SELECT COUNT(*) AS n FROM users WHERE user_geom IS NULL", PII),
    # --- PII through functions
    ("concat_pii", "SELECT CONCAT(first_name, ' ', last_name) AS n FROM users", PII | FUNC),
    ("dpipe_pii", "SELECT first_name || last_name AS n FROM users", PII | FUNC),
    ("substr_pii", "SELECT SUBSTR(email, 1, 3) AS s FROM users", PII | FUNC),
    ("to_json_string_row", "SELECT TO_JSON_STRING(u) AS j FROM users u", FUNC | PII),
    ("to_json_string_pii", "SELECT TO_JSON_STRING(email) AS j FROM users", FUNC | PII),
    ("string_agg_pii", "SELECT STRING_AGG(email, ',') AS s FROM users", PII | FUNC),
    ("array_agg_pii", "SELECT ARRAY_AGG(email) AS a FROM users", PII | FUNC),
    ("array_agg_any", "SELECT ARRAY_AGG(brand) AS a FROM products", FUNC),
    ("struct_pii", "SELECT STRUCT(email) AS s FROM users", PII | FUNC),
    ("md5_pii", "SELECT MD5(email) AS h FROM users", PII | FUNC),
    ("format_pii", "SELECT FORMAT('%s', email) AS f FROM users", PII | FUNC),
    ("safe_prefix", "SELECT SAFE.SUBSTR(email, 1, 2) AS s FROM users", PII | FUNC),
    ("udf_call", "SELECT my_ds.fn(brand) AS b FROM products", FUNC),
    ("anonymous_fn", "SELECT leak(brand) AS b FROM products", FUNC),
    ("error_fn", "SELECT ERROR(brand) AS b FROM products", FUNC),
    ("session_user", "SELECT SESSION_USER() AS u", FUNC),
    ("json_extract", "SELECT JSON_EXTRACT(brand, '$.a') AS b FROM products", FUNC),
    ("split_offset", "SELECT SPLIT(brand, ' ')[OFFSET(0)] AS b FROM products", FUNC),
    # --- exfiltration and side channels
    ("export_data", "EXPORT DATA OPTIONS (uri = 'gs://bucket-example/x*.csv', format = 'CSV') "
     "AS SELECT brand FROM products", STMT),
    ("external_query_from", "SELECT x FROM EXTERNAL_QUERY('conn', 'SELECT 1')", SRC),
    ("external_query_scalar", "SELECT EXTERNAL_QUERY('conn', 'SELECT 1') AS x", FUNC | SRC),
    ("ml_predict", "SELECT x FROM ML.PREDICT(MODEL m, TABLE products)",
     SRC | frozenset({"sql_syntax"})),
    ("ml_scalar", "SELECT ML.STANDARD_SCALER(retail_price) OVER () AS s FROM products",
     FUNC | frozenset({"sql_syntax", "unsupported_syntax"})),
    ("time_travel", "SELECT brand FROM products FOR SYSTEM_TIME AS OF "
     "TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)", SRC),
    ("time_travel_fq", "SELECT brand FROM `bigquery-public-data.thelook_ecommerce.products` "
     "FOR SYSTEM_TIME AS OF '2024-01-01'", SRC),
    ("temp_udf", "CREATE TEMP FUNCTION f(x STRING) AS (x); SELECT f(email) AS e FROM users",
     STMT),
    ("temp_udf_js", "CREATE TEMPORARY FUNCTION f(x STRING) RETURNS STRING LANGUAGE js "
     "AS 'return x'; SELECT 1 AS x", STMT),
    ("system_variable", "SELECT @@project_id AS p", SRC),
    ("system_variable_where", "SELECT brand FROM products WHERE @@dataset_id IS NOT NULL", SRC),
    ("query_param", "SELECT brand FROM products WHERE brand = @b", SRC),
    ("information_schema_ds",
     "SELECT column_name FROM thelook_ecommerce.INFORMATION_SCHEMA.COLUMNS", SRC),
    ("information_schema_fq", "SELECT table_name FROM "
     "`bigquery-public-data.thelook_ecommerce.INFORMATION_SCHEMA.TABLES`", SRC),
    ("information_schema_region", "SELECT job_id FROM `region-us`.INFORMATION_SCHEMA.JOBS", SRC),
    ("information_schema_cte", "WITH c AS (SELECT table_name FROM "
     "thelook_ecommerce.INFORMATION_SCHEMA.TABLES) SELECT table_name FROM c", SRC),
    ("wildcard_table", "SELECT brand FROM `bigquery-public-data.thelook_ecommerce.*`", SRC),
    ("wildcard_prefix", "SELECT id FROM `bigquery-public-data.thelook_ecommerce.user*` "
     "WHERE _TABLE_SUFFIX = 's'", SRC),
    ("other_dataset", "SELECT x FROM `bigquery-public-data.samples.shakespeare`", SRC),
    ("other_project", "SELECT brand FROM `attacker-proj.thelook_ecommerce.products`", SRC),
    ("events_table", "SELECT session_id FROM thelook_ecommerce.events", SRC),
    ("four_part", "SELECT brand FROM a.b.c.products", SRC | frozenset({"sql_syntax"})),
    ("unnest", "SELECT x FROM UNNEST(GENERATE_ARRAY(1, 3)) AS x", SRC | FUNC),
    ("tablesample", "SELECT brand FROM products TABLESAMPLE SYSTEM (1 PERCENT)", SRC),
    ("subquery_bad_source", "SELECT brand FROM products WHERE id IN "
     "(SELECT id FROM thelook_ecommerce.distribution_centers)", SRC),
    # --- statement smuggling
    ("execute_immediate", "EXECUTE IMMEDIATE 'SELECT email FROM users'", STMT),
    ("execute_immediate_after", "SELECT 1 AS x; EXECUTE IMMEDIATE 'DROP TABLE t'", STMT),
    ("chain_drop", "SELECT brand FROM products; DROP TABLE products", STMT),
    ("chain_select", "SELECT brand FROM products; SELECT email FROM users", STMT),
    ("chain_comment", "SELECT brand FROM products; -- trailing\nSELECT email FROM users", STMT),
    ("declare", "DECLARE x STRING; SELECT 1 AS y", STMT),
    ("begin_block", "BEGIN SELECT email FROM users; END", STMT),
    ("call_proc", "CALL ds.proc()", STMT),
    ("set_var", "SET @@dataset_id = 'x'", STMT | SRC),
    ("insert_select", "INSERT INTO ds.t SELECT email FROM users", STMT),
    ("create_view", "CREATE VIEW v AS SELECT email FROM users", STMT),
    ("leading_with_dml", "WITH c AS (SELECT 1 AS x) DELETE FROM orders WHERE TRUE",
     STMT | frozenset({"sql_syntax"})),
    ("recursive_cte", "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 AS n FROM r "
     "WHERE n < 5) SELECT n FROM r", frozenset({"recursive_cte"})),
    # --- comment tricks
    ("comment_hides_semicolon", "SELECT brand FROM products /* ; */ ; SELECT email FROM users",
     STMT),
    ("comment_inside_name", "SELECT em/**/ail FROM users",
     frozenset({"sql_syntax", "unknown_column", "pii_projection"})),
    ("comment_nested", "SELECT brand FROM products /* /* */ , users WHERE email = 'x' --*/",
     PII),
    ("comment_hash", "SELECT state # harmless\n, email FROM users", PII),
    ("comment_dash_newline", "SELECT -- note\n email FROM users", PII),
    ("comment_only", "/* SELECT brand FROM products */", frozenset({"sql_syntax"})),
    ("comment_before_dml", "/* hi */ DROP TABLE products", STMT),
    ("string_with_semicolon_then_pii", "SELECT ';' AS s, email FROM users", PII),
    # --- quoting and case
    ("quoted_column", "SELECT `email` FROM users", PII),
    ("quoted_qualified", "SELECT u.`email` FROM users AS u", PII),
    ("quoted_table_and_column", "SELECT `u`.`last_name` FROM `users` AS `u`", PII),
    ("upper_column", "SELECT EMAIL FROM users", PII),
    ("mixed_case_column", "SELECT EmAiL FROM users", PII),
    ("mixed_case_alias", "SELECT U.Email FROM users AS u", PII),
    ("mixed_case_keywords", "sElEcT eMaIl FrOm users", PII),
    ("quoted_backtick_path", "SELECT email FROM `bigquery-public-data`.`thelook_ecommerce`.`users`",
     PII),
    ("quoted_cte_shadow", "WITH `Users` AS (SELECT 1 AS id) SELECT id FROM `Users`",
     frozenset({"cte_shadows_table"})),
    ("quoted_numnum", "SELECT `num_of_item` FROM orders", frozenset({"unknown_column"})),
    ("upper_qi_row", "SELECT ID, AGE FROM users", frozenset({"qi_at_id_grain"})),
    # --- backslash escapes and junk inside backtick identifiers (BigQuery decodes them)
    ("escape_hex_scope_cte", r"WITH `\x5f_p` AS (SELECT product_id AS id FROM order_items) "
     r"SELECT COUNT(*) AS n FROM `\x5f_p`", IDENT),
    ("escape_hex_users_cte", r"WITH `\x75sers` AS (SELECT id, city FROM users) "
     r"SELECT city, COUNT(*) AS n FROM `\x75sers` GROUP BY city", IDENT),
    ("escape_backslash_table", r"SELECT COUNT(*) AS n FROM `us\ers`", IDENT),
    ("escape_backtick", r"SELECT 1 AS `a\`b`", IDENT | frozenset({"sql_syntax"})),
    ("space_in_alias", "SELECT 1 AS `a b`", IDENT),
    ("semicolon_in_table", "SELECT COUNT(*) AS n FROM `users;`", IDENT),
    ("newline_in_alias", "SELECT brand AS `b\nx` FROM products", IDENT),
    ("escape_in_column", r"SELECT `em\x61il` FROM users", IDENT),
    # --- aliases named like PII columns
    ("pii_output_alias", "SELECT id AS email FROM users", PII),
    ("pii_output_alias_case", "SELECT brand AS Last_Name FROM products", PII),
    ("pii_table_alias", "SELECT COUNT(*) AS n FROM users AS postal_code", PII),
    ("pii_cte_name", "WITH email AS (SELECT brand FROM products) SELECT brand FROM email", PII),
    # --- id-packing
    ("string_agg_ids", "SELECT STRING_AGG(CAST(id AS STRING)) AS s FROM users",
     frozenset({"function_denied"})),
    ("string_agg_ids_qi", "SELECT STRING_AGG(CAST(id AS STRING), ',') AS s FROM users "
     "WHERE city = 'X'", frozenset({"function_denied", "qi_at_id_grain"})),
    ("string_agg_ids_outer_qi", "SELECT c.city, c.s FROM (SELECT city, "
     "STRING_AGG(CAST(id AS STRING)) AS s FROM users GROUP BY city) AS c "
     "WHERE c.city = 'X'", frozenset({"function_denied", "qi_at_id_grain"})),
    ("string_agg_ids_via_cte", "WITH o AS (SELECT user_id AS k FROM orders) "
     "SELECT STRING_AGG(CAST(k AS STRING)) AS s FROM o", frozenset({"function_denied"})),
    ("array_agg_ids", "SELECT ARRAY_AGG(id) AS a FROM users", FUNC),
    # --- unicode look-alikes
    ("cyrillic_e_column", "SELECT еmail FROM users", frozenset({"non_ascii_identifier"})),
    ("cyrillic_quoted", "SELECT `еmail` FROM users", frozenset({"non_ascii_identifier"})),
    ("fullwidth_table", "SELECT brand FROM ｐroducts",
     frozenset({"non_ascii_identifier", "sql_syntax"})),
    ("zero_width_column", "SELECT e​mail FROM users",
     frozenset({"non_ascii_identifier", "sql_syntax"})),
    ("homoglyph_cte", "WITH ѕcope AS (SELECT 1 AS x) SELECT x FROM ѕcope",
     frozenset({"non_ascii_identifier"})),
    # --- alias/value tricks and QI evasions
    ("alias_as_value", "SELECT u FROM users AS u", frozenset({"unresolved_value"})),
    ("cte_alias_as_value", "WITH c AS (SELECT id FROM users) SELECT c FROM c",
     frozenset({"unresolved_value"})),
    ("struct_path", "SELECT u.a.b FROM users AS u", frozenset({"unresolved_value"})),
    ("select_as_struct", "SELECT AS STRUCT id, age FROM users", frozenset({"unresolved_value"})),
    ("star_except", "SELECT * EXCEPT (email) FROM users", frozenset({"select_star"})),
    ("star_replace", "SELECT * REPLACE ('x' AS email) FROM users", frozenset({"select_star"})),
    ("qualified_star", "SELECT u.* FROM users AS u", frozenset({"select_star"})),
    ("qi_row_via_cte", "WITH c AS (SELECT id, city FROM users) SELECT id, city FROM c",
     frozenset({"qi_at_id_grain"})),
    ("qi_group_by_id", "SELECT id, gender, COUNT(*) AS n FROM users GROUP BY id, gender",
     frozenset({"qi_at_id_grain"})),
    ("qi_max_trick", "SELECT state, MAX(age) AS a FROM users GROUP BY state",
     frozenset({"qi_position"})),
    ("qi_min_id", "SELECT MIN(id) AS i FROM users WHERE age = 77", frozenset({"qi_at_id_grain"})),
    ("qi_any_value_id", "SELECT state, ANY_VALUE(id) AS i FROM users GROUP BY state",
     frozenset({"qi_at_id_grain"})),
    ("qi_scalar_min_id", "SELECT (SELECT MAX(id) FROM users WHERE city = 'X') AS i",
     frozenset({"qi_at_id_grain"})),
    ("qi_window_leak", "SELECT id, FIRST_VALUE(city) OVER (PARTITION BY id) AS c FROM users",
     frozenset({"qi_position", "qi_at_id_grain"})),
    ("qi_scalar_subquery", "SELECT o.order_id, (SELECT u.city FROM users u WHERE u.id = o.user_id)"
     " AS c FROM orders o", frozenset({"qi_at_id_grain"})),
    ("qi_cast", "SELECT CAST(age AS STRING) AS a, COUNT(*) AS n FROM users GROUP BY 1",
     frozenset({"function_denied"})),
    ("qi_id_literal", "SELECT age, COUNT(*) AS n FROM users WHERE id = 1234 GROUP BY age",
     frozenset({"small_cell_unplaceable"})),
    ("qi_id_literal_order_id", "SELECT u.state, COUNT(*) AS n FROM orders o JOIN users u "
     "ON u.id = o.user_id WHERE o.order_id = 55 GROUP BY 1", frozenset({"small_cell_unplaceable"})),
    ("id_literal_conditional", "SELECT COUNT(IF(user_id = 9, 1, NULL)) AS n FROM orders",
     frozenset({"qi_position"})),
    ("join_using", "SELECT status FROM orders JOIN order_items USING (order_id)",
     frozenset({"unsupported_syntax"})),
    ("pivot", "SELECT x FROM (SELECT status, order_id FROM orders) "
     "PIVOT (COUNT(order_id) FOR status IN ('Shipped'))", frozenset({"unsupported_syntax",
                                                                      "source_not_allowed"})),
    ("ambiguous", "SELECT status FROM orders o JOIN order_items oi ON oi.order_id = o.order_id",
     frozenset({"ambiguous_column"})),
    ("too_long", "SELECT brand FROM products WHERE " + " OR ".join(["brand = 'b'"] * 800),
     frozenset({"sql_too_long"})),
    ("too_deep", "SELECT " + "(" * 400 + "1" + ")" * 400 + " AS x",
     frozenset({"too_complex", "sql_syntax"})),
]


@pytest.mark.parametrize(("name", "sql", "codes"), _CASES, ids=[c[0] for c in _CASES])
def test_redteam_rejected(name: str, sql: str, codes: frozenset[str]) -> None:
    decision = check_sql(sql)
    assert not decision.allowed, name
    assert decision.reason_code.value in codes, (name, decision.reason_code.value)


_ALLOWED: list[tuple[str, str]] = [
    ("brand_revenue_month",
     "SELECT p.brand, DATE_TRUNC(DATE(oi.created_at), MONTH) AS m, SUM(oi.sale_price) AS rev "
     "FROM `bigquery-public-data.thelook_ecommerce.order_items` oi "
     "JOIN `bigquery-public-data.thelook_ecommerce.products` p ON p.id = oi.product_id "
     "GROUP BY 1, 2"),
    ("top_users_spend", "SELECT user_id, SUM(sale_price) AS spend FROM order_items "
     "GROUP BY user_id ORDER BY spend DESC LIMIT 10"),
    ("count_by_state", "SELECT state, COUNT(*) AS n FROM users GROUP BY state"),
    ("users_by_brand_date", "SELECT DISTINCT oi.user_id FROM order_items oi "
     "JOIN products p ON p.id = oi.product_id WHERE p.brand = 'Acme' "
     "AND DATE(oi.created_at) BETWEEN '2024-01-01' AND '2024-03-31'"),
    ("mixed_case_keywords_ok", "select Brand, count(*) as N from Products group by Brand"
     .replace("Products", "products")),
    ("quoted_ok", "SELECT `brand`, COUNT(*) AS n FROM `products` GROUP BY `brand`"),
    ("comments_ok", "/* revenue */ SELECT brand -- the brand\n, COUNT(*) AS n # count\n "
     "FROM products GROUP BY brand;"),
    ("unicode_literal_ok", "SELECT COUNT(*) AS n FROM products WHERE brand = 'Café'"),
    ("qi_where_aggregate", "SELECT COUNT(*) AS n FROM users WHERE age > 65"),
    ("qi_group_alias", "SELECT country AS c, COUNT(DISTINCT id) AS n FROM users GROUP BY c"),
    ("qi_count_distinct", "SELECT COUNT(DISTINCT city) AS n FROM users"),
    ("cte_aggregate_join", "WITH s AS (SELECT user_id, SUM(sale_price) AS spend "
     "FROM order_items GROUP BY user_id) SELECT u.country, AVG(s.spend) AS avg_spend "
     "FROM s JOIN users u ON u.id = s.user_id GROUP BY u.country"),
    ("in_subquery_ok", "SELECT COUNT(*) AS n FROM orders WHERE user_id IN "
     "(SELECT id FROM users WHERE country = 'Brazil')"),
    ("exists_ok", "SELECT status, COUNT(*) AS n FROM orders o WHERE EXISTS (SELECT 1 AS x "
     "FROM order_items oi WHERE oi.order_id = o.order_id AND oi.sale_price > 100) "
     "GROUP BY status"),
    ("window_no_qi", "SELECT order_id, created_at, LAG(created_at) OVER "
     "(PARTITION BY user_id ORDER BY created_at) AS prev FROM orders"),
    ("case_no_qi", "SELECT SUM(CASE WHEN status = 'Returned' THEN 1 ELSE 0 END) AS r "
     "FROM orders"),
    ("safe_divide", "SELECT brand, SAFE_DIVIDE(SUM(retail_price - cost), SUM(retail_price)) "
     "AS margin FROM products GROUP BY brand"),
    ("timestamp_trunc", "SELECT TIMESTAMP_TRUNC(created_at, WEEK) AS w, COUNT(*) AS n "
     "FROM orders GROUP BY w ORDER BY w"),
    ("union_distinct", "SELECT category AS v FROM products UNION DISTINCT "
     "SELECT department AS v FROM products"),
    ("backtick_fq_table", "SELECT COUNT(*) AS n FROM "
     "`bigquery-public-data.thelook_ecommerce.users`"),
    ("backtick_parts_table", "SELECT COUNT(*) AS n FROM "
     "`bigquery-public-data`.`thelook_ecommerce`.`users`"),
    ("backtick_alias", "SELECT COUNT(*) AS `n` FROM products AS `p` ORDER BY `n`"),
    ("string_agg_names", "SELECT category, STRING_AGG(DISTINCT brand, ', ') AS brands "
     "FROM products GROUP BY category"),
    ("trailing_comment_after_semicolon", "SELECT 1 AS x; -- trailing"),
    ("order_ids_by_status", "SELECT order_id, created_at FROM orders WHERE status = 'Shipped' "
     "ORDER BY created_at DESC LIMIT 50"),
]


@pytest.mark.parametrize(("name", "sql"), _ALLOWED, ids=[c[0] for c in _ALLOWED])
def test_redteam_positive_controls(name: str, sql: str) -> None:
    decision = check_sql(sql)
    assert decision.allowed, (name, decision.reason_code.value)
